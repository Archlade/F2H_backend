from datetime import datetime
from ..extensions import db
from ..models import PurchaseRequest, RequestStatusHistory, Chat, Notification
from ..models.product import Product
from ..models.request import ACTIVE_FILTER, CLOSED_STATUSES
from .notification_service import create_notification
from . import order_money
from . import stock_service as stock
from ..extensions import socketio


def create_purchase_request(customer_id: int, data: dict, delivery_charge: float = 0.0):
    product = Product.query.get(data['product_id'])
    if not product or not product.is_active or product.deleted_at:
        raise ValueError('Product not available')

    # Farmers buy from each other, so the buyer may well own listings of their
    # own — but not this one. Selling to yourself would inflate your own sales
    # figures, move stock between your own hands, and spend a single-use coupon
    # on a transaction that never happened.
    if product.farmer_id == customer_id:
        raise ValueError('This is your own listing')

    # Basket-only items cannot be bought one at a time.
    #
    # They are sourced by F2H against the baskets that were ordered, and there
    # is no farm behind one to accept a request or pick it. `product_service`
    # already hides them from every listing, so reaching here means a stale
    # screen or a hand-made request — either way it stops here rather than
    # creating an order nobody can fulfil.
    if getattr(product, 'basket_only', False):
        raise ValueError(f'{product.name} is only available as part of a weekly basket')

    if product.stock_status == 'out_of_stock':
        raise ValueError('Product is out of stock')

    qty = float(data['quantity'])
    if qty < float(product.min_quantity):
        raise ValueError(f"Minimum quantity is {product.min_quantity} {product.unit}")
    if qty > float(product.available_quantity):
        raise ValueError('Requested quantity exceeds available stock')

    # Check for duplicate active request
    active_statuses = ['pending', 'admin_review', 'accepted', 'chat_active', 'confirmed', 'preparing']
    existing = PurchaseRequest.query.filter(
        PurchaseRequest.customer_id == customer_id,
        PurchaseRequest.product_id == data['product_id'],
        PurchaseRequest.status.in_(active_statuses),
    ).first()
    if existing:
        raise ValueError('You already have an active request for this product')

    # An address ID is attacker-controlled, and the address is echoed back in the
    # response — so it has to belong to the customer placing the request.
    address_id = data.get('delivery_address_id')
    if address_id:
        from ..models import Address
        if not Address.query.filter_by(id=address_id, user_id=customer_id).first():
            raise ValueError('That delivery address does not belong to you')

    unit_price = float(product.effective_price)
    subtotal = round(unit_price * qty, 2)

    # Resolved before anything is created, so an invalid code fails the whole
    # request rather than leaving an order at the wrong price. The discount is
    # recomputed here from the server's own prices — the client's preview is
    # for display only and is never trusted.
    from .coupon_service import apply_to_total, redeem
    coupon, discount, total = apply_to_total(data.get('coupon_code'), subtotal)

    # The smallest order F2H will take. Under cash on delivery every order is a
    # physical trip with someone collecting money at the end, and below this the
    # trip costs more than the order is worth.
    #
    # Checked against `total` rather than `subtotal` — the payable amount after
    # any discount — so a coupon cannot be used to slip under the floor.
    #
    # Skipped when the caller is the cart, which has already applied the same
    # floor to the whole basket. Enforcing it per line as well would make a
    # valid ₹400 cart of two ₹200 items impossible to check out.
    if not data.get('skip_minimum'):
        # `min_order_value()` rather than the config directly: the figure is
        # admin-editable now and lives in platform_settings, with the configured
        # value as its fallback. Imported here to match how `current_app` was
        # imported here before — this module is loaded during app setup and a
        # top-level model import would be circular.
        from ..models import min_order_value
        minimum = min_order_value()
        if total < minimum:
            raise ValueError(
                f'The minimum order is ₹{minimum:.0f}. This one comes to '
                f'₹{total:.2f} — add more to your basket to continue.'
            )

    # The flat delivery fee, added after the minimum has been checked.
    #
    # Order matters. Checking the floor against a total that already included
    # delivery would let the fee itself carry a too-small basket over the line —
    # a ₹280 cart plus a ₹40 fee would pass a ₹300 minimum, which is not what
    # "minimum order" means to anyone. The floor is about the produce.
    #
    # Passed in rather than read here, because the caller is the only one who
    # knows whether this order is the one that carries it: a cart checkout fans
    # out into one order per line and the fee belongs to the delivery, not to
    # each line. `routes/cart.py` gives it to the first and zero to the rest.
    #
    # An explicit argument rather than a key in `data`. `data` is the request
    # body on the single-product path — `routes/requests.py` hands it straight
    # through — so a `delivery_charge` read out of it would be a number the
    # customer chose. Every caller now has to name the figure deliberately, and
    # the one place it can come from is `delivery_charge()` on the server.
    delivery = 0.0
    if data['purchase_mode'] != 'pickup':
        delivery = round(float(delivery_charge or 0), 2)
    total = round(total + delivery, 2)

    req = PurchaseRequest(
        customer_id=customer_id,
        farmer_id=product.farmer_id,
        product_id=data['product_id'],
        quantity=qty,
        unit_price=unit_price,
        subtotal=subtotal,
        discount_amount=discount,
        delivery_charge=delivery,
        total_price=total,
        coupon_id=coupon.id if coupon else None,
        purchase_mode=data['purchase_mode'],
        delivery_address_id=data.get('delivery_address_id'),
        delivery_notes=data.get('delivery_notes', ''),
        pickup_notes=data.get('pickup_notes', ''),
        customer_message=data.get('customer_message', ''),
    )
    db.session.add(req)
    db.session.flush()

    # Claimed inside the same transaction as the order, so the two land
    # together: if the commit below fails, the code stays available.
    if coupon:
        redeem(coupon, customer_id, subtotal, discount, request_id=req.id)

    _add_status_history(req.id, None, 'pending', customer_id)
    db.session.commit()

    # Notify farmer
    create_notification(
        recipient_id=product.farmer_id,
        sender_id=customer_id,
        notif_type='new_request',
        title='New Purchase Request',
        body=f"You have a new purchase request for {product.name}",
        data={'request_id': req.id, 'product_id': product.id},
    )
    # The order above is already committed, so this commit is the notification's
    # own. Without it the row is discarded when the session is torn down at the
    # end of the request — the farmer never sees the order in their list, and
    # the push that now rides on the same commit never goes out either.
    db.session.commit()

    # Real-time notification
    socketio.emit('new_notification', {'type': 'new_request', 'request_id': req.id},
                  room=f"user_{product.farmer_id}")

    return req


