from datetime import datetime

from ..extensions import db


class DeviceToken(db.Model):
    """One FCM registration token — that is, one *install* of the mobile app.

    Not one user and not one phone. Reinstalling, clearing app data or
    restoring onto a new handset all mint a fresh token, and the old one keeps
    working for whoever still holds the old install. So a user has many rows
    here (phone, tablet, an uninstall nobody has pruned yet) and every one of
    them gets the push.

    The flip side is that a token can outlive its owner's session, which is why
    `token` is unique rather than `(user_id, token)`: when a second person signs
    in on the same phone the row moves to them instead of duplicating, and the
    previous owner stops receiving deliveries on a device they no longer hold.
    """

    __tablename__ = 'device_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    # FCM tokens are around 160 characters today and the format is not
    # guaranteed, so 255 is the usual headroom. Unique because the upsert in
    # `/api/devices` keys on it.
    token = db.Column(db.String(255), unique=True, nullable=False)
    platform = db.Column(db.String(16))  # 'android' | 'ios'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Bumped on every registration. The app re-sends its token on each launch,
    # so this doubles as "when was this install last opened" — which is what
    # tells a months-dead row apart from a live one FCM simply hasn't
    # rejected yet.
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id], back_populates='device_tokens')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'platform': self.platform,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_seen_at': self.last_seen_at.isoformat() if self.last_seen_at else None,
        }
