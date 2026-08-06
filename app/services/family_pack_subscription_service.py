"""Weekly recurring family pack baskets.

A customer picks products from one farm plus a weekday. The farmer accepts once,
after which one confirmed delivery is generated per week automatically.
"""
from datetime import date, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from ..extensions import db, socketio
from ..models import (FamilyPackSubscription, FamilyPackSubscriptionItem,
                      FamilyPackOrder, Product, Address, RequestStatusHistory)
from .notification_service import create_notification
from . import stock_service as stock

# How many days ahead a delivery is created, so the farmer can prepare.
LEAD_DAYS = 2
# Safety valve for a subscription that has been dormant for a long time.
MAX_CATCH_UP = 6


# ── helpers ────────────────────────────────────────────────────────────────

def _validated_items(farmer_id, items_data):
    """Resolve incoming {product_id, quantity} rows against one farmer's catalogue."""
    if not items_data:
        raise ValueError('Add at least one product to your basket')

    resolved = []
    seen = set()
    for row in items_data:
        product_id = row.get('product_id')
        qty = row.get('quantity')
        if not product_id or qty is None or float(qty) <= 0:
            continue
        if product_id in seen:
            raise ValueError('The same product was added twice')
        seen.add(product_id)

        product = Product.query.get(product_id)
        if not product or product.deleted_at or not product.is_active:
            raise ValueError(f'Product {product_id} is not available')
        if product.farmer_id != farmer_id:
            raise ValueError('All products in a weekly basket must come from the same farm')
        resolved.append((product, float(qty)))

    if not resolved:
        raise ValueError('Add at least one product to your basket')
    return resolved


def _notify(recipient_id, sender_id, notif_type, title, body, payload):
    create_notification(recipient_id, sender_id, notif_type, title, body, payload)
    socketio.emit('new_notification', {'type': notif_type, **payload}, room=f"user_{recipient_id}")


# ── subscription lifecycle ─────────────────────────────────────────────────

def create_subscription(customer_id: int, data: dict):
    farmer_id = data.get('farmer_id')
    if not farmer_id:
        raise ValueError('farmer_id is required')

    # A farmer may subscribe to another farm's basket, but not to their own —
    # it would generate weekly orders from themselves to themselves.
    if int(farmer_id) == customer_id:
        raise ValueError('This is your own farm')

    weekday = data.get('delivery_weekday')
    if weekday is None or not (0 <= int(weekday) <= 6):
        raise ValueError('Choose a delivery day between Monday (0) and Sunday (6)')

    address_id = data.get('delivery_address_id')
    if not address_id:
        raise ValueError('A delivery address is required')
    address = Address.query.filter_by(id=address_id, user_id=customer_id).first()
    if not address:
        raise ValueError('That delivery address does not belong to you')

    existing = FamilyPackSubscription.query.filter(
        FamilyPackSubscription.customer_id == customer_id,
        FamilyPackSubscription.farmer_id == farmer_id,
        FamilyPackSubscription.status.in_(['pending', 'active', 'paused'])
    ).first()
    if existing:
        raise ValueError('You already have a weekly basket with this farm')

    items = _validated_items(farmer_id, data.get('items', []))

    sub = FamilyPackSubscription(
        customer_id=customer_id,
        farmer_id=farmer_id,
        delivery_address_id=address_id,
        delivery_weekday=int(weekday),
        status='pending',
        delivery_notes=data.get('delivery_notes', ''),
        customer_message=data.get('customer_message', ''),
        start_date=date.today(),
    )
    db.session.add(sub)
    db.session.flush()

    for product, qty in items:
        db.session.add(FamilyPackSubscriptionItem(
            subscription_id=sub.id, product_id=product.id,
            quantity=qty, unit=product.unit,
        ))
    db.session.flush()

    # A coupon on a repeating order needs a rule, because the code can only be
    # spent once. It is redeemed here and now — so it genuinely cannot be used
    # again, and the customer finds out immediately if the code is bad — and
    # the discount is held on the subscription until the first delivery is
    # generated, which is where it comes off.
    coupon_code = data.get('coupon_code')
    if coupon_code:
        from .coupon_service import apply_to_total, redeem
        weekly_total = round(sum(
            float(product.effective_price) * float(qty) for product, qty in items
        ), 2)
        coupon, discount, _ = apply_to_total(coupon_code, weekly_total)
        if coupon:
            redeem(coupon, customer_id, weekly_total, discount)
            sub.coupon_id = coupon.id
            sub.coupon_discount = discount
            sub.coupon_applied = False

    db.session.commit()

    _notify(farmer_id, customer_id, 'new_request', 'New weekly basket request',
            f"A customer wants a weekly basket every {sub.weekday_name}.",
            {'subscription_id': sub.id})
    return sub


