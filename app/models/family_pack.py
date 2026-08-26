from ..extensions import db
from datetime import datetime
from .request import VALID_TRANSITIONS

class FamilyPack(db.Model):
    __tablename__ = 'family_packs'

    id = db.Column(db.Integer, primary_key=True)
    # Nullable: an F2H-sold basket has no farm behind it. The customer builds
    # from the whole catalogue and F2H sources the items, so there is no single
    # seller to name — `create_subscription` sets this to None on purpose and
    # every reader already branches on it (`if sub.farmer_id is not None`).
    #
    # It stayed NOT NULL after the single-farm basket was removed, which made
    # every new basket die on `Column 'farmer_id' cannot be null`. Legacy rows
    # from the single-farm days keep their farmer and keep working.
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(250), nullable=False)
    description = db.Column(db.Text)
    banner_image = db.Column(db.String(500))
    price = db.Column(db.Numeric(10, 2), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    is_approved = db.Column(db.Boolean, default=False)
    is_featured = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = db.Column(db.DateTime)

    farmer = db.relationship('User', foreign_keys=[farmer_id])
    items = db.relationship('FamilyPackItem', back_populates='pack', cascade='all, delete-orphan')
    orders = db.relationship('FamilyPackOrder', back_populates='pack')

    def to_dict(self, include_farmer=True, include_items=True):
        data = {
            'id': self.id,
            'farmer_id': self.farmer_id,
            'name': self.name,
            'slug': self.slug,
            'description': self.description,
            'banner_image': self.banner_image,
            'price': float(self.price),
            'is_active': self.is_active,
            'is_approved': self.is_approved,
            'is_featured': self.is_featured,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_items:
            data['items'] = [item.to_dict() for item in self.items]
        if include_farmer and self.farmer:
            fp = self.farmer.farmer_profile
            data['farmer'] = {
                'id': self.farmer.id,
                'full_name': self.farmer.full_name,
                'farm_name': fp.farm_name if fp else self.farmer.full_name,
                'is_verified': fp.is_verified if fp else False,
                'rating_avg': float(fp.rating_avg) if fp and fp.rating_avg else 0.0,
                'avatar_url': fp.avatar_url or self.farmer.avatar_url if fp else self.farmer.avatar_url,
            }
        return data


class FamilyPackItem(db.Model):
    __tablename__ = 'family_pack_items'

    id = db.Column(db.Integer, primary_key=True)
    pack_id = db.Column(db.Integer, db.ForeignKey('family_packs.id', ondelete='CASCADE'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    quantity = db.Column(db.Numeric(10, 3), nullable=False)
    unit = db.Column(db.String(50), nullable=False)

    pack = db.relationship('FamilyPack', back_populates='items')
    product = db.relationship('Product')

    def to_dict(self):
        return {
            'id': self.id,
            'pack_id': self.pack_id,
            'product_id': self.product_id,
            'quantity': float(self.quantity),
            'unit': self.unit,
            'product': {
                'id': self.product.id,
                'name': self.product.name,
                'unit': self.product.unit,
                'primary_image': self.product.primary_image.image_url if self.product and self.product.primary_image else None,
                'price': float(self.product.price) if self.product else 0,
            } if self.product else None
        }


class FamilyPackOrder(db.Model):
    __tablename__ = 'family_pack_orders'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    # A one-off order references a curated pack; a recurring delivery references
    # the customer's weekly subscription instead. Exactly one is set.
    pack_id = db.Column(db.Integer, db.ForeignKey('family_packs.id'), nullable=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('family_pack_subscriptions.id', ondelete='CASCADE'),
                                nullable=True)
    delivery_date = db.Column(db.Date)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    # total_price is the payable amount; subtotal is the pre-discount figure.
    total_price = db.Column(db.Numeric(10, 2), nullable=False)
    subtotal = db.Column(db.Numeric(10, 2))
    discount_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    # See the matching column on PurchaseRequest. A basket is one order, so it
    # carries the fee once by construction.
    delivery_charge = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupons.id', ondelete='SET NULL'))
    purchase_mode = db.Column(db.String(20), default='delivery', nullable=False)
    status = db.Column(
        db.Enum('pending', 'admin_review', 'accepted', 'rejected', 'chat_active',
                'confirmed', 'preparing', 'picked_up', 'ready_for_pickup',
                'out_for_delivery', 'completed', 'cancelled'),
        default='pending'
    )
    # See the matching column on PurchaseRequest — it is what authorises a
    # delivery account to touch this order at all.
    assigned_delivery_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), index=True)
    delivery_address_id = db.Column(db.Integer, db.ForeignKey('addresses.id', ondelete='SET NULL'))
    delivery_notes = db.Column(db.Text)
    customer_message = db.Column(db.Text)
    rejection_reason = db.Column(db.Text)
    # Why a generated delivery is sitting in admin_review rather than confirmed
    # — normally the items nobody had in stock that week. Without it the queue
    # shows orders needing attention and no reason why.
    hold_reason = db.Column(db.String(500))
    cancellation_reason = db.Column(db.Text)
    cancelled_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    # Whether this order's items have been taken off the farmer's listings.
    # Set when the order is confirmed, cleared if a confirmed order is later
    # cancelled. See the same field on PurchaseRequest.
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
    pack = db.relationship('FamilyPack', back_populates='orders')
    coupon = db.relationship('Coupon', foreign_keys=[coupon_id])
    subscription = db.relationship('FamilyPackSubscription', back_populates='deliveries')
    delivery_address = db.relationship('Address', foreign_keys=[delivery_address_id])
    canceller = db.relationship('User', foreign_keys=[cancelled_by])
    courier = db.relationship('User', foreign_keys=[assigned_delivery_id])
    chat = db.relationship('Chat', foreign_keys='Chat.family_pack_order_id',
                           back_populates='family_pack_order', uselist=False)
    status_history = db.relationship('RequestStatusHistory',
                                     foreign_keys='RequestStatusHistory.family_pack_order_id',
                                     order_by='RequestStatusHistory.created_at')

    def can_transition_to(self, new_status):
        return new_status in VALID_TRANSITIONS.get(self.status, [])

    def to_dict(self, include_pack=True, include_users=True):
        data = {
            'id': self.id,
            'customer_id': self.customer_id,
            'farmer_id': self.farmer_id,
            'pack_id': self.pack_id,
            'subscription_id': self.subscription_id,
            'is_recurring': self.subscription_id is not None,
            'delivery_date': self.delivery_date.isoformat() if self.delivery_date else None,
            'unit_price': float(self.unit_price),
            'total_price': float(self.total_price),
            # Orders created before coupons existed have no subtotal; for those
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
            'customer_message': self.customer_message,
            'rejection_reason': self.rejection_reason,
            'hold_reason': self.hold_reason,
            'cancellation_reason': self.cancellation_reason,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_pack:
            if self.pack:
                data['pack'] = self.pack.to_dict(include_farmer=False, include_items=True)
            elif self.subscription:
                # Recurring deliveries have no curated pack — present the
                # subscription's basket in the same shape so clients can share code.
                data['pack'] = {
                    'id': None,
                    'name': 'Weekly Basket',
                    'description': 'Your recurring weekly basket',
                    'price': float(self.total_price),
                    'items': [i.to_dict() for i in self.subscription.items],
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
        """This basket as the assigned delivery account should see it.

        The twin of `PurchaseRequest.for_courier`; see that one for why the
        farmer's payout never appears here. Kept as two methods rather than a
        shared mixin because the two models have no common base and inventing
        one for six lines would be the larger change — but they must stay in
        step, and a difference between them is a delivery account seeing more
        on one kind of order than the other.
        """
        data = self.to_dict()

        if self.customer:
            data.setdefault('customer', {})
            data['customer']['phone'] = self.customer.phone

        data['amount_to_collect'] = float(self.total_price or 0)
        return data


WEEKDAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                 'Friday', 'Saturday', 'Sunday']


class FamilyPackSubscription(db.Model):
    """A customer's standing weekly basket, sold by F2H.

    The customer chooses products from the whole catalogue and a weekday, an
    admin approves it, and one delivery is generated automatically each week
    while it is active.

    A basket is deliberately not tied to a farm. A household wants tomatoes from
    whoever has good tomatoes this week, and which farms supply each item is a
    sourcing question F2H answers — not a constraint the customer has to work
    within when building their weekly shop.
    """
    __tablename__ = 'family_pack_subscriptions'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    # Nullable, and NULL on everything created since baskets went catalogue-wide.
    # Older rows keep the farm they were built against and the generator still
    # honours it, so a subscription in flight keeps running across the change
    # rather than breaking on it.
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    # The courier who normally carries this basket.
    #
    # A weekly basket goes to the same door every week, so the round is usually
    # the same person's. Set once here and every generated delivery inherits it;
    # any single week can still be reassigned on the order itself, which is what
    # covers illness and holidays.
    #
    # SET NULL, not CASCADE: retiring a delivery account must not delete the
    # customer's standing order along with it.
    assigned_delivery_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), index=True)
    delivery_address_id = db.Column(db.Integer, db.ForeignKey('addresses.id', ondelete='SET NULL'))
    # 0 = Monday ... 6 = Sunday, matching datetime.date.weekday()
    delivery_weekday = db.Column(db.Integer, nullable=False)
    status = db.Column(db.Enum('pending', 'active', 'paused', 'cancelled'), default='pending')
    delivery_notes = db.Column(db.Text)
    customer_message = db.Column(db.Text)
    start_date = db.Column(db.Date, nullable=False)
    paused_until = db.Column(db.Date)
    last_generated_date = db.Column(db.Date)
    # The delivery date this basket was last reminded about. Not a timestamp:
    # the question the reminder job asks is "have I already warned them about
    # *this* delivery", and a date answers it idempotently however many times
    # the job runs.
    last_reminded_for = db.Column(db.Date)
    # A coupon offered at signup is redeemed immediately — so it is genuinely
    # spent and cannot be double-used — and the stored discount is applied to
    # the first delivery this subscription generates.
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupons.id', ondelete='SET NULL'))
    coupon_discount = db.Column(db.Numeric(10, 2))
    coupon_applied = db.Column(db.Boolean, nullable=False, default=False)
    cancelled_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = db.relationship('User', foreign_keys=[customer_id])
    farmer = db.relationship('User', foreign_keys=[farmer_id])
    coupon = db.relationship('Coupon', foreign_keys=[coupon_id])
    delivery_address = db.relationship('Address', foreign_keys=[delivery_address_id])
    items = db.relationship('FamilyPackSubscriptionItem', back_populates='subscription',
                            cascade='all, delete-orphan')
    courier = db.relationship('User', foreign_keys=[assigned_delivery_id])
    deliveries = db.relationship('FamilyPackOrder', back_populates='subscription',
                                 order_by='FamilyPackOrder.delivery_date')

    @property
    def weekly_total(self):
        """Priced from the farmer's current rates, so it tracks price changes."""
        return round(sum(i.line_total for i in self.items), 2)

    @property
    def weekday_name(self):
        return WEEKDAY_NAMES[self.delivery_weekday] if self.delivery_weekday is not None else None

    def next_delivery_date(self, from_date=None):
        """The next scheduled date strictly after whatever was last generated."""
        from datetime import timedelta
        base = from_date or self.start_date
        if self.last_generated_date and self.last_generated_date >= base:
            base = self.last_generated_date + timedelta(days=1)
        offset = (self.delivery_weekday - base.weekday()) % 7
        return base + timedelta(days=offset)

    @property
    def suppliers(self):
        """Which farms this basket's items come from, and what to buy from each.

        The customer builds from the whole catalogue and F2H sells the basket,
        so nothing else in the data model records who actually grows what is in
        it. Without this an admin approving a basket can see the items but has
        no idea who to ring — which is the first thing they need to do.

        Grouped rather than listed per item, because a sourcing round is
        organised by farm, not by vegetable.
        """
        by_farm = {}
        for item in self.items:
            product = item.product
            if product is None:
                continue
            farmer = product.farmer
            key = product.farmer_id
            entry = by_farm.setdefault(key, {
                'farmer_id': key,
                'farm_name': None,
                'phone': None,
                'items': [],
                'subtotal': 0.0,
            })
            if entry['farm_name'] is None and farmer is not None:
                profile = getattr(farmer, 'farmer_profile', None)
                entry['farm_name'] = (profile.farm_name if profile else None) or farmer.full_name
                entry['phone'] = farmer.phone
            entry['items'].append({
                'product_id': product.id,
                'name': product.name,
                'quantity': float(item.quantity),
                'unit': item.unit,
                'line_total': item.line_total,
            })
            entry['subtotal'] = round(entry['subtotal'] + item.line_total, 2)
        return sorted(by_farm.values(), key=lambda e: (e['farm_name'] or '').lower())

    def to_dict(self, include_items=True, include_users=True, include_deliveries=False,
                include_suppliers=False):
        data = {
            'id': self.id,
            'customer_id': self.customer_id,
            'farmer_id': self.farmer_id,
            'delivery_address_id': self.delivery_address_id,
            'delivery_weekday': self.delivery_weekday,
            'weekday_name': self.weekday_name,
            'status': self.status,
            'delivery_notes': self.delivery_notes,
            'customer_message': self.customer_message,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'paused_until': self.paused_until.isoformat() if self.paused_until else None,
            'last_generated_date': self.last_generated_date.isoformat() if self.last_generated_date else None,
            'next_delivery_date': self.next_delivery_date().isoformat() if self.status == 'active' else None,
            'weekly_total': self.weekly_total,
            'coupon': {
                'id': self.coupon.id,
                'code': self.coupon.code,
                'label': self.coupon.label,
                # False until the first delivery consumes it, which is what the
                # customer's "applies to your first basket" note keys off.
                'applied': self.coupon_applied,
                'discount': float(self.coupon_discount or 0),
            } if self.coupon else None,
            'assigned_delivery_id': self.assigned_delivery_id,
            'courier_name': self.courier.full_name if self.courier else None,
            'deliveries_count': len(self.deliveries),
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        if include_items:
            data['items'] = [i.to_dict() for i in self.items]
        if include_suppliers:
            # Admin-only. Carries farm phone numbers, so it is opt-in and never
            # rides on a customer-facing response.
            data['suppliers'] = self.suppliers
        if include_users:
            if self.customer:
                data['customer'] = {
                    'id': self.customer.id,
                    'full_name': self.customer.full_name,
                    'avatar_url': self.customer.avatar_url,
                    'phone': self.customer.phone,
                }
            if self.farmer:
                fp = self.farmer.farmer_profile
                data['farmer'] = {
                    'id': self.farmer.id,
                    'full_name': self.farmer.full_name,
                    'farm_name': fp.farm_name if fp else self.farmer.full_name,
                    'is_verified': fp.is_verified if fp else False,
                    'avatar_url': fp.avatar_url or self.farmer.avatar_url if fp else self.farmer.avatar_url,
                }
        if self.delivery_address:
            data['delivery_address'] = self.delivery_address.to_dict()
        if include_deliveries:
            data['deliveries'] = [d.to_dict(include_pack=False, include_users=False)
                                  for d in self.deliveries]
        return data


class FamilyPackSubscriptionItem(db.Model):
    __tablename__ = 'family_pack_subscription_items'

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(db.Integer,
                                db.ForeignKey('family_pack_subscriptions.id', ondelete='CASCADE'),
                                nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    quantity = db.Column(db.Numeric(10, 3), nullable=False)
    unit = db.Column(db.String(50), nullable=False)

    subscription = db.relationship('FamilyPackSubscription', back_populates='items')
    product = db.relationship('Product')

    @property
    def line_total(self):
        if not self.product:
            return 0.0
        return round(float(self.product.effective_price) * float(self.quantity), 2)

    def to_dict(self):
        return {
            'id': self.id,
            'subscription_id': self.subscription_id,
            'product_id': self.product_id,
            'quantity': float(self.quantity),
            'unit': self.unit,
            'line_total': self.line_total,
            'product': {
                'id': self.product.id,
                'name': self.product.name,
                'unit': self.product.unit,
                'primary_image': self.product.primary_image.image_url if self.product.primary_image else None,
                'price': float(self.product.effective_price),
                'available_quantity': float(self.product.available_quantity),
            } if self.product else None,
        }
