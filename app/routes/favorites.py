from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..models import Favorite
from ..extensions import db

favorites_bp = Blueprint('favorites', __name__)


@favorites_bp.route('', methods=['GET'])
@jwt_required()
def list_favorites():
    user_id = int(get_jwt_identity())
    fav_type = request.args.get('type', 'product')  # product or farmer
    if fav_type == 'farmer':
        favs = Favorite.query.filter_by(user_id=user_id).filter(Favorite.farmer_id.isnot(None)).all()
        items = []
        for f in favs:
            if f.farmer:
                fp = f.farmer.farmer_profile
                items.append({
                    'id': f.id,
                    'farmer_id': f.farmer_id,
                    'farm_name': fp.farm_name if fp else f.farmer.full_name,
                    'avatar_url': fp.avatar_url if fp else f.farmer.avatar_url,
                    'rating_avg': float(fp.rating_avg) if fp else 0.0,
                    'is_verified': fp.is_verified if fp else False,
                })
    else:
        favs = Favorite.query.filter_by(user_id=user_id).filter(Favorite.product_id.isnot(None)).all()
        items = []
        for f in favs:
            if f.product:
                items.append({
                    'id': f.id,
                    'product_id': f.product_id,
                    **f.product.to_dict(),
                })
    return jsonify(items), 200


@favorites_bp.route('/product/<int:product_id>', methods=['POST'])
@jwt_required()
def favorite_product(product_id):
    user_id = int(get_jwt_identity())
    existing = Favorite.query.filter_by(user_id=user_id, product_id=product_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({'favorited': False}), 200
    fav = Favorite(user_id=user_id, product_id=product_id)
    db.session.add(fav)
    db.session.commit()
    return jsonify({'favorited': True}), 201


@favorites_bp.route('/farmer/<int:farmer_id>', methods=['POST'])
@jwt_required()
def favorite_farmer(farmer_id):
    user_id = int(get_jwt_identity())
    existing = Favorite.query.filter_by(user_id=user_id, farmer_id=farmer_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({'favorited': False}), 200
    fav = Favorite(user_id=user_id, farmer_id=farmer_id)
    db.session.add(fav)
    db.session.commit()
    return jsonify({'favorited': True}), 201


@favorites_bp.route('/check', methods=['GET'])
@jwt_required()
def check_favorite():
    user_id = int(get_jwt_identity())
    product_id = request.args.get('product_id', type=int)
    farmer_id = request.args.get('farmer_id', type=int)

    if product_id:
        exists = Favorite.query.filter_by(user_id=user_id, product_id=product_id).first()
        return jsonify({'favorited': bool(exists)}), 200
    if farmer_id:
        exists = Favorite.query.filter_by(user_id=user_id, farmer_id=farmer_id).first()
        return jsonify({'favorited': bool(exists)}), 200
    return jsonify({'favorited': False}), 200
