from datetime import datetime
from urllib.parse import quote

from flask import Blueprint, current_app, request, jsonify
from flask_jwt_extended import (
    jwt_required, get_jwt_identity, get_jwt,
    set_access_cookies, set_refresh_cookies, unset_jwt_cookies,
    create_access_token, create_refresh_token
)
from ..services.auth_service import (
    register_user, login_user, update_user_profile, change_password,
    create_password_reset, get_valid_reset_token, reset_password_with_token,
)
from ..services.mail_service import send_password_reset_email
from ..services.notification_service import get_unread_count
from ..models import User
from ..utils.decorators import role_required
from ..utils.validators import (email_problem, normalise_phone, password_problem,
                               phone_problem)
from ..extensions import db, limiter

auth_bp = Blueprint('auth', __name__)


def _is_native_client():
    """True when the caller is the Flutter app rather than the website.

    Native clients can't use the httpOnly cookie the browser flow relies on, so
    they identify themselves and get the raw tokens in the response body. The
    header is a routing hint only — it grants nothing on its own.
    """
    return (request.headers.get('X-Client-Type', '').lower() == 'mobile'
            or (request.get_json(silent=True) or {}).get('client') == 'mobile')


def _auth_response(user, payload, status=200):
    """One login/register/reset reply that serves both clients.

    Both clients get a refresh token; only the app gets it in the body, because
    only the app has somewhere to put it. The browser gets it as an httpOnly
    cookie it cannot read, which is the point.

    This used to issue the browser an access cookie and nothing else. Access
    tokens last 24 hours and refresh tokens 30 days, so the app stayed signed in
    for a month while the website silently signed people out once a day, with no
    way to renew — there was no refresh cookie to renew *with*. Anyone using both
    experienced that as the website being broken.
    """
    access = create_access_token(identity=str(user.id),
                                 additional_claims={'role': user.role_name})
    refresh = create_refresh_token(identity=str(user.id),
                                   additional_claims={'role': user.role_name})
    if _is_native_client():
        payload = dict(payload)
        payload['access_token'] = access
        payload['refresh_token'] = refresh
        payload['token_type'] = 'Bearer'
        payload['expires_in'] = current_app.config['JWT_ACCESS_TOKEN_EXPIRES']
    resp = jsonify(payload)
    set_access_cookies(resp, access)
    # Scoped by flask-jwt-extended to the refresh endpoint's path and paired
    # with its own CSRF cookie, so it is not sent on ordinary API calls.
    set_refresh_cookies(resp, refresh)
    return resp, status


@auth_bp.route('/refresh', methods=['POST'])
@jwt_required(refresh=True)
def refresh():
    """Swap a refresh token for a new access token (native clients).

    The user is re-read from the database, so a deactivated or deleted account
    can't refresh its way into a fresh 24-hour session.
    """
    user = User.query.get(int(get_jwt_identity()))
    if not user or not user.is_active or user.deleted_at:
        return jsonify({'error': 'Account is no longer active',
                        'code': 'TOKEN_INVALID'}), 401

    # A refresh token lives for thirty days, so this is the one that matters
    # most after a password reset: without this check an intruder who grabbed a
    # refresh token could keep minting fresh 24-hour access tokens for a month
    # after the owner locked them out.
    #
    # The JWT loader applies the same rule to every request, but it is repeated
    # here deliberately — this route already re-reads the user for exactly this
    # class of check, and the two lines are cheaper than assuming.
    changed_at = user.password_changed_at
    issued_at = get_jwt().get('iat')
    if changed_at and issued_at:
        from datetime import timezone
        issued = datetime.fromtimestamp(issued_at, tz=timezone.utc).replace(tzinfo=None)
        if issued < changed_at.replace(microsecond=0):
            return jsonify({'error': 'Your password was changed. Please sign in again.',
                            'code': 'TOKEN_INVALID'}), 401

    access = create_access_token(identity=str(user.id),
                                 additional_claims={'role': user.role_name})
    resp = jsonify({
        'access_token': access,
        'token_type': 'Bearer',
        'expires_in': current_app.config['JWT_ACCESS_TOKEN_EXPIRES'],
        'user': user.to_dict(include_private=True),
    })
    set_access_cookies(resp, access)
    return resp, 200


