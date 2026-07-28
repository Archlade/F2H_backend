from ..extensions import socketio
from flask_socketio import join_room, leave_room, emit
from flask_jwt_extended import decode_token
from flask import request


def get_user_from_token():
    """Extract user ID from cookie or auth header."""
    try:
        token = request.cookies.get('access_token_cookie')
        if not token:
            auth = request.headers.get('Authorization', '')
            if auth.startswith('Bearer '):
                token = auth[7:]
        if token:
            data = decode_token(token)
            return int(data.get('sub')), data.get('role')
    except Exception:
        pass
    return None, None


@socketio.on('connect')
def on_connect():
    user_id, role = get_user_from_token()
    if user_id:
        join_room(f"user_{user_id}")
        emit('connected', {'user_id': user_id})


@socketio.on('disconnect')
def on_disconnect():
    pass


@socketio.on('join_chat')
def on_join_chat(data):
    user_id, role = get_user_from_token()
    if not user_id:
        return

    chat_id = data.get('chat_id')
    if not chat_id:
        return

    # Verify user is part of this chat
    from ..models import Chat
    chat = Chat.query.get(chat_id)
    if chat and (chat.customer_id == user_id or chat.farmer_id == user_id):
        join_room(f"chat_{chat_id}")
        emit('joined_chat', {'chat_id': chat_id})


@socketio.on('leave_chat')
def on_leave_chat(data):
    chat_id = data.get('chat_id')
    if chat_id:
        leave_room(f"chat_{chat_id}")


@socketio.on('typing')
def on_typing(data):
    user_id, role = get_user_from_token()
    if not user_id:
        return

    chat_id = data.get('chat_id')
    is_typing = data.get('is_typing', False)
    if chat_id:
        from ..models import Chat
        chat = Chat.query.get(chat_id)
        if chat and (chat.customer_id == user_id or chat.farmer_id == user_id):
            emit('user_typing', {
                'user_id': user_id,
                'chat_id': chat_id,
                'is_typing': is_typing,
            }, room=f"chat_{chat_id}", include_self=False)
