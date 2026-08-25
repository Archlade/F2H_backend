import re
import os
from dotenv import load_dotenv

# Which env file to read, overridable by a *real* environment variable.
#
# `.env` is committed and holds the development settings, so every `git pull` on
# the server used to overwrite the production config with a laptop's — wrong
# database user, a macOS path for the Firebase key, localhost CORS. That is not
# a hypothetical: it took the API down with `Access denied for user
# 'root'@'localhost'` and killed push in the same pull.
#
# So the server sets F2H_ENV_FILE in its systemd unit and reads
# `.env.production` instead. Neither file overwrites the other, and a deploy is
# once again just `git pull`.
#
#   [Service]
#   Environment=F2H_ENV_FILE=/srv/webapps/farmapp/.env.production
#
# Note this must come from the process environment, not from an env file —
# a setting that says which file to read cannot live inside the file it names.
_ENV_FILE = os.environ.get('F2H_ENV_FILE')
if _ENV_FILE:
    load_dotenv(_ENV_FILE, override=True)
else:
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
    # Which domain the auth cookies belong to.
    #
    # Unset means host-only: a cookie set by api.f2hmarket.com is readable only
    # by scripts on api.f2hmarket.com. That breaks the website, because the
    # double-submit CSRF scheme requires the *page* to read `csrf_access_token`
    # and echo it in a header — and the page is on f2hmarket.com. The cookie is
    # still sent (same-site), so browsing and signing in work; every order,
    # cart change and profile update fails a CSRF check instead.
    #
    # Set to the parent domain in production (`.f2hmarket.com`) so both hosts
    # share the cookies. Left unset in development, where Vite proxies the API
    # through localhost:5173 and everything is genuinely same-origin — which is
    # exactly why this never shows up until the site is deployed.
    JWT_COOKIE_DOMAIN = os.environ.get('JWT_COOKIE_DOMAIN') or None
    JWT_ACCESS_TOKEN_EXPIRES = 3600 * 24  # 24 hours
    # Sessions shouldn't expire every day, so both clients hold a long-lived
    # refresh token and swap it for a fresh access token in the background. The
    # app keeps it in secure storage; the browser gets it as an httpOnly cookie.
    JWT_REFRESH_TOKEN_EXPIRES = 3600 * 24 * 30  # 30 days
    # Send the refresh cookie to the one endpoint that consumes it, instead of
    # attaching a 30-day credential to every request the browser makes. The
    # default is '/', which puts it on image loads and polling alike — far more
    # exposure than it needs, for a token that is far more valuable than the
    # 24-hour access token beside it.
    #
    # This must match the mounted URL: the auth blueprint is registered at
    # /api/auth, so the endpoint is /api/auth/refresh. Get this wrong and the
    # cookie is never sent, refresh always 401s, and the symptom is the exact
    # thing it was added to fix.
    JWT_REFRESH_COOKIE_PATH = '/api/auth/refresh'

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

    # Payment is cash on delivery. There is nothing to configure — no keys, no
    # secrets, no third party — which is why there is no PAYMENTS_ENABLED flag
    # here: a switch whose only setting is "on" is a switch someone will
    # eventually turn off by accident.

    # What F2H keeps from each order, as a percentage of what the customer
    # actually paid. Snapshotted onto every payment row when it is created, so
    # changing this never rewrites what past orders owed.
    PLATFORM_COMMISSION_RATE = float(os.environ.get('PLATFORM_COMMISSION_RATE', 20))
    # A redemption below this is not worth a bank transfer's effort.
    MIN_PAYOUT_AMOUNT = float(os.environ.get('MIN_PAYOUT_AMOUNT', 200))

    # The smallest order F2H will take, in rupees.
    #
    # Applies to a single purchase and to a cart total alike. Under cash on
    # delivery every order is a physical trip with someone collecting money at
    # the end of it, and below this the trip costs more than the order is worth.
    #
    # Checked against the payable total *after* any coupon, so a discount code
    # cannot be used to slip under the floor.
    #
    # This is the *default*, not the live figure. An admin sets the real one
    # from the admin page and it is stored in `platform_settings`; this value
    # applies until they do, and is what the code falls back to if that table is
    # unreadable. Nothing enforces the floor from here directly — every check
    # goes through `min_order_value()` in app/models/settings.py.
    MIN_ORDER_VALUE = float(os.environ.get('MIN_ORDER_VALUE', 300))

    # Flat delivery fee, charged once per checkout and never on a pickup order.
    #
    # Defaults to 0 — the feature is off until an admin sets a figure on the
    # admin page. A deploy that silently started adding money to every bill
    # would be a worse bug than the feature is a feature, so the default has to
    # be the harmless one. Same arrangement as MIN_ORDER_VALUE above: this is
    # the fallback, `delivery_charge()` in app/models/settings.py is the figure
    # everything actually reads.
    DELIVERY_CHARGE = float(os.environ.get('DELIVERY_CHARGE', 0))

    # Push notifications — optional, like email. Without a credential the app
    # still creates every notification row and still delivers over Socket.IO;
    # only the push to a backgrounded phone is missing.
    #
    # This is the *service account* key (Firebase Console → Project settings →
    # Service accounts → Generate new private key), not the app's
    # google-services.json. That one holds public client identifiers; this one
    # can send a notification to every user you have, so keep it out of the
    # repository. Falls back to GOOGLE_APPLICATION_CREDENTIALS / the metadata
    # server when unset, which is what a Cloud Run deployment already has.
    FIREBASE_CREDENTIALS = os.environ.get('FIREBASE_CREDENTIALS')

    # ── Farmer & stock report, published to Google Drive ──────────────────────
    #
    # The id of the Drive folder the every-2-days report is written into. It is
    # the long string in the folder's URL:
    #
    #   https://drive.google.com/drive/folders/1AbC...XyZ
    #                                          ^^^^^^^^^^
    #
    # That folder must be shared with the service account's email as an Editor.
    # A service account has no Drive anyone can browse, so without a shared
    # parent the upload succeeds into storage with no UI — the file exists and
    # nobody can find it.
    #
    # Unset means the publish job is off, not broken: it logs why and returns
    # cleanly rather than failing the whole cron run.
    DRIVE_FOLDER_ID = os.environ.get('DRIVE_FOLDER_ID')

    # Reuses the Firebase key by default — same Google project, and one
    # credential to rotate rather than two. Point this elsewhere only if you
    # want the report uploaded by a different identity.
    DRIVE_CREDENTIALS = (os.environ.get('DRIVE_CREDENTIALS')
                         or os.environ.get('FIREBASE_CREDENTIALS'))

    # Admin seed
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL')
    # Weekly baskets are built from the whole catalogue and sold by F2H rather
    # than by a farm, but an order still needs a seller. This nominates the
    # admin account that acts as it — its name is what customers see on a
    # basket delivery. Falls back to ADMIN_EMAIL; if neither is set, basket
    # generation stops with an explanatory error rather than picking someone.
    PLATFORM_SELLER_EMAIL = os.environ.get('PLATFORM_SELLER_EMAIL')

    # Shared secret for /api/cron/run, which the VPS crontab calls to send
    # basket reminders and generate weekly deliveries. Unset disables the
    # endpoint entirely — a missing setting must not leave a public trigger
    # open, so absence means off rather than unguarded.
    CRON_TOKEN = os.environ.get('CRON_TOKEN')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')