@auth_bp.route('/register', methods=['POST'])
@limiter.limit('10 per hour')
def register():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Phone is required: it is how a farmer reaches a customer when a delivery
    # is on its way, and an account without one is a dead end at handover.
    required = {
        'email': 'Email',
        'password': 'Password',
        'first_name': 'First name',
        'last_name': 'Last name',
        'phone': 'Phone number',
    }
    for field, label in required.items():
        if not str(data.get(field) or '').strip():
            return jsonify({'error': f'{label} is required'}), 400

    # Checked before the password so a mistyped address is reported first —
    # it is the field the account is recovered through, and until now it was
    # only checked for presence. 'asdf' was a valid signup.
    problem = email_problem(data.get('email'))
    if problem:
        return jsonify({'error': problem}), 400

    problem = password_problem(data['password'])
    if problem:
        return jsonify({'error': problem}), 400

    problem = phone_problem(data.get('phone'))
    if problem:
        return jsonify({'error': problem}), 400

    # Stored as the bare ten digits, whatever the person typed. '+91 98765
    # 43210' and '9876543210' are one number, and keeping them as written means
    # the same contact looks like two — and a `tel:` link built from one of them
    # may not dial. Safe to normalise unconditionally: `phone_problem` above has
    # already established there are exactly ten digits here.
    data['phone'] = normalise_phone(data.get('phone'))

    role = data.get('role', 'customer')
    if role not in ('customer', 'farmer'):
        return jsonify({'error': 'Invalid role'}), 400

    try:
        user = register_user(data, role)
    except ValueError as e:
        return jsonify({'error': str(e)}), 409

    return _auth_response(user, {'message': 'Registration successful',
                                 'user': user.to_dict(include_private=True)}, 201)


