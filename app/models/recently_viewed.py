from ..extensions import db
from datetime import datetime


class RecentlyViewed(db.Model):
    __tablename__ = 'recently_viewed'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False)
    viewed_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id])
    product = db.relationship('Product', foreign_keys=[product_id])
