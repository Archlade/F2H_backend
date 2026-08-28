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
            # What was reviewed, by name.
            #
            # Only the ids were sent, so every client showing "my reviews" had
            # nothing to put in the heading — the website rendered a blank line
            # where the product should be. A review nobody can tell apart from
            # the next one is not much of a record.
            #
            # One of the two is always null: a review is of a product or of a
            # farm, never both.
            'product': {
                'id': self.product.id,
                'name': self.product.name,
                'unit': self.product.unit,
                'primary_image': (self.product.primary_image.image_url
                                  if self.product.primary_image else None),
            } if self.product else None,
            'farmer': {
                'id': self.farmer_user.id,
                'name': (self.farmer_user.farmer_profile.farm_name
                         if self.farmer_user.farmer_profile
                         else self.farmer_user.full_name),
            } if self.farmer_user else None,
        }
