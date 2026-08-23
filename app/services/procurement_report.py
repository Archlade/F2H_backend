"""What to buy, and from whom, for the next weekly basket delivery.

F2H sells the basket, so the customer never sees which farm their spinach came
from. That is what makes cheapest-sourcing possible: a basket item points at one
farmer's listing because that is how the customer picked it, but nothing
requires F2H to buy it there. This report ignores the chosen listing and buys
the same vegetable wherever it is cheapest and in stock.

── What one plan covers ───────────────────────────────────────────────────────

The **next delivery date** only — the soonest date any basket is due, and every
basket due on it. Not a week, not a fortnight. Produce cannot be bought early,
and a buying list you cannot act on today is a list nobody uses.

── How products are matched across farms ──────────────────────────────────────

By `(name, unit)`, case-insensitively. Nothing in the schema says one farm's
"Tomatoes" is the same thing as another's — there is no shared catalogue, each
farmer types their own product name — so the name is the only handle available.

That has a real failure mode worth knowing: **"Tomato" and "Tomatoes" are two
different products to this code**, and a farm listing the singular will not be
considered. The `Unmatched name` column exists to make that visible rather than
silently expensive. If it starts showing up, the fix is a shared catalogue, not
fuzzy matching — guessing that two names mean the same vegetable is how you buy
the wrong thing.

The unit is part of the key on purpose: 2 kg of demand cannot be filled from a
listing priced per bundle, and treating them as interchangeable would produce a
plan that looks cheap and cannot be bought.

── Why greedy cheapest-first is not a heuristic here ──────────────────────────

Take from the cheapest farm until it runs out, then the next. With no fixed cost
per farm and no per-farm discount, that is provably the minimum-cost plan, not
an approximation: any plan buying a unit from a dearer source when a cheaper one
still had stock can be improved by swapping that unit, so the cheapest plan
contains no such swap. `tests` in the verification run this against brute force.

The one thing it does not price is the trip. Collecting 3kg from five farms is
cheaper on paper and dearer in practice, which is why every product sourced from
more than one farm is flagged rather than quietly split.
"""

from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from ..models import FamilyPackOrder, FamilyPackSubscription, Product

EXCLUDED_STATUSES = ('cancelled', 'rejected')

# How far ahead to look for the next delivery date. Only the soonest date found
# is planned; this is just the search window, wide enough to cover a basket
# whose weekday is a full cycle away.
SEARCH_DAYS = 14


def _next_delivery_date(today, horizon):
    """The soonest date any basket is due, or None if nothing is coming.

    Considers both deliveries that already exist and ones still to be
    generated — the generator only runs two days ahead, so on most days the
    next delivery is a projection rather than a row in the orders table.
    """
    dates = []

    soonest_order = (
        FamilyPackOrder.query
        .with_entities(func.min(FamilyPackOrder.delivery_date))
        .filter(
            FamilyPackOrder.delivery_date.isnot(None),
            FamilyPackOrder.delivery_date >= today,
            FamilyPackOrder.delivery_date <= horizon,
            FamilyPackOrder.status.notin_(EXCLUDED_STATUSES),
            FamilyPackOrder.subscription_id.isnot(None),
        )
        .scalar()
    )
    if soonest_order:
        dates.append(soonest_order)

    for subscription in _active_subscriptions():
        day = _first_projected_date(subscription, today, horizon)
        if day:
            dates.append(day)

    return min(dates) if dates else None


def _active_subscriptions():
    return (
        FamilyPackSubscription.query
        .options(joinedload(FamilyPackSubscription.items))
        .filter(FamilyPackSubscription.status == 'active')
        .all()
    )


def _first_projected_date(subscription, today, horizon):
    """The next date this subscription is due that has no order yet."""
    if not subscription.items:
        return None
    base = subscription.start_date
    if subscription.last_generated_date and subscription.last_generated_date >= base:
        base = subscription.last_generated_date + timedelta(days=1)
    if base < today:
        base = today

    offset = (subscription.delivery_weekday - base.weekday()) % 7
    day = base + timedelta(days=offset)
    while day <= horizon:
        if not (subscription.paused_until and day <= subscription.paused_until):
            return day
        day += timedelta(days=7)
    return None