def update_subscription(subscription_id: int, customer_id: int, data: dict):
    sub = FamilyPackSubscription.query.filter_by(id=subscription_id, customer_id=customer_id).first()
    if not sub:
        return None
    if sub.status == 'cancelled':
        raise ValueError('This weekly basket has been cancelled')

    if 'delivery_weekday' in data:
        weekday = int(data['delivery_weekday'])
        if not (0 <= weekday <= 6):
            raise ValueError('Choose a delivery day between Monday (0) and Sunday (6)')
        sub.delivery_weekday = weekday

    if 'delivery_address_id' in data and data['delivery_address_id']:
        address = Address.query.filter_by(id=data['delivery_address_id'], user_id=customer_id).first()
        if not address:
            raise ValueError('That delivery address does not belong to you')
        sub.delivery_address_id = address.id

    if 'delivery_notes' in data:
        sub.delivery_notes = data['delivery_notes']

    if 'items' in data:
        items = _validated_items(sub.farmer_id, data['items'])
        sub.items = [FamilyPackSubscriptionItem(product_id=p.id, quantity=q, unit=p.unit)
                     for p, q in items]

    db.session.commit()
    return sub


def set_subscription_status(subscription_id: int, actor_id: int, actor_role: str,
                            new_status: str, data: dict = None):
    """Farmer accepts (pending → active); either side pauses, resumes or cancels."""
    data = data or {}
    sub = FamilyPackSubscription.query.get(subscription_id)
    if not sub:
        return None

    # By side of the subscription, not account role — a farmer who subscribes
    # to another farm is the buyer on that row.
    from ..models.request import party_for
    if party_for(sub, actor_id, actor_role) is None:
        raise PermissionError('Not authorized')

    allowed = {
        'pending': ['active', 'cancelled'],
        'active': ['paused', 'cancelled'],
        'paused': ['active', 'cancelled'],
        'cancelled': [],
    }
    if new_status not in allowed.get(sub.status, []):
        raise ValueError(f"Cannot go from '{sub.status}' to '{new_status}'")

    if new_status == 'active' and sub.status == 'pending' and actor_role != 'farmer':
        raise PermissionError('Only the farmer can accept a weekly basket')

    was = sub.status
    sub.status = new_status

    if new_status == 'paused':
        weeks = int(data.get('weeks') or 0)
        sub.paused_until = date.today() + timedelta(weeks=weeks) if weeks else None
    elif new_status == 'active':
        sub.paused_until = None
    elif new_status == 'cancelled':
        sub.cancelled_at = datetime.utcnow()

    db.session.commit()

    other = sub.customer_id if actor_id == sub.farmer_id else sub.farmer_id
    if was == 'pending' and new_status == 'active':
        _notify(sub.customer_id, actor_id, 'request_accepted', 'Weekly basket confirmed',
                f"Your basket will arrive every {sub.weekday_name}.", {'subscription_id': sub.id})
    else:
        _notify(other, actor_id, 'status_update', 'Weekly basket updated',
                f"The weekly basket is now {new_status}.", {'subscription_id': sub.id})
    return sub


# ── weekly delivery generation ─────────────────────────────────────────────

