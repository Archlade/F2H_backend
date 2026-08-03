from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_socketio import SocketIO
from flask_cors import CORS
from flask_migrate import Migrate
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Email is optional — the app still runs (logging messages instead of sending
# them) when Flask-Mail isn't installed.
try:
    from flask_mail import Mail
    mail = Mail()
except ImportError:  # pragma: no cover - depends on the deployment image
    mail = None

db = SQLAlchemy()
jwt = JWTManager()
socketio = SocketIO()
cors = CORS()
migrate = Migrate()
limiter = Limiter(key_func=get_remote_address, default_limits=["200 per minute"])