def update_request_status(request_id: int, actor_id: int, actor_role: str, new_status: str, data: dict = None):
    # Locked for the length of this transition. Two status changes racing on
    # the same order — a farmer tapping "Completed" while a customer taps
    # "Cancel" — would otherwise both read status='confirmed', both pass the
    # transition check, and one would credit the farmer while the other
    # refunded the customer: the platform pays out a share on an order it just
    # gave the money back for. With the lock the second transition waits, then
    # re-reads the committed status and the state machine rejects the now-
    # illegal move.
    from ..utils.locking import lock_row
    req = lock_row(PurchaseRequest, request_id)
    if req is None:
        from flask import abort
        abort(404)
    data = data or {}

    # Authorization — the actor must be a party to this request, and the status
    # must be one their side of it is allowed to set.
    #
    # Decided from the row, not the account role: a farmer buying from another
    # farmer is the buyer here and may only cancel, even though their account
    # can accept and confirm orders on the listings they sell.
    from ..models.request import (buyer_may_cancel, cancellation_refused_reason,
                                  party_for, party_may_set)
    party = party_for(req, actor_id, actor_role)
    if party is None:
        raise PermissionError('Not authorized')

    if not party_may_set(party, new_status, req.status):
        noun = {'buyer': 'buyer', 'seller': 'seller'}.get(party, actor_role)
        raise PermissionError(f"The {noun} cannot set an order to '{new_status}'")

    # The buyer's cancellation window closes when the seller confirms. Enforced
    # here rather than left to the app, because the app is what *promises* this
    # at checkout — and a promise only the client keeps is one a modified client
    # does not. The seller and an admin are unaffected.
    if new_status == 'cancelled' and party == 'buyer' and not buyer_may_cancel(req):
        raise PermissionError(cancellation_refused_reason(req))

    if not req.can_transition_to(new_status):
        raise ValueError(f"Cannot transition from '{req.status}' to '{new_status}'")

    # An order is not closed out before its cash is recorded.
    #
    # Checked here rather than trusted to the UI, because the UI is not what
    # stops a farmer from tapping "Mark complete" on an uncollected order — and
    # a completed order is one nobody chases for money. Since the farmer was
    # already paid at pickup, an order closed out without collection is a loss
    # the platform absorbs silently, which is exactly what this prevents.
    if order_money.payment_blocks(req, new_status):
        raise ValueError(order_money.payment_block_reason(req, new_status))

    # Stock moves here, before anything else in this transition is written.
    #
    # Confirming is the moment the farmer promises the goods, so it is the
    # moment they come off the listing. Placing a request does not reserve
    # anything — which means two customers can both ask for 3kg of a 5kg
    # listing, and the farmer confirming the second one is refused with the
    # real remaining figure rather than quietly overselling.
    #
    # Done first so that an InsufficientStock aborts the whole transition: no
    # status change, no history row, no notification about an order that was
    # never actually confirmed.
    order_money.settle(req, new_status, f'order #{req.id}', actor_id=actor_id)

    if new_status == 'confirmed' and not req.stock_committed and req.product:
        stock.commit(req.product, req.quantity)
        req.stock_committed = True
    elif new_status == 'cancelled' and req.stock_committed and req.product:
        # Only a *confirmed* order has stock out on it. Cancelling from pending
        # or chat_active has nothing to give back, which is exactly what the
        # flag records — 'cancelled' is reachable from both sides of confirm.
        stock.restore(req.product, req.quantity)
        req.stock_committed = False

    old_status = req.status
    req.status = new_status

    if new_status == 'rejected':
        req.rejection_reason = data.get('reason', '')
    if new_status == 'cancelled':
        req.cancellation_reason = data.get('reason', '')
        req.cancelled_by = actor_id

    _add_status_history(req.id, old_status, new_status, actor_id, data.get('note', ''))

    # Create chat when accepted
    if new_status == 'accepted':
        chat = Chat(
            request_id=req.id,
            customer_id=req.customer_id,
            farmer_id=req.farmer_id,
        )
        db.session.add(chat)
        db.session.flush()
        req.status = 'chat_active'
        _add_status_history(req.id, 'accepted', 'chat_active', actor_id, 'Chat created')

        # Notify both
        create_notification(req.customer_id, actor_id, 'request_accepted',
                            'Request Accepted!',
                            f"Your request for {req.product.name if req.product else 'product'} was accepted. Chat is now open.",
                            {'request_id': req.id, 'chat_id': chat.id})
        socketio.emit('new_notification', {'type': 'request_accepted', 'request_id': req.id, 'chat_id': chat.id},
                      room=f"user_{req.customer_id}")
    elif new_status == 'rejected':
        create_notification(req.customer_id, actor_id, 'request_rejected',
                            'Request Rejected',
                            f"Your request was rejected. Reason: {req.rejection_reason or 'No reason given.'}",
                            {'request_id': req.id})
        socketio.emit('new_notification', {'type': 'request_rejected', 'request_id': req.id},
                      room=f"user_{req.customer_id}")
    else:
        # Notify customer of status change
        recipient = req.customer_id if actor_id == req.farmer_id else req.farmer_id
        create_notification(recipient, actor_id, 'status_update',
                            'Order Status Updated',
                            f"Your order status changed to {new_status.replace('_', ' ').title()}",
                            {'request_id': req.id})
        socketio.emit('new_notification', {'type': 'status_update', 'request_id': req.id, 'status': new_status},
                      room=f"user_{recipient}")

    # The courier has the customer's cash, and an order only closes once that
    # money reaches F2H. Told to every admin rather than left to be noticed: the
    # courier cannot finish this themselves by design, so if nobody records the
    # handover the order sits at `cash_collected` indefinitely and the cash sits
    # in a pocket.
    if new_status == 'cash_collected':
        _notify_admins_cash_collected(req, actor_id, 'Order')

    db.session.commit()
    return req


