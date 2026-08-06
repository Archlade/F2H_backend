"""What a change of order status does to the money.

Shared by purchase requests and family pack orders, which have different tables
but identical money rules. Three of them:

* **Confirming fixes what will be owed.** The farmer has committed the stock, so
  the total stops moving and the split is frozen — commission rate included.
* **Completing pays the farmer.** Not the moment the cash is collected — the
  moment the goods are delivered *and* the money is in. Crediting on collection
  alone would let a farmer bank the proceeds of an order they then never close
  out, and the platform holds the money in the meantime precisely so that
  cannot happen.
* **Cancelling refunds, and takes the credit back if one was made.**

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
from . import wallet_service as wallet

logger = logging.getLogger(__name__)


def settle(order, new_status, label):
    """Apply the money consequences of moving `order` to `new_status`.

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

    elif new_status == 'completed':
        if payment is not None and payment.status == 'paid':
            wallet.credit_for_order(payment, label)

    elif new_status == 'cancelled':
        if payment is not None and payment.status == 'paid':
            # Cash was already taken, so it has to go back by hand. Reverse the
            # credit first: if anything downstream fails, the farmer's balance
            # is still correct and an admin returns the money themselves. The
            # other order leaves a farmer holding a credit for an order the
            # customer has already been refunded for.
            wallet.reverse_for_refund(payment, label)
            payments.mark_refunded(payment, reason=f'{label} cancelled')
        elif order.payment_status == 'pending':
            # Nothing was ever collected, so there is nothing to return and no
            # credit to reverse. Said plainly rather than left sitting at
            # 'pending', which would show a cancelled order as still owing
            # money — including on orders confirmed before payments existed,
            # which have no payment row at all.
            order.payment_status = 'not_required'


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