@auth_bp.route('/login', methods=['POST'])
# Password guessing is otherwise limited only by the global 200/min default,
# which allows roughly 288,000 attempts a day from a single address.
@limiter.limit('10 per 15 minutes; 50 per day')
def login():
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Email and password are required'}), 400

    try:
        user, _token = login_user(data['email'], data['password'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 401

    return _auth_response(user, {'message': 'Login successful',
                                 'user': user.to_dict(include_private=True)}, 200)


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


@auth_bp.route('/become-farmer', methods=['POST'])
@jwt_required()
@limiter.limit('5 per hour')
def become_farmer_route():
    """Upgrade the signed-in customer to a farmer account.

    Issues a fresh token so the JWT's role claim matches the database, then
    returns the full user record the client needs to re-render as a farmer.
    """
    from ..services.auth_service import become_farmer

    user_id = int(get_jwt_identity())
    try:
        user = become_farmer(user_id, request.get_json() or {})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    result = user.to_dict(include_private=True)
    if user.farmer_profile:
        result['farmer_profile'] = user.farmer_profile.to_dict(include_user=False)

    return _auth_response(user, {
        'message': 'Your farm account is ready',
        'user': result,
    }, 200)


@auth_bp.route('/me', methods=['PUT'])
@jwt_required()
def update_me():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    try:
        user = update_user_profile(user_id, data)
        return jsonify({'message': 'Profile updated', 'user': user.to_dict(include_private=True)}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


def _reset_url(raw_token):
    base = (current_app.config.get('FRONTEND_URL') or '').rstrip('/')
    return f"{base}/reset-password?token={quote(raw_token)}"


def _reset_deep_link(raw_token):
    """The same reset, addressed to the installed app.

    FRONTEND_URL is nearly always http://localhost:5173, which a phone cannot
    open. The app registers the f2h:// scheme, so this link reaches the reset
    screen from a phone regardless of what the web address is set to.
    """
    scheme = current_app.config.get('MOBILE_APP_SCHEME') or 'f2h'
    return f"{scheme}://reset-password?token={quote(raw_token)}"


@auth_bp.route('/forgot-password', methods=['POST'])
# Tight: this endpoint sends mail and reveals nothing, so the only reason to
# call it repeatedly is to spam an inbox.
@limiter.limit('5 per hour; 20 per day')
def forgot_password():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip()
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    # Always the same reply, whether or not the address has an account —
    # otherwise this endpoint tells an attacker who is registered.
    generic = {'message': 'If an account exists for that email, a reset link is on its way.'}

    user, raw_token = create_password_reset(email, request.remote_addr)
    if not user:
        return jsonify(generic), 200

    url = _reset_url(raw_token)
    app_url = _reset_deep_link(raw_token)
    delivered = send_password_reset_email(user, url, app_url)
    if not delivered:
        # SMTP isn't configured; mail_service logged the message including the
        # link so the flow still works during development.
        current_app.logger.warning(
            'Password reset for %s\n  web: %s\n  app: %s', user.email, url, app_url)
    return jsonify(generic), 200


@auth_bp.route('/reset-password/verify', methods=['GET'])
@limiter.limit('30 per hour')
def verify_reset_token():
    """Lets the reset page show 'this link has expired' before the user types
    out a new password."""
    row = get_valid_reset_token(request.args.get('token', ''))
    if not row:
        return jsonify({'valid': False,
                        'error': 'This reset link is invalid or has expired.'}), 400
    return jsonify({'valid': True, 'email': row.user.email}), 200


@auth_bp.route('/reset-password', methods=['POST'])
@limiter.limit('10 per hour')
def reset_password():
    data = request.get_json() or {}
    token = data.get('token', '')
    new_password = data.get('new_password') or data.get('password') or ''

    if not token:
        return jsonify({'error': 'Reset token is missing'}), 400

    problem = password_problem(new_password)
    if problem:
        return jsonify({'error': problem}), 400

    try:
        user = reset_password_with_token(token, new_password)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # Sign the user straight in — they just proved control of the inbox.
    return _auth_response(user, {'message': 'Password reset successfully',
                                 'user': user.to_dict(include_private=True)}, 200)


@auth_bp.route('/delete-account', methods=['GET'])
@jwt_required()
def delete_account_status():
    """Whether this account can be deleted, and what is in the way.

    Asked before the form is shown so somebody mid-delivery is told up front
    rather than after typing their password and pressing a red button.
    """
    from ..services.account_deletion import deletion_blockers

    user = User.query.get(int(get_jwt_identity()))
    if not user or user.deleted_at:
        return jsonify({'error': 'Account not found'}), 404

    blocker = deletion_blockers(user)
    return jsonify({'can_delete': blocker is None, 'blocker': blocker}), 200


@auth_bp.route('/delete-account', methods=['POST'])
@jwt_required()
@limiter.limit('5 per hour')
def delete_account_route():
    """Delete the signed-in account.

    Requires the password again. This is irreversible and one tap from a
    settings screen, so re-authenticating is the difference between a decision
    and an accident — and it stops a borrowed unlocked phone from wiping
    somebody's farm.
    """
    from ..services.account_deletion import DeletionRefused, delete_account
    from ..services.auth_service import verify_password

    data = request.get_json(silent=True) or {}
    user = User.query.get(int(get_jwt_identity()))
    if not user or user.deleted_at:
        return jsonify({'error': 'Account not found'}), 404

    password = data.get('password') or ''
    if not password or not verify_password(password, user.password_hash):
        # Deliberately not "wrong password" — same wording whatever the reason,
        # so this endpoint cannot be used to probe a password.
        return jsonify({'error': 'That password is not correct'}), 403

    try:
        delete_account(user, reason=data.get('reason'))
    except DeletionRefused as e:
        return jsonify({'error': str(e)}), 409

    db.session.commit()

    resp = jsonify({'message': 'Your account has been deleted.'})
    unset_jwt_cookies(resp)
    return resp, 200


@auth_bp.route('/change-password', methods=['POST'])
@jwt_required()
@limiter.limit('5 per 15 minutes')
def change_pwd():
    user_id = int(get_jwt_identity())
    data = request.get_json() or {}
    new_password = data.get('new_password', '')

    problem = password_problem(new_password)
    if problem:
        return jsonify({'error': problem}), 400

    try:
        current = data.get('old_password') or data.get('current_password') or ''
        change_password(user_id, current, new_password)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # Re-issue the cookie so the session that just changed the password stays
    # valid and any stale one is replaced.
    resp = jsonify({'message': 'Password changed successfully'})
    user = User.query.get(user_id)
    set_access_cookies(resp, create_access_token(identity=str(user_id),
                                                 additional_claims={'role': user.role_name}))
    return resp, 200
