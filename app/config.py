import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'dev-jwt-secret-change-in-production')
    JWT_TOKEN_LOCATION = ['cookies']
    JWT_COOKIE_SECURE = os.environ.get('FLASK_ENV') == 'production'
    JWT_COOKIE_HTTPONLY = True
    JWT_COOKIE_SAMESITE = 'Lax'
    JWT_ACCESS_TOKEN_EXPIRES = 3600 * 24  # 24 hours

    # MySQL
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
    ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}

    # CORS
    CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'http://localhost:5173').split(',')

    # Socket.IO
    SOCKETIO_ASYNC_MODE = os.environ.get('SOCKETIO_ASYNC_MODE', 'eventlet')

    # Admin seed
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')


class DevelopmentConfig(Config):
    DEBUG = True
    JWT_COOKIE_SECURE = False


class ProductionConfig(Config):
    DEBUG = False
    JWT_COOKIE_SECURE = True


config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
}


def get_config():
    env = os.environ.get('FLASK_ENV', 'development')
    return config_map.get(env, DevelopmentConfig)
