from types import SimpleNamespace

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..models import Review, Product, FarmerProfile, PurchaseRequest, FamilyPackOrder
from ..extensions import db
from sqlalchemy import func
from ..utils.validators import clamp_page

reviews_bp = Blueprint('reviews', __name__)


@reviews_bp.route('', methods=['POST'])
@jwt_required()
def create_review():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    try:
        rating = int(data.get('rating'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Rating must be 1-5'}), 400
    if not (1 <= rating <= 5):
        return jsonify({'error': 'Rating must be 1-5'}), 400

    product_id = data.get('product_id')
    farmer_id = data.get('farmer_id')
    if not product_id and not farmer_id:
        return jsonify({'error': 'A product or a farmer is required'}), 400

    # Only people who actually bought can review, otherwise ratings can be
    # moved at will by anyone holding an account.
    completed = ['completed']
    purchase_q = PurchaseRequest.query.filter(
        PurchaseRequest.customer_id == user_id,
        PurchaseRequest.status.in_(completed),
    )
    if product_id:
        purchase_q = purchase_q.filter(PurchaseRequest.product_id == product_id)
    else:
        purchase_q = purchase_q.filter(PurchaseRequest.farmer_id == farmer_id)
    purchase = purchase_q.order_by(PurchaseRequest.created_at.desc()).first()

    if not purchase:
        pack_q = FamilyPackOrder.query.filter(
            FamilyPackOrder.customer_id == user_id,
            FamilyPackOrder.status.in_(completed),
        )
        if farmer_id:
            pack_q = pack_q.filter(FamilyPackOrder.farmer_id == farmer_id)
        if not pack_q.first():
            return jsonify({
                'error': 'You can only review after a completed order',
                'code': 'NO_PURCHASE',
            }), 403

    # One review per customer per product/farm.
    duplicate = Review.query.filter_by(
        reviewer_id=user_id,
        product_id=product_id or None,
        farmer_id=farmer_id or None,
    ).first()
    if duplicate:
        return jsonify({'error': 'You have already reviewed this'}), 409

    review = Review(
        reviewer_id=user_id,
        product_id=product_id,
        farmer_id=farmer_id,
        # Derived from the verified purchase, never taken from the request body.
        request_id=purchase.id if purchase else None,
        rating=rating,
        title=(data.get('title') or '')[:255],
        content=(data.get('content') or '')[:5000],
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


@reviews_bp.route('/<int:review_id>', methods=['DELETE'])
@jwt_required()
def delete_review(review_id):
    """Withdraw a review.

    Both clients have been calling this for a while — `reviewsAPI.delete` in
    frontend/src/api/index.js and `AccountRepository.delete` in the app — against
    a route that did not exist, so the button 404'd on the website and in the
    app alike. The clients were right about the shape; the server was simply
    missing.

    Only the person who wrote it. An admin who wants a review gone has
    `PATCH /admin/reviews/<id>/approve` to unpublish it, which keeps the row for
    moderation history; deleting outright is the author's own call.
    """
    user_id = int(get_jwt_identity())
    review = Review.query.get_or_404(review_id)

    if review.reviewer_id != user_id:
        # 404 rather than 403 on purpose: a 403 confirms the review exists,
        # which is a small enumeration leak for something the caller has no
        # business seeing either way.
        return jsonify({'error': 'Review not found'}), 404

    # `_update_ratings` recomputes a product's or farmer's average from the rows
    # that remain, so the delete has to be flushed before it runs or the query
    # still counts the row being removed. The ids are read off the review first
    # for the same reason — after the flush, the instance is expired and reading
    # `review.product_id` would go back to a row that is no longer there.
    scope = SimpleNamespace(product_id=review.product_id, farmer_id=review.farmer_id)

    db.session.delete(review)
    db.session.flush()

    _update_ratings(scope)
    db.session.commit()

    return jsonify({'message': 'Review deleted'}), 200


@reviews_bp.route('', methods=['GET'])
def list_reviews():
    product_id = request.args.get('product_id', type=int)
    farmer_id = request.args.get('farmer_id', type=int)
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=50)

    query = Review.query.filter_by(is_approved=True)
    if product_id:
        query = query.filter_by(product_id=product_id)
    if farmer_id:
        query = query.filter_by(farmer_id=farmer_id)

    total = query.count()
    reviews = query.order_by(Review.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    return jsonify({'items': [r.to_dict() for r in reviews], 'total': total}), 200
