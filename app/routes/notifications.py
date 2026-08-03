from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..services.notification_service import (
    get_notifications, mark_notification_read, mark_all_read, get_unread_count
)
from ..utils.helpers import paginate_response
from ..utils.validators import clamp_page

notifications_bp = Blueprint('notifications', __name__)


@notifications_bp.route('', methods=['GET'])
@jwt_required()
def list_notifications():
    user_id = int(get_jwt_identity())
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=50)
    unread_only = request.args.get('unread_only', type=lambda x: x == 'true')

    items, total = get_notifications(user_id, unread_only, page, per_page)
    return jsonify(paginate_response([n.to_dict() for n in items], total, page, per_page)), 200


@notifications_bp.route('/unread-count', methods=['GET'])
@jwt_required()
def unread_count():
    user_id = int(get_jwt_identity())
    return jsonify({'count': get_unread_count(user_id)}), 200


@notifications_bp.route('/<int:notif_id>/read', methods=['PATCH'])
@jwt_required()
def read_notification(notif_id):
    user_id = int(get_jwt_identity())
    n = mark_notification_read(notif_id, user_id)
    if not n:
        return jsonify({'error': 'Notification not found'}), 404
    return jsonify(n.to_dict()), 200


@notifications_bp.route('/read-all', methods=['PATCH'])
@jwt_required()
def read_all():
    user_id = int(get_jwt_identity())
    mark_all_read(user_id)
    return jsonify({'message': 'All notifications marked as read'}), 200
