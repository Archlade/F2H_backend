from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from ..services.product_service import (
    get_products, get_product_by_id, create_product,
    update_product, delete_product, apply_discount, remove_discount, track_view
)
from ..models import Location
from ..utils.decorators import farmer_required
from ..utils.helpers import paginate_response

products_bp = Blueprint('products', __name__)


def _get_customer_location(user_id):
    loc = Location.query.filter_by(user_id=user_id, location_type='current', is_active=True).first()
    if not loc:
        loc = Location.query.filter_by(user_id=user_id, is_primary=True, is_active=True).first()
    return loc


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
        'delivery_available': request.args.get('delivery_available', type=lambda x: x == 'true'),
        'pickup_available': request.args.get('pickup_available', type=lambda x: x == 'true'),
        'stock_status': request.args.get('stock_status'),
        'search': request.args.get('q', '').strip() or None,
        'has_discount': request.args.get('has_discount', type=lambda x: x == 'true'),
        'sort': request.args.get('sort', 'newest'),
    }

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 50)

    results, total = get_products(filters, customer_lat, customer_lon, page, per_page)
    items = [r['product'].to_dict(distance=r['distance']) for r in results]
    return jsonify(paginate_response(items, total, page, per_page)), 200


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

    required = ['name', 'category_id', 'price', 'unit']
    for f in required:
        if f not in data:
            return jsonify({'error': f'{f} is required'}), 400

    try:
        product = create_product(user_id, data)
        return jsonify(product.to_dict()), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@products_bp.route('/<int:product_id>', methods=['PUT'])
@jwt_required()
@farmer_required
def update(product_id):
    user_id = int(get_jwt_identity())
    data = request.get_json()
    product = update_product(product_id, user_id, data)
    if not product:
        return jsonify({'error': 'Product not found or not authorized'}), 404
    return jsonify(product.to_dict()), 200


@products_bp.route('/<int:product_id>', methods=['DELETE'])
@jwt_required()
@farmer_required
def delete(product_id):
    user_id = int(get_jwt_identity())
    claims = get_jwt()
    # Admin can delete any
    farmer_id = user_id if claims.get('role') == 'farmer' else None
    if claims.get('role') == 'admin':
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
