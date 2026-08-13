"""Weekly recurring family pack baskets.

A customer picks products from one farm plus a weekday. The farmer accepts once,
after which one confirmed delivery is generated per week automatically.
"""
import logging
from datetime import date, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from ..extensions import db, socketio
from ..models import (FamilyPackSubscription, FamilyPackSubscriptionItem,
                      FamilyPackOrder, Product, Address, RequestStatusHistory)
from .notification_service import create_notification
from . import stock_service as stock

logger = logging.getLogger(__name__)

# How many days ahead a delivery is created, so there is time to source it.
LEAD_DAYS = 2
# Safety valve for a subscription that has been dormant for a long time.
MAX_CATCH_UP = 6


# ── helpers ────────────────────────────────────────────────────────────────

def _validated_items(items_data):
    """Resolve incoming {product_id, quantity} rows against the basket catalogue.

    No farm restriction — a basket is sold by F2H and sourced from wherever the
    produce is, so picking tomatoes from one farm and spinach from another is
    the normal case rather than an error.

    There *is* a catalogue restriction: only products an admin has marked
    `basket_eligible` may go in. Enforced here and not just in the builder,
    because the builder is a filtered list and a request body is whatever the
    caller sends — an old client, a stale tab, or a crafted call would otherwise
    commit F2H to sourcing something weekly that it never agreed to.
    """
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
        if not product.basket_eligible:
            raise ValueError(f'{product.name} is not available for weekly baskets')

        # The farmer's minimum applies here exactly as it does to a one-off
        # purchase request, which enforces it in request_service.
        #
        # This path did not, so a weekly basket could commit a farmer to
        # picking half a kilo every week against a two-kilo minimum — and
        # unlike a single order, that repeats forever.
        if float(qty) < float(product.min_quantity):
            raise ValueError(
                f'{product.name}: the minimum order is '
                f'{float(product.min_quantity):g} {product.unit}'
            )

        resolved.append((product, float(qty)))

    if not resolved:
        raise ValueError('Add at least one product to your basket')
    return resolved


def _notify(recipient_id, sender_id, notif_type, title, body, payload):
    create_notification(recipient_id, sender_id, notif_type, title, body, payload)
    socketio.emit('new_notification', {'type': notif_type, **payload}, room=f"user_{recipient_id}")


def _notify_admins(sender_id, notif_type, title, body, payload):
    """Tell every admin. Baskets are approved by whoever gets to it first.

    Sent to all of them rather than a nominated one: a basket waiting on the one
    admin who is on holiday is a customer waiting with nobody able to see why,
    which is the failure this whole screen exists to prevent.
    """
    from ..models import Role, User
    admins = (User.query.join(Role, User.role_id == Role.id)
              .filter(Role.name == 'admin', User.is_active.is_(True),
                      User.deleted_at.is_(None))
              .all())
    for admin in admins:
        _notify(admin.id, sender_id, notif_type, title, body, payload)
    return len(admins)


# ── subscription lifecycle ─────────────────────────────────────────────────

