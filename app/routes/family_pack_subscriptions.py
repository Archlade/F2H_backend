from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt

from ..services.family_pack_subscription_service import (
    create_subscription, update_subscription, set_subscription_status,
    get_subscriptions_for_customer, get_subscriptions_for_farmer,
    get_subscription, generate_due_deliveries,
)
from ..utils.decorators import current_user_role

family_pack_subscriptions_bp = Blueprint('family_pack_subscriptions', __name__)


@family_pack_subscriptions_bp.route('', methods=['POST'])
@jwt_required()
def create():
    user_id = int(get_jwt_identity())
    # Farmers buy too; subscribing to your own produce is refused in the
    # service, where the farmer behind the basket is known.
    if current_user_role()[1] not in ('customer', 'farmer'):
        return jsonify({'error': 'Please sign in to start a weekly basket'}), 403
    try:
        sub = create_subscription(user_id, request.get_json() or {})
        return jsonify(sub.to_dict()), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@family_pack_subscriptions_bp.route('', methods=['GET'])
@jwt_required()
def list_subscriptions():
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    status = request.args.get('status')

    # Catch up on any deliveries that fell due since the last request.
    generate_due_deliveries()

    # `side=buying` gives a farmer the baskets they subscribe to, rather than
    # the ones they supply. See requests.list_requests.
    side = request.args.get('side')

    if role == 'farmer' and side != 'buying':
        subs = get_subscriptions_for_farmer(user_id, status)
    elif role in ('customer', 'farmer'):
        subs = get_subscriptions_for_customer(user_id, status)
    elif role == 'admin':
        subs = get_subscriptions_for_customer(user_id, status)
    else:
        return jsonify({'error': 'Forbidden'}), 403

    return jsonify({'items': [s.to_dict() for s in subs], 'total': len(subs)}), 200


@family_pack_subscriptions_bp.route('/<int:subscription_id>', methods=['GET'])
@jwt_required()
def get_one(subscription_id):
    user_id = int(get_jwt_identity())
    try:
        sub = get_subscription(subscription_id, user_id)
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    if not sub:
        return jsonify({'error': 'Weekly basket not found'}), 404
    return jsonify(sub.to_dict(include_deliveries=True)), 200


@family_pack_subscriptions_bp.route('/<int:subscription_id>', methods=['PUT'])
@jwt_required()
def update(subscription_id):
    user_id = int(get_jwt_identity())
    if current_user_role()[1] != 'customer':
        return jsonify({'error': 'Only the customer can edit their basket'}), 403
    try:
        sub = update_subscription(subscription_id, user_id, request.get_json() or {})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if not sub:
        return jsonify({'error': 'Weekly basket not found'}), 404
    return jsonify(sub.to_dict()), 200


@family_pack_subscriptions_bp.route('/<int:subscription_id>/status', methods=['PATCH'])
@jwt_required()
def change_status(subscription_id):
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    data = request.get_json() or {}
    if not data.get('status'):
        return jsonify({'error': 'status is required'}), 400
    try:
        sub = set_subscription_status(subscription_id, user_id, role, data['status'], data)
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if not sub:
        return jsonify({'error': 'Weekly basket not found'}), 404
    return jsonify(sub.to_dict()), 200


@family_pack_subscriptions_bp.route('/run-due', methods=['POST'])
@jwt_required()
def run_due():
    """Manual trigger for the weekly generator — handy for cron or admin."""
    if current_user_role()[1] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    created = generate_due_deliveries()
    return jsonify({'created': len(created),
                    'orders': [o.id for o in created]}), 200
