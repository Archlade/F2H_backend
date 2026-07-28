from ..extensions import db
from datetime import datetime


class Category(db.Model):
    __tablename__ = 'categories'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    slug = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text)
    icon = db.Column(db.String(100))
    image_url = db.Column(db.String(500))
    parent_id = db.Column(db.Integer, db.ForeignKey('categories.id', ondelete='SET NULL'))
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    products = db.relationship('Product', back_populates='category')
    children = db.relationship('Category', backref=db.backref('parent', remote_side=[id]))

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'slug': self.slug,
            'description': self.description,
            'icon': self.icon,
            'image_url': self.image_url,
            'parent_id': self.parent_id,
            'sort_order': self.sort_order,
            'is_active': self.is_active,
        }
