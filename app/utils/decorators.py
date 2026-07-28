from functools import wraps
from flask import jsonify
from flask_jwt_extended import verify_jwt_in_request, get_jwt


def role_required(*roles):
    """Decorator that requires the authenticated user to have one of the given roles."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            verify_jwt_in_request()
            claims = get_jwt()
            user_role = claims.get('role')
            if user_role not in roles:
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
