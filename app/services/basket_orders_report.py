"""Weekly basket deliveries coming up, and what has to be sourced for them.

A forward-looking sourcing list, not a history. One row per (delivery, product),
so sorting by product gives total demand for the fortnight — which is the
question this answers: how much spinach do we need to buy, and from when.

── Why this projects rather than reading the orders table ─────────────────────

`FamilyPackOrder` rows are only created `LEAD_DAYS` (2) days ahead, by the
nightly cron. Reading "future orders" straight out of that table would return
at most two days of deliveries, so a fortnightly report would arrive nearly
empty and look broken.

So the window is built from both ends:

  * **Deliveries that already exist** — real orders, with their real id and
    status. These are the next couple of days.
  * **Deliveries not yet generated** — projected from each active subscription's
    weekday, marked `Scheduled`, with no order number because there is not one
    yet.

The projection deliberately mirrors `generate_due_deliveries` in
`family_pack_subscription_service.py`: same weekday arithmetic, same treatment
of pauses and of `last_generated_date`. If that logic changes, this has to
change with it, or the report will promise deliveries that never appear.

Prices are today's `effective_price`, and the sheet says so. A basket is priced
when its delivery is generated, so a projected line is an estimate — accurate
unless a farmer changes a price between now and then.
"""

from datetime import date, timedelta

from sqlalchemy.orm import joinedload

from ..models import FamilyPackOrder, FamilyPackSubscription

# Two weeks, matching how often the job runs — each file covers the period up to
# the next one.
HORIZON_DAYS = 14

# Cancelled and rejected deliveries are not work anybody has to do, and in a
# picking list they are noise that makes the real rows harder to see.
EXCLUDED_STATUSES = ('cancelled', 'rejected')


def _projected_dates(subscription, today, horizon):
    """Delivery dates for this subscription that do not have an order yet.

    Starts after `last_generated_date` so an already-created delivery is not
    also projected — it is counted from the orders table instead, where it has
    a real status.
    """
    base = subscription.start_date
    if subscription.last_generated_date and subscription.last_generated_date >= base:
        base = subscription.last_generated_date + timedelta(days=1)
    if base < today:
        base = today

    # A basket paused until a future date resumes on its own weekday after that,
    # so skip anything falling inside the pause rather than dropping it entirely.
    paused_until = subscription.paused_until

    offset = (subscription.delivery_weekday - base.weekday()) % 7
    day = base + timedelta(days=offset)
    while day <= horizon:
        if not (paused_until and day <= paused_until):
            yield day
        day += timedelta(days=7)


def _item_rows(subscription, delivery_date, status, order_id, source):
    """One row per product in this basket."""
    customer = subscription.customer
    rows = []
    for item in subscription.items:
        product = item.product
        quantity = float(item.quantity or 0)
        unit_price = float(product.effective_price) if product else 0.0
        rows.append({
            # Blue on a projection: it is a real commitment but not yet a real
            # order row, and the difference matters if someone goes looking for
            # the order number.
            '_highlight': 'info' if source == 'Scheduled' else None,
            'delivery_date': delivery_date.isoformat(),
            'weekday': delivery_date.strftime('%A'),
            'order_id': order_id or '',
            'status': status,
            'source': source,
            'subscription_id': subscription.id,
            'customer_name': customer.full_name if customer else '',
            'customer_phone': (customer.phone or '') if customer else '',
            'product_name': product.name if product else f'Product {item.product_id}',
            'farm_name': _farm_of(product),
            'quantity': quantity,
            'unit': item.unit or (product.unit if product else ''),
            'unit_price': unit_price,
            'line_total': round(unit_price * quantity, 2),
        })
    return rows


def _farm_of(product):
    """Which farm supplies this item — the first thing to know when sourcing."""
    if product is None or product.farmer is None:
        return ''
    profile = product.farmer.farmer_profile
    return profile.farm_name if profile else product.farmer.full_name


