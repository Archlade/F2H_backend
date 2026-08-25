"""Cash handed over by a delivery partner.

A delivery account collects the customer's cash at the door and hands it to an
admin later, in a lump. Two figures matter and only one of them is stored:

* **Collected** — the sum of `total_price` over that account's completed
  orders. Derived, never written down. It is already recorded on each order,
  and a second copy is a second thing to keep in step; the first time they
  disagree nobody knows which is right.

* **Handed over** — one row here per handover. This genuinely is new
  information: nothing else in the system knows that money moved from a
  person's pocket to a desk.

Outstanding is the subtraction. Which means it cannot drift — there is one
number stored, one derived, and no way for the pair to contradict each other.

Rows are never edited or deleted. A handover that was recorded wrongly is
corrected by recording a negative one, so the trail shows both the mistake and
the correction rather than quietly becoming a different history. This is money
somebody is personally accountable for; the log has to survive being wrong.
"""

from datetime import datetime

from ..extensions import db


class DeliveryRemittance(db.Model):
    __tablename__ = 'delivery_remittances'

    id = db.Column(db.Integer, primary_key=True)

    # Whose cash this was. Not ON DELETE CASCADE — removing a delivery account
    # must not erase the record of money they handed over.
    delivery_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='RESTRICT'),
        nullable=False, index=True)

    # Negative is allowed, and is how a wrong entry is corrected. See the module
    # docstring — reversing by subtraction keeps the trail honest where editing
    # the original would not.
    amount = db.Column(db.Numeric(10, 2), nullable=False)

    # The admin who took the cash. The other half of "who is accountable": a
    # handover with no named recipient is one person's word.
    received_by = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)

    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    partner = db.relationship('User', foreign_keys=[delivery_id])
    receiver = db.relationship('User', foreign_keys=[received_by])

    def to_dict(self):
        return {
            'id': self.id,
            'delivery_id': self.delivery_id,
            'delivery_name': self.partner.full_name if self.partner else None,
            'amount': float(self.amount),
            'received_by': self.received_by,
            'received_by_name': self.receiver.full_name if self.receiver else None,
            'note': self.note,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