def create_subscription(customer_id: int, data: dict):
    # No farmer_id. The customer builds from the whole catalogue and F2H sells
    # the basket; any `farmer_id` in the request body is ignored rather than
    # honoured, so an old client cannot pin a basket to a single farm.

    weekday = data.get('delivery_weekday')
    if weekday is None or not (0 <= int(weekday) <= 6):
        raise ValueError('Choose a delivery day between Monday (0) and Sunday (6)')

    address_id = data.get('delivery_address_id')
    if not address_id:
        raise ValueError('A delivery address is required')
    address = Address.query.filter_by(id=address_id, user_id=customer_id).first()
    if not address:
        raise ValueError('That delivery address does not belong to you')

    # One live basket per customer.
    #
    # It used to be one per farm, which only made sense while a basket belonged
    # to a farm. Now that a basket is the whole weekly shop, a second one is
    # almost always a customer who meant to edit the first — and two standing
    # baskets on different weekdays is a support conversation nobody wants.
    existing = FamilyPackSubscription.query.filter(
        FamilyPackSubscription.customer_id == customer_id,
        FamilyPackSubscription.status.in_(['pending', 'active', 'paused'])
    ).first()
    if existing:
        raise ValueError('You already have a weekly basket. Edit it instead of '
                         'starting a second one.')

    items = _validated_items(data.get('items', []))

    sub = FamilyPackSubscription(
        customer_id=customer_id,
        # No farm. F2H sources the items and sells the basket.
        farmer_id=None,
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

    # Admins, not a farmer — there is no farmer to tell, and a basket sits at
    # 'pending' doing nothing until somebody approves it.
    _notify_admins(customer_id, 'new_request', 'New weekly basket to approve',
                   f"A customer wants a weekly basket every {sub.weekday_name}. "
                   f"{len(items)} item{'s' if len(items) != 1 else ''}, "
                   f"₹{sub.weekly_total:.2f} a week.",
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
        items = _validated_items(data['items'])
        sub.items = [FamilyPackSubscriptionItem(product_id=p.id, quantity=q, unit=p.unit)
                     for p, q in items]

    db.session.commit()
    return sub


def set_subscription_status(subscription_id: int, actor_id: int, actor_role: str,
                            new_status: str, data: dict = None):
    """Admin approves (pending → active); customer or admin pauses, resumes, cancels."""
    data = data or {}
    sub = FamilyPackSubscription.query.get(subscription_id)
    if not sub:
        return None

    # A basket has no farm any more, so the only parties are the customer who
    # owns it and an admin. `party_for` still resolves a seller on legacy rows
    # that kept their farmer_id, which is what lets those keep working.
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

    # Approving a basket is F2H's call, not the customer's.
    #
    # It commits the platform to sourcing that produce every week and to selling
    # it, so a customer cannot approve their own request — otherwise the
    # approval step is decoration. A farmer on a legacy single-farm basket can
    # still accept theirs, which is what keeps those running until they are
    # edited across.
    if new_status == 'active' and sub.status == 'pending':
        legacy_farmer = sub.farmer_id is not None and actor_id == sub.farmer_id
        if actor_role != 'admin' and not legacy_farmer:
            raise PermissionError('Only an admin can approve a weekly basket')

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

    if was == 'pending' and new_status == 'active':
        _notify(sub.customer_id, actor_id, 'request_accepted', 'Weekly basket approved',
                f"Your basket will arrive every {sub.weekday_name}.", {'subscription_id': sub.id})
    else:
        # Tell whoever did not do it.
        #
        # This was `sub.customer_id if actor_id == sub.farmer_id else sub.farmer_id`,
        # which on a basket with no farm resolves to None for everyone — an
        # admin pausing a basket would have notified nobody, and the customer
        # would have found out when produce stopped arriving.
        if actor_id == sub.customer_id:
            _notify_admins(actor_id, 'status_update', 'Weekly basket updated',
                           f"A customer's weekly basket is now {new_status}.",
                           {'subscription_id': sub.id})
        else:
            _notify(sub.customer_id, actor_id, 'status_update', 'Weekly basket updated',
                    f"Your weekly basket is now {new_status}.", {'subscription_id': sub.id})
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

    # Who sells this week's basket.
    #
    # F2H, unless this is a legacy single-farm subscription that still carries a
    # farmer. Resolved before the stock deduction so a misconfigured platform
    # seller fails without having moved anything.
    if sub.farmer_id is not None:
        seller_id = sub.farmer_id
    else:
        from .platform_seller import NoPlatformSeller, platform_seller
        try:
            seller_id = platform_seller().id
        except NoPlatformSeller as exc:
            # Loud, and for this subscription only — the batch continues so one
            # bad setting does not stop every customer's basket. It will recur
            # every run until fixed, which is the intent.
            logger.error('Cannot generate basket #%s for %s: %s',
                         sub.id, delivery_date, exc)
            return None

    # Stock comes off before the delivery row is created, not after.
    #
    # Taking it first means a shortfall costs nothing to unwind: there is no
    # order yet to delete, and crucially no rollback, which would take every
    # other subscription's delivery in this batch down with it.
    #
    # This used to be `max(0.0, available - wanted)`: a shortfall clamped to
    # zero and the delivery was generated anyway, leaving a basket owed that
    # nobody had stock for, with nothing recorded to say so.
    stock_items = [(item.product, item.quantity) for item in sub.items if item.product]
    hold_reason = None
    try:
        stock.commit_items(stock_items)
        committed = True
    except stock.InsufficientStock as short:
        # Held for an admin to substitute rather than skipped.
        #
        # A basket now spans several farms, so one grower being short is routine
        # and should not cost the customer their week's groceries. The delivery
        # is still created — at `admin_review`, carrying the reason — so it
        # appears in the admin queue with the missing items named. No stock is
        # taken: whatever is substituted in will be deducted when it is confirmed.
        hold_reason = str(short)[:500]
        committed = False

    order = FamilyPackOrder(
        customer_id=sub.customer_id,
        farmer_id=seller_id,
        pack_id=None,
        subscription_id=sub.id,
        delivery_date=delivery_date,
        unit_price=subtotal,
        subtotal=subtotal,
        discount_amount=discount,
        total_price=total,
        coupon_id=coupon_id,
        purchase_mode='delivery',
        # Approved once at the subscription, so ordinary weeks skip straight
        # past the accept/chat stages. A short week waits for an admin instead.
        status='admin_review' if hold_reason else 'confirmed',
        hold_reason=hold_reason,
        delivery_address_id=sub.delivery_address_id,
        delivery_notes=sub.delivery_notes,
        customer_message=sub.customer_message,
        # The deduction above is part of this transaction, so if anything below
        # rolls back the stock goes back with it — and if the order survives,
        # this flag is what lets a later cancellation return it.
        stock_committed=committed,
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
        family_pack_order_id=order.id, from_status=None, to_status=order.status,
        changed_by=sub.customer_id,
        note=('Auto-generated from weekly basket — held for substitution: '
              f'{hold_reason}') if hold_reason else 'Auto-generated from weekly basket',
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

    db.session.commit()

    for order in created:
        payload = {'order_id': order.id, 'subscription_id': order.subscription_id}

        if order.status == 'admin_review':
            # Nobody is told their basket is coming, because it is not — it is
            # waiting on a substitution. Telling the customer "confirmed" here
            # and cancelling it later is worse than a day's silence.
            _notify_admins(order.customer_id, 'status_update',
                           'Weekly basket needs a substitution',
                           f"The basket due {order.delivery_date:%a %d %b} is short: "
                           f"{order.hold_reason}",
                           payload)
            continue

        _notify(order.farmer_id, order.customer_id, 'status_update',
                'Weekly basket to prepare',
                f"A weekly basket is scheduled for {order.delivery_date:%a %d %b}.",
                payload)
        _notify(order.customer_id, order.farmer_id, 'status_update',
                'Weekly basket on the way',
                f"Your basket is confirmed for {order.delivery_date:%a %d %b}.",
                payload)
    return created


def remove_product_from_baskets(product):
    """Take a delisted product out of every live basket. Returns affected subs.

    Called when an admin removes a product from the weekly basket catalogue.
    Running baskets are edited rather than left alone, so nobody is delivered an
    item F2H has stopped sourcing — but that means a customer's weekly shop
    changes without them asking, so every one of them is told.

    Nothing is committed here; the caller owns the transaction, so the flag and
    its consequences land together. Half of this applied would mean a product
    delisted in the admin panel and still arriving on doorsteps.
    """
    subs = (FamilyPackSubscription.query
            .join(FamilyPackSubscriptionItem,
                  FamilyPackSubscriptionItem.subscription_id == FamilyPackSubscription.id)
            .filter(FamilyPackSubscriptionItem.product_id == product.id,
                    FamilyPackSubscription.status.in_(['pending', 'active', 'paused']))
            .distinct().all())

    affected = []
    for sub in subs:
        removed = [i for i in sub.items if i.product_id == product.id]
        if not removed:
            continue
        for item in removed:
            db.session.delete(item)
        # Read before the delete is flushed, so "what is left" is not a guess.
        remaining = [i for i in sub.items if i.product_id != product.id]

        if not remaining:
            # An empty basket generates nothing — `generate_due_deliveries`
            # skips subscriptions with no items — so it would sit "active" and
            # silently deliver nothing forever. Paused says the same thing
            # honestly, and the customer's notification tells them why.
            sub.status = 'paused'
            sub.paused_until = None

        affected.append((sub, not remaining))

    return affected


def notify_product_removed(affected, product):
    """Tell customers their basket changed. Call after the transaction commits."""
    for sub, emptied in affected:
        if emptied:
            _notify(sub.customer_id, None, 'status_update',
                    'Your weekly basket has been paused',
                    f'{product.name} is no longer available for weekly baskets, '
                    'and it was the only item in yours. Add something else and '
                    'resume whenever you like.',
                    {'subscription_id': sub.id})
        else:
            _notify(sub.customer_id, None, 'status_update',
                    'An item was removed from your weekly basket',
                    f'{product.name} is no longer available for weekly baskets, '
                    f'so we have taken it out. The rest of your basket arrives '
                    f'every {sub.weekday_name} as usual — now ₹{sub.weekly_total:.2f}.',
                    {'subscription_id': sub.id})


# ── reminders ──────────────────────────────────────────────────────────────

# How far ahead of a delivery to remind. Deliberately one day *more* than
# LEAD_DAYS: the reminder has to land before the delivery is generated, because
# generating it confirms the order and commits a farmer's stock. A reminder that
# arrives after that can only describe what is already happening.
REMIND_DAYS = LEAD_DAYS + 1


def send_basket_reminders(today=None):
    """Remind customers whose weekly basket is about to be prepared.

    Returns the subscriptions reminded, so a caller (and the cron log) can see
    whether it did anything.

    Idempotent by design. This runs from cron and will be run twice sooner or
    later — a retry, a manual trigger, an overlapping schedule. `last_reminded_for`
    records the delivery date already covered, so a second run in the same window
    finds nothing to do. Three copies of the same reminder is how people learn to
    mute an app.
    """
    today = today or date.today()
    window = today + timedelta(days=REMIND_DAYS)
    reminded = []

    subs = FamilyPackSubscription.query.filter_by(status='active').all()
    for sub in subs:
        if sub.paused_until and sub.paused_until > today:
            continue
        if not sub.items:
            continue

        due = sub.next_delivery_date()
        if due is None or due > window:
            continue
        # Already covered — either by an earlier run today, or because this
        # delivery has since been generated and the customer has the real
        # confirmation instead.
        if sub.last_reminded_for == due:
            continue
        if sub.last_generated_date and sub.last_generated_date >= due:
            continue

        sub.last_reminded_for = due
        reminded.append((sub, due))

    # Committed before notifying, so a failure while sending cannot leave the
    # marker unset and re-send the whole batch on the next run.
    db.session.commit()

    for sub, due in reminded:
        days = (due - today).days
        when = 'tomorrow' if days == 1 else f'in {days} days' if days > 1 else 'today'
        _notify(sub.customer_id, None, 'basket_reminder',
                f'Your weekly basket arrives {when}',
                f"{len(sub.items)} item{'s' if len(sub.items) != 1 else ''}, "
                f"₹{sub.weekly_total:.2f}, due {due:%a %d %b}. "
                'Change or pause it before we start picking.',
                {'subscription_id': sub.id, 'delivery_date': due.isoformat()})

    return [sub for sub, _ in reminded]


# ── queries ────────────────────────────────────────────────────────────────

def get_subscriptions_for_customer(customer_id: int, status=None):
    query = FamilyPackSubscription.query.filter_by(customer_id=customer_id)
    if status:
        query = query.filter(FamilyPackSubscription.status == status)
    else:
        query = query.filter(FamilyPackSubscription.status != 'cancelled')
    return query.order_by(FamilyPackSubscription.created_at.desc()).all()


def get_subscriptions_for_farmer(farmer_id: int, status=None):
    """Baskets this farmer supplies — by their produce, not by ownership.

    This used to be `filter_by(farmer_id=...)`, which was right when a basket
    belonged to one farm. Now that F2H sells them the column is NULL, and that
    filter would have returned nothing: every farmer's Weekly Baskets screen
    would have read "no weekly baskets yet" no matter how much of their produce
    was going out every week.

    Matched through the items instead, so a farmer sees any basket containing
    something they grow.
    """
    query = (FamilyPackSubscription.query
             .join(FamilyPackSubscriptionItem,
                   FamilyPackSubscriptionItem.subscription_id == FamilyPackSubscription.id)
             .join(Product, Product.id == FamilyPackSubscriptionItem.product_id)
             .filter(db.or_(FamilyPackSubscription.farmer_id == farmer_id,
                            Product.farmer_id == farmer_id))
             .distinct())
    if status:
        query = query.filter(FamilyPackSubscription.status == status)
    else:
        query = query.filter(FamilyPackSubscription.status != 'cancelled')
    return query.order_by(FamilyPackSubscription.created_at.desc()).all()


def _supplies(sub, user_id) -> bool:
    """Does this user grow anything in this basket?"""
    return any(item.product is not None and item.product.farmer_id == user_id
               for item in sub.items)


def get_subscription(subscription_id: int, user_id: int, actor_role=None):
    sub = FamilyPackSubscription.query.get(subscription_id)
    if not sub:
        return None
    # Admins see every basket — they approve and source them. Suppliers see the
    # ones they grow for; on an F2H-sold basket `farmer_id` is NULL, so
    # ownership alone would have locked out every farmer who fills one.
    if (actor_role != 'admin'
            and user_id not in (sub.customer_id, sub.farmer_id)
            and not _supplies(sub, user_id)):
        raise PermissionError('Not authorized')
    return sub
