from ..extensions import db
from datetime import datetime


class Review(db.Model):
    __tablename__ = 'reviews'

    id = db.Column(db.Integer, primary_key=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'))
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'))
    request_id = db.Column(db.Integer, db.ForeignKey('purchase_requests.id', ondelete='SET NULL'))
    rating = db.Column(db.SmallInteger, nullable=False)
    title = db.Column(db.String(255))
    content = db.Column(db.Text)
    is_approved = db.Column(db.Boolean, default=True)
    is_flagged = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    reviewer = db.relationship('User', foreign_keys=[reviewer_id], back_populates='reviews_given')
    product = db.relationship('Product', foreign_keys=[product_id])
    farmer_user = db.relationship('User', foreign_keys=[farmer_id])

    def to_dict(self):
        return {
            'id': self.id,
            'reviewer_id': self.reviewer_id,
            'product_id': self.product_id,
            'farmer_id': self.farmer_id,
            'request_id': self.request_id,
            'rating': self.rating,
            'title': self.title,
            'content': self.content,
            'is_approved': self.is_approved,
            'is_flagged': self.is_flagged,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'reviewer': {
                'id': self.reviewer.id,
                'full_name': self.reviewer.full_name,
                'avatar_url': self.reviewer.avatar_url,
            } if self.reviewer else None,
        }
