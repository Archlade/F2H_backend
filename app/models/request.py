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
    #
    # `accepted` and `chat_active` are gone: they came from a flow where the
    # farmer accepted and then negotiated in a chat that no longer exists, so an
    # order passed through both without anybody doing anything. The farmer now
    # confirms once and marks it prepared.
    #
    # `completed` remains theirs for the pickup lane only — a customer who
    # collects at the farm hands their cash straight to the farmer, so there is
    # no courier and no handover. The transition graph is what confines it to
    # `ready_for_pickup`.
    'seller': {'rejected', 'confirmed', 'preparing',
               'ready_for_pickup', 'completed', 'cancelled'},
    # Two states, and neither of them is `picked_up` or `completed`.
    #
    # A delivery account loads from the store room, carries the order
    # (`out_for_delivery`) and takes the customer's cash (`cash_collected`).
    # That is the whole of the job.
    #
    # **Not `completed`.** Delivering and settling up used to be one button: the
    # courier pressed it and the order was closed while the money was still in
    # their pocket. An order is only finished when that cash reaches F2H, and
    # the person holding it is not the person who should be able to declare
    # that. Completion belongs to the admin recording the handover.
    #
    # `picked_up` is deliberately not theirs even though it sounds like it is.
    # It means F2H collecting produce *from the farm* and handing the farmer
    # their cash — it happens before anything reaches the store room, and the
    # person driving to a customer's door is never at that gate. Giving it to
    # them would let a delivery account record a farmer as paid for a
    # collection they had no part in, and would put a payout figure in front of
    # somebody who has no reason to see one.
    #
    # **Cannot cancel.** A delivery account only ever touches an order the farm
    # has already been paid for and the store room has already released. A
    # cancellation there is not an order being called off, it is a write-off,
    # and that should cost a phone call to an admin rather than one tap by
    # whoever is standing at a door nobody answered.
    'delivery': {'out_for_delivery', 'cash_collected'},
    # `admin_review` stays admin-only and off the main route: the weekly-basket
    # generator parks a short basket there so somebody can substitute the
    # missing items before it goes out.
    'admin': {'rejected', 'admin_review', 'confirmed',
              'preparing', 'picked_up', 'ready_for_pickup', 'out_for_delivery',
              'cash_collected', 'completed', 'cancelled'},
}

# The old names, so nothing that still speaks in account roles breaks.
ROLE_TRANSITIONS = {
    'customer': PARTY_TRANSITIONS['buyer'],
    'farmer': PARTY_TRANSITIONS['seller'],
    'delivery': PARTY_TRANSITIONS['delivery'],
    'admin': PARTY_TRANSITIONS['admin'],
}

_ROLE_ALIASES = {'customer': 'buyer', 'farmer': 'seller'}


def party_for(order, actor_id, actor_role):
    """Which side of this order the actor is on, or None.

    'buyer', 'seller', 'delivery', 'admin' — or None for someone with no stake
    in the order, which callers treat as "not authorised".

    An admin is an admin everywhere. Otherwise the answer comes from the row:
    whoever placed it is the buyer, whoever is selling is the seller, and
    whoever it was assigned to is the delivery party — regardless of what kind
    of account any of them holds.

    **Delivery is decided by the assignment, not by the account role**, which is
    the same rule the other two follow and matters for the same reason. A
    `delivery` account with no assignment on this order is a stranger to it and
    gets None, so holding the role grants nothing on its own; it has to have
    been given the job. That is what keeps one delivery account out of another's
    orders without a single explicit check in any route.
    """
    if actor_role == 'admin':
        return 'admin'
    if getattr(order, 'customer_id', None) == actor_id:
        return 'buyer'
    if getattr(order, 'farmer_id', None) == actor_id:
        return 'seller'
    # Last, so that a delivery account which somehow also owns the order is
    # treated as its buyer or seller first — the stronger claim wins.
    if (actor_role == 'delivery'
            and getattr(order, 'assigned_delivery_id', None) == actor_id):
        return 'delivery'
    return None


# The customer has paid, whoever is still holding the cash.
#
# `completed` alone used to mean this, because the courier closed the order at
# the door. Completion now waits for the handover, so a sale would drop out of
# every revenue figure for as long as the money sat in somebody's pocket — the
# dashboard would dip because a courier had not been in yet, which says nothing
# about trading. Both states together preserve what `completed` used to mean.
SETTLED_STATUSES = ('cash_collected', 'completed')

# An order that is over, however it ended.
CLOSED_STATUSES = ('completed', 'cancelled', 'rejected')

# A named filter, not a status.
#
# Both clients ask for `?status=active_orders` on the orders screens, and the
# list endpoint compared it to the column — which matches nothing, so those
# pages have been showing an empty list to every farmer and every customer.
# Naming it here makes it mean what the clients always intended: everything
# still in progress.
ACTIVE_FILTER = 'active_orders'


