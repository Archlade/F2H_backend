import os
from flask import Flask
from .config import get_config
from .extensions import db, jwt, socketio, cors, migrate, limiter, mail


def create_app():
    app = Flask(__name__)
    config = get_config()
    app.config.from_object(config)

    # Ensure upload folder exists
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'products'), exist_ok=True)
    os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'avatars'), exist_ok=True)

    # Init extensions
    db.init_app(app)
    jwt.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)
    if mail is not None:
        mail.init_app(app)
    cors.init_app(app, resources={r"/api/*": {"origins": app.config['CORS_ORIGINS']}},
                  supports_credentials=True)
    socketio.init_app(
        app,
        cors_allowed_origins=app.config['CORS_ORIGINS'],
        async_mode=app.config.get('SOCKETIO_ASYNC_MODE', 'eventlet'),
        logger=False,
        engineio_logger=False,
    )

    # Register blueprints
    from .routes.auth import auth_bp
    from .routes.products import products_bp
    from .routes.farmers import farmers_bp
    from .routes.customers import customers_bp
    from .routes.requests import requests_bp
    from .routes.chat import chat_bp
    from .routes.notifications import notifications_bp
    from .routes.devices import devices_bp
    from .routes.reviews import reviews_bp
    from .routes.favorites import favorites_bp
    from .routes.categories import categories_bp
    from .routes.locations import locations_bp
    from .routes.admin import admin_bp
    from .routes.uploads import uploads_bp
    from .routes.homepage import homepage_bp
    from .routes.banners import banners_bp
    from .routes.payments import payments_bp
    from .routes.payouts import payouts_bp
    from .routes.family_packs import family_packs_bp
    from .routes.family_pack_orders import family_pack_orders_bp
    from .routes.family_pack_subscriptions import family_pack_subscriptions_bp
    from .routes.coupons import coupons_bp

    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(products_bp, url_prefix='/api/products')
    app.register_blueprint(farmers_bp, url_prefix='/api/farmers')
    app.register_blueprint(customers_bp, url_prefix='/api/customers')
    app.register_blueprint(requests_bp, url_prefix='/api/requests')
    app.register_blueprint(chat_bp, url_prefix='/api/chats')
    app.register_blueprint(notifications_bp, url_prefix='/api/notifications')
    app.register_blueprint(devices_bp, url_prefix='/api/devices')
    app.register_blueprint(reviews_bp, url_prefix='/api/reviews')
    app.register_blueprint(favorites_bp, url_prefix='/api/favorites')
    app.register_blueprint(categories_bp, url_prefix='/api/categories')
    app.register_blueprint(locations_bp, url_prefix='/api/locations')
    app.register_blueprint(admin_bp, url_prefix='/api/admin')
    app.register_blueprint(uploads_bp, url_prefix='/api/uploads')
    app.register_blueprint(homepage_bp, url_prefix='/api/homepage')
    app.register_blueprint(banners_bp, url_prefix='/api/banners')
    app.register_blueprint(payments_bp, url_prefix='/api/payments')
    app.register_blueprint(payouts_bp, url_prefix='/api/payouts')
    app.register_blueprint(family_packs_bp, url_prefix='/api/family-packs')
    app.register_blueprint(family_pack_orders_bp, url_prefix='/api/family-pack-orders')
    app.register_blueprint(family_pack_subscriptions_bp, url_prefix='/api/family-pack-subscriptions')
    app.register_blueprint(coupons_bp, url_prefix='/api/coupons')


    # Register socket events
    from .sockets import events  # noqa

    # A token stays valid for 24h, so every request re-checks the account behind
    # it. Deactivating or soft-deleting a user now takes effect immediately.
    @jwt.user_lookup_loader
    def load_user_from_token(_jwt_header, jwt_data):
        from .models import User
        try:
            user = User.query.get(int(jwt_data['sub']))
        except (KeyError, TypeError, ValueError):
            return None
        if not user or not user.is_active or user.deleted_at:
            return None
        if _token_predates_password_change(user, jwt_data):
            return None
        return user

    def _token_predates_password_change(user, jwt_data):
        """True when this token was minted before the password last changed.

        This is what makes a password reset actually evict anyone else who is
        signed in. Without it, the one situation the reset flow exists for —
        somebody else has my account — leaves the intruder with a working
        access token for the rest of its 24 hours, and a refresh token good for
        thirty days.

        Enforced here rather than at the reset endpoint because this loader is
        the single gate every authenticated request already passes through; a
        check anywhere else would have to be remembered on each new route.
        """
        changed_at = getattr(user, 'password_changed_at', None)
        if changed_at is None:
            # Never changed, or an account that predates the column. Treated as
            # "no cutoff" so the migration signs nobody out.
            return False

        issued_at = jwt_data.get('iat')
        if issued_at is None:
            # No issued-at claim to compare against. Fail open: refusing here
            # would lock out every session on a deployment whose tokens were
            # minted without one, and the account checks above still apply.
            return False

        from datetime import datetime, timezone
        issued = datetime.fromtimestamp(issued_at, tz=timezone.utc).replace(tzinfo=None)

        # `iat` is whole seconds while password_changed_at has microseconds, so
        # a token issued in the same second as the change would otherwise be
        # rejected — which is exactly the token the reset endpoint hands back to
        # the person who just reset their own password. The second of grace
        # costs nothing: an attacker cannot mint a token in that window without
        # the new password.
        return issued < changed_at.replace(microsecond=0)

    @jwt.user_lookup_error_loader
    def user_lookup_failed(_jwt_header, _jwt_data):
        return {'error': 'Account is no longer active', 'code': 'TOKEN_INVALID'}, 401

    # Baseline security headers on every response.
    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('Permissions-Policy', 'geolocation=(self), camera=(), microphone=()')
        if not app.config.get('DEBUG'):
            response.headers.setdefault(
                'Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        return response

    # Werkzeug aborts oversized uploads before the view runs; without this the
    # client gets a bare 413 and no idea what the limit is.
    from werkzeug.exceptions import RequestEntityTooLarge

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_error):
        limit_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
        return {'error': f'That file is too large. Maximum size is {limit_mb}MB.',
                'code': 'FILE_TOO_LARGE'}, 413

    # Never leak stack traces or internal messages to clients.
    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        from werkzeug.exceptions import HTTPException
        if isinstance(error, HTTPException):
            return {'error': error.description, 'code': error.name}, error.code
        app.logger.exception('Unhandled error')
        return {'error': 'Something went wrong. Please try again.',
                'code': 'SERVER_ERROR'}, 500

    # JWT error handlers
    @jwt.expired_token_loader
    def expired_token_callback(jwt_header, jwt_data):
        return {'error': 'Token expired', 'code': 'TOKEN_EXPIRED'}, 401

    @jwt.invalid_token_loader
    def invalid_token_callback(error):
        return {'error': 'Invalid token', 'code': 'TOKEN_INVALID'}, 401

    @jwt.unauthorized_loader
    def missing_token_callback(error):
        return {'error': 'Authentication required', 'code': 'TOKEN_MISSING'}, 401

    # CSRFError subclasses NoAuthorizationError, so without this it would be
    # reported as TOKEN_MISSING and the client would log the user out.
    from flask_jwt_extended.exceptions import CSRFError

    @app.errorhandler(CSRFError)
    def csrf_error_callback(error):
        return {'error': str(error), 'code': 'CSRF_ERROR'}, 401

    # Say so at boot when password resets cannot actually be delivered.
    #
    # Without this the failure is silent and looks like a bug in the app: the
    # request succeeds, the API answers "a reset link is on its way", and the
    # link goes to the server log instead of the inbox. That is fine in
    # development and invisible in production, which is the worst combination.
    with app.app_context():
        from .services.mail_service import mail_config_problem
        problem = mail_config_problem()
        if problem:
            app.logger.warning(
                'Email is OFF — password reset links will be written to this log '
                'instead of being sent.\n  Reason: %s\n  See PASSWORD_RESET.md.',
                problem,
            )

    # No payment startup check any more: payment is cash on delivery, which
    # needs no keys and cannot be misconfigured. See PAYMENTS.md.

    # Health check
    @app.route('/api/health')
    def health():
        # `email` tells a deployment check whether password reset can actually
        # reach anyone, which is otherwise only discoverable by trying it.
        from .services.mail_service import mail_config_problem
        problem = mail_config_problem()
        return {'status': 'ok', 'service': 'F2H API',
                'email': 'configured' if problem is None else 'not configured',
                # Named rather than just flagged, so a deploy check can say what
                # is wrong instead of only that something is.
                'email_problem': problem,
                # Kept in the response, with the same key, so existing deploy
                # checks and dashboards do not start reporting a missing field
                # the day this shipped.
                'payments': 'cash_on_delivery',
                'payments_problem': None}

    # Serve uploaded files
    from flask import send_from_directory
    @app.route('/uploads/<path:filename>')
    def serve_upload(filename):
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

    return app