class DevelopmentConfig(Config):
    DEBUG = True
    JWT_COOKIE_SECURE = False


INSECURE_DEFAULTS = {
    'dev-secret-key-change-in-production',
    'dev-jwt-secret-change-in-production',
    'f2h-dev-secret', 'f2h-jwt-secret', 'changeme', 'secret',
}

# Substrings that mean "this is a placeholder", whatever else is around them.
#
# Exact matching was not enough. The key actually in this project's .env is
# `f2h-dev-secret-key-2024-change-in-production` — 44 characters, so it passed
# the length test, and not character-for-character on the list above, so it
# passed that too. A key that is long enough to look strong while literally
# containing the words "change in production" is the exact case the check exists
# for, and it sailed through.
#
# Matched as whole words, not bare substrings.
#
# A naive `marker in value` test looked safe and was not: a random 64-character
# token contains a stray "todo" or "example" often enough to matter — measured
# at roughly 1 in 14,000, which is a server refusing to boot with a baffling
# complaint about a key that is perfectly good. Requiring the marker to be
# bounded by a separator or the ends of the string keeps every real placeholder
# (`change-in-production`, `dev-secret`, `TODO-set-this`) while making an
# accidental match essentially impossible, because a placeholder is written by a
# human and humans use separators.
PLACEHOLDER_MARKERS = (
    'change-in-production', 'change_in_production', 'changeinproduction',
    'change-me', 'changeme', 'dev-secret', 'dev_secret', 'jwt-secret-key',
    'your-secret', 'placeholder', 'example', 'insecure', 'todo',
)

# A marker counts only when it stands alone: at the start or end of the value,
# or fenced by something that is not a letter or a digit.
_MARKER_RES = tuple(
    (marker, re.compile(rf'(?:^|[^a-z0-9]){re.escape(marker)}(?:$|[^a-z0-9])'))
    for marker in PLACEHOLDER_MARKERS
)


def secret_problem(name, value):
    """Why this key is unfit for production, or None.

    Split out from the class so it can be tested without instantiating a config,
    and so the two rules are visible side by side rather than buried in a
    boolean.
    """
    value = value or ''
    if len(value) < 32:
        return f'{name} is only {len(value)} characters — it must be at least 32.'
    if value in INSECURE_DEFAULTS:
        return f'{name} is a known placeholder value.'
    lowered = value.lower()
    for marker, pattern in _MARKER_RES:
        if pattern.search(lowered):
            return (f'{name} contains "{marker}", so it is a placeholder rather '
                    'than a real secret — length alone does not make it one.')
    return None


class ProductionConfig(Config):
    DEBUG = False
    JWT_COOKIE_SECURE = True
    JWT_COOKIE_SAMESITE = 'Strict'

    def __init__(self):
        # Refuse to boot with a guessable signing key — anyone who knows it can
        # mint a valid admin token, and this project's keys have already been
        # published once in a public repository's history.
        for name in ('SECRET_KEY', 'JWT_SECRET_KEY'):
            problem = secret_problem(name, getattr(self, name, ''))
            if problem:
                raise RuntimeError(
                    f'{problem} Refusing to start in production.\n'
                    '  Generate one with: '
                    'python -c "import secrets; print(secrets.token_urlsafe(48))"'
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
