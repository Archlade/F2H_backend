"""The cart, and turning it into orders.

A cart holds products from any number of farms, but an order cannot: a
`PurchaseRequest` is one product from one farmer, accepted, prepared, delivered
and paid for on its own. So checkout **fans out** — one order per line — and the
customer sees one confirmation covering all of them.

The order minimum is checked against the cart total, not per order. That is a
deliberate choice with a known cost: a cart that just clears the floor split
across three farms can send one of them out for ₹40, and under cash on delivery
that trip costs more than the order. Watch for it; if it bites, the rule to
change is `min_order_value()` applied per farmer in `checkout` rather than once
on the total.

The figure itself is set by an admin and lives in `platform_settings` — see
`app/models/settings.py`. Both checks below read it live rather than caching it,
so raising the floor takes effect on the next request instead of the next
deploy.
"""

import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from ..extensions import db
from ..models import CartItem, Product, min_order_value
from ..services.request_service import create_purchase_request
from ..utils.locking import lock_row

logger = logging.getLogger(__name__)

cart_bp = Blueprint('cart', __name__)


def _items(customer_id):
    return (CartItem.query.filter_by(customer_id=customer_id)
            .order_by(CartItem.created_at).all())


def _summary(customer_id):
    """The cart, its total, and how far it is from being orderable."""
    items = _items(customer_id)
    subtotal = round(sum(i.line_total for i in items), 2)
    minimum = min_order_value()
    blocked = [i for i in items if i.problem()]

    return {
        'items': [i.to_dict() for i in items],
        'count': len(items),
        'subtotal': subtotal,
        'minimum_order_value': minimum,
        'meets_minimum': subtotal >= minimum,
        # Pre-computed so the screen can say "add ₹120 more" without repeating
        # the arithmetic — and without the app and the website disagreeing about
        # the rounding.
        'short_by': round(max(0.0, minimum - subtotal), 2),
        'has_problems': bool(blocked),
    }


@cart_bp.route('', methods=['GET'])
@jwt_required()
def get_cart():
    return jsonify(_summary(int(get_jwt_identity()))), 200


@cart_bp.route('/items', methods=['POST'])
@jwt_required()
def add_item():
    """Add a product, or increase it if it is already there.

    Adding the same product twice is an increase, not a second line — the unique
    constraint enforces that, and this reads the existing row first so two taps
    on "Add" do not race into an IntegrityError.
    """
    customer_id = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}

    product = Product.query.get(data.get('product_id'))
    if not product or product.deleted_at or not product.is_active:
        return jsonify({'error': 'That product is not available'}), 404
    if product.farmer_id == customer_id:
        return jsonify({'error': 'This is your own listing'}), 400

    try:
        qty = float(data.get('quantity') or product.min_quantity)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid quantity'}), 400

    existing = CartItem.query.filter_by(customer_id=customer_id,
                                        product_id=product.id).first()
    total_qty = qty + (float(existing.quantity) if existing else 0)

    if total_qty < float(product.min_quantity):
        return jsonify({'error': f'The minimum order for {product.name} is '
                                 f'{float(product.min_quantity):g} {product.unit}'}), 400
    if total_qty > float(product.available_quantity):
        return jsonify({'error': f'Only {float(product.available_quantity):g} '
                                 f'{product.unit} available'}), 400

    if existing:
        existing.quantity = total_qty
    else:
        db.session.add(CartItem(customer_id=customer_id, product_id=product.id,
                                quantity=qty))
    db.session.commit()
    return jsonify(_summary(customer_id)), 200


