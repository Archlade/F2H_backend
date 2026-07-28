from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_socketio import SocketIO
from flask_cors import CORS
from flask_migrate import Migrate
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
jwt = JWTManager()
socketio = SocketIO()
cors = CORS()
migrate = Migrate()
limiter = Limiter(key_func=get_remote_address, default_limits=["200 per minute"])
