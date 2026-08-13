"""Cash on delivery: what is owed, and recording that it was handed over.

There is no payment gateway here and no external call anywhere in this file.
The customer pays cash to whoever brings the produce, and someone with
authority over the order records that it happened. That single recorded fact —
`mark_collected` — is the whole integration.

Four rules this file exists to enforce. They are the same four that mattered
when this was an online gateway, because they were never really about the
gateway.

**The amount is computed here, never received.** It comes from the order row in
our own database. An amount that arrives in a request body is an amount the
payer chose, and a marketplace that trusts it sells vegetables for ₹1. Under
cash this matters *more*, not less: the figure this file produces is the figure
a person reads off a screen at a doorstep and asks for.

**The split is frozen when the order is confirmed.** Commission rate included.
Changing the platform rate next month must not rewrite what a farmer was owed
last month, and under COD the farmer may well have the printed slip.

**Only the farmer or an admin can say cash arrived.** The customer cannot mark
their own order paid, and neither can the app. There is no signature to check
because there is no third party to sign anything — so authority is the whole
of the security model, and it is enforced in the route.

**Collection is idempotent.** A double-tapped button, a retried request, an
admin nudging an order forward twice: the second one through must be a no-op,
not a second credit to a farmer's balance.

Historical note: `PAYMENT_STATUSES` still reads `created / paid / failed /
refunded` because those are the values in the live `payments` enum. Under cash
they mean: `created` — confirmed, money due at the door; `paid` — cash in hand;
`refunded` — cash given back on a cancellation. `failed` is legacy and is never
written any more; it is kept only so rows written by the old gateway still load.
"""

import logging
from datetime import datetime

from flask import current_app

from ..extensions import db
from ..models.payment import Payment, money

logger = logging.getLogger(__name__)


class PaymentError(ValueError):
    """Something the customer, farmer or admin should be told about. Routes map
    this to a 400 with the message shown verbatim."""


# ── Availability ──────────────────────────────────────────────────────────────
#
# Cash needs no keys, no secrets and no third party, so it cannot be
# misconfigured and is never off. These two are kept because the health check,
# the startup log and the app's `payments_available` flag all still ask — and
# an endpoint that answers honestly is better than callers that have to know
# the question stopped mattering.

def payment_config_problem():
    """The reason payments cannot run, or None. Always None: cash always works."""
    return None


def is_configured():
    return True


# ── Creating the amount due ───────────────────────────────────────────────────

def ensure_for_order(order):
    """Record what this order will owe at the door. Returns the Payment row.

    Called when a farmer confirms an order, not when someone opens a screen.
    Confirmation is the moment the price stops moving — stock is committed and
    the total is final — so it is the moment to freeze the split.

    Idempotent, and deliberately more than "create if missing": an order that
    is re-confirmed after an edit re-reads its total, so a corrected price is
    reflected in what gets collected. An order already paid is left completely
    alone, because rewriting the amount on a settled payment would change what
    a farmer is owed for money already in a tin.
    """
    existing = _payment_for(order)
    if existing is not None and existing.status in ('paid', 'refunded'):
        return existing

    amount = money(order.total_price)
    if amount <= 0:
        return existing

    rate = current_app.config.get('PLATFORM_COMMISSION_RATE', 20)

    # A basket F2H sells itself pays no farmer share.
    #
    # Weekly baskets are sold by the platform, so the "farmer" on the order is
    # F2H's own selling account. Splitting 80/20 there would record F2H owing
    # itself 80% of every basket — which the admin orders screen would then
    # display as "pay the farmer ₹X at pickup", against an account that is not a
    # farmer and is standing at no gate. The growers who supplied the produce
    # are paid separately; that is the trade this model makes.
    if _is_platform_sale(order):
        rate = 100

    commission, farmer_share = Payment.split(amount, rate)

    payment = existing or Payment(
        customer_id=order.customer_id,
        farmer_id=order.farmer_id,
        **_order_key(order),
    )
    payment.method = 'cod'
    payment.amount = amount
    payment.commission_rate = rate
    payment.commission_amount = commission
    payment.farmer_amount = farmer_share
    payment.status = 'created'

    db.session.add(payment)
    # No commit. The caller owns the transaction so the confirmation and the
    # amount it implies land together or not at all.
    return payment


def _is_platform_sale(order) -> bool:
    """True when F2H itself is the seller — a weekly basket it sources and sells.

    Never raises. A misconfigured platform seller must not stop an ordinary
    farm order from having its payment created; it only means this one check
    answers "no", and a real farm order is unaffected either way.
    """
    from .platform_seller import platform_seller_or_none
    seller = platform_seller_or_none()
    return seller is not None and getattr(order, 'farmer_id', None) == seller.id


def amount_due(order):
    """What to ask for at the door, as a Decimal.

    Reads the payment row when there is one, because that is the frozen figure,
    and falls back to the order total for orders confirmed before this existed.
    """
    payment = _payment_for(order)
    if payment is not None and payment.status != 'refunded':
        return money(payment.amount)
    return money(order.total_price)


# ── Collecting ────────────────────────────────────────────────────────────────

