from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..services.request_service import (
    create_purchase_request, update_request_status,
    get_requests_for_customer, get_requests_for_farmer
)
from ..models import PurchaseRequest
from ..utils.helpers import paginate_response
from flask_jwt_extended import get_jwt

requests_bp = Blueprint('requests', __name__)


@requests_bp.route('', methods=['POST'])
@jwt_required()
def create_request():
    user_id = int(get_jwt_identity())
    claims = get_jwt()
    if claims.get('role') not in ('customer', 'admin'):
        return jsonify({'error': 'Only customers can submit requests'}), 403

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
    claims = get_jwt()
    role = claims.get('role')
    status = request.args.get('status')
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 50)

    if role == 'farmer':
        reqs, total = get_requests_for_farmer(user_id, status, page, per_page)
    elif role == 'customer':
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
    claims = get_jwt()
    req = PurchaseRequest.query.get_or_404(request_id)

    # Access control
    if claims.get('role') == 'customer' and req.customer_id != user_id:
        return jsonify({'error': 'Forbidden'}), 403
    if claims.get('role') == 'farmer' and req.farmer_id != user_id:
        return jsonify({'error': 'Forbidden'}), 403

    return jsonify(req.to_dict()), 200


@requests_bp.route('/<int:request_id>/status', methods=['PATCH'])
@jwt_required()
def update_status(request_id):
    user_id = int(get_jwt_identity())
    claims = get_jwt()
    role = claims.get('role')
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
