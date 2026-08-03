from datetime import datetime
from ..extensions import db, socketio
from ..models import Chat, Message
from .notification_service import create_notification


def get_chat_by_id(chat_id: int, user_id: int):
    chat = Chat.query.get(chat_id)
    if not chat:
        return None
    if chat.customer_id != user_id and chat.farmer_id != user_id:
        raise PermissionError('Not authorized to access this chat')
    return chat


def get_user_chats(user_id: int, role: str):
    """Every chat the user is a party to, whichever side they are on.

    Selecting by role used to mean a farmer saw only the chats attached to
    their sales; the conversations about produce they had *bought* were
    invisible even though they were half of them.
    """
    from ..extensions import db
    return (Chat.query
            .filter(db.or_(Chat.customer_id == user_id, Chat.farmer_id == user_id))
            .order_by(Chat.last_message_at.desc())
            .all())


def get_messages(chat_id: int, user_id: int, page=1, per_page=50):
    chat = get_chat_by_id(chat_id, user_id)
    if not chat:
        return [], 0
    total = Message.query.filter_by(chat_id=chat_id).count()
    messages = (Message.query.filter_by(chat_id=chat_id)
                .order_by(Message.created_at.asc())
                .offset((page - 1) * per_page)
                .limit(per_page)
                .all())
    # Mark received messages as read
    Message.query.filter(
        Message.chat_id == chat_id,
        Message.sender_id != user_id,
        Message.is_read == False
    ).update({'is_read': True, 'read_at': datetime.utcnow()})
    db.session.commit()
    return messages, total


def send_message(chat_id: int, sender_id: int, content: str):
    chat = Chat.query.get(chat_id)
    if not chat:
        raise ValueError('Chat not found')
    if chat.customer_id != sender_id and chat.farmer_id != sender_id:
        raise PermissionError('Not authorized')
    if not chat.is_active:
        raise ValueError('Chat is no longer active')

    msg = Message(chat_id=chat_id, sender_id=sender_id, content=content.strip())
    db.session.add(msg)
    chat.last_message_at = datetime.utcnow()
    db.session.flush()

    # Determine recipient
    recipient_id = chat.farmer_id if sender_id == chat.customer_id else chat.customer_id

    create_notification(
        recipient_id=recipient_id,
        sender_id=sender_id,
        notif_type='new_message',
        title='New Message',
        body=content[:100],
        data={'chat_id': chat_id, 'message_id': msg.id},
    )
    db.session.commit()

    # Emit via Socket.IO
    msg_dict = msg.to_dict()
    socketio.emit('new_message', msg_dict, room=f"chat_{chat_id}")
    socketio.emit('new_notification',
                  {'type': 'new_message', 'chat_id': chat_id},
                  room=f"user_{recipient_id}")

    return msg
