from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from ..services.chat_service import get_chat_by_id, get_user_chats, get_messages, send_message
from ..utils.decorators import current_user_role

chat_bp = Blueprint('chat', __name__)


@chat_bp.route('', methods=['GET'])
@jwt_required()
def list_chats():
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    chats = get_user_chats(user_id, role)
    return jsonify([c.to_dict(current_user_id=user_id) for c in chats]), 200


@chat_bp.route('/<int:chat_id>', methods=['GET'])
@jwt_required()
def get_chat(chat_id):
    user_id = int(get_jwt_identity())
    try:
        chat = get_chat_by_id(chat_id, user_id)
        if not chat:
            return jsonify({'error': 'Chat not found'}), 404
        return jsonify(chat.to_dict(current_user_id=user_id)), 200
    except PermissionError:
        return jsonify({'error': 'Forbidden'}), 403


@chat_bp.route('/<int:chat_id>/messages', methods=['GET'])
@jwt_required()
def list_messages(chat_id):
    user_id = int(get_jwt_identity())
    page = request.args.get('page', 1, type=int)
    try:
        messages, total = get_messages(chat_id, user_id, page=page)
        return jsonify({
            'messages': [m.to_dict() for m in messages],
            'total': total,
            'page': page,
        }), 200
    except PermissionError:
        return jsonify({'error': 'Forbidden'}), 403


@chat_bp.route('/<int:chat_id>/messages', methods=['POST'])
@jwt_required()
def post_message(chat_id):
    user_id = int(get_jwt_identity())
    data = request.get_json()
    if not data or not data.get('content', '').strip():
        return jsonify({'error': 'Message content is required'}), 400

    try:
        msg = send_message(chat_id, user_id, data['content'])
        return jsonify(msg.to_dict()), 201
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
