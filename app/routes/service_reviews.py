"""What customers think of the service, and what of it reaches the homepage.

Three audiences, three shapes of the same row:

    GET  /api/service-reviews      public   approved only, first name + initial
    POST /api/service-reviews      customer their own, created or updated
    GET  /api/service-reviews/mine customer their own, whatever its state

The admin queue lives in `routes/admin.py` with the rest of the admin surface.
"""

from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from ..extensions import db, limiter
from ..models import ServiceReview
from ..utils.validators import clamp_page

service_reviews_bp = Blueprint('service_reviews', __name__)

_MAX_COMMENT = 1000


@service_reviews_bp.route('', methods=['GET'])
def list_public():
    """Approved reviews, newest first — the homepage testimonials.

    No authentication: this is what visitors read. `is_approved` is the only
    filter that matters and it is applied here rather than left to the caller,
    so there is no query string that returns the unapproved ones.
    """
    page, per_page = clamp_page(request.args.get('page'),
                                request.args.get('per_page'), max_per_page=50)

    query = (ServiceReview.query
             .filter(ServiceReview.is_approved.is_(True))
             .order_by(ServiceReview.updated_at.desc()))

    total = query.count()
    rows = query.offset((page - 1) * per_page).limit(per_page).all()

    average = db.session.query(db.func.avg(ServiceReview.rating)).filter(
        ServiceReview.is_approved.is_(True)).scalar()

    return jsonify({
        'items': [r.to_dict() for r in rows],
        'total': total,
        'page': page,
        'per_page': per_page,
        # Rounded to one decimal because two implies a precision that a handful
        # of reviews does not have.
        'average': round(float(average), 1) if average is not None else None,
    }), 200


@service_reviews_bp.route('/mine', methods=['GET'])
@jwt_required()
def mine():
    """This customer's own review, approved or not, or null if they have none."""
    row = ServiceReview.query.filter_by(user_id=int(get_jwt_identity())).first()
    return jsonify(row.to_dict() if row else None), 200


@service_reviews_bp.route('', methods=['POST'])
@jwt_required()
@limiter.limit('10 per hour')
def submit():
    """Leave feedback about the service, or replace what you left before.

    An upsert rather than an insert. One person has one opinion of the service
    at a time; without this the way to get published is to keep submitting until
    something slips through the queue.

    **Editing always returns the review to the queue.** A review approved as
    praise must not be quietly rewritten into something else while keeping its
    place on the homepage, which is the one way an approval step can be worked
    around from the outside.
    """
    user_id = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}

    try:
        rating = int(data.get('rating'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Choose a rating from 1 to 5'}), 400
    if not 1 <= rating <= 5:
        return jsonify({'error': 'Choose a rating from 1 to 5'}), 400

    comment = (data.get('comment') or '').strip()
    if len(comment) > _MAX_COMMENT:
        return jsonify({'error': f'Please keep it under {_MAX_COMMENT} characters'}), 400

    row = ServiceReview.query.filter_by(user_id=user_id).first()
    created = row is None
    if created:
        row = ServiceReview(user_id=user_id)
        db.session.add(row)

    row.rating = rating
    row.comment = comment or None
    # Back to the queue, every time.
    row.is_approved = False
    row.approved_by = None
    row.approved_at = None
    row.updated_at = datetime.utcnow()

    db.session.commit()

    return jsonify({
        **row.to_dict(),
        'message': 'Thank you — your feedback has been sent to our team.'
                   if created else
                   'Thank you — your updated feedback has been sent to our team.',
    }), 201 if created else 200


@service_reviews_bp.route('/mine', methods=['DELETE'])
@jwt_required()
def withdraw():
    """Take your review back, published or not."""
    row = ServiceReview.query.filter_by(user_id=int(get_jwt_identity())).first()
    if row is None:
        return jsonify({'error': 'You have not left any feedback'}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({'message': 'Your feedback has been removed'}), 200
