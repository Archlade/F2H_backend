import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'dev-jwt-secret-change-in-production')
    # The website keeps its JWT in an httpOnly cookie (protected by the
    # double-submit CSRF token). Native apps have no cookie jar and no
    # same-origin to protect, so they send a Bearer header instead. Listing
    # both lets one backend serve the web app and the Flutter app: a request
    # carrying a cookie is still CSRF-checked, a header-only request is not.
    JWT_TOKEN_LOCATION = ['cookies', 'headers']
    JWT_HEADER_NAME = 'Authorization'
    JWT_HEADER_TYPE = 'Bearer'
    JWT_COOKIE_SECURE = os.environ.get('FLASK_ENV') == 'production'
    JWT_COOKIE_HTTPONLY = True
    JWT_COOKIE_SAMESITE = 'Lax'
    JWT_ACCESS_TOKEN_EXPIRES = 3600 * 24  # 24 hours
    # Mobile sessions shouldn't expire every day, so the app holds a long-lived
    # refresh token and swaps it for a fresh access token in the background.
    JWT_REFRESH_TOKEN_EXPIRES = 3600 * 24 * 30  # 30 days

    # Database
    db_type = os.environ.get('DB_TYPE', 'mysql')
    if db_type == 'sqlite':
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'f2h_dev.db')
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
    else:
        SQLALCHEMY_DATABASE_URI = (
            f"mysql+pymysql://{os.environ.get('DB_USER', 'root')}:"
            f"{os.environ.get('DB_PASSWORD', '')}@"
            f"{os.environ.get('DB_HOST', 'localhost')}:"
            f"{os.environ.get('DB_PORT', '3306')}/"
            f"{os.environ.get('DB_NAME', 'f2h_db')}?charset=utf8mb4"
        )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_recycle': 280,
        'pool_pre_ping': True,
    }

    # File uploads
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH_MB', 10)) * 1024 * 1024
    # HEIC is what iPhones shoot by default; the server converts it to JPEG.
    ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'heic', 'heif'}

    # CORS
    CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'http://localhost:5173').split(',')

    # Where password-reset links point. Defaults to the first allowed origin so
    # a dev machine needs no extra configuration.
    FRONTEND_URL = os.environ.get('FRONTEND_URL') or CORS_ORIGINS[0].strip()

    # Email — optional. Without MAIL_SERVER the app logs messages instead of
    # sending them, which keeps password reset usable in development.
    MAIL_SERVER = os.environ.get('MAIL_SERVER')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
    MAIL_USE_SSL = os.environ.get('MAIL_USE_SSL', 'false').lower() == 'true'
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    # Gmail refuses to send as an address the account doesn't own, so when no
    # explicit sender is given fall back to the authenticated mailbox with a
    # friendly display name. Flask-Mail accepts the (name, address) form.
    _sender = os.environ.get('MAIL_DEFAULT_SENDER')
    MAIL_DEFAULT_SENDER = _sender or (
        ('F2H', os.environ['MAIL_USERNAME']) if os.environ.get('MAIL_USERNAME') else None)

    # The custom scheme the mobile app registers. Reset emails carry an
    # "open in the app" link alongside the web one, because FRONTEND_URL is
    # usually a localhost address that a phone cannot reach.
    MOBILE_APP_SCHEME = os.environ.get('MOBILE_APP_SCHEME', 'f2h')

    # Socket.IO
    SOCKETIO_ASYNC_MODE = os.environ.get('SOCKETIO_ASYNC_MODE', 'eventlet')

    # Admin seed
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')


class DevelopmentConfig(Config):
    DEBUG = True
    JWT_COOKIE_SECURE = False


INSECURE_DEFAULTS = {
    'dev-secret-key-change-in-production',
    'dev-jwt-secret-change-in-production',
    'f2h-dev-secret', 'f2h-jwt-secret', 'changeme', 'secret',
}


class ProductionConfig(Config):
    DEBUG = False
    JWT_COOKIE_SECURE = True
    JWT_COOKIE_SAMESITE = 'Strict'

    def __init__(self):
        # Refuse to boot with a guessable signing key — anyone who knows it can
        # mint a valid admin token.
        for name in ('SECRET_KEY', 'JWT_SECRET_KEY'):
            value = getattr(self, name, '')
            if value in INSECURE_DEFAULTS or len(value) < 32:
                raise RuntimeError(
                    f'{name} must be set to a unique random value of at least 32 '
                    'characters before running in production. '
                    'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
                )


config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
}


def get_config():
    env = os.environ.get('FLASK_ENV', 'development')
    config_class = config_map.get(env, DevelopmentConfig)
    # Instantiating runs ProductionConfig's secret checks; from_object accepts
    # either a class or an instance.
    return config_class() if env == 'production' else config_class