def _create_delivery(sub, delivery_date):
    """One confirmed delivery. Returns None if it already exists."""
    subtotal = sub.weekly_total
    if subtotal <= 0:
        return None

    # The signup coupon comes off the first delivery only; every week after
    # that is full price, because the code was single use.
    discount = 0.0
    coupon_id = None
    if sub.coupon_id and not sub.coupon_applied:
        discount = min(float(sub.coupon_discount or 0), subtotal)
        coupon_id = sub.coupon_id

    total = round(subtotal - discount, 2)

    # Stock comes off before the delivery row is created, not after.
    #
    # A weekly delivery is born already `confirmed` — the farmer accepted the
    # subscription once and these runs skip the accept step — so it owes the
    # same deduction as any other confirmed order. Taking it first means a
    # shortfall costs nothing to unwind: there is no order yet to delete, and
    # crucially no rollback, which would take every other subscription's
    # delivery in this batch down with it.
    #
    # This used to be `max(0.0, available - wanted)`: a shortfall clamped to
    # zero and the delivery was generated anyway, leaving the farmer owing a
    # basket they had no stock for with nothing recorded to say so.
    stock_items = [(item.product, item.quantity) for item in sub.items if item.product]
    try:
        stock.commit_items(stock_items)
    except stock.InsufficientStock as short:
        # This week is skipped rather than half-filled. The notification is
        # queued on the session and lands with the batch's own commit.
        _notify(sub.farmer_id, sub.customer_id, 'status_update',
                'Weekly basket could not be prepared',
                f"The basket due {delivery_date:%a %d %b} was not created. {short}",
                {'subscription_id': sub.id})
        return None

    order = FamilyPackOrder(
        customer_id=sub.customer_id,
        farmer_id=sub.farmer_id,
        pack_id=None,
        subscription_id=sub.id,
        delivery_date=delivery_date,
        unit_price=subtotal,
        subtotal=subtotal,
        discount_amount=discount,
        total_price=total,
        coupon_id=coupon_id,
        purchase_mode='delivery',
        # The farmer already accepted the subscription, so weekly runs skip
        # straight past the accept/chat stages.
        status='confirmed',
        delivery_address_id=sub.delivery_address_id,
        delivery_notes=sub.delivery_notes,
        customer_message=sub.customer_message,
        # The deduction above is part of this transaction, so if anything below
        # rolls back the stock goes back with it — and if the order survives,
        # this flag is what lets a later cancellation return it.
        stock_committed=True,
    )
    db.session.add(order)
    try:
        db.session.flush()
    except IntegrityError:
        # Another request generated this same week concurrently.
        db.session.rollback()
        return None

    # Marked only after the delivery row survives the flush, so a duplicate
    # week that rolls back above doesn't silently consume the discount.
    if coupon_id:
        sub.coupon_applied = True
        # Point the redemption at the order that actually carried the discount,
        # so the admin's usage report links to a real order rather than nothing.
        from ..models import CouponRedemption
        redemption = CouponRedemption.query.filter_by(coupon_id=coupon_id).first()
        if redemption and redemption.family_pack_order_id is None:
            redemption.family_pack_order_id = order.id

    db.session.add(RequestStatusHistory(
        family_pack_order_id=order.id, from_status=None, to_status='confirmed',
        changed_by=sub.customer_id, note='Auto-generated from weekly basket',
    ))

    # Freeze what will be collected at the door.
    #
    # A weekly delivery is born at 'confirmed' rather than transitioning into
    # it, so it never passes through `order_money.settle` and would otherwise
    # reach the customer with no payment row at all. The collect endpoint does
    # create one on the spot when it finds none — so this is not the difference
    # between working and broken — but it would snapshot the commission rate on
    # the *delivery day* instead of the day the order was made. Two baskets
    # generated the same morning could then split differently if the rate
    # changed in between, which is exactly the drift `commission_rate` is a
    # column to prevent.
    #
    # After the flush, so `order.id` exists for the foreign key.
    from . import payment_service as payments
    payments.ensure_for_order(order)

    return order


def generate_due_deliveries(today=None):
    """Create any deliveries now due. Safe to call repeatedly."""
    today = today or date.today()
    horizon = today + timedelta(days=LEAD_DAYS)
    created = []

    subs = FamilyPackSubscription.query.filter_by(status='active').all()
    for sub in subs:
        if sub.paused_until and sub.paused_until > today:
            continue
        if not sub.items:
            continue

        for _ in range(MAX_CATCH_UP):
            due = sub.next_delivery_date()
            if due > horizon:
                break
            order = _create_delivery(sub, due)
            sub.last_generated_date = due
            if order is not None:
                created.append(order)

    if created:
        db.session.commit()
        for order in created:
            _notify(order.farmer_id, order.customer_id, 'status_update',
                    'Weekly basket to prepare',
                    f"A weekly basket is scheduled for {order.delivery_date:%a %d %b}.",
                    {'order_id': order.id, 'subscription_id': order.subscription_id})
            _notify(order.customer_id, order.farmer_id, 'status_update',
                    'Weekly basket on the way',
                    f"Your basket is confirmed for {order.delivery_date:%a %d %b}.",
                    {'order_id': order.id, 'subscription_id': order.subscription_id})
    else:
        db.session.commit()
    return created


# ── queries ────────────────────────────────────────────────────────────────

def get_subscriptions_for_customer(customer_id: int, status=None):
    query = FamilyPackSubscription.query.filter_by(customer_id=customer_id)
    if status:
        query = query.filter(FamilyPackSubscription.status == status)
    else:
        query = query.filter(FamilyPackSubscription.status != 'cancelled')
    return query.order_by(FamilyPackSubscription.created_at.desc()).all()


def get_subscriptions_for_farmer(farmer_id: int, status=None):
    query = FamilyPackSubscription.query.filter_by(farmer_id=farmer_id)
    if status:
        query = query.filter(FamilyPackSubscription.status == status)
    else:
        query = query.filter(FamilyPackSubscription.status != 'cancelled')
    return query.order_by(FamilyPackSubscription.created_at.desc()).all()


def get_subscription(subscription_id: int, user_id: int):
    sub = FamilyPackSubscription.query.get(subscription_id)
    if not sub:
        return None
    if user_id not in (sub.customer_id, sub.farmer_id):
        raise PermissionError('Not authorized')
    return sub
