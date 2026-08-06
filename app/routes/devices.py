"""Where the mobile app parks its FCM registration token.

Two endpoints, both on the caller's own session: the app registers on sign-in
and on every launch after that, and retires the token on sign-out. Sending is
`services/push_service.py`; this file only keeps the address book.
"""

from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from ..extensions import db
from ..models import DeviceToken

devices_bp = Blueprint('devices', __name__)

_PLATFORMS = {'android', 'ios', 'web'}

# Long enough for any FCM token with headroom, short enough that a client
# bug cannot write megabytes into the table. Matches the column width.
_MAX_TOKEN_LENGTH = 255


@devices_bp.route('', methods=['POST'])
@jwt_required()
def register_device():
    """Upsert this install's token against the signed-in user.

    Upsert, not insert: the app re-sends the same token on every launch because
    it has no reliable way to know the server still has it, and an insert would
    turn that into a duplicate row and a duplicate notification per launch.

    If the token already belongs to a *different* user it moves rather than
    being rejected — one phone has one owner, and the only way a token changes
    hands is the previous owner signing out on that handset. Leaving it put is
    how a shared phone ends up showing one person's orders to the next.
    """
    user_id = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}

    token = (data.get('token') or '').strip()
    if not token:
        return jsonify({'error': 'A device token is required', 'code': 'TOKEN_REQUIRED'}), 400
    if len(token) > _MAX_TOKEN_LENGTH:
        return jsonify({'error': 'That device token is not valid',
                        'code': 'TOKEN_INVALID'}), 400

    platform = (data.get('platform') or '').strip().lower()
    if platform not in _PLATFORMS:
        platform = None

    now = datetime.utcnow()
    row = DeviceToken.query.filter_by(token=token).first()

    if row is None:
        row = DeviceToken(token=token, user_id=user_id, platform=platform,
                          created_at=now, last_seen_at=now)
        db.session.add(row)
    else:
        row.user_id = user_id
        row.last_seen_at = now
        if platform:
            row.platform = platform

    try:
        db.session.commit()
    except Exception:
        # Two launches racing on a cold start can both miss the SELECT above
        # and both insert. The loser retries as an update rather than 500ing at
        # a client that is only saying hello.
        db.session.rollback()
        row = DeviceToken.query.filter_by(token=token).first()
        if row is None:
            raise
        row.user_id = user_id
        row.last_seen_at = now
        db.session.commit()

    return jsonify({'success': True}), 200


@devices_bp.route('', methods=['DELETE'])
@jwt_required()
def unregister_device():
    """Retire a token on sign-out.

    Scoped to the caller so one account cannot silence another's phone by
    guessing a token. Deleting something that is already gone is a success:
    the app calls this on every sign-out and must not be made to care whether
    the row survived.
    """
    user_id = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}

    token = (data.get('token') or '').strip()
    if not token:
        return jsonify({'error': 'A device token is required', 'code': 'TOKEN_REQUIRED'}), 400

    DeviceToken.query.filter_by(token=token, user_id=user_id).delete()
    db.session.commit()

    return jsonify({'success': True}), 200
