import hashlib
import secrets
from datetime import datetime, timedelta

from ..extensions import db

# Long enough that guessing is hopeless, short enough to survive an email client
# that wraps long URLs.
TOKEN_BYTES = 32
TOKEN_TTL_MINUTES = 60


def hash_token(raw_token: str) -> str:
    """Tokens are stored hashed so a leaked database can't be used to reset accounts."""
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    # SHA-256 hex digest, never the token itself.
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)
    requested_ip = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id])

    @classmethod
    def issue(cls, user_id: int, ip_address: str = None):
        """Create a token row and return (row, raw_token). The raw token is
        returned exactly once — it is never recoverable from the database."""
        raw = secrets.token_urlsafe(TOKEN_BYTES)
        row = cls(
            user_id=user_id,
            token_hash=hash_token(raw),
            expires_at=datetime.utcnow() + timedelta(minutes=TOKEN_TTL_MINUTES),
            requested_ip=(ip_address or '')[:45] or None,
        )
        return row, raw

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and self.expires_at > datetime.utcnow()