def _demand_for(target_date, today, horizon):
    """Total quantity needed per (name, unit) for `target_date`.

    Returns `{(lower_name, unit): {'name', 'unit', 'quantity', 'baskets'}}`.
    """
    demand = {}

    def add(product_name, unit, quantity, subscription_id):
        key = (product_name.strip().lower(), (unit or '').strip().lower())
        entry = demand.setdefault(key, {
            'name': product_name.strip(),
            'unit': (unit or '').strip(),
            'quantity': 0.0,
            'baskets': set(),
        })
        entry['quantity'] += float(quantity or 0)
        entry['baskets'].add(subscription_id)

    # Deliveries that already exist on that date.
    orders = (
        FamilyPackOrder.query
        .options(joinedload(FamilyPackOrder.subscription)
                 .joinedload(FamilyPackSubscription.items))
        .filter(
            FamilyPackOrder.delivery_date == target_date,
            FamilyPackOrder.status.notin_(EXCLUDED_STATUSES),
            FamilyPackOrder.subscription_id.isnot(None),
        )
        .all()
    )
    planned = set()
    for order in orders:
        subscription = order.subscription
        if subscription is None:
            continue
        planned.add(subscription.id)
        for item in subscription.items:
            product = item.product
            add(product.name if product else f'Product {item.product_id}',
                item.unit or (product.unit if product else ''),
                item.quantity, subscription.id)

    # Deliveries still to be generated for that date.
    for subscription in _active_subscriptions():
        if subscription.id in planned:
            continue
        if _first_projected_date(subscription, today, horizon) != target_date:
            continue
        for item in subscription.items:
            product = item.product
            add(product.name if product else f'Product {item.product_id}',
                item.unit or (product.unit if product else ''),
                item.quantity, subscription.id)

    return demand


def _listings():
    """Everything sellable right now, grouped by `(name, unit)`."""
    products = (
        Product.query
        .options(joinedload(Product.discount))
        .filter(Product.is_active.is_(True), Product.deleted_at.is_(None))
        .all()
    )
    grouped = {}
    for product in products:
        if float(product.available_quantity or 0) <= 0:
            continue
        key = ((product.name or '').strip().lower(), (product.unit or '').strip().lower())
        grouped.setdefault(key, []).append(product)

    # Cheapest first. `effective_price` is what F2H actually pays, so a farm
    # running a discount sorts where its real price puts it.
    for candidates in grouped.values():
        candidates.sort(key=lambda p: (float(p.effective_price),
                                       float(p.available_quantity or 0) * -1))
    return grouped


def _farm_name(product):
    farmer = product.farmer
    if farmer is None:
        return ''
    profile = farmer.farmer_profile
    return profile.farm_name if profile else farmer.full_name


