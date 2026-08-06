from ..extensions import socketio
from flask_socketio import join_room, leave_room, emit
from flask_jwt_extended import decode_token
from flask import request


def get_user_from_token():
    """The user behind this socket, or (None, None).

    `decode_token` checks the signature and the expiry — it does **not** know
    anything about revocation. So this repeats the one revocation rule the HTTP
    side enforces in the JWT loader: a token minted before the account's
    password last changed is dead.

    Without that, the whole point of "resetting my password signs everyone
    else out" leaked straight past the socket layer. An intruder's HTTP calls
    would start failing while their socket carried on delivering the victim's
    order notifications in real time — quieter than a live session, and
    invisible to the person who thought they had locked the account.
    """
    try:
        token = request.cookies.get('access_token_cookie')
        if not token:
            auth = request.headers.get('Authorization', '')
            if auth.startswith('Bearer '):
                token = auth[7:]
        if not token:
            return None, None

        data = decode_token(token)
        user_id = int(data.get('sub'))

        if _token_revoked(user_id, data.get('iat')):
            return None, None
        return user_id, data.get('role')
    except Exception:
        pass
    return None, None


def _token_revoked(user_id, issued_at):
    """True when this token predates the account's last password change.

    Mirrors `_token_predates_password_change` in app/__init__.py, including the
    microsecond truncation: `iat` is whole seconds while `password_changed_at`
    is not, so without it the token handed to someone who just reset their own
    password is refused about half the time.
    """
    from datetime import datetime, timezone

    from ..models import User

    if issued_at is None:
        return False                      # nothing to compare; the checks below still apply

    user = User.query.get(user_id)
    changed_at = getattr(user, 'password_changed_at', None) if user else None
    if changed_at is None:
        return False

    issued = datetime.fromtimestamp(issued_at, tz=timezone.utc).replace(tzinfo=None)
    return issued < changed_at.replace(microsecond=0)


@socketio.on('connect')
def on_connect():
    user_id, role = get_user_from_token()
    if not user_id:
        # Returning False rejects the handshake instead of leaving an
        # unauthenticated socket connected.
        return False

    from ..models import User
    user = User.query.get(user_id)
    if not user or not user.is_active or user.deleted_at:
        return False

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