def build_rows(today=None):
    """Upcoming basket deliveries expanded per product, soonest first."""
    today = today or date.today()
    horizon = today + timedelta(days=HORIZON_DAYS)

    rows = []

    # ── Deliveries that already exist ──────────────────────────────────────
    orders = (
        FamilyPackOrder.query
        .options(
            joinedload(FamilyPackOrder.subscription)
            .joinedload(FamilyPackSubscription.items),
            joinedload(FamilyPackOrder.customer),
        )
        .filter(
            FamilyPackOrder.delivery_date.isnot(None),
            FamilyPackOrder.delivery_date >= today,
            FamilyPackOrder.delivery_date <= horizon,
            FamilyPackOrder.status.notin_(EXCLUDED_STATUSES),
            FamilyPackOrder.subscription_id.isnot(None),
        )
        .all()
    )
    for order in orders:
        subscription = order.subscription
        if subscription is None:
            continue
        rows.extend(_item_rows(
            subscription, order.delivery_date,
            status=(order.status or '').replace('_', ' ').title(),
            order_id=order.id, source='Created',
        ))

    # ── Deliveries still to be generated ───────────────────────────────────
    subscriptions = (
        FamilyPackSubscription.query
        .options(joinedload(FamilyPackSubscription.items),
                 joinedload(FamilyPackSubscription.customer))
        .filter(FamilyPackSubscription.status == 'active')
        .all()
    )
    for subscription in subscriptions:
        if not subscription.items:
            continue
        for day in _projected_dates(subscription, today, horizon):
            rows.extend(_item_rows(
                subscription, day, status='Scheduled',
                order_id=None, source='Scheduled',
            ))

    # Soonest first, then grouped by basket so one delivery reads as a block.
    rows.sort(key=lambda r: (r['delivery_date'], r['subscription_id'], r['product_name']))
    return rows


COLUMNS = [
    {'key': 'delivery_date', 'label': 'Delivery date', 'width': 14},
    {'key': 'weekday', 'label': 'Day', 'width': 11},
    {'key': 'order_id', 'label': 'Order #', 'width': 10},
    {'key': 'status', 'label': 'Status', 'width': 14},
    {'key': 'source', 'label': 'Created / Scheduled', 'width': 12},
    {'key': 'subscription_id', 'label': 'Basket #', 'width': 10},
    {'key': 'customer_name', 'label': 'Customer', 'width': 22},
    {'key': 'customer_phone', 'label': 'Phone', 'width': 14},
    {'key': 'product_name', 'label': 'Product', 'width': 24},
    {'key': 'farm_name', 'label': 'Supplied by', 'width': 22},
    {'key': 'quantity', 'label': 'Quantity', 'format': 'quantity'},
    {'key': 'unit', 'label': 'Unit', 'width': 10},
    {'key': 'unit_price', 'label': 'Unit price', 'format': 'money'},
    {'key': 'line_total', 'label': 'Line total', 'format': 'money'},
]

SUMMARY_ROWS = [
    {'label': 'Product lines', 'formula': 'COUNTA({product_name})'},
    {'label': 'Deliveries already created',
     'formula': 'COUNTIF({source},"Created")'},
    {'label': 'Deliveries still to generate',
     'formula': 'COUNTIF({source},"Scheduled")'},
    {'label': 'Distinct baskets',
     'formula': 'SUMPRODUCT(({subscription_id}<>"")/COUNTIF({subscription_id},'
                '{subscription_id}&""))'},
    {'label': 'Distinct products',
     'formula': 'SUMPRODUCT(({product_name}<>"")/COUNTIF({product_name},'
                '{product_name}&""))'},
    {'label': 'Total quantity', 'formula': 'SUM({quantity})'},
    {'label': 'Value of the fortnight', 'formula': 'SUM({line_total})',
     'format': 'money'},
]

TITLE = 'F2H Market — weekly baskets due in the next fortnight'
SHEET_NAME = 'Upcoming Baskets'
SLUG = 'basket-orders'


def subtitle(generated_at, today=None):
    today = today or date.today()
    return (f'Deliveries due {today.isoformat()} to '
            f'{(today + timedelta(days=HORIZON_DAYS)).isoformat()}. '
            f'Generated {generated_at} (UTC). Prices are today’s — a '
            f'scheduled line is priced when its delivery is created.')


def summary(rows):
    """Headline counts, for the caller's logs."""
    return {
        'lines': len(rows),
        'deliveries': len({(r['delivery_date'], r['subscription_id']) for r in rows}),
        'baskets': len({r['subscription_id'] for r in rows}),
        'already_created': sum(1 for r in rows if r['source'] == 'Created'),
        'value': round(sum(r['line_total'] for r in rows), 2),
    }
