from ..extensions import db
from datetime import datetime


class FarmerProfile(db.Model):
    __tablename__ = 'farmer_profiles'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    farm_name = db.Column(db.String(200), nullable=False)
    bio = db.Column(db.Text)
    farm_description = db.Column(db.Text)
    farm_size = db.Column(db.String(100))
    farming_type = db.Column(db.String(100))
    years_farming = db.Column(db.Integer, default=0)
    is_verified = db.Column(db.Boolean, default=False)
    is_suspended = db.Column(db.Boolean, default=False)
    verification_date = db.Column(db.DateTime)
    verified_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    avatar_url = db.Column(db.String(500))
    cover_image_url = db.Column(db.String(500))
    rating_avg = db.Column(db.Numeric(3, 2), default=0.00)
    rating_count = db.Column(db.Integer, default=0)
    total_sales = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id], back_populates='farmer_profile')
    verifier = db.relationship('User', foreign_keys=[verified_by])
    products = db.relationship('Product', foreign_keys='Product.farmer_id', back_populates='farmer')
    farm_location = db.relationship('Location',
                                    primaryjoin="and_(Location.user_id==FarmerProfile.user_id, Location.location_type=='farm')",
                                    foreign_keys='Location.user_id',
                                    overlaps='locations,user',
                                    uselist=False,
                                    viewonly=True)

    def to_dict(self, include_user=True):
        data = {
            'id': self.id,
            'user_id': self.user_id,
            'farm_name': self.farm_name,
            'bio': self.bio,
            'farm_description': self.farm_description,
            'farm_size': self.farm_size,
            'farming_type': self.farming_type,
            'years_farming': self.years_farming,
            'is_verified': self.is_verified,
            'is_suspended': self.is_suspended,
            'avatar_url': self.avatar_url or (self.user.avatar_url if self.user else None),
            'cover_image_url': self.cover_image_url,
            'rating_avg': float(self.rating_avg) if self.rating_avg else 0.0,
            'rating_count': self.rating_count,
            'total_sales': self.total_sales,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        if include_user and self.user:
            data['user'] = {
                'id': self.user.id,
                'full_name': self.user.full_name,
                'first_name': self.user.first_name,
                'last_name': self.user.last_name,
                'email': self.user.email,
                'phone': self.user.phone,
                'avatar_url': self.user.avatar_url,
            }
        return data
