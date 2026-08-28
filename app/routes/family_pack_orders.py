from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from ..services.family_pack_order_service import (
    create_family_pack_order, update_family_pack_order_status,
    get_family_pack_orders_for_customer, get_family_pack_orders_for_farmer
)
from ..models.request import ACTIVE_FILTER, CLOSED_STATUSES
from ..models import FamilyPackOrder
from ..utils.helpers import paginate_response
from ..utils.validators import clamp_page
from ..utils.decorators import current_user_role

family_pack_orders_bp = Blueprint('family_pack_orders', __name__)

@family_pack_orders_bp.route('', methods=['POST'])
@jwt_required()
def create_order():
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    # Farmers buy from each other. Ordering your own pack is rejected in the
    # service, where the pack's owner is known.
    if role not in ('customer', 'farmer', 'admin'):
        return jsonify({'error': 'Please sign in to order a Family Pack'}), 403

    data = request.get_json() or {}
    if 'pack_id' not in data:
        return jsonify({'error': 'pack_id is required'}), 400

    try:
        order = create_family_pack_order(user_id, data)
        return jsonify(order.to_dict()), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': 'Server error'}), 500


@family_pack_orders_bp.route('', methods=['GET'])
@jwt_required()
def list_orders():
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    status = request.args.get('status')
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=50)

    # Weekly baskets materialise their deliveries lazily, so nothing is missed
    # even without a cron job running.
    from ..services.family_pack_subscription_service import generate_due_deliveries
    generate_due_deliveries()

    # See requests.list_requests: `side=buying` returns what a farmer ordered
    # rather than what they are fulfilling.
    side = request.args.get('side')

    if role == 'farmer' and side != 'buying':
        orders, total = get_family_pack_orders_for_farmer(user_id, status, page, per_page)
    elif role in ('customer', 'farmer'):
        orders, total = get_family_pack_orders_for_customer(user_id, status, page, per_page)
    elif role == 'delivery':
        # Only what this account was assigned. The filter *is* the
        # authorisation — see the twin branch in requests.list_requests.
        query = FamilyPackOrder.query.filter(
            FamilyPackOrder.assigned_delivery_id == user_id)
        if status:
            if status == ACTIVE_FILTER:
                query = query.filter(FamilyPackOrder.status.notin_(CLOSED_STATUSES))
            else:
                query = query.filter(FamilyPackOrder.status == status)
        else:
            query = query.filter(
                FamilyPackOrder.status.notin_(['completed', 'cancelled', 'rejected']))
        total = query.count()
        orders = (query.order_by(FamilyPackOrder.created_at.desc())
                  .offset((page - 1) * per_page).limit(per_page).all())
        return jsonify(paginate_response(
            [o.for_courier() for o in orders], total, page, per_page)), 200
    elif role == 'admin':
        query = FamilyPackOrder.query
        if status:
            if status == ACTIVE_FILTER:
                query = query.filter(FamilyPackOrder.status.notin_(CLOSED_STATUSES))
            else:
                query = query.filter(FamilyPackOrder.status == status)
        total = query.count()
        orders = query.order_by(FamilyPackOrder.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    else:
        return jsonify({'error': 'Forbidden'}), 403

    items = [o.to_dict() for o in orders]
    return jsonify(paginate_response(items, total, page, per_page)), 200


@family_pack_orders_bp.route('/<int:order_id>', methods=['GET'])
@jwt_required()
def get_order(order_id):
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    order = FamilyPackOrder.query.get_or_404(order_id)

    # By side of the order, so a farmer can open a pack they bought.
    from ..models.request import party_for
    party = party_for(order, user_id, role)
    if party is None:
        return jsonify({'error': 'Forbidden'}), 403

    if party == 'delivery':
        return jsonify(order.for_courier()), 200

    return jsonify(order.to_dict()), 200


@family_pack_orders_bp.route('/<int:order_id>/status', methods=['PATCH'])
@jwt_required()
def update_status(order_id):
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    data = request.get_json() or {}

    new_status = data.get('status')
    if not new_status:
        return jsonify({'error': 'status is required'}), 400

    try:
        order = update_family_pack_order_status(order_id, user_id, role, new_status, data)
        return jsonify(order.to_dict()), 200
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': 'Server error'}), 500
