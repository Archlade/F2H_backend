import bcrypt
import math
from datetime import datetime
from flask_jwt_extended import create_access_token
from ..extensions import db
from ..models import User, Role, FarmerProfile
from ..utils.helpers import log_audit


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(12)).decode('utf-8')


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))


def register_user(data: dict, role_name: str = 'customer'):
    role = Role.query.filter_by(name=role_name).first()
    if not role:
        raise ValueError(f'Role {role_name} not found')

    if User.query.filter_by(email=data['email'].lower()).first():
        raise ValueError('Email already registered')

    user = User(
        role_id=role.id,
        email=data['email'].lower().strip(),
        password_hash=hash_password(data['password']),
        first_name=data['first_name'].strip(),
        last_name=data['last_name'].strip(),
        phone=data.get('phone', '').strip() or None,
    )
    db.session.add(user)
    db.session.flush()  # Get user.id

    if role_name == 'farmer':
        fp = FarmerProfile(
            user_id=user.id,
            farm_name=data.get('farm_name', f"{user.first_name}'s Farm").strip(),
            bio=data.get('bio', ''),
            farm_description=data.get('farm_description', ''),
            farming_type=data.get('farming_type', ''),
        )
        db.session.add(fp)

    db.session.commit()
    return user


def login_user(email: str, password: str):
    user = User.query.filter_by(email=email.lower().strip(), deleted_at=None).first()
    if not user or not verify_password(password, user.password_hash):
        raise ValueError('Invalid email or password')
    if not user.is_active:
        raise ValueError('Account is deactivated. Please contact support.')
    user.last_login_at = datetime.utcnow()
    db.session.commit()
    token = create_access_token(identity=str(user.id),
                                 additional_claims={'role': user.role_name})
    return user, token


def get_user_by_id(user_id: int):
    return User.query.get(user_id)


def update_user_profile(user_id: int, data: dict):
    user = User.query.get_or_404(user_id)
    allowed = ['first_name', 'last_name', 'phone', 'avatar_url']
    for field in allowed:
        if field in data:
            setattr(user, field, data[field])
    db.session.commit()
    return user


def change_password(user_id: int, old_password: str, new_password: str):
    user = User.query.get_or_404(user_id)
    if not verify_password(old_password, user.password_hash):
        raise ValueError('Current password is incorrect')
    user.password_hash = hash_password(new_password)
    db.session.commit()
    return user
