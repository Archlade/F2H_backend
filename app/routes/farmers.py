from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from ..models import User, FarmerProfile, Location
from ..services.product_service import get_products, calculate_distance
from ..utils.decorators import farmer_required
from ..extensions import db

farmers_bp = Blueprint('farmers', __name__)


def _get_farmers_with_distance(customer_lat=None, customer_lon=None, page=1, per_page=20):
    query = (User.query
             .join(FarmerProfile, User.id == FarmerProfile.user_id)
             .filter(User.is_active == True, FarmerProfile.is_suspended == False))

    total = query.count()
    farmers = query.offset((page - 1) * per_page).limit(per_page).all()

    result = []
    for farmer in farmers:
        farm_loc = Location.query.filter_by(
            user_id=farmer.id, location_type='farm', is_active=True
        ).first()
        dist = None
        if customer_lat and customer_lon and farm_loc:
            dist = calculate_distance(customer_lat, customer_lon,
                                       farm_loc.latitude, farm_loc.longitude)
        result.append({'farmer': farmer, 'distance': dist, 'location': farm_loc})
    return result, total


@farmers_bp.route('', methods=['GET'])
def list_farmers():
    customer_lat = customer_lon = None
    if request.args.get('lat') and request.args.get('lon'):
        customer_lat = float(request.args.get('lat'))
        customer_lon = float(request.args.get('lon'))
    else:
        try:
            from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
            verify_jwt_in_request(optional=True)
            uid = get_jwt_identity()
            if uid:
                loc = Location.query.filter_by(user_id=int(uid), location_type='current', is_active=True).first()
                if loc:
                    customer_lat, customer_lon = loc.latitude, loc.longitude
        except Exception:
            pass

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 50)
    search = request.args.get('q', '').strip()

    query = (User.query
             .join(FarmerProfile, User.id == FarmerProfile.user_id)
             .filter(User.is_active == True, FarmerProfile.is_suspended == False))
    if search:
        query = query.filter(
            db.or_(
                User.first_name.ilike(f'%{search}%'),
                User.last_name.ilike(f'%{search}%'),
                FarmerProfile.farm_name.ilike(f'%{search}%'),
            )
        )
    total = query.count()
    farmers = query.offset((page - 1) * per_page).limit(per_page).all()

    items = []
    for farmer in farmers:
        fp = farmer.farmer_profile
        farm_loc = Location.query.filter_by(user_id=farmer.id, location_type='farm', is_active=True).first()
        dist = None
        if customer_lat and customer_lon and farm_loc:
            dist = calculate_distance(customer_lat, customer_lon, farm_loc.latitude, farm_loc.longitude)
        d = fp.to_dict()
        if dist is not None:
            d['distance_km'] = round(dist, 2)
        if farm_loc:
            d['location'] = farm_loc.to_dict()
        items.append(d)

    if customer_lat and customer_lon:
        items.sort(key=lambda x: (x.get('distance_km') is None, x.get('distance_km') or 9999))

    return jsonify({'items': items, 'total': total, 'page': page, 'per_page': per_page}), 200


@farmers_bp.route('/<int:farmer_id>', methods=['GET'])
def get_farmer(farmer_id):
    farmer = User.query.get(farmer_id)
    if not farmer or farmer.role_name != 'farmer':
        return jsonify({'error': 'Farmer not found'}), 404
    fp = farmer.farmer_profile
    if not fp:
        return jsonify({'error': 'Farmer profile not found'}), 404

    farm_loc = Location.query.filter_by(user_id=farmer_id, location_type='farm', is_active=True).first()
    result = fp.to_dict()
    if farm_loc:
        result['location'] = farm_loc.to_dict()

    # Product count
    from ..models import Product
    result['product_count'] = Product.query.filter_by(farmer_id=farmer_id, is_active=True, deleted_at=None).count()

    return jsonify(result), 200


@farmers_bp.route('/profile', methods=['PUT'])
@jwt_required()
@farmer_required
def update_profile():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    fp = FarmerProfile.query.filter_by(user_id=user_id).first()
    if not fp:
        return jsonify({'error': 'Farmer profile not found'}), 404

    allowed = ['farm_name', 'bio', 'farm_description', 'farm_size',
               'farming_type', 'years_farming', 'avatar_url', 'cover_image_url']
    for field in allowed:
        if field in data:
            setattr(fp, field, data[field])
    db.session.commit()
    return jsonify(fp.to_dict()), 200


@farmers_bp.route('/dashboard/stats', methods=['GET'])
@jwt_required()
@farmer_required
def dashboard_stats():
    user_id = int(get_jwt_identity())
    from ..models import Product, PurchaseRequest
    from sqlalchemy import func

    total_products = Product.query.filter_by(farmer_id=user_id, deleted_at=None).count()
    active_products = Product.query.filter_by(farmer_id=user_id, is_active=True, deleted_at=None).count()
    low_stock = Product.query.filter_by(farmer_id=user_id, stock_status='low_stock', deleted_at=None).count()

    pending_reqs = PurchaseRequest.query.filter_by(farmer_id=user_id, status='pending').count()
    accepted_reqs = PurchaseRequest.query.filter(
        PurchaseRequest.farmer_id == user_id,
        PurchaseRequest.status.in_(['accepted', 'chat_active', 'confirmed', 'preparing',
                                     'ready_for_pickup', 'out_for_delivery'])
    ).count()
    completed = PurchaseRequest.query.filter_by(farmer_id=user_id, status='completed').count()

    total_revenue = db.session.query(func.sum(PurchaseRequest.total_price)).filter(
        PurchaseRequest.farmer_id == user_id,
        PurchaseRequest.status == 'completed'
    ).scalar() or 0

    return jsonify({
        'total_products': total_products,
        'active_products': active_products,
        'low_stock_products': low_stock,
        'pending_requests': pending_reqs,
        'accepted_requests': accepted_reqs,
        'completed_orders': completed,
        'total_revenue': float(total_revenue),
    }), 200