@cart_bp.route('/items/<int:item_id>', methods=['PATCH'])
@jwt_required()
def update_item(item_id):
    """Set a line's quantity. Below the product's minimum removes it."""
    customer_id = int(get_jwt_identity())
    item = CartItem.query.filter_by(id=item_id, customer_id=customer_id).first_or_404()

    try:
        qty = float((request.get_json(silent=True) or {}).get('quantity'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid quantity'}), 400

    product = item.product
    if product and qty < float(product.min_quantity):
        # Below the farmer's minimum is not a smaller order, it is no order.
        db.session.delete(item)
    elif product and qty > float(product.available_quantity):
        return jsonify({'error': f'Only {float(product.available_quantity):g} '
                                 f'{product.unit} available'}), 400
    else:
        item.quantity = qty
    db.session.commit()
    return jsonify(_summary(customer_id)), 200


@cart_bp.route('/items/<int:item_id>', methods=['DELETE'])
@jwt_required()
def remove_item(item_id):
    customer_id = int(get_jwt_identity())
    item = CartItem.query.filter_by(id=item_id, customer_id=customer_id).first_or_404()
    db.session.delete(item)
    db.session.commit()
    return jsonify(_summary(customer_id)), 200


@cart_bp.route('', methods=['DELETE'])
@jwt_required()
def clear_cart():
    customer_id = int(get_jwt_identity())
    CartItem.query.filter_by(customer_id=customer_id).delete(synchronize_session=False)
    db.session.commit()
    return jsonify(_summary(customer_id)), 200


@cart_bp.route('/checkout', methods=['POST'])
@jwt_required()
def checkout():
    """Turn the cart into orders — one per line — and empty it.

    Everything is re-validated here against the live product rows. The cart
    screen's own checks are for the customer's benefit; these are the ones that
    decide, because a cart can sit for days while a price changes or the last
    5kg is sold to somebody else.

    All or nothing. A partial checkout would leave the customer with some orders
    placed, some items still in the cart, and no clear idea which — so a single
    failure rolls the whole thing back.
    """
    customer_id = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}

    items = _items(customer_id)
    if not items:
        return jsonify({'error': 'Your cart is empty'}), 400

    # Re-read every product under a lock, in a stable order, so two devices
    # checking out the same cart cannot both pass the stock check.
    for item in sorted(items, key=lambda i: i.product_id):
        lock_row(Product, item.product_id)

    for item in items:
        problem = item.problem()
        if problem:
            name = item.product.name if item.product else f'Product {item.product_id}'
            return jsonify({'error': f'{name} is {problem}. '
                                     'Please update your cart and try again.'}), 409

    subtotal = round(sum(i.line_total for i in items), 2)
    # Read again here rather than trusting what the cart screen was told. An
    # admin can raise the floor while a cart is open, and this is the check that
    # decides — the same reason every product is re-validated a few lines up.
    minimum = min_order_value()
    if subtotal < minimum:
        return jsonify({
            'error': f'The minimum order is ₹{minimum:.0f}. '
                     f'Your cart is ₹{subtotal:.2f} — please add '
                     f'₹{minimum - subtotal:.2f} more.'
        }), 400

    created = []
    try:
        for item in items:
            order = create_purchase_request(customer_id, {
                'product_id': item.product_id,
                'quantity': float(item.quantity),
                'purchase_mode': data.get('purchase_mode', 'delivery'),
                'delivery_address_id': data.get('delivery_address_id'),
                'delivery_notes': data.get('delivery_notes', ''),
                'customer_message': data.get('customer_message', ''),
                # The floor has already been applied to the cart total above.
                # Applying it per line too would make a valid ₹400 cart of two
                # ₹200 items impossible to check out.
                'skip_minimum': True,
                # Deliberately not passed through. A coupon is written for one
                # order, and spending it across a fan-out would either apply it
                # several times or silently pick a line to favour. Coupons stay
                # on the single-product path until the rule for splitting one
                # across orders is decided.
            })
            created.append(order)

        CartItem.query.filter_by(customer_id=customer_id).delete(synchronize_session=False)
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception:
        db.session.rollback()
        logger.exception('Cart checkout failed for customer %s', customer_id)
        return jsonify({'error': 'Could not place your order. Please try again.'}), 500

    return jsonify({
        'message': f'{len(created)} order{"s" if len(created) != 1 else ""} placed',
        'orders': [o.to_dict() for o in created],
    }), 201
