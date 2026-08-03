from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..services.request_service import (
    create_purchase_request, update_request_status,
    get_requests_for_customer, get_requests_for_farmer
)
from ..models import PurchaseRequest
from ..utils.helpers import paginate_response
from flask_jwt_extended import get_jwt
from ..utils.validators import clamp_page
from ..utils.decorators import current_user_role

requests_bp = Blueprint('requests', __name__)


@requests_bp.route('', methods=['POST'])
@jwt_required()
def create_request():
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    # Farmers buy from each other, so anyone signed in may place a request.
    # Buying your own listing is rejected in the service, where the product —
    # and therefore its owner — is actually known.
    if role not in ('customer', 'farmer', 'admin'):
        return jsonify({'error': 'Please sign in to place an order'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required = ['product_id', 'quantity', 'purchase_mode']
    for f in required:
        if f not in data:
            return jsonify({'error': f'{f} is required'}), 400

    if data['purchase_mode'] == 'delivery' and not data.get('delivery_address_id'):
        return jsonify({'error': 'delivery_address_id is required for delivery'}), 400

    try:
        req = create_purchase_request(user_id, data)
        return jsonify(req.to_dict()), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': 'Server error'}), 500


@requests_bp.route('', methods=['GET'])
@jwt_required()
def list_requests():
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    status = request.args.get('status')
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=50)

    # `side` picks which half of a farmer's activity to return. Farmers both
    # sell and buy, and the two need separate screens: the actions differ
    # (accept/reject vs cancel) and mixing them invites acting on the wrong one.
    #   selling  — orders for their listings   (default for farmers)
    #   buying   — orders they placed          (their My purchases screen)
    side = request.args.get('side')

    if role == 'farmer' and side != 'buying':
        reqs, total = get_requests_for_farmer(user_id, status, page, per_page)
    elif role in ('customer', 'farmer'):
        reqs, total = get_requests_for_customer(user_id, status, page, per_page)
    elif role == 'admin':
        query = PurchaseRequest.query
        if status:
            query = query.filter(PurchaseRequest.status == status)
        total = query.count()
        reqs = query.order_by(PurchaseRequest.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    else:
        return jsonify({'error': 'Forbidden'}), 403

    items = [r.to_dict() for r in reqs]
    return jsonify(paginate_response(items, total, page, per_page)), 200


@requests_bp.route('/<int:request_id>', methods=['GET'])
@jwt_required()
def get_request(request_id):
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    req = PurchaseRequest.query.get_or_404(request_id)

    # Access control by side of the order, not by account role — a farmer who
    # placed this order is its buyer and must be able to open it. Checking
    # `farmer_id` alone used to 403 a farmer on their own purchases.
    from ..models.request import party_for
    if party_for(req, user_id, role) is None:
        return jsonify({'error': 'Forbidden'}), 403

    return jsonify(req.to_dict()), 200


@requests_bp.route('/<int:request_id>/status', methods=['PATCH'])
@jwt_required()
def update_status(request_id):
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    data = request.get_json() or {}

    new_status = data.get('status')
    if not new_status:
        return jsonify({'error': 'status is required'}), 400

    try:
        req = update_request_status(request_id, user_id, role, new_status, data)
        return jsonify(req.to_dict()), 200
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': 'Server error'}), 500
