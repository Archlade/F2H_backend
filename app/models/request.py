from ..extensions import db
from datetime import datetime

# Which party is allowed to move an order into a given state. Without this a
# buyer could accept and confirm their own order — including the stock
# deduction that confirming performs on the seller's inventory.
#
# Keyed on the party's side of *this order*, not on their account role. Farmers
# buy from each other, so the same account is the seller on one row and the
# buyer on the next; deciding from the account role alone would hand a farmer
# seller powers over an order they merely placed.
PARTY_TRANSITIONS = {
    'buyer': {'cancelled'},
    # `picked_up` and `out_for_delivery` are deliberately not the seller's to
    # set. Pickup is the moment F2H collects the produce *and hands the farmer
    # their cash*, so it is a record of a payment made to them — the party
    # paying records it, not the party being paid. Leaving it to the seller
    # would let a farmer mark themselves paid for a collection that never
    # happened. `out_for_delivery` follows pickup and is equally F2H's.
    'seller': {'accepted', 'rejected', 'chat_active', 'confirmed', 'preparing',
               'ready_for_pickup', 'completed', 'cancelled'},
    'admin': {'accepted', 'rejected', 'admin_review', 'chat_active', 'confirmed',
              'preparing', 'picked_up', 'ready_for_pickup', 'out_for_delivery',
              'completed', 'cancelled'},
}

# The old names, so nothing that still speaks in account roles breaks.
ROLE_TRANSITIONS = {
    'customer': PARTY_TRANSITIONS['buyer'],
    'farmer': PARTY_TRANSITIONS['seller'],
    'admin': PARTY_TRANSITIONS['admin'],
}

_ROLE_ALIASES = {'customer': 'buyer', 'farmer': 'seller'}


def party_for(order, actor_id, actor_role):
    """Which side of this order the actor is on: 'buyer', 'seller', 'admin' or None.

    An admin is an admin everywhere. Otherwise the answer comes from the row:
    whoever placed it is the buyer and whoever is selling is the seller,
    regardless of what kind of account either of them holds.

    Returns None for someone with no stake in the order, which callers treat
    as "not authorised".
    """
    if actor_role == 'admin':
        return 'admin'
    if getattr(order, 'customer_id', None) == actor_id:
        return 'buyer'
    if getattr(order, 'farmer_id', None) == actor_id:
        return 'seller'
    return None


def party_may_set(party, new_status):
    return new_status in PARTY_TRANSITIONS.get(party, set())


# How far a buyer can get before their order stops being theirs to call off.
#
# Up to here the seller has promised nothing: no stock is committed, no produce
# is picked, and a cancellation costs them only the time they spent reading it.
# `confirmed` is where that changes — confirming deducts the seller's inventory
# and, under cash on delivery, sets them preparing goods they will not be paid
# for until someone stands at the customer's door. A cancellation after that
# point is a real loss to a real person, so it stops being unilateral.
#
# Cancelling *is* still possible from later states — the seller can, and so can
# an admin. What this removes is the buyer's ability to do it alone, which is
# what the app promises them at checkout. A promise the API does not keep is
# worse than no promise.
BUYER_CANCELLABLE_FROM = {'pending', 'admin_review', 'accepted', 'chat_active'}


def buyer_may_cancel(order) -> bool:
    """Whether the buyer can still call this order off by themselves."""
    return getattr(order, 'status', None) in BUYER_CANCELLABLE_FROM


def cancellation_refused_reason(order) -> str:
    """What to tell a buyer who tries anyway."""
    return ('The farmer has already confirmed this order and set the produce '
            'aside, so it can no longer be cancelled. Please contact them if '
            'something has gone wrong.')


def role_may_set(actor_role, new_status):
    """Deprecated: prefer party_may_set, which is correct when a farmer buys."""
    return party_may_set(_ROLE_ALIASES.get(actor_role, actor_role), new_status)


# The lifecycle forks after `preparing`, on how the produce leaves the farm:
#
#   delivery        preparing -> picked_up -> out_for_delivery -> completed
#   customer pickup preparing -> ready_for_pickup            -> completed
#
# `picked_up` is F2H collecting stock from the farm. `ready_for_pickup` is the
# customer collecting it themselves — the two have always read alike and mean
# opposite things, which is worth saying once here rather than guessing at each
# call site.
#
# `preparing -> out_for_delivery` was removed on purpose. It let a delivery
# order reach the customer without ever passing through pickup, and pickup is
# now where the farmer gets paid — so that shortcut was a way to deliver an
# order the farmer was never paid for.
VALID_TRANSITIONS = {
    'pending': ['accepted', 'rejected', 'cancelled', 'admin_review'],
    # `confirmed` is reachable from here for weekly baskets held short of stock:
    # an admin substitutes the missing items and sends the basket on its way.
    # Routing that through accepted → chat_active → confirmed would be three
    # clicks and a chat thread to fix a missing bunch of spinach.
    'admin_review': ['accepted', 'confirmed', 'rejected', 'cancelled'],
    'accepted': ['chat_active', 'cancelled'],
    'rejected': [],
    'chat_active': ['confirmed', 'cancelled'],
    'confirmed': ['preparing', 'cancelled'],
    'preparing': ['picked_up', 'ready_for_pickup', 'cancelled'],
    'picked_up': ['out_for_delivery', 'cancelled'],
    'ready_for_pickup': ['completed', 'cancelled'],
    'out_for_delivery': ['completed', 'cancelled'],
    'completed': [],
    'cancelled': [],
}


