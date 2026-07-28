from flask import Blueprint, request, jsonify
from flask_jwt_extended import (
    jwt_required, get_jwt_identity, get_jwt,
    set_access_cookies, unset_jwt_cookies, create_access_token
)
from ..services.auth_service import register_user, login_user, update_user_profile, change_password
from ..services.notification_service import get_unread_count
from ..models import User
from ..utils.decorators import role_required

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required = ['email', 'password', 'first_name', 'last_name']
    for field in required:
        if not data.get(field, '').strip():
            return jsonify({'error': f'{field} is required'}), 400

    if len(data['password']) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    role = data.get('role', 'customer')
    if role not in ('customer', 'farmer'):
        return jsonify({'error': 'Invalid role'}), 400

    try:
        user = register_user(data, role)
    except ValueError as e:
        return jsonify({'error': str(e)}), 409

    token = create_access_token(identity=str(user.id), additional_claims={'role': user.role_name})
    resp = jsonify({'message': 'Registration successful', 'user': user.to_dict(include_private=True)})
    set_access_cookies(resp, token)
    return resp, 201


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Email and password are required'}), 400

    try:
        user, token = login_user(data['email'], data['password'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 401

    resp = jsonify({
        'message': 'Login successful',
        'user': user.to_dict(include_private=True),
    })
    set_access_cookies(resp, token)
    return resp, 200


@auth_bp.route('/logout', methods=['POST'])
def logout():
    resp = jsonify({'message': 'Logged out'})
    unset_jwt_cookies(resp)
    return resp, 200


@auth_bp.route('/me', methods=['GET'])
@jwt_required()
def me():
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    result = user.to_dict(include_private=True)
    if user.role_name == 'farmer' and user.farmer_profile:
        result['farmer_profile'] = user.farmer_profile.to_dict(include_user=False)
    result['unread_notifications'] = get_unread_count(user_id)
    return jsonify(result), 200


@auth_bp.route('/me', methods=['PUT'])
@jwt_required()
def update_me():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    try:
        user = update_user_profile(user_id, data)
        return jsonify({'message': 'Profile updated', 'user': user.to_dict(include_private=True)}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@auth_bp.route('/change-password', methods=['POST'])
@jwt_required()
def change_pwd():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    try:
        change_password(user_id, data.get('old_password', ''), data.get('new_password', ''))
        return jsonify({'message': 'Password changed successfully'}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
