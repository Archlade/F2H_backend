from ..extensions import db
from datetime import datetime


class Announcement(db.Model):
    __tablename__ = 'announcements'

    id = db.Column(db.Integer, primary_key=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    type = db.Column(db.Enum('info', 'success', 'warning', 'error'), default='info')
    target_role = db.Column(db.Enum('all', 'customer', 'farmer'), default='all')
    is_active = db.Column(db.Boolean, default=True)
    starts_at = db.Column(db.DateTime)
    ends_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'content': self.content,
            'type': self.type,
            'target_role': self.target_role,
            'is_active': self.is_active,
            'starts_at': self.starts_at.isoformat() if self.starts_at else None,
            'ends_at': self.ends_at.isoformat() if self.ends_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