def party_may_set(party, new_status, from_status=None):
    """Whether this side of the order may move it to `new_status`.

    `from_status` narrows one case that the party table alone cannot express.
    A seller closes an order only on the pickup lane, where the customer
    collected at the farm and paid them directly. On a delivery order the money
    goes courier → admin, and `cash_collected -> completed` is the handover —
    the farmer was paid at the gate long before and has no part in it. Without
    this a farmer could close a delivery order whose cash never reached F2H,
    and the courier's outstanding balance would silently drop.

    Callers that do not pass `from_status` get the old, looser behaviour, which
    is safe for everything except that one pair.
    """
    if new_status not in PARTY_TRANSITIONS.get(party, set()):
        return False
    if (new_status == 'completed' and party == 'seller'
            and from_status is not None and from_status != 'ready_for_pickup'):
        return False
    return True


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
    'pending': ['confirmed', 'rejected', 'cancelled', 'admin_review'],
    # Weekly baskets held short of stock: an admin substitutes the missing items
    # and sends the basket on its way, or calls it off.
    'admin_review': ['confirmed', 'rejected', 'cancelled'],
    # Retired. Kept as keys with a way forward so that anything still sitting in
    # one — a row the migration missed, a client posting an old value — can be
    # confirmed rather than stranded. Nothing enters them any more: no party may
    # set either, and no state leads to them.
    'accepted': ['confirmed', 'cancelled'],
    'chat_active': ['confirmed', 'cancelled'],
    'rejected': [],
    'confirmed': ['preparing', 'cancelled'],
    'preparing': ['picked_up', 'ready_for_pickup', 'cancelled'],
    'picked_up': ['out_for_delivery', 'cancelled'],
    # The customer collects at the farm and pays the farmer directly. No
    # courier, no cash to hand over, so it finishes in one step.
    'ready_for_pickup': ['completed', 'cancelled'],
    # Delivered and paid for — but the money is in the courier's pocket, not
    # F2H's. `completed` is no longer reachable from here.
    'out_for_delivery': ['cash_collected', 'cancelled'],
    # Only a handover finishes it. An admin records the cash and these close
    # together; see `record_remittance`.
    #
    # Still cancellable, because a delivery can be written off after the fact —
    # a customer who paid and then returned everything, say — and that has to
    # be recordable without inventing a state for it.
    'cash_collected': ['completed', 'cancelled'],
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
    # Included in total_price, and F2H's in full — the farmer is not paid a
    # share of it. `payment_service.freeze` subtracts this before splitting, so
    # commission is charged on produce only. Zero on every order but the first
    # of a cart checkout, because the fee is per delivery, not per line.
    delivery_charge = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupons.id', ondelete='SET NULL'))
    purchase_mode = db.Column(db.Enum('delivery', 'pickup'), nullable=False)
    status = db.Column(
        db.Enum('pending', 'admin_review', 'accepted', 'rejected', 'chat_active',
                'confirmed', 'preparing', 'picked_up', 'ready_for_pickup',
                'out_for_delivery', 'cash_collected', 'completed', 'cancelled'),
        default='pending'
    )
    # Which delivery account is carrying this order, or NULL for unassigned.
    #
    # This is the whole of the delivery role's authorisation: `party_for` reads
    # it, so an account that is not named here has no standing on the order and
    # every read and write refuses. ON DELETE SET NULL rather than CASCADE —
    # removing a delivery account must return their orders to the pool, not
    # delete the orders.
    assigned_delivery_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), index=True)
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
    courier = db.relationship('User', foreign_keys=[assigned_delivery_id])
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
            'delivery_charge': float(self.delivery_charge or 0),
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
        if self.courier:
            data['courier'] = {'id': self.courier.id,
                               'full_name': self.courier.full_name,
                               'phone': self.courier.phone}
        return data

    def for_courier(self):
        """This order as the assigned delivery account should see it.

        Two things on top of [to_dict], and one thing deliberately left off.

        **The customer's phone number**, because a delivery nobody answers the
        door for is a phone call, and without it the driver rings the office to
        have the office ring the customer.

        **The cash to collect** — `total_price`, delivery charge included,
        stated plainly so the amount asked for at the door is the amount the
        order says. It is already in `to_dict`; naming it again as
        `amount_to_collect` is for the driver's screen, where "total" among
        seven other figures is how somebody asks for the wrong number.

        **Never the farmer's payout.** A delivery account loads from the store
        room and never stands at a farm gate, so it has no reason to know what
        any farm is paid, and the field is absent from the payload rather than
        hidden by the app.
        """
        data = self.to_dict()

        if self.customer:
            data.setdefault('customer', {})
            data['customer']['phone'] = self.customer.phone

        data['amount_to_collect'] = float(self.total_price or 0)
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
