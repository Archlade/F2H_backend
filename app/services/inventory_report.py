"""Every farmer, every live product, and what is left in stock.

One row per product, so the result drops straight into a spreadsheet without
anyone having to unpack a nested structure. A farmer with three products is
three rows repeating their details — redundant in a database, but exactly what
you want when the destination is a sheet somebody sorts and filters.

Farmers who have listed nothing still get a row, with the product columns empty.
Leaving them out would make a farmer who signed up and never listed anything
invisible in the one report that would reveal it.

"Live product" means active and not deleted. Out-of-stock rows are kept
deliberately: a report on stock levels that hides everything at zero cannot
answer the question it exists to answer.
"""

from sqlalchemy.orm import joinedload

from ..models import FarmerProfile, Product, User


def _stock_label(product):
    """`stock_status` in words, falling back to the quantity if it is unset.

    The column is maintained by `Product.update_stock_status()`, but rows
    predating that call can hold NULL, and a blank cell in a stock report reads
    as "no data" when it usually means zero.
    """
    if product.stock_status:
        return product.stock_status.replace('_', ' ').title()
    return 'Out Of Stock' if float(product.available_quantity or 0) <= 0 else 'In Stock'


def build_rows():
    """The report as a list of flat dicts, ordered farm then product."""
    farmers = (
        User.query
        .join(FarmerProfile, FarmerProfile.user_id == User.id)
        .options(joinedload(User.farmer_profile))
        .order_by(FarmerProfile.farm_name)
        .all()
    )

    # One query for every product rather than one per farmer. With a few hundred
    # farmers the per-farmer version is a few hundred round trips, and this runs
    # unattended on a schedule where nobody is watching it be slow.
    products = (
        Product.query
        .options(joinedload(Product.category), joinedload(Product.discount))
        .filter(Product.is_active.is_(True), Product.deleted_at.is_(None))
        .order_by(Product.name)
        .all()
    )
    by_farmer = {}
    for p in products:
        by_farmer.setdefault(p.farmer_id, []).append(p)

    rows = []
    for user in farmers:
        profile = user.farmer_profile
        base = {
            'farm_name': profile.farm_name if profile else user.full_name,
            'farmer_name': user.full_name,
            'email': user.email,
            'phone': user.phone or '',
            'verified': 'Yes' if profile and profile.is_verified else 'No',
            'suspended': 'Yes' if profile and profile.is_suspended else 'No',
        }

        owned = by_farmer.get(user.id, [])
        if not owned:
            rows.append({
                **base,
                # Amber: a farm that signed up and never listed anything. Not an
                # error, but the thing this report exists to make visible.
                '_highlight': 'note',
                'product_name': 'No products listed',
                'category': '',
                'unit': '',
                'price': None,
                'selling_price': None,
                'available_quantity': None,
                'stock_status': '',
                'min_order_quantity': None,
                'basket_eligible': '',
                'updated_at': '',
            })
            continue

        for p in owned:
            rows.append({
                **base,
                # Pink: run dry. Shaded rather than left to be spotted by
                # reading every line.
                '_highlight': 'warn' if float(p.available_quantity or 0) <= 0 else None,
                'product_name': p.name,
                'category': p.category.name if p.category else '',
                'unit': p.unit or '',
                'price': float(p.price),
                # What a customer actually pays today. Kept alongside `price`
                # rather than replacing it, so a discount is visible as the gap
                # between the two instead of silently changing the list price.
                'selling_price': p.effective_price,
                'available_quantity': float(p.available_quantity or 0),
                'stock_status': _stock_label(p),
                'min_order_quantity': float(p.min_quantity or 0),
                'basket_eligible': 'Yes' if getattr(p, 'basket_eligible', False) else 'No',
                'updated_at': p.updated_at.isoformat() if p.updated_at else '',
            })

    return rows


# Column order, headings, widths and number formats, defined here rather than in
# any client so the sheet cannot drift from the data. Keys must match the dicts
# `build_rows` returns.
COLUMNS = [
    {'key': 'farm_name', 'label': 'Farm', 'width': 24},
    {'key': 'farmer_name', 'label': 'Farmer', 'width': 20},
    {'key': 'email', 'label': 'Email', 'width': 26},
    {'key': 'phone', 'label': 'Phone'},
    {'key': 'verified', 'label': 'Verified', 'width': 10},
    {'key': 'suspended', 'label': 'Suspended', 'width': 11},
    {'key': 'product_name', 'label': 'Product', 'width': 24},
    {'key': 'category', 'label': 'Category', 'width': 16},
    {'key': 'unit', 'label': 'Unit', 'width': 10},
    {'key': 'available_quantity', 'label': 'Stock available', 'format': 'quantity'},
    {'key': 'stock_status', 'label': 'Stock status', 'width': 15},
    {'key': 'price', 'label': 'List price', 'format': 'money'},
    {'key': 'selling_price', 'label': 'Selling price', 'format': 'money'},
    {'key': 'min_order_quantity', 'label': 'Min order qty', 'format': 'quantity'},
    {'key': 'basket_eligible', 'label': 'Basket eligible'},
    {'key': 'updated_at', 'label': 'Product updated', 'width': 22},
]

SUMMARY_ROWS = [
    {'label': 'Rows in the sheet', 'formula': 'COUNTA({product_name})'},
    {'label': 'Products listed',
     'formula': 'COUNTA({product_name})-COUNTIF({product_name},"No products listed")'},
    {'label': 'Farms with no products',
     'formula': 'COUNTIF({product_name},"No products listed")'},
    {'label': 'Products out of stock', 'formula': 'COUNTIF({available_quantity},0)'},
    {'label': 'Total stock across all products', 'formula': 'SUM({available_quantity})'},
    # Distinct count has no single-function form that LibreOffice evaluates.
    # SUMPRODUCT over 1/COUNTIF is the standard idiom; the `<>""` guard stops a
    # blank cell producing a divide-by-zero.
    {'label': 'Distinct farms',
     'formula': 'SUMPRODUCT(({farm_name}<>"")/COUNTIF({farm_name},{farm_name}&""))'},
]

TITLE = 'F2H Market — farmers, products and stock'
SHEET_NAME = 'Farmers & Stock'
SLUG = 'farmer-stock'


def summary(rows):
    """Headline counts, so the sheet can lead with them."""
    farms = {r['farm_name'] for r in rows}
    listed = [r for r in rows if r['product_name'] != 'No products listed']
    return {
        'farmers': len(farms),
        'products': len(listed),
        'out_of_stock': sum(1 for r in listed if (r['available_quantity'] or 0) <= 0),
        'farms_with_no_products': sum(
            1 for r in rows if r['product_name'] == 'No products listed'
        ),
    }
