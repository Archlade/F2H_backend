from datetime import datetime
from ..extensions import db
from ..models import Notification


def create_notification(recipient_id: int, sender_id: int, notif_type: str,
                         title: str, body: str = '', data: dict = None):
    n = Notification(
        recipient_id=recipient_id,
        sender_id=sender_id,
        type=notif_type,
        title=title,
        body=body,
        data=data or {},
    )
    db.session.add(n)
    # Don't commit here — let caller commit
    return n


def get_notifications(user_id: int, unread_only=False, page=1, per_page=20):
    query = Notification.query.filter_by(recipient_id=user_id)
    if unread_only:
        query = query.filter_by(is_read=False)
    query = query.order_by(Notification.created_at.desc())
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return items, total


def mark_notification_read(notif_id: int, user_id: int):
    n = Notification.query.filter_by(id=notif_id, recipient_id=user_id).first()
    if n and not n.is_read:
        n.is_read = True
        n.read_at = datetime.utcnow()
        db.session.commit()
    return n


def mark_all_read(user_id: int):
    Notification.query.filter_by(recipient_id=user_id, is_read=False).update({
        'is_read': True,
        'read_at': datetime.utcnow(),
    })
    db.session.commit()


def get_unread_count(user_id: int):
    return Notification.query.filter_by(recipient_id=user_id, is_read=False).count()
