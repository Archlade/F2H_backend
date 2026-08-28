"""What a change of order status does to the money.

Shared by purchase requests and family pack orders, which have different tables
but identical money rules. Three of them:

* **Confirming fixes what will be owed.** The farmer has committed the stock, so
  the total stops moving and the split is frozen — commission rate included.
* **Pickup pays the farmer.** In cash, at the farm gate, when F2H collects the
  produce. This is the change that inverted the file a second time: the farmer
  is now paid *before* the customer pays, not after. There is no balance, no
  ledger and nothing to redeem — the money is handed over and written down.
* **Completing does nothing to the money.** The customer's cash is recorded when
  it is collected and the farmer was paid at pickup; by the time an order
  completes both facts already exist.
* **Cancelling refunds the customer and does not claw back the farmer.** They
  grew the produce and handed it over; it is not theirs to give back. A
  cancellation after pickup is F2H's loss, logged as such.

**The order of goods and money inverted when this became cash on delivery, and
that is the one thing to understand about this file.** Under the old online
flow, payment came *before* preparing: an unpaid order could not move, because
shipping unpaid produce was a loss the platform absorbed. Under cash the
opposite is true and enforcing the old rule would deadlock every order — the
customer cannot pay until someone is standing at their door, and nobody can get
to their door without the order first moving through `preparing` and
`out_for_delivery`. So the gate moved to the end: an order cannot be marked
`completed` until the cash has actually been collected. That is now the only
point where money must precede a status, and it is the one that matters, because
`completed` is what credits the farmer.

Kept out of the two order services so the rules exist once. It was the
*divergence* between those two files that produced the last two money bugs in
this codebase — the silently skipped stock deduction in one and the clamp in
the other.
"""

import logging

from . import payment_service as payments

logger = logging.getLogger(__name__)


def settle(order, new_status, label, actor_id=None):
    """Apply the money consequences of moving `order` to `new_status`.

    `actor_id` is whoever performed the transition. It only matters for
    `picked_up`, where it records the person who physically handed the farmer
    their cash — the sole named party on that payment.

    Nothing here commits — the caller owns the transaction, so the status
    change and its financial effect land together or not at all.
    """
    payment = payments.payment_for_order(order)

    if new_status == 'confirmed':
        # Freeze what will be collected at the door. Re-confirming an edited
        # order re-reads the total; an order already settled is left alone, so
        # an admin nudging a paid order back through confirm cannot make it
        # demand money twice.
        if order.payment_status not in ('paid', 'refunded'):
            created = payments.ensure_for_order(order)
            # A zero-total order — fully covered by a coupon, or a correction —
            # has nothing to collect, and `ensure_for_order` declines to invent
            # a payment for it. Marking it 'pending' anyway would put a
            # "collect ₹0.00" band on the farmer's screen and then block the
            # order from ever completing.
            order.payment_status = 'pending' if created is not None else 'not_required'

    elif new_status == 'picked_up':
        # F2H has the produce and the farmer has their cash. Recorded here so
        # the payment and the status land in one transaction — a pickup that
        # moved the order but failed to record the money would leave a farmer
        # with no evidence they were paid.
        #
        # `actor_id` is who handed it over. It is passed through from the route
        # rather than inferred, because under cash this row is the only record
        # that names a person.
        if payment is not None:
            payments.mark_farmer_paid(payment, actor_id)

    elif new_status == 'cash_collected':
        # The customer just paid, at the door, to the courier. This *is* the
        # collection — recording it anywhere else would mean the one moment
        # money changes hands is the one moment nothing is written down.
        #
        # `actor_id` names who took it, which under cash is the only record of
        # that. Guarded rather than assumed: a refunded payment must not be
        # re-collected, and `mark_collected` raises on one.
        if payment is not None and payment.status not in ('paid', 'refunded'):
            payments.mark_collected(payment, actor_id, note=f'{label} delivered')

    elif new_status == 'completed':
        # Nothing to do. The farmer was paid at pickup and the customer paid the
        # courier at the door; completing now means only that the courier has
        # handed that cash to F2H, which `DeliveryRemittance` records on its own.
        #
        # Still nothing here for a pickup order either: the customer collected
        # at the farm and paid the farmer directly, so there was never any F2H
        # cash in the middle to settle.
        pass

    elif new_status == 'cancelled':
        if payment is not None and payment.status == 'paid':
            # Cash was already taken from the customer, so it has to go back by
            # hand. The farmer's money is *not* clawed back: they grew and
            # handed over produce, and were paid for it at the gate.
            payments.mark_refunded(payment, reason=f'{label} cancelled')
        elif order.payment_status == 'pending':
            # Nothing was ever collected, so there is nothing to return. Said
            # plainly rather than left sitting at 'pending', which would show a
            # cancelled order as still owing money — including on orders
            # confirmed before payments existed, which have no payment row.
            order.payment_status = 'not_required'

        # The loss, recorded once, whatever the customer's side did.
        #
        # Deliberately outside the branches above, because the branch it would
        # otherwise miss is the likeliest one: F2H collects the produce and pays
        # the farmer, and *then* the customer cancels or refuses at the door —
        # so the payment never reached 'paid' and the order falls into the
        # 'nothing was collected' case. That is precisely a loss, and it would
        # have been written off as 'not_required' without a word.
        if payment is not None and payment.farmer_paid_at is not None:
            logger.warning(
                'LOSS on %s: farmer %s was paid ₹%s in cash at pickup and the '
                'order was cancelled with the customer having paid ₹%s.',
                label, payment.farmer_id, payment.farmer_paid_amount,
                payment.amount if payment.status == 'refunded' else 0)


def payment_blocks(order, new_status) -> bool:
    """True when this transition must not happen because the cash is not in.

    Only `completed` is gated — see the module docstring. Everything before it
    has to be reachable unpaid, because reaching the customer is *how* payment
    happens under cash on delivery.
    """
    if new_status != 'completed':
        return False
    return order.payment_status == 'pending'


def payment_block_reason(order, new_status) -> str:
    """What to tell the farmer when `payment_blocks` says no."""
    return (f'Record the ₹{float(payments.amount_due(order)):.2f} cash payment '
            f'before marking this order complete.')
