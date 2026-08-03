from datetime import datetime

from ..extensions import db


class Coupon(db.Model):
    """A single-use discount voucher.

    One code, one redemption, ever. `redeemed_at` is the flag the admin list
    reads; `is_active` is a separate, admin-controlled switch so a code can be
    withdrawn before use without being recorded as spent.
    """

    __tablename__ = 'coupons'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False, unique=True)
    description = db.Column(db.String(255))
    discount_type = db.Column(db.Enum('percentage', 'fixed'), nullable=False, default='percentage')
    discount_value = db.Column(db.Numeric(10, 2), nullable=False)

    # Optional guards; None means no limit.
    min_order_value = db.Column(db.Numeric(10, 2))
    max_discount = db.Column(db.Numeric(10, 2))
    expires_at = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    redeemed_at = db.Column(db.DateTime)
    redeemed_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    redeemed_amount = db.Column(db.Numeric(10, 2))

    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    redeemer = db.relationship('User', foreign_keys=[redeemed_by])
    creator = db.relationship('User', foreign_keys=[created_by])
    redemption = db.relationship('CouponRedemption', back_populates='coupon',
                                 uselist=False, cascade='all, delete-orphan')

    @property
    def is_redeemed(self):
        return self.redeemed_at is not None

    @property
    def is_expired(self):
        return self.expires_at is not None and self.expires_at < datetime.utcnow()

    @property
    def status(self):
        """What the admin list shows as a badge.

        Order matters: a spent coupon reads as "used" even if it has since
        expired or been deactivated, because that is the fact the admin cares
        about when auditing.
        """
        if self.is_redeemed:
            return 'used'
        if not self.is_active:
            return 'inactive'
        if self.is_expired:
            return 'expired'
        return 'available'

    def unavailable_reason(self, subtotal=None):
        """Why this coupon can't be applied, or None if it can.

        Returns copy the customer sees, so each message says what to do next
        rather than just refusing.
        """
        if self.is_redeemed:
            return 'This coupon has already been used.'
        if not self.is_active:
            return 'This coupon is no longer available.'
        if self.is_expired:
            return 'This coupon has expired.'
        if subtotal is not None and self.min_order_value is not None:
            minimum = float(self.min_order_value)
            if float(subtotal) < minimum:
                return f'Spend at least ₹{minimum:.0f} to use this coupon.'
        return None

    def discount_for(self, subtotal):
        """The amount this coupon takes off `subtotal`.

        Rounded to paise, never negative, and never more than the order itself —
        a ₹500 fixed coupon on a ₹200 order discounts ₹200, not ₹500, so the
        total can't go below zero.
        """
        subtotal = float(subtotal or 0)
        if subtotal <= 0:
            return 0.0

        if self.discount_type == 'percentage':
            amount = subtotal * float(self.discount_value) / 100
            if self.max_discount is not None:
                amount = min(amount, float(self.max_discount))
        else:
            amount = float(self.discount_value)

        return round(min(max(amount, 0.0), subtotal), 2)

    @property
    def label(self):
        """Human-readable discount, e.g. "10% off" or "₹50 off"."""
        value = float(self.discount_value)
        if self.discount_type == 'percentage':
            text = f'{value:g}% off'
            if self.max_discount is not None:
                text += f' (up to ₹{float(self.max_discount):.0f})'
            return text
        return f'₹{value:.0f} off'

    def to_dict(self, include_admin=False):
        data = {
            'id': self.id,
            'code': self.code,
            'description': self.description,
            'discount_type': self.discount_type,
            'discount_value': float(self.discount_value),
            'min_order_value': float(self.min_order_value) if self.min_order_value is not None else None,
            'max_discount': float(self.max_discount) if self.max_discount is not None else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'is_active': self.is_active,
            'is_redeemed': self.is_redeemed,
            'status': self.status,
            'label': self.label,
        }
        if include_admin:
            data.update({
                'redeemed_at': self.redeemed_at.isoformat() if self.redeemed_at else None,
                'redeemed_amount': float(self.redeemed_amount) if self.redeemed_amount is not None else None,
                'redeemed_by': {
                    'id': self.redeemer.id,
                    'full_name': self.redeemer.full_name,
                } if self.redeemer else None,
                'created_at': self.created_at.isoformat() if self.created_at else None,
                'created_by': self.creator.full_name if self.creator else None,
                'order': self.redemption.order_ref if self.redemption else None,
            })
        return data


class CouponRedemption(db.Model):
    """One coupon, one order, one time.

    The unique constraint on `coupon_id` is the real single-use guarantee.
    Two checkouts can both read the coupon as unspent, but only one INSERT
    survives — the other hits an IntegrityError and is told the code is gone.
    Relying on a `redeemed_at IS NULL` check in Python would leave a window
    between the read and the write.
    """

    __tablename__ = 'coupon_redemptions'

    id = db.Column(db.Integer, primary_key=True)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupons.id', ondelete='CASCADE'),
                          nullable=False, unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # Exactly one of these is set, matching how chats attach to either order.
    request_id = db.Column(db.Integer, db.ForeignKey('purchase_requests.id', ondelete='SET NULL'))
    family_pack_order_id = db.Column(
        db.Integer, db.ForeignKey('family_pack_orders.id', ondelete='SET NULL'))

    subtotal = db.Column(db.Numeric(10, 2), nullable=False)
    discount_amount = db.Column(db.Numeric(10, 2), nullable=False)
    total_after_discount = db.Column(db.Numeric(10, 2), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    coupon = db.relationship('Coupon', back_populates='redemption')
    customer = db.relationship('User', foreign_keys=[customer_id])

    @property
    def order_ref(self):
        if self.request_id:
            return {'type': 'request', 'id': self.request_id}
        if self.family_pack_order_id:
            return {'type': 'family_pack_order', 'id': self.family_pack_order_id}
        return None

    def to_dict(self):
        return {
            'id': self.id,
            'coupon_id': self.coupon_id,
            'customer_id': self.customer_id,
            'customer': {
                'id': self.customer.id,
                'full_name': self.customer.full_name,
            } if self.customer else None,
            'order': self.order_ref,
            'subtotal': float(self.subtotal),
            'discount_amount': float(self.discount_amount),
            'total_after_discount': float(self.total_after_discount),
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
