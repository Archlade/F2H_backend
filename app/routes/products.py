from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from ..extensions import db
from ..services.product_service import (
    get_products, get_product_by_id, create_product,
    update_product, delete_product, apply_discount, remove_discount, track_view
)
from ..models import Location, Category
from ..utils.decorators import farmer_required, current_user_role
from ..utils.helpers import paginate_response
from ..utils.validators import clamp_page

products_bp = Blueprint('products', __name__)


def _get_customer_location(user_id):
    loc = Location.query.filter_by(user_id=user_id, location_type='current', is_active=True).first()
    if not loc:
        loc = Location.query.filter_by(user_id=user_id, is_primary=True, is_active=True).first()
    return loc


# Mirrors the products.unit column. A value outside this set reaches MySQL as an
# invalid ENUM and fails at commit, so it is caught here with a readable message.
VALID_UNITS = ('kg', 'gram', 'litre', 'ml', 'piece', 'bundle', 'dozen', 'box')


def _listing_problem(data):
    """Return a human-readable problem with a listing payload, or None.

    Checking here rather than letting the database complain means the farmer
    gets 'Price must be a number greater than zero' instead of a MySQL type
    error, and it keeps a bad request from ever opening a transaction.
    """
    if not str(data.get('name') or '').strip():
        return 'Product name is required'

    category_id = data.get('category_id')
    if category_id in (None, ''):
        return 'Choose a category'
    try:
        category_id = int(category_id)
    except (TypeError, ValueError):
        return 'Choose a category'
    if not Category.query.get(category_id):
        return 'That category no longer exists'

    try:
        price = float(data.get('price'))
    except (TypeError, ValueError):
        return 'Price must be a number'
    if price <= 0:
        return 'Price must be greater than zero'

    unit = data.get('unit') or 'kg'
    if unit not in VALID_UNITS:
        return f"'{unit}' is not a unit we support"

    # Optional, but if present they must be numbers — an empty text field
    # arrives as '' and would otherwise fail deep inside the insert.
    for field, label in (('available_quantity', 'Available quantity'),
                         ('min_quantity', 'Minimum quantity'),
                         ('low_stock_threshold', 'Low stock threshold')):
        if data.get(field) in (None, ''):
            continue
        try:
            if float(data[field]) < 0:
                return f'{label} cannot be negative'
        except (TypeError, ValueError):
            return f'{label} must be a number'

    return None


@products_bp.route('', methods=['GET'])
def list_products():
    user_id = None
    customer_lat = customer_lon = None
    try:
        from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
        verify_jwt_in_request(optional=True)
        uid = get_jwt_identity()
        if uid:
            user_id = int(uid)
            loc = _get_customer_location(user_id)
            if loc:
                customer_lat, customer_lon = loc.latitude, loc.longitude
    except Exception:
        pass

    # Allow explicit lat/lon from query params
    if request.args.get('lat') and request.args.get('lon'):
        customer_lat = float(request.args.get('lat'))
        customer_lon = float(request.args.get('lon'))

    filters = {
        'category_id': request.args.get('category_id', type=int),
        'category_slug': request.args.get('category'),
        'farmer_id': request.args.get('farmer_id', type=int),
        'min_price': request.args.get('min_price', type=float),
        'max_price': request.args.get('max_price', type=float),
        'is_organic': request.args.get('is_organic', type=lambda x: x == 'true'),
        'basket_eligible': request.args.get('basket_eligible', type=lambda x: x == 'true'),
        'delivery_available': request.args.get('delivery_available', type=lambda x: x == 'true'),
        'pickup_available': request.args.get('pickup_available', type=lambda x: x == 'true'),
        'stock_status': request.args.get('stock_status'),
        'search': request.args.get('q', '').strip() or None,
        'has_discount': request.args.get('has_discount', type=lambda x: x == 'true'),
        'sort': request.args.get('sort', 'newest'),
    }

    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=50)

    results, total = get_products(filters, customer_lat, customer_lon, page, per_page)
    items = [r['product'].to_dict(distance=r['distance']) for r in results]

    # Which of these the caller has already favourited.
    #
    # Without it the heart on every card renders empty regardless of the truth,
    # and tapping one on a product you already like *removes* it while saying
    # "Added to favorites". One query for the page, not one per card.
    _annotate_favourites(items)

    return jsonify(paginate_response(items, total, page, per_page)), 200