def _notify_admins_cash_collected(order, actor_id, label):
    """Tell every active admin that a courier is holding cash for this order."""
    from ..models import Role, User

    courier = User.query.get(actor_id)
    who = courier.full_name if courier else 'A delivery partner'
    admins = (User.query.join(Role, User.role_id == Role.id)
              .filter(Role.name == 'admin', User.is_active.is_(True),
                      User.deleted_at.is_(None))
              .all())
    payload = {'order_id': order.id, 'delivery_id': actor_id}
    for admin in admins:
        create_notification(
            admin.id, actor_id, 'cash_collected',
            'Cash collected — record the handover',
            f"{who} collected {order.total_price} for {label} #{order.id}. "
            f"The order closes when you record the handover.",
            payload)
        socketio.emit('new_notification', {'type': 'cash_collected', **payload},
                      room=f"user_{admin.id}")


def _add_status_history(request_id, from_status, to_status, changed_by, note=''):
    h = RequestStatusHistory(
        request_id=request_id,
        from_status=from_status,
        to_status=to_status,
        changed_by=changed_by,
        note=note,
    )
    db.session.add(h)


def get_requests_for_customer(customer_id: int, status=None, page=1, per_page=20):
    query = PurchaseRequest.query.filter_by(customer_id=customer_id)
    if status == ACTIVE_FILTER:
        query = query.filter(PurchaseRequest.status.notin_(CLOSED_STATUSES))
    elif status:
        query = query.filter(PurchaseRequest.status == status)
    query = query.order_by(PurchaseRequest.created_at.desc())
    total = query.count()
    reqs = query.offset((page - 1) * per_page).limit(per_page).all()
    return reqs, total


def get_requests_for_farmer(farmer_id: int, status=None, page=1, per_page=20):
    query = PurchaseRequest.query.filter_by(farmer_id=farmer_id)
    if status == ACTIVE_FILTER:
        query = query.filter(PurchaseRequest.status.notin_(CLOSED_STATUSES))
    elif status:
        query = query.filter(PurchaseRequest.status == status)
    query = query.order_by(PurchaseRequest.created_at.desc())
    total = query.count()
    reqs = query.offset((page - 1) * per_page).limit(per_page).all()
    return reqs, total
