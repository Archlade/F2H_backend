from ..extensions import db
from datetime import datetime


class Role(db.Model):
    __tablename__ = 'roles'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    users = db.relationship('User', back_populates='role')

    def to_dict(self):
        return {'id': self.id, 'name': self.name}


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    role_id = db.Column(db.Integer, db.ForeignKey('roles.id'), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(30))
    avatar_url = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True)
    is_verified = db.Column(db.Boolean, default=False)
    email_verified_at = db.Column(db.DateTime)
    last_login_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = db.Column(db.DateTime)

    role = db.relationship('Role', back_populates='users')
    farmer_profile = db.relationship('FarmerProfile', back_populates='user',
                                     foreign_keys='FarmerProfile.user_id', uselist=False)
    locations = db.relationship('Location', foreign_keys='Location.user_id', back_populates='user')
    addresses = db.relationship('Address', foreign_keys='Address.user_id', back_populates='user')
    notifications = db.relationship('Notification', foreign_keys='Notification.recipient_id',
                                    back_populates='recipient')
    sent_messages = db.relationship('Message', foreign_keys='Message.sender_id', back_populates='sender')
    favorites = db.relationship('Favorite', foreign_keys='Favorite.user_id', back_populates='user')
    reviews_given = db.relationship('Review', foreign_keys='Review.reviewer_id', back_populates='reviewer')

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def role_name(self):
        return self.role.name if self.role else None

    def to_dict(self, include_private=False):
        data = {
            'id': self.id,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'full_name': self.full_name,
            'avatar_url': self.avatar_url,
            'role': self.role_name,
            'is_active': self.is_active,
            'is_verified': self.is_verified,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        if include_private:
            data.update({
                'email': self.email,
                'phone': self.phone,
                'last_login_at': self.last_login_at.isoformat() if self.last_login_at else None,
                'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            })
        return data
