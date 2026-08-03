from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from ..extensions import limiter
from ..services import coupon_service

coupons_bp = Blueprint('coupons', __name__)


@coupons_bp.route('/preview', methods=['POST'])
@jwt_required()
# Codes are short and guessable, so an unthrottled preview endpoint is a
# brute-force oracle: an attacker could enumerate valid vouchers without ever
# placing an order.
@limiter.limit('20 per minute; 100 per hour')
def preview():
    """Check a code and show the discount, without claiming it.

    Nothing is reserved here. The coupon is only consumed when the order is
    created, which is where two people racing for the same code is settled.
    """
    data = request.get_json() or {}
    code = data.get('code', '')

    try:
        subtotal = float(data.get('subtotal', 0))
    except (TypeError, ValueError):
        return jsonify({'valid': False, 'error': 'Invalid order total'}), 400
    if subtotal <= 0:
        return jsonify({'valid': False, 'error': 'Add something to your order first'}), 400

    coupon, discount, error = coupon_service.preview(code, subtotal)
    if error:
        # 200 with valid:false — a wrong code is an expected outcome of typing,
        # not a client error, and the app renders the message inline.
        return jsonify({'valid': False, 'error': error}), 200

    return jsonify({
        'valid': True,
        'coupon': coupon.to_dict(),
        'subtotal': round(subtotal, 2),
        'discount': discount,
        'total': round(subtotal - discount, 2),
    }), 200


@coupons_bp.route('/my-redemptions', methods=['GET'])
@jwt_required()
def my_redemptions():
    """The coupons this customer has used."""
    from ..models import CouponRedemption
    user_id = int(get_jwt_identity())
    rows = (CouponRedemption.query
            .filter_by(customer_id=user_id)
            .order_by(CouponRedemption.created_at.desc())
            .limit(50).all())
    return jsonify([r.to_dict() for r in rows]), 200
