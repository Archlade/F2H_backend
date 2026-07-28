from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..models import Review, Product, FarmerProfile
from ..extensions import db
from sqlalchemy import func

reviews_bp = Blueprint('reviews', __name__)


@reviews_bp.route('', methods=['POST'])
@jwt_required()
def create_review():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    rating = data.get('rating')
    if not rating or not (1 <= int(rating) <= 5):
        return jsonify({'error': 'Rating must be 1-5'}), 400

    review = Review(
        reviewer_id=user_id,
        product_id=data.get('product_id'),
        farmer_id=data.get('farmer_id'),
        request_id=data.get('request_id'),
        rating=int(rating),
        title=data.get('title', ''),
        content=data.get('content', ''),
    )
    db.session.add(review)
    db.session.flush()

    # Update avg rating
    _update_ratings(review)
    db.session.commit()
    return jsonify(review.to_dict()), 201


def _update_ratings(review):
    if review.product_id:
        product = Product.query.get(review.product_id)
        if product:
            result = db.session.query(
                func.avg(Review.rating), func.count(Review.id)
            ).filter(Review.product_id == review.product_id, Review.is_approved == True).one()
            product.rating_avg = round(float(result[0] or 0), 2)
            product.rating_count = result[1]

    if review.farmer_id:
        fp = FarmerProfile.query.filter_by(user_id=review.farmer_id).first()
        if fp:
            result = db.session.query(
                func.avg(Review.rating), func.count(Review.id)
            ).filter(Review.farmer_id == review.farmer_id, Review.is_approved == True).one()
            fp.rating_avg = round(float(result[0] or 0), 2)
            fp.rating_count = result[1]


@reviews_bp.route('', methods=['GET'])
def list_reviews():
    product_id = request.args.get('product_id', type=int)
    farmer_id = request.args.get('farmer_id', type=int)
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 10, type=int), 50)

    query = Review.query.filter_by(is_approved=True)
    if product_id:
        query = query.filter_by(product_id=product_id)
    if farmer_id:
        query = query.filter_by(farmer_id=farmer_id)

    total = query.count()
    reviews = query.order_by(Review.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    return jsonify({'items': [r.to_dict() for r in reviews], 'total': total}), 200
