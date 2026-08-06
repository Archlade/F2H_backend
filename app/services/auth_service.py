import bcrypt
import math
from datetime import datetime
from flask_jwt_extended import create_access_token
from ..extensions import db
from ..models import User, Role, FarmerProfile, PasswordResetToken
from ..models.password_reset import hash_token
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


def become_farmer(user_id: int, data: dict):
    """Turn an existing customer account into a farmer account.

    Upgrading in place rather than making them register again keeps their order
    history, addresses, favourites and chats intact — a customer who starts
    selling is the same person, not a second account.

    The role is re-read from the database on every request (see
    `current_user_role`), so the change takes effect immediately even though
    any already-issued JWT still carries the old role claim.
    """
    user = User.query.get(user_id)
    if not user:
        raise ValueError('Account not found')

    if user.role_name == 'farmer':
        raise ValueError('This account already sells on F2H')
    if user.role_name == 'admin':
        # An admin trading produce would muddle moderation with selling.
        raise ValueError('Admin accounts cannot be converted to farm accounts')

    farm_name = (data.get('farm_name') or '').strip()
    if not farm_name:
        raise ValueError('Farm name is required')

    farmer_role = Role.query.filter_by(name='farmer').first()
    if not farmer_role:
        raise ValueError('The farmer role is not configured')

    # A profile can already exist if an earlier upgrade half-completed.
    profile = FarmerProfile.query.filter_by(user_id=user.id).first()
    if profile is None:
        profile = FarmerProfile(user_id=user.id, farm_name=farm_name)
        db.session.add(profile)
    else:
        profile.farm_name = farm_name

    profile.bio = (data.get('bio') or '').strip()
    profile.farm_description = (data.get('farm_description') or '').strip()
    profile.farming_type = (data.get('farming_type') or '').strip()

    user.role_id = farmer_role.id
    db.session.commit()
    return user


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
    # Signs every other session out — see _invalidate_sessions. The route
    # re-issues a cookie for the caller straight afterwards, so the person who
    # changed their own password stays where they are.
    _invalidate_sessions(user)
    db.session.commit()
    return user


def _invalidate_sessions(user, drop_devices=True):
    """Cut off every token and device already attached to this account.

    Called whenever the password changes, which is the moment someone is
    saying "I no longer trust who has access". Two things have to go:

    * **Tokens.** `password_changed_at` is checked by the JWT loader on every
      request, so an access token minted before now stops working immediately
      and the 30-day refresh token cannot be exchanged either.
    * **Push registrations.** Otherwise the intruder's phone keeps receiving
      notifications about the victim's orders — the account is locked but the
      leak continues, which is worse for being invisible.

    Not committed here; the caller owns the transaction so the new password and
    the eviction land together or not at all.
    """
    user.password_changed_at = datetime.utcnow()

    if drop_devices:
        from ..models import DeviceToken
        DeviceToken.query.filter_by(user_id=user.id).delete(synchronize_session=False)


# ─── Password reset ──────────────────────────────────────────────────────────

def create_password_reset(email: str, ip_address: str = None):
    """Issue a reset token for the address, or return (None, None) if no active
    account matches. Callers must respond identically either way — a differing
    response turns this endpoint into an account-enumeration oracle."""
    user = User.query.filter_by(email=(email or '').lower().strip(),
                                deleted_at=None).first()
    if not user or not user.is_active:
        return None, None

    # One live token per account: requesting a new link invalidates the old one.
    PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).update(
        {'used_at': datetime.utcnow()})

    row, raw_token = PasswordResetToken.issue(user.id, ip_address)
    db.session.add(row)
    db.session.commit()
    return user, raw_token


def get_valid_reset_token(raw_token: str):
    """Return the token row if it exists, is unused and hasn't expired."""
    if not raw_token:
        return None
    row = PasswordResetToken.query.filter_by(token_hash=hash_token(raw_token)).first()
    if not row or not row.is_usable:
        return None
    user = User.query.filter_by(id=row.user_id, deleted_at=None).first()
    if not user or not user.is_active:
        return None
    return row


def reset_password_with_token(raw_token: str, new_password: str):
    row = get_valid_reset_token(raw_token)
    if not row:
        raise ValueError('This reset link is invalid or has expired. Please request a new one.')

    user = User.query.get(row.user_id)
    user.password_hash = hash_password(new_password)
    row.used_at = datetime.utcnow()

    # The reason this endpoint exists is that somebody may have lost control of
    # the account, so every session and every registered phone goes with the
    # old password. The caller is signed back in with a fresh token immediately
    # afterwards, and their device re-registers on the next launch.
    _invalidate_sessions(user)

    # Burn every other outstanding token for this account — if someone else
    # requested a reset too, their link should stop working now.
    PasswordResetToken.query.filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
    ).update({'used_at': datetime.utcnow()})

    db.session.commit()
    return user
