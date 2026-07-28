from ..extensions import db
from datetime import datetime


class Report(db.Model):
    __tablename__ = 'reports'

    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    report_type = db.Column(db.Enum('product', 'farmer', 'review', 'chat'), nullable=False)
    target_id = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.Enum('pending', 'reviewed', 'resolved', 'dismissed'), default='pending')
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    reporter = db.relationship('User', foreign_keys=[reporter_id])
    reviewer = db.relationship('User', foreign_keys=[reviewed_by])

    def to_dict(self):
        return {
            'id': self.id,
            'reporter_id': self.reporter_id,
            'report_type': self.report_type,
            'target_id': self.target_id,
            'reason': self.reason,
            'description': self.description,
            'status': self.status,
            'reviewed_by': self.reviewed_by,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'reporter': {'id': self.reporter.id, 'full_name': self.reporter.full_name} if self.reporter else None,
        }
