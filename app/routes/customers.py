from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..models import User, RecentlyViewed, Favorite, PurchaseRequest
from ..extensions import db

customers_bp = Blueprint('customers', __name__)


@customers_bp.route('/dashboard', methods=['GET'])
@jwt_required()
def dashboard():
    user_id = int(get_jwt_identity())

    recent_requests = (PurchaseRequest.query
                       .filter_by(customer_id=user_id)
                       .order_by(PurchaseRequest.created_at.desc())
                       .limit(5).all())

    recently_viewed = (RecentlyViewed.query
                       .filter_by(user_id=user_id)
                       .order_by(RecentlyViewed.viewed_at.desc())
                       .limit(10).all())

    fav_products = (Favorite.query
                    .filter_by(user_id=user_id)
                    .filter(Favorite.product_id.isnot(None))
                    .limit(6).all())

    return jsonify({
        'recent_requests': [r.to_dict() for r in recent_requests],
        'recently_viewed': [
            rv.product.to_dict() for rv in recently_viewed if rv.product
        ],
        'favorite_products': [
            fav.product.to_dict() for fav in fav_products if fav.product
        ],
    }), 200