def mark_collected(payment: Payment, collected_by_id, note=None):
    """Record that the cash was handed over. Idempotent.

    Sets the order's `payment_status` too, so a list of orders can show
    "collected" without a query per row.

    Nothing here commits. `collected_by` is stored rather than inferred so that
    a disputed collection has a name and a timestamp attached to it — under cash
    there is no bank statement to fall back on, and this row is the only record
    that the money was ever asked for.
    """
    if payment is None:
        return None
    if payment.status == 'paid':
        return payment
    if payment.status == 'refunded':
        raise PaymentError('This order was refunded — it cannot be collected again.')

    payment.status = 'paid'
    payment.paid_at = datetime.utcnow()
    payment.collected_by = collected_by_id
    payment.collected_at = payment.paid_at
    payment.collection_note = (note or '').strip()[:255] or None

    order = payment_order(payment)
    if order is not None:
        order.payment_status = 'paid'

    return payment


def mark_refunded(payment: Payment, reason='Order cancelled'):
    """Record that cash was given back on a cancelled order.

    There is no API call to make: someone physically returned the money, or
    never took it in the first place. This only writes down which of those
    happened.

    An order cancelled *before* collection has nothing to refund — no money
    ever moved — so it is closed out rather than marked refunded, and no
    reversal is needed on the farmer's balance either.
    """
    if payment is None:
        return None
    if payment.status != 'paid':
        return None

    payment.status = 'refunded'
    payment.refunded_amount = payment.amount
    payment.refunded_at = datetime.utcnow()
    payment.refund_reason = (reason or '')[:255] or None

    order = payment_order(payment)
    if order is not None:
        order.payment_status = 'refunded'

    logger.info('Payment %s marked refunded (%s) — ₹%s to be returned in cash',
                payment.id, reason, payment.amount)
    return payment


def is_collected(order) -> bool:
    """Has the money for this order actually arrived?"""
    payment = _payment_for(order)
    return payment is not None and payment.status == 'paid'


# ── Paying the farmer ─────────────────────────────────────────────────────────
#
# F2H hands the farmer their share in cash at the farm gate when it collects the
# produce. That is *earlier* than the customer pays, which is the one thing to
# hold on to about this file: the platform is out of pocket between pickup and
# the door, and that gap is deliberate — it is what the farmer is spared.

def farmer_amount_due(order):
    """What to hand the farmer at pickup, as a Decimal.

    The frozen share from the payment row, which was fixed at confirmation.
    Falls back to zero rather than guessing: an order with no payment row was
    never confirmed, so nothing is owed on it yet.
    """
    payment = _payment_for(order)
    if payment is None:
        return money(0)
    return money(payment.farmer_amount)


def mark_farmer_paid(payment: Payment, paid_by_id, amount=None, note=None):
    """Record cash handed to the farmer at pickup. Idempotent.

    `amount` defaults to the frozen `farmer_amount` but can be less — a short
    pickup, or an agreed deduction for quality. It is written down rather than
    recomputed later, because what actually changed hands at a farm gate is not
    recoverable from anything else.

    Idempotent for the same reason `mark_collected` is: a double-tapped button
    must not read as a second payment. There is no ledger behind this any more,
    so a duplicate would not merely double a balance — it would be the only
    record, and it would be wrong.
    """
    if payment is None:
        return None
    if payment.farmer_paid_at is not None:
        return payment

    payment.farmer_paid_amount = money(payment.farmer_amount if amount is None else amount)
    payment.farmer_paid_at = datetime.utcnow()
    payment.farmer_paid_by = paid_by_id
    payment.farmer_paid_note = (note or '').strip()[:255] or None

    logger.info('Farmer %s paid ₹%s in cash at pickup for %s',
                payment.farmer_id, payment.farmer_paid_amount, order_label(payment))
    return payment


def is_farmer_paid(order) -> bool:
    payment = _payment_for(order)
    return payment is not None and payment.farmer_paid_at is not None


def farmer_payment_summary(order):
    """The farmer's side of an order's money, for a screen. None if not confirmed.

    Deliberately small and deliberately opt-in — callers ask for it per order
    rather than it riding on every `to_dict`, because it costs a lookup and most
    screens never show it.

        due    what will be handed over at pickup
        paid   what actually was, once it has been
    """
    payment = _payment_for(order)
    if payment is None:
        return None
    return {
        'due': float(money(payment.farmer_amount)),
        'paid_at': payment.farmer_paid_at.isoformat() if payment.farmer_paid_at else None,
        'paid_amount': (float(payment.farmer_paid_amount)
                        if payment.farmer_paid_amount is not None else None),
        'paid_by_name': payment.farmer_payer.full_name if payment.farmer_payer else None,
        'note': payment.farmer_paid_note,
    }


# ── Order plumbing ────────────────────────────────────────────────────────────
#
# Purchase requests and family pack orders are separate tables with the same
# shape for our purposes. These helpers are the only place that has to care
# which one it is holding.

def _order_key(order):
    from ..models import PurchaseRequest
    return ({'request_id': order.id} if isinstance(order, PurchaseRequest)
            else {'family_pack_order_id': order.id})


def _order_key_name(order):
    from ..models import PurchaseRequest
    return 'request' if isinstance(order, PurchaseRequest) else 'pack-order'


def _payment_for(order):
    return Payment.query.filter_by(**_order_key(order)).order_by(Payment.id.desc()).first()


def payment_for_order(order):
    """The live payment for an order, if any."""
    return _payment_for(order)


def payment_order(payment: Payment):
    """The order a payment belongs to, whichever table it lives in."""
    from ..models import FamilyPackOrder, PurchaseRequest
    if payment.request_id:
        return PurchaseRequest.query.get(payment.request_id)
    if payment.family_pack_order_id:
        return FamilyPackOrder.query.get(payment.family_pack_order_id)
    return None


def order_label(payment: Payment):
    if payment.request_id:
        return f'order #{payment.request_id}'
    return f'family pack order #{payment.family_pack_order_id}'
