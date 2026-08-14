from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from ..models import User, FarmerProfile, Location
from ..services.product_service import get_products, calculate_distance
from ..utils.decorators import farmer_required
from ..extensions import db
from ..utils.validators import clamp_page

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

    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=50)
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


@farmers_bp.route('/dashboard/analytics', methods=['GET'])
@jwt_required()
@farmer_required
def dashboard_analytics():
    """Real numbers for the farmer's charts.

    This endpoint exists because the analytics page had none: it rendered a
    hardcoded Jan–Jun line and a bar chart of Tomatoes/Potatoes/Carrots/Apples,
    identical for every farmer, behind a fake half-second spinner. A chart of
    invented numbers is worse than no chart — it is a number somebody might
    plan around.

    Two series, both from this farmer's own orders:

      monthly      order count and revenue, last 6 calendar months
      top_products the five products with the most orders

    Revenue counts completed orders only, matching the total on the dashboard.
    An order that was placed but never delivered is not money.
    """
    # Imported here, not at module scope, matching dashboard_stats above —
    # `func` is not a module-level name in this file.
    from datetime import date

    from sqlalchemy import func

    from ..models import Product, PurchaseRequest

    user_id = int(get_jwt_identity())
    today = date.today()

    # Six buckets, oldest first, including months with nothing in them — a gap
    # in a trend line is information, and skipping empty months would draw a
    # flat line through a month the farmer sold nothing.
    months = []
    y, m = today.year, today.month
    for _ in range(6):
        months.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()

    start = date(months[0][0], months[0][1], 1)

    rows = (db.session.query(
                func.year(PurchaseRequest.created_at).label('y'),
                func.month(PurchaseRequest.created_at).label('m'),
                func.count(PurchaseRequest.id).label('orders'),
                func.sum(db.case((PurchaseRequest.status == 'completed',
                                  PurchaseRequest.total_price), else_=0)).label('revenue'))
            .filter(PurchaseRequest.farmer_id == user_id,
                    PurchaseRequest.created_at >= start)
            .group_by('y', 'm').all())
    by_month = {(r.y, r.m): r for r in rows}

    LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    monthly = []
    for (yy, mm) in months:
        r = by_month.get((yy, mm))
        monthly.append({
            'name': LABELS[mm - 1],
            'year': yy,
            'orders': int(r.orders) if r else 0,
            'revenue': float(r.revenue or 0) if r else 0.0,
        })

    top = (db.session.query(
               Product.name,
               func.count(PurchaseRequest.id).label('orders'),
               func.sum(db.case((PurchaseRequest.status == 'completed',
                                 PurchaseRequest.total_price), else_=0)).label('revenue'))
           .join(PurchaseRequest, PurchaseRequest.product_id == Product.id)
           .filter(PurchaseRequest.farmer_id == user_id)
           .group_by(Product.id, Product.name)
           .order_by(func.count(PurchaseRequest.id).desc())
           .limit(5).all())

    return jsonify({
        'monthly': monthly,
        'top_products': [
            {'name': n, 'orders': int(o), 'revenue': float(rv or 0)} for n, o, rv in top
        ],
    }), 200
