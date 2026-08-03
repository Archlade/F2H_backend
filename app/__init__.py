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
    from .routes.reviews import reviews_bp
    from .routes.favorites import favorites_bp
    from .routes.categories import categories_bp
    from .routes.locations import locations_bp
    from .routes.admin import admin_bp
    from .routes.uploads import uploads_bp
    from .routes.homepage import homepage_bp
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
    app.register_blueprint(reviews_bp, url_prefix='/api/reviews')
    app.register_blueprint(favorites_bp, url_prefix='/api/favorites')
    app.register_blueprint(categories_bp, url_prefix='/api/categories')
    app.register_blueprint(locations_bp, url_prefix='/api/locations')
    app.register_blueprint(admin_bp, url_prefix='/api/admin')
    app.register_blueprint(uploads_bp, url_prefix='/api/uploads')
    app.register_blueprint(homepage_bp, url_prefix='/api/homepage')
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
        return user

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

    # Health check
    @app.route('/api/health')
    def health():
        return {'status': 'ok', 'service': 'F2H API'}

    # Serve uploaded files
    from flask import send_from_directory
    @app.route('/uploads/<path:filename>')
    def serve_upload(filename):
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

    return app
