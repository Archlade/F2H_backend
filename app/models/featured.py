from ..extensions import db
from datetime import datetime


class FeaturedFarmer(db.Model):
    __tablename__ = 'featured_farmers'

    id = db.Column(db.Integer, primary_key=True)
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    added_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    farmer = db.relationship('User', foreign_keys=[farmer_id])
    adder = db.relationship('User', foreign_keys=[added_by])

    def to_dict(self):
        fp = self.farmer.farmer_profile if self.farmer else None
        return {
            'id': self.id,
            'farmer_id': self.farmer_id,
            'sort_order': self.sort_order,
            'is_active': self.is_active,
            'farmer': {
                'id': self.farmer.id,
                'full_name': self.farmer.full_name,
                'farm_name': fp.farm_name if fp else self.farmer.full_name,
                'bio': fp.bio if fp else None,
                'avatar_url': fp.avatar_url if fp else self.farmer.avatar_url,
                'rating_avg': float(fp.rating_avg) if fp else 0.0,
                'is_verified': fp.is_verified if fp else False,
            } if self.farmer else None,
        }


class FeaturedProduct(db.Model):
    __tablename__ = 'featured_products'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False, unique=True)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    added_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship('Product')
    adder = db.relationship('User', foreign_keys=[added_by])

    def to_dict(self):
        return {
            'id': self.id,
            'product_id': self.product_id,
            'sort_order': self.sort_order,
            'is_active': self.is_active,
            'product': self.product.to_dict() if self.product else None,
        }
