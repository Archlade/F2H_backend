import logging
from datetime import datetime

from sqlalchemy import event

from ..extensions import db
from ..models import Notification

# Notifications created in the current transaction that still owe someone a
# push. Parked on `session.info` rather than in a module global because that is
# per-session state: two greenlets serving two requests each get their own.
_PENDING_PUSHES = 'f2h_pending_pushes'


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

    # Queued, not sent. A push is the one side effect here that cannot be taken
    # back, so it waits for the transaction that created the row to actually
    # commit — see `_send_pending_pushes` below.
    _queue_push(n)

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


# ── Push notifications ────────────────────────────────────────────────────────
#
# Every notification row is also a push. Hooking it here rather than at each of
# the fifteen call sites means no service has to remember to do it and the two
# channels cannot drift apart — which matters, because the app assumes a push
# and a row describe the same event and refreshes its unread count when one
# arrives.
#
# The push fires on commit, never before. `create_notification` is called
# mid-transaction, sometimes more than once, and a transaction can still roll
# back after it — an order that fails to save must not leave the farmer holding
# a notification about it. Commit is also the first moment a fresh session can
# see the new row, which is what makes the iOS badge count come out right.


def _queue_push(notification):
    db.session.info.setdefault(_PENDING_PUSHES, []).append({
        'recipient_id': notification.recipient_id,
        'title': notification.title,
        'body': notification.body or '',
        # `type` rides along with the ids so the payload the app receives has
        # the same shape as the row the in-app list shows.
        'data': {**(notification.data or {}), 'type': notification.type},
    })


@event.listens_for(db.session, 'after_commit')
def _send_pending_pushes(session):
    pending = session.info.pop(_PENDING_PUSHES, None)
    if not pending:
        return

    # Imported here rather than at module scope: push_service imports the
    # models package, which imports this module, and at import time that is a
    # cycle.
    from .push_service import dispatch_to_user

    for push in pending:
        try:
            dispatch_to_user(push['recipient_id'], push['title'], push['body'], push['data'])
        except Exception:
            # A notification that saved is worth more than the push about it,
            # so a delivery problem never propagates back into the request that
            # created the row.
            logging.getLogger(__name__).exception(
                'Could not queue a push for user %s', push['recipient_id'])


@event.listens_for(db.session, 'after_rollback')
def _drop_pending_pushes(session):
    """The rows never happened, so neither should the pushes."""
    session.info.pop(_PENDING_PUSHES, None)


@event.listens_for(db.session, 'after_soft_rollback')
def _drop_pending_pushes_on_soft_rollback(session, _previous_transaction):
    """The same, for the rollback of a savepoint or a never-begun transaction.

    Declared separately rather than stacked on the function above because the
    two events hand the listener different numbers of arguments.
    """
    session.info.pop(_PENDING_PUSHES, None)
