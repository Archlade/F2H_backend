"""A farmer's balance: what they have earned, and what they can take out.

The balance is never stored. It is always `SUM(credit) - SUM(debit)` over
`farmer_ledger`, computed in SQL at the moment it is asked for.

That is a deliberate trade. A stored balance is one column to read instead of
an aggregate — but it is also a number that can drift from the entries that are
supposed to explain it, and once it has drifted there is no way to tell which
of the two is wrong. Deriving it means the balance and its explanation cannot
disagree, and the query is trivial for the row counts a farmer will ever have.

**When a farmer is credited is the important decision here.** Not when the
customer pays — when the order is *completed*. Paying on confirmation means
money arrives days before the vegetables do, and crediting at that moment would
let a farmer withdraw the proceeds of an order they then never deliver. The
platform is holding the money in the meantime, which is the point of holding it.
"""

from decimal import Decimal

from ..extensions import db
from ..models.payment import LedgerEntry, Payment, money


def balance(farmer_id) -> Decimal:
    """What this farmer could withdraw right now."""
    total = db.session.query(
        db.func.coalesce(
            db.func.sum(
                db.case((LedgerEntry.entry_type == 'credit', LedgerEntry.amount),
                        else_=-LedgerEntry.amount)
            ), 0)
    ).filter(LedgerEntry.farmer_id == farmer_id).scalar()
    return money(total)


def credit(farmer_id, amount, description, payment_id=None):
    """Add to a farmer's balance. Not committed — the caller owns the transaction."""
    amount = money(amount)
    if amount <= 0:
        return None
    entry = LedgerEntry(farmer_id=farmer_id, entry_type='credit', amount=amount,
                        description=description, payment_id=payment_id)
    db.session.add(entry)
    return entry


def debit(farmer_id, amount, description, payment_id=None, payout_id=None):
    """Take from a farmer's balance. Not committed."""
    amount = money(amount)
    if amount <= 0:
        return None
    entry = LedgerEntry(farmer_id=farmer_id, entry_type='debit', amount=amount,
                        description=description, payment_id=payment_id, payout_id=payout_id)
    db.session.add(entry)
    return entry


def credit_for_order(payment: Payment, order_label: str):
    """Credit a farmer their share once an order is delivered.

    Idempotent by construction: the ledger is searched for an existing credit
    against this payment first. Status transitions can be replayed — a retried
    request, a double-tapped button, an admin nudging an order forward twice —
    and a credit applied twice is money invented out of nothing.
    """
    if payment is None or payment.status != 'paid':
        return None

    already = (LedgerEntry.query
               .filter_by(payment_id=payment.id, entry_type='credit')
               .first())
    if already:
        return already

    return credit(payment.farmer_id, payment.farmer_amount,
                  f'Earnings from {order_label}', payment_id=payment.id)


def reverse_for_refund(payment: Payment, order_label: str):
    """Take back a credit when a paid order is refunded.

    A debit rather than deleting the credit, so the history still shows the sale
    happened and was reversed. This can push a balance negative if the farmer
    withdrew in between — which is correct, and honest: they owe it back. The
    payout request path refuses to pay out on a negative balance, so it settles
    itself out of their next order.
    """
    if payment is None:
        return None

    credited = (LedgerEntry.query
                .filter_by(payment_id=payment.id, entry_type='credit')
                .first())
    if not credited:
        return None                     # never credited, nothing to reverse

    already_reversed = (LedgerEntry.query
                        .filter_by(payment_id=payment.id, entry_type='debit')
                        .first())
    if already_reversed:
        return already_reversed

    return debit(payment.farmer_id, credited.amount,
                 f'Refund reversal — {order_label}', payment_id=payment.id)


def summary(farmer_id):
    """Balance plus the totals a farmer actually wants to see on the screen."""
    earned = db.session.query(db.func.coalesce(db.func.sum(LedgerEntry.amount), 0)).filter(
        LedgerEntry.farmer_id == farmer_id, LedgerEntry.entry_type == 'credit').scalar()
    withdrawn = db.session.query(db.func.coalesce(db.func.sum(LedgerEntry.amount), 0)).filter(
        LedgerEntry.farmer_id == farmer_id, LedgerEntry.entry_type == 'debit').scalar()

    # Money taken from customers for this farmer's orders that have not been
    # delivered yet — visible so "where is my money" has an answer before the
    # order completes.
    pending = db.session.query(db.func.coalesce(db.func.sum(Payment.farmer_amount), 0)).filter(
        Payment.farmer_id == farmer_id,
        Payment.status == 'paid',
        ~db.session.query(LedgerEntry.id).filter(
            LedgerEntry.payment_id == Payment.id,
            LedgerEntry.entry_type == 'credit').exists(),
    ).scalar()

    return {
        'balance': float(money(Decimal(str(earned)) - Decimal(str(withdrawn)))),
        'total_earned': float(money(earned)),
        'total_withdrawn': float(money(withdrawn)),
        'pending_clearance': float(money(pending)),
    }