def build_rows(today=None):
    """The buying plan, one row per purchase to make."""
    today = today or date.today()
    horizon = today + timedelta(days=SEARCH_DAYS)

    target = _next_delivery_date(today, horizon)
    if target is None:
        return []

    demand = _demand_for(target, today, horizon)
    supply = _listings()
    rows = []

    for key in sorted(demand, key=lambda k: demand[k]['name'].lower()):
        need = demand[key]
        needed = round(need['quantity'], 3)
        candidates = supply.get(key, [])
        remaining = needed
        sources = []

        for product in candidates:
            if remaining <= 0:
                break
            stock = float(product.available_quantity or 0)
            take = min(remaining, stock)

            # A farmer's own minimum applies to F2H too. Buying 2kg where the
            # listing says 5kg minimum is not a cheaper plan, it is a plan the
            # farmer refuses — so round up and show the surplus, rather than
            # producing a total nobody can actually pay.
            minimum = float(product.min_quantity or 0)
            if minimum and take < minimum:
                take = min(minimum, stock)

            if take <= 0:
                continue
            sources.append((product, round(take, 3)))
            remaining = round(remaining - take, 3)

        split = len(sources) > 1
        for index, (product, quantity) in enumerate(sources):
            price = float(product.effective_price)
            rows.append({
                '_highlight': 'note' if split else None,
                'product_name': need['name'],
                'unit': need['unit'],
                'needed': needed if index == 0 else None,
                'baskets': len(need['baskets']) if index == 0 else None,
                'buy_from': _farm_name(product),
                'farmer_phone': (product.farmer.phone or '') if product.farmer else '',
                'buy_quantity': quantity,
                'unit_price': price,
                'line_cost': round(price * quantity, 2),
                'in_stock_there': float(product.available_quantity or 0),
                'split': 'Yes' if split else '',
                'shortfall': None,
                'note': '',
            })

        if remaining > 0.0001 or not sources:
            # Nothing to buy, or not enough of it. This is the row somebody has
            # to act on, so it is a row rather than a footnote.
            rows.append({
                '_highlight': 'warn',
                'product_name': need['name'],
                'unit': need['unit'],
                'needed': needed if not sources else None,
                'baskets': len(need['baskets']) if not sources else None,
                'buy_from': '— no farm has stock —' if not candidates else '— not enough stock —',
                'farmer_phone': '',
                'buy_quantity': None,
                'unit_price': None,
                'line_cost': None,
                'in_stock_there': None,
                'split': '',
                'shortfall': round(remaining, 3),
                'note': ('No farm lists this name and unit — check for a spelling '
                         'variant' if not candidates else
                         'Short by this much across every farm with stock'),
            })

    return rows


COLUMNS = [
    {'key': 'product_name', 'label': 'Product', 'width': 22},
    {'key': 'unit', 'label': 'Unit', 'width': 10},
    {'key': 'needed', 'label': 'Total needed', 'format': 'quantity'},
    {'key': 'baskets', 'label': 'Baskets', 'format': 'integer', 'width': 10},
    {'key': 'buy_from', 'label': 'Buy from', 'width': 24},
    {'key': 'farmer_phone', 'label': 'Phone', 'width': 14},
    {'key': 'buy_quantity', 'label': 'Qty to buy', 'format': 'quantity'},
    {'key': 'unit_price', 'label': 'Unit price', 'format': 'money'},
    {'key': 'line_cost', 'label': 'Cost', 'format': 'money'},
    {'key': 'in_stock_there', 'label': 'They have', 'format': 'quantity'},
    {'key': 'split', 'label': 'Split buy', 'width': 11},
    {'key': 'shortfall', 'label': 'Short by', 'format': 'quantity'},
    {'key': 'note', 'label': 'Note', 'width': 42},
]

SUMMARY_ROWS = [
    {'label': 'Purchases to make', 'formula': 'COUNT({buy_quantity})'},
    {'label': 'Total to pay the farmers', 'formula': 'SUM({line_cost})',
     'format': 'money'},
    {'label': 'Farms to collect from',
     'formula': 'SUMPRODUCT(({buy_from}<>"")*({buy_quantity}<>"")/'
                'COUNTIF({buy_from},{buy_from}&""))'},
    {'label': 'Products short or unavailable', 'formula': 'COUNT({shortfall})'},
    {'label': 'Products split across farms',
     'formula': 'COUNTIF({split},"Yes")'},
]

TITLE = 'F2H Market — what to buy for the next basket delivery'
SHEET_NAME = 'Buying Plan'
SLUG = 'buying-plan'


def subtitle(generated_at, today=None):
    today = today or date.today()
    target = _next_delivery_date(today, today + timedelta(days=SEARCH_DAYS))
    when = (f'{target.isoformat()} ({target.strftime("%A")})' if target
            else 'no delivery scheduled')
    return (f'For the delivery on {when}. Cheapest farm with stock, per product. '
            f'Generated {generated_at} (UTC). Prices are today’s — confirm on the '
            f'phone before collecting.')


def summary(rows):
    buys = [r for r in rows if r['buy_quantity']]
    return {
        'purchases': len(buys),
        'cost': round(sum(r['line_cost'] or 0 for r in buys), 2),
        'farms': len({r['buy_from'] for r in buys}),
        'shortfalls': sum(1 for r in rows if r['shortfall']),
        'split_products': len({r['product_name'] for r in rows if r['split'] == 'Yes'}),
    }
