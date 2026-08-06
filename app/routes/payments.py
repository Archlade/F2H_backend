"""Cash on delivery: what an order owes, and recording that it was handed over.

Two endpoints. There is no checkout, no verify and no webhook, because there is
no third party involved — the customer pays cash to whoever brings the produce.

The security model changed shape when the gateway went. It used to rest on a
signature: only Razorpay could produce the HMAC that marked an order paid, so it
did not much matter who called the endpoint. Under cash there is nothing to
sign, so **authority is the whole of it**. `/collect` is the one endpoint in the
app that turns an unpaid order into a paid one, and the only defence against a
customer marking their own order paid is the check in `_may_collect`. It is
deliberately written out in full rather than folded into a decorator.
"""

import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..extensions import db, limiter
from ..models import FamilyPackOrder, PurchaseRequest
from ..services import payment_service as payments
from ..services.notification_service import create_notification
from ..utils.decorators import current_user_role
from ..utils.locking import lock_row

logger = logging.getLogger(__name__)

payments_bp = Blueprint('payments', __name__)

# Before this, the produce has not reached the customer, so there is nobody to
# take money from. Collecting earlier would let a farmer mark an order paid on
# the strength of an intention.
COLLECTABLE_STATUSES = ('out_for_delivery', 'ready_for_pickup', 'completed')


def _load_order(order_type, order_id, lock=False):
    """The order, optionally locked for the rest of the transaction.

    `lock=True` takes `SELECT … FOR UPDATE` on the same row that
    `request_service.update_status` locks. That shared row is the point: it
    serialises a cash collection against a status change on the same order, and
    those two racing is how money goes missing. See `collect` for the specific
    sequence.
    """
    model = None
    if order_type == 'request':
        model = PurchaseRequest
    elif order_type in ('pack-order', 'family_pack_order'):
        model = FamilyPackOrder
    if model is None:
        return None
    return lock_row(model, order_id) if lock else model.query.get(order_id)


def _may_collect(user, role, order) -> bool:
    """Who is allowed to say the cash arrived.

    The farmer whose order it is, or an admin. Explicitly **not** the customer:
    they are the one handing the money over, and letting the payer confirm their
    own payment is the same mistake as trusting an amount from a request body.

    `role` comes from `current_user_role`, which re-reads it from the database
    rather than trusting the token's claim — so a farmer suspended an hour ago
    cannot still record collections on a token that has not expired yet. Note
    that `User.role` is a relationship to a Role row, not a string; comparing it
    to `'admin'` directly is always false and would quietly lock admins out.
    """
    if user is None:
        return False
    if role == 'admin':
        return True
    return user.id == order.farmer_id


@payments_bp.route('/status/<order_type>/<int:order_id>', methods=['GET'])
@jwt_required()
def payment_status(order_type, order_id):
    """What this order owes, and whether the cash has been collected yet.

    Both sides call this: the customer to see what to have ready, the farmer to
    see what to ask for.
    """
    order = _load_order(order_type, order_id)
    if order is None:
        return jsonify({'error': 'Order not found'}), 404

    user, role = current_user_role()
    if user is None:
        return jsonify({'error': 'Account is no longer active',
                        'code': 'TOKEN_INVALID'}), 401
    if role != 'admin' and user.id not in (order.customer_id, order.farmer_id):
        return jsonify({'error': 'Not authorized'}), 403

    payment = payments.payment_for_order(order)
    return jsonify({
        'order_payment_status': order.payment_status,
        'amount_due': float(payments.amount_due(order)),
        'payment_method': 'cod',
        'payment': payment.to_dict() if payment else None,
        # Cash needs no keys and cannot be misconfigured. Kept so older builds
        # of the app, which hide the payment UI when this is false, keep working
        # rather than going blank.
        'payments_available': True,
        'can_collect': (_may_collect(user, role, order)
                        and order.payment_status == 'pending'
                        and order.status in COLLECTABLE_STATUSES),
    }), 200