class PurchaseRequest(db.Model):
    __tablename__ = 'purchase_requests'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    quantity = db.Column(db.Numeric(10, 3), nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    # total_price is what the customer actually pays, so every existing revenue
    # sum keeps working without coupon awareness. subtotal is the pre-discount
    # figure kept alongside it.
    total_price = db.Column(db.Numeric(10, 2), nullable=False)
    subtotal = db.Column(db.Numeric(10, 2))
    discount_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupons.id', ondelete='SET NULL'))
    purchase_mode = db.Column(db.Enum('delivery', 'pickup'), nullable=False)
    status = db.Column(
        db.Enum('pending', 'admin_review', 'accepted', 'rejected', 'chat_active',
                'confirmed', 'preparing', 'picked_up', 'ready_for_pickup',
                'out_for_delivery', 'completed', 'cancelled'),
        default='pending'
    )
    delivery_address_id = db.Column(db.Integer, db.ForeignKey('addresses.id', ondelete='SET NULL'))
    delivery_notes = db.Column(db.Text)
    pickup_notes = db.Column(db.Text)
    customer_message = db.Column(db.Text)
    rejection_reason = db.Column(db.Text)
    cancellation_reason = db.Column(db.Text)
    cancelled_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    # Whether this order's quantity has been taken off the farmer's listing.
    # Set when the farmer confirms, cleared if a confirmed order is later
    # cancelled. Without it a cancellation cannot tell "give the stock back"
    # from "there was never any stock taken" — and status alone does not say,
    # because 'cancelled' is reachable from both sides of the confirm step.
    stock_committed = db.Column(db.Boolean, nullable=False, default=False)
    # Denormalised from the payments table so a list of orders can be rendered
    # without a join per row. 'not_required' covers everything placed before
    # online payment existed — those must not appear as unpaid forever.
    payment_status = db.Column(
        db.Enum('not_required', 'pending', 'paid', 'refunded', name='order_payment_status'),
        nullable=False, default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = db.relationship('User', foreign_keys=[customer_id])
    farmer = db.relationship('User', foreign_keys=[farmer_id])
    coupon = db.relationship('Coupon', foreign_keys=[coupon_id])
    product = db.relationship('Product', back_populates='requests')
    delivery_address = db.relationship('Address', foreign_keys=[delivery_address_id])
    canceller = db.relationship('User', foreign_keys=[cancelled_by])
    status_history = db.relationship('RequestStatusHistory', back_populates='request',
                                      cascade='all, delete-orphan', order_by='RequestStatusHistory.created_at')
    chat = db.relationship('Chat', back_populates='request', uselist=False)

    def can_transition_to(self, new_status):
        return new_status in VALID_TRANSITIONS.get(self.status, [])

    def to_dict(self, include_product=True, include_users=True):
        data = {
            'id': self.id,
            'customer_id': self.customer_id,
            'farmer_id': self.farmer_id,
            'product_id': self.product_id,
            'quantity': float(self.quantity),
            'unit_price': float(self.unit_price),
            'total_price': float(self.total_price),
            # Rows created before coupons existed have no subtotal; for those
            # the subtotal simply is the total.
            'subtotal': float(self.subtotal) if self.subtotal is not None else float(self.total_price),
            'discount_amount': float(self.discount_amount or 0),
            'coupon': {
                'id': self.coupon.id,
                'code': self.coupon.code,
                'label': self.coupon.label,
            } if self.coupon else None,
            'purchase_mode': self.purchase_mode,
            'status': self.status,
            'payment_status': self.payment_status,
            'delivery_address_id': self.delivery_address_id,
            'delivery_notes': self.delivery_notes,
            'pickup_notes': self.pickup_notes,
            'customer_message': self.customer_message,
            'rejection_reason': self.rejection_reason,
            'cancellation_reason': self.cancellation_reason,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'chat_id': self.chat.id if self.chat else None,
        }
        if include_product and self.product:
            data['product'] = {
                'id': self.product.id,
                'name': self.product.name,
                'unit': self.product.unit,
                'primary_image': self.product.primary_image.image_url if self.product.primary_image else None,
            }
        if include_users:
            if self.customer:
                data['customer'] = {
                    'id': self.customer.id,
                    'full_name': self.customer.full_name,
                    'avatar_url': self.customer.avatar_url,
                }
            if self.farmer:
                fp = self.farmer.farmer_profile
                data['farmer'] = {
                    'id': self.farmer.id,
                    'full_name': self.farmer.full_name,
                    'farm_name': fp.farm_name if fp else self.farmer.full_name,
                    'avatar_url': fp.avatar_url if fp else self.farmer.avatar_url,
                }
        if self.delivery_address:
            data['delivery_address'] = self.delivery_address.to_dict()
        return data


class RequestStatusHistory(db.Model):
    __tablename__ = 'request_status_history'

    id = db.Column(db.Integer, primary_key=True)
    # A history row belongs to either a purchase request or a family pack order.
    request_id = db.Column(db.Integer, db.ForeignKey('purchase_requests.id', ondelete='CASCADE'), nullable=True)
    family_pack_order_id = db.Column(db.Integer, db.ForeignKey('family_pack_orders.id', ondelete='CASCADE'),
                                     nullable=True)
    from_status = db.Column(db.String(50))
    to_status = db.Column(db.String(50), nullable=False)
    changed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    request = db.relationship('PurchaseRequest', back_populates='status_history')
    changer = db.relationship('User', foreign_keys=[changed_by])

    def to_dict(self):
        return {
            'id': self.id,
            'from_status': self.from_status,
            'to_status': self.to_status,
            'note': self.note,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'changed_by': {
                'id': self.changer.id,
                'full_name': self.changer.full_name,
            } if self.changer else None,
        }
