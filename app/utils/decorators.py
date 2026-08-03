from functools import wraps

from flask import jsonify
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity


def current_user_role():
    """The caller's role as it is *right now*, not as it was when they logged in.

    The JWT carries a role claim, but tokens live for 24 hours. Reading the role
    from the database means a demotion, suspension or deactivation takes effect
    immediately instead of whenever the token happens to expire.
    """
    from ..models import User
    identity = get_jwt_identity()
    if identity is None:
        return None, None
    user = User.query.get(int(identity))
    if not user or not user.is_active or user.deleted_at:
        return None, None
    return user, user.role_name


def role_required(*roles):
    """Require the authenticated user to currently hold one of the given roles."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            verify_jwt_in_request()
            user, role = current_user_role()
            if user is None:
                return jsonify({'error': 'Account is no longer active',
                                'code': 'TOKEN_INVALID'}), 401
            if role not in roles:
                return jsonify({'error': 'Insufficient permissions', 'code': 'FORBIDDEN'}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def admin_required(fn):
    return role_required('admin')(fn)


def farmer_required(fn):
    return role_required('farmer', 'admin')(fn)


def customer_required(fn):
    return role_required('customer', 'admin')(fn)