@payments_bp.route('/collect', methods=['POST'])
@jwt_required()
# Generous, because a farmer working through a delivery round legitimately
# fires several of these in a few minutes. It only stops a runaway client.
@limiter.limit('120 per hour')
def collect():
    """Record that the customer paid cash.

    Idempotent: a second call for an already-collected order answers 200 with
    the same payment rather than crediting anyone twice. Delivery happens on
    patchy mobile data and a retried request must not cost the platform a
    farmer's share.

    **The order row is locked for the whole transaction**, the same way a status
    transition locks it. Without that, two things go wrong and one of them costs
    real money:

    * Two taps, or two devices, both read `status = 'created'`, both pass the
      idempotency check and both write. The second overwrites `collected_by`,
      and under cash that column is the *only* evidence of who took the money —
      so a disputed collection would name the wrong person.
    * Worse: a collection racing a cancellation. Cancel reads the payment,
      sees it is not yet paid, and closes the order out as `not_required`;
      collect then commits `paid`. The order now says nothing was owed while
      the payment row says cash was taken, and because the farmer is only
      credited on `completed` — which a cancelled order never reaches — their
      share simply vanishes. This is the same shape as finding 3 in
      SECURITY_REVIEW_ADVANCED.md, which is why it locks the same row.
    """
    data = request.get_json(silent=True) or {}
    order = _load_order(data.get('order_type'), data.get('order_id'), lock=True)
    if order is None:
        return jsonify({'error': 'Order not found'}), 404

    user, role = current_user_role()
    if user is None:
        return jsonify({'error': 'Account is no longer active',
                        'code': 'TOKEN_INVALID'}), 401
    if not _may_collect(user, role, order):
        # Deliberately the same wording whoever asks. Telling a stranger that
        # the order exists but is not theirs is more than they need to know.
        return jsonify({'error': 'You cannot record payment for this order'}), 403

    if order.status not in COLLECTABLE_STATUSES:
        return jsonify({'error': 'This order has not reached the customer yet, '
                                 'so there is nothing to collect.'}), 400

    payment = payments.payment_for_order(order)
    if payment is None:
        # Confirmed before this feature existed, or an order that never got a
        # payment row. Create it now from the order's own total rather than
        # refusing — the produce is at the door and the money is real.
        payment = payments.ensure_for_order(order)
        if payment is None:
            return jsonify({'error': 'This order has nothing to collect'}), 400

    already_paid = payment.status == 'paid'

    try:
        payments.mark_collected(payment, user.id, data.get('note'))
    except payments.PaymentError as error:
        return jsonify({'error': str(error)}), 400

    db.session.commit()

    if not already_paid:
        _notify_collected(payment, user)

    return jsonify({'success': True, 'payment': payment.to_dict()}), 200


def _notify_collected(payment, collector):
    """Tell both sides the money is accounted for.

    The customer gets a receipt they did not have to ask for — under cash there
    is no card statement, so this notification is the only record on their side
    that the payment was acknowledged. The farmer is told their share and when
    it lands, because the commission is easier to accept when it was never a
    surprise.

    Never allowed to fail the collection: the money has been taken and written
    down, and a notification that did not send is not a reason to unwind that.
    """
    try:
        label = payments.order_label(payment)
        create_notification(
            recipient_id=payment.customer_id,
            sender_id=collector.id,
            notif_type='payment_received',
            title='Payment received',
            body=(f'₹{float(payment.amount):.2f} received in cash for {label}. '
                  f'Thank you!'),
            data=_order_ref(payment),
        )
        if payment.farmer_id != collector.id:
            create_notification(
                recipient_id=payment.farmer_id,
                sender_id=collector.id,
                notif_type='payment_received',
                title='Cash collected',
                body=(f'₹{float(payment.amount):.2f} collected for {label}. '
                      f'₹{float(payment.farmer_amount):.2f} will be added to your '
                      f'balance once the order is delivered.'),
                data=_order_ref(payment),
            )
        db.session.commit()
    except Exception:
        logger.exception('Could not send collection notifications for payment %s',
                         payment.id)
        db.session.rollback()


def _order_ref(payment):
    return {k: v for k, v in (('request_id', payment.request_id),
                              ('order_id', payment.family_pack_order_id))
            if v is not None}
