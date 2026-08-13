"""Money: what was collected, what is owed, and what has been paid out.

Three tables, and the split between them is deliberate.

**`payments`** is the record of cash taken at a doorstep. It is the only thing
in the system that corresponds to real money changing hands, and its amounts are
frozen when the order is confirmed — including the commission rate, so changing
the rate next month does not silently rewrite what a farmer was owed last month.

Under cash on delivery this row carries more weight than it did when a gateway
was involved: there is no bank statement to reconcile against, so `collected_by`
and `collected_at` are the only evidence that the money was ever asked for.

**`farmer_ledger`** is an append-only list of what each farmer earned and was
paid. A balance is never stored as a single mutable number: a number you cannot
explain is unanswerable when a farmer says "this is wrong", and a mutable
balance is one bad concurrent write away from being wrong forever. Balance is
always `SUM(credit) - SUM(debit)`, and every entry says which order or payout
caused it.

**`payouts`** is a redemption request and its approval trail.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from ..extensions import db

# `created` — order confirmed, cash due at the door.
# `paid`    — cash collected, recorded by the farmer or an admin.
# `refunded`— a collected order was cancelled and the money handed back.
# `failed`  — legacy. Written only by the removed online-payment flow; kept in
#             the tuple so historic rows still load, never written any more.
PAYMENT_STATUSES = ('created', 'paid', 'failed', 'refunded')
PAYMENT_METHODS = ('cod',)
PAYOUT_STATUSES = ('requested', 'approved', 'paid', 'rejected')
LEDGER_TYPES = ('credit', 'debit')


def money(value) -> Decimal:
    """Round to paise, half-up.

    Bankers' rounding is Python's default and would settle 2.5 paise to 2. For
    money owed to someone, round the way an invoice does.
    """
    return Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class Payment(db.Model):
    """One cash collection, and the split it implies once the money is in."""

    __tablename__ = 'payments'

    id = db.Column(db.Integer, primary_key=True)

    # Exactly one of these is set. Two nullable columns rather than a polymorphic
    # key, because a real foreign key that the database enforces is worth more
    # than the tidiness of one column holding an id whose meaning depends on a
    # type string next to it.
    request_id = db.Column(db.Integer, db.ForeignKey('purchase_requests.id', ondelete='CASCADE'))
    family_pack_order_id = db.Column(db.Integer,
                                     db.ForeignKey('family_pack_orders.id', ondelete='CASCADE'))

    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    # What the customer is charged — the order's total_price, computed on the
    # server. Never accepted from the client: a payment amount that arrives in a
    # request body is an amount an attacker chooses.
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    commission_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    farmer_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    # Snapshotted, not read from config at display time. Changing the platform
    # rate must not retroactively alter what past orders paid.
    commission_rate = db.Column(db.Numeric(5, 2), nullable=False, default=20)
    currency = db.Column(db.String(3), nullable=False, default='INR')

    # One value today. A column rather than an assumption, because the day a
    # second method appears every row written before it needs to already say
    # what it was — retrofitting that onto historic rows is guesswork.
    method = db.Column(db.String(20), nullable=False, default='cod')

    status = db.Column(db.Enum(*PAYMENT_STATUSES, name='payment_status'),
                       nullable=False, default='created', index=True)

    # Who took the money, and when. Under cash this is the entire audit trail:
    # there is no gateway record and no bank statement to check it against, so
    # a collection nobody's name is on is a collection nobody can be asked
    # about.
    collected_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    collected_at = db.Column(db.DateTime)
    collection_note = db.Column(db.String(255))

    refunded_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    refunded_at = db.Column(db.DateTime)
    refund_reason = db.Column(db.String(255))

    # ── What the farmer was handed, at pickup ──
    #
    # F2H pays the farmer their share in cash when it collects the produce, so
    # the payment to the farmer happens *before* the payment from the customer.
    # These three columns are the whole record of it — there is no wallet
    # balance behind them any more.
    #
    # `farmer_paid_amount` is stored rather than read back off `farmer_amount`
    # because the two can legitimately differ: a short pickup, an agreed
    # deduction for quality. What was actually handed over is the fact worth
    # keeping.
    farmer_paid_at = db.Column(db.DateTime)
    farmer_paid_amount = db.Column(db.Numeric(10, 2))
    farmer_paid_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    farmer_paid_note = db.Column(db.String(255))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = db.relationship('User', foreign_keys=[customer_id])
    farmer = db.relationship('User', foreign_keys=[farmer_id])
    collector = db.relationship('User', foreign_keys=[collected_by])
    farmer_payer = db.relationship('User', foreign_keys=[farmer_paid_by])
    request = db.relationship('PurchaseRequest', foreign_keys=[request_id])
    family_pack_order = db.relationship('FamilyPackOrder', foreign_keys=[family_pack_order_id])

    @staticmethod
    def split(amount, rate):
        """(commission, farmer_share) for `amount` at `rate` percent.

        The farmer's share is the remainder rather than its own multiplication,
        so the two always add back to exactly what the customer paid. Computing
        both independently leaves a stray paisa on some amounts — which is the
        kind of discrepancy nobody notices until a farmer adds up their orders.
        """
        total = money(amount)
        commission = money(total * Decimal(str(rate)) / Decimal('100'))
        return commission, total - commission

    def to_dict(self):
        return {
            'id': self.id,
            'request_id': self.request_id,
            'family_pack_order_id': self.family_pack_order_id,
            'amount': float(self.amount),
            'commission_amount': float(self.commission_amount),
            'farmer_amount': float(self.farmer_amount),
            'commission_rate': float(self.commission_rate),
            'currency': self.currency,
            'status': self.status,
            'method': self.method,
            'collected_by': self.collected_by,
            'collected_by_name': self.collector.full_name if self.collector else None,
            'collected_at': self.collected_at.isoformat() if self.collected_at else None,
            'collection_note': self.collection_note,
            'refunded_amount': float(self.refunded_amount),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'paid_at': self.paid_at.isoformat() if self.paid_at else None,
            # What the farmer got, and when. `farmer_paid_at` being null is how
            # every screen decides whether to say "due at pickup" or "paid".
            'farmer_paid_at': self.farmer_paid_at.isoformat() if self.farmer_paid_at else None,
            'farmer_paid_amount': (float(self.farmer_paid_amount)
                                   if self.farmer_paid_amount is not None else None),
            'farmer_paid_by': self.farmer_paid_by,
            'farmer_paid_note': self.farmer_paid_note,
        }


class LedgerEntry(db.Model):
    """One movement in a farmer's balance. Append-only.

    Nothing here is ever updated or deleted. A refund is a new debit, not an
    edit to the original credit — so the history always explains the balance,
    and "why is my balance lower than yesterday" has an answer with a date on
    it.
    """

    __tablename__ = 'farmer_ledger'

    id = db.Column(db.Integer, primary_key=True)
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    entry_type = db.Column(db.Enum(*LEDGER_TYPES, name='ledger_entry_type'), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)

    payment_id = db.Column(db.Integer, db.ForeignKey('payments.id', ondelete='SET NULL'))
    payout_id = db.Column(db.Integer, db.ForeignKey('payouts.id', ondelete='SET NULL'))
    description = db.Column(db.String(255), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    payment = db.relationship('Payment', foreign_keys=[payment_id])

    def to_dict(self):
        return {
            'id': self.id,
            'type': self.entry_type,
            'amount': float(self.amount),
            'description': self.description,
            'payment_id': self.payment_id,
            'payout_id': self.payout_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class Payout(db.Model):
    """A farmer asking for their balance, and what happened to that request."""

    __tablename__ = 'payouts'

    id = db.Column(db.Integer, primary_key=True)
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.Enum(*PAYOUT_STATUSES, name='payout_status'),
                       nullable=False, default='requested', index=True)

    # Copied from the profile at request time, not read live. The admin must pay
    # the account the farmer nominated when they asked — if the farmer edits
    # their details afterwards, that must not silently redirect a payout that is
    # already in flight.
    method = db.Column(db.String(10), nullable=False, default='upi')   # upi | bank
    upi_id = db.Column(db.String(255))
    account_name = db.Column(db.String(200))
    account_number = db.Column(db.String(50))
    ifsc = db.Column(db.String(20))

    # UTR or whatever the bank gives back. Without it a disputed payout is one
    # person's word against another's.
    reference = db.Column(db.String(100))
    note = db.Column(db.Text)

    requested_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime)
    processed_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))

    farmer = db.relationship('User', foreign_keys=[farmer_id])
    processor = db.relationship('User', foreign_keys=[processed_by])

    def to_dict(self, admin=False):
        data = {
            'id': self.id,
            'amount': float(self.amount),
            'status': self.status,
            'method': self.method,
            'reference': self.reference,
            'note': self.note,
            'requested_at': self.requested_at.isoformat() if self.requested_at else None,
            'processed_at': self.processed_at.isoformat() if self.processed_at else None,
        }
        if admin:
            data.update({
                'farmer_id': self.farmer_id,
                'farmer_name': self.farmer.full_name if self.farmer else None,
                'upi_id': self.upi_id,
                'account_name': self.account_name,
                # Only the tail. An admin approving a payout needs to recognise
                # the account, not to be able to read it out over the phone.
                'account_number': f'••••{self.account_number[-4:]}' if self.account_number else None,
                'ifsc': self.ifsc,
            })
        return data