def _annotate_favourites(items):
    """Set `is_favorited` on each product dict, for the signed-in caller.

    Silent for anonymous visitors — they have no favourites, and a failure to
    resolve the token must not take down a public product listing.
    """
    from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
    from ..models import Favorite

    try:
        verify_jwt_in_request(optional=True)
        uid = get_jwt_identity()
        user_id = int(uid) if uid else None
    except Exception:
        user_id = None

    if not user_id or not items:
        for it in items:
            it['is_favorited'] = False
        return

    ids = [it['id'] for it in items]
    liked = {f.product_id for f in Favorite.query.filter(
        Favorite.user_id == user_id, Favorite.product_id.in_(ids)).all()}
    for it in items:
        it['is_favorited'] = it['id'] in liked


@products_bp.route('/<int:product_id>', methods=['GET'])
def get_product(product_id):
    from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
    user_id = None
    try:
        verify_jwt_in_request(optional=True)
        uid = get_jwt_identity()
        if uid:
            user_id = int(uid)
    except Exception:
        pass

    product = get_product_by_id(product_id)
    if not product:
        return jsonify({'error': 'Product not found'}), 404

    track_view(product_id, user_id)
    return jsonify(product.to_dict()), 200


@products_bp.route('', methods=['POST'])
@jwt_required()
@farmer_required
def create():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    problem = _listing_problem(data)
    if problem:
        return jsonify({'error': problem}), 400

    try:
        product = create_product(user_id, data)
        return jsonify(product.to_dict()), 201
    except Exception:
        # A half-applied insert leaves the session dirty, and SQLAlchemy will
        # then reject every later statement on this connection — so one bad
        # listing would break unrelated requests until the worker recycled.
        db.session.rollback()
        current_app.logger.exception('Failed to create a listing for user %s', user_id)
        # The raw driver message used to be handed straight to the farmer,
        # which is both unreadable and a description of our schema.
        return jsonify({'error': 'Could not publish this listing. '
                                 'Please check the details and try again.'}), 400


@products_bp.route('/<int:product_id>', methods=['PUT'])
@jwt_required()
@farmer_required
def update(product_id):
    user_id = int(get_jwt_identity())
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # A PUT may be a partial edit — the inline stock control on the products
    # screen sends available_quantity alone — so only validate what was sent.
    problem = _listing_problem({**_existing_defaults(product_id, user_id), **data})
    if problem:
        return jsonify({'error': problem}), 400

    try:
        product = update_product(product_id, user_id, data)
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to update listing %s', product_id)
        return jsonify({'error': 'Could not save this listing. '
                                 'Please check the details and try again.'}), 400

    if not product:
        return jsonify({'error': 'Product not found or not authorized'}), 404
    return jsonify(product.to_dict()), 200


def _existing_defaults(product_id, farmer_id):
    """The stored values a partial update is allowed to inherit.

    Without this, validating a one-field PATCH-style PUT would reject it for
    missing a name it was never trying to change.
    """
    from ..models import Product
    product = Product.query.filter_by(
        id=product_id, farmer_id=farmer_id, deleted_at=None).first()
    if not product:
        # Let update_product own the 404; give validation something complete
        # so it doesn't fail first with a confusing message.
        return {'name': 'x', 'category_id': 1, 'price': 1, 'unit': 'kg'}
    return {
        'name': product.name,
        'category_id': product.category_id,
        'price': float(product.price),
        'unit': product.unit,
    }


@products_bp.route('/<int:product_id>', methods=['DELETE'])
@jwt_required()
@farmer_required
def delete(product_id):
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    # Admin can delete any
    farmer_id = user_id if role == 'farmer' else None
    if role == 'admin':
        from ..models import Product
        p = Product.query.get(product_id)
        if p:
            from datetime import datetime
            p.deleted_at = datetime.utcnow()
            p.is_active = False
            from ..extensions import db
            db.session.commit()
            return jsonify({'message': 'Deleted'}), 200
    ok = delete_product(product_id, user_id)
    if not ok:
        return jsonify({'error': 'Not found or not authorized'}), 404
    return jsonify({'message': 'Deleted'}), 200


@products_bp.route('/<int:product_id>/discount', methods=['POST'])
@jwt_required()
@farmer_required
def add_discount(product_id):
    user_id = int(get_jwt_identity())
    data = request.get_json()
    if not data or 'discount_type' not in data or 'discount_value' not in data:
        return jsonify({'error': 'discount_type and discount_value required'}), 400
    product = apply_discount(product_id, user_id, data)
    if not product:
        return jsonify({'error': 'Not found or not authorized'}), 404
    return jsonify(product.to_dict()), 200


@products_bp.route('/<int:product_id>/discount', methods=['DELETE'])
@jwt_required()
@farmer_required
def del_discount(product_id):
    user_id = int(get_jwt_identity())
    ok = remove_discount(product_id, user_id)
    if not ok:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'message': 'Discount removed'}), 200
