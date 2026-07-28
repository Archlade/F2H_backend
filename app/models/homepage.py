from ..extensions import db
from datetime import datetime


class HomepageSection(db.Model):
    __tablename__ = 'homepage_sections'

    id = db.Column(db.Integer, primary_key=True)
    section_key = db.Column(db.String(100), nullable=False, unique=True)
    title = db.Column(db.String(255))
    subtitle = db.Column(db.Text)
    cta_label = db.Column(db.String(100))
    cta_url = db.Column(db.String(500))
    is_visible = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    data = db.Column(db.JSON)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    updater = db.relationship('User', foreign_keys=[updated_by])

    def to_dict(self):
        return {
            'id': self.id,
            'section_key': self.section_key,
            'title': self.title,
            'subtitle': self.subtitle,
            'cta_label': self.cta_label,
            'cta_url': self.cta_url,
            'is_visible': self.is_visible,
            'sort_order': self.sort_order,
            'data': self.data,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
