"""Push notifications, sent through Firebase Cloud Messaging.

The mobile app registers a token per install (`/api/devices`) and this is the
half that actually delivers. It exists because the Socket.IO channel only
reaches a phone with the app open, which is precisely not the case that matters
— a farmer whose handset is in their pocket when an order comes in.

Three rules shape everything below.

**Off is a valid state.** No `firebase-admin` installed, no service-account
credential, or a credential that will not load, all end in "push is off" and a
line in the log — never in a failed request. The in-app notification list and
the socket badge carry on regardless, exactly as they did before push existed,
and the same is true on the client side (see the mobile app's `PushService`).

**Sending must not block the response.** A send is an HTTPS round trip to
Google, and there is no reason to make the customer who tapped *Place order*
wait for it. Everything goes out on an eventlet background greenlet.

**Dead tokens get pruned.** FCM tells you when a token is finished; a table
that never acts on that grows without bound and wastes one send per stale row
forever.
"""

import logging
import os
import threading

from flask import current_app, has_app_context

from ..extensions import db, socketio
from ..models import DeviceToken

logger = logging.getLogger(__name__)

# firebase-admin is optional, the same way Flask-Mail is: a deployment that has
# not installed it should still boot and serve every other endpoint.
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except ImportError:  # pragma: no cover - depends on the deployment image
    firebase_admin = None
    credentials = messaging = None

# The initialised firebase_admin App, or False once we have tried and failed.
# None means "not tried yet". Kept at module scope because initialising twice
# raises, and because the failure is worth logging once rather than per push.
_app = None

_FCM_APP_NAME = 'f2h-push'

# Mirrors `default_notification_channel_id` in the mobile app's
# android/app/src/main/res/values/strings.xml. Changing it in one place only
# means Android drops every push on the floor.
ANDROID_CHANNEL_ID = 'f2h_updates'

# Errors that mean "this token will never work again", as opposed to "try
# later". Anything else is treated as transient and the row is kept.
_DEAD_TOKEN_ERRORS = ('UnregisteredError', 'SenderIdMismatchError', 'InvalidArgumentError')


# ── Initialisation ────────────────────────────────────────────────────────────

def _firebase_app():
    """The FCM app, initialising it on first use. None when push is off.

    Credentials are looked for in the order most deployments want them:

    1. ``FIREBASE_CREDENTIALS`` — a path to the service-account JSON. This is
       the explicit one, and what a normal server should use.
    2. ``GOOGLE_APPLICATION_CREDENTIALS`` / the metadata server — application
       default credentials, which is what Cloud Run and GCE already have.

    The file is *not* the same as the app's ``google-services.json``. That one
    holds public client identifiers; this one can send a notification to every
    user you have, so it lives outside the repository. Firebase Console →
    Project settings → Service accounts → Generate new private key.
    """
    global _app
    if _app is not None:
        return _app or None

    if firebase_admin is None:
        logger.info('firebase-admin is not installed — push notifications are off.')
        _app = False
        return None

    path = (current_app.config.get('FIREBASE_CREDENTIALS')
            if has_app_context() else os.environ.get('FIREBASE_CREDENTIALS'))

    try:
        if path:
            if not os.path.exists(path):
                logger.warning(
                    'FIREBASE_CREDENTIALS points at %s, which does not exist — '
                    'push notifications are off.', path)
                _app = False
                return None
            credential = credentials.Certificate(path)
        elif os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
            credential = credentials.ApplicationDefault()
        else:
            logger.info(
                'No Firebase service-account credential configured (set '
                'FIREBASE_CREDENTIALS) — push notifications are off.')
            _app = False
            return None

        _app = firebase_admin.initialize_app(credential, name=_FCM_APP_NAME)
        logger.info('Firebase Cloud Messaging is ready.')
    except Exception:
        # A malformed key, a revoked service account, a clock so far out that
        # the JWT is rejected. None of it should take the API down with it.
        logger.exception('Could not initialise Firebase — push notifications are off.')
        _app = False
        return None

    return _app


def is_enabled():
    """Whether a push would actually go anywhere. For health checks and tests."""
    return _firebase_app() is not None


def push_config_problem():
    """The specific reason push cannot run, or None.

    Same idea as the mail service: "not configured" on its own tells nobody
    anything, and this failure is close to invisible from the outside. The app
    still shows its in-app banner over Socket.IO whether or not FCM works, so
    "notifications appear inside the app but never on the phone" is exactly what
    a missing service-account key looks like — and nothing anywhere says so.
    """
    if firebase_admin is None:
        return 'firebase-admin is not installed (pip install firebase-admin)'

    path = (current_app.config.get('FIREBASE_CREDENTIALS')
            if has_app_context() else os.environ.get('FIREBASE_CREDENTIALS'))

    if not path and not os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
        return ('FIREBASE_CREDENTIALS is not set in backend/.env — no push is '
                'ever sent, so notifications only appear while the app is open')
    if path and not os.path.exists(path):
        return f'FIREBASE_CREDENTIALS points at {path}, which does not exist'
    if _firebase_app() is None:
        return 'the Firebase service account was rejected — see the server log'
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def dispatch_to_user(user_id, title, body='', data=None):
    """Queue a push to every device `user_id` has registered.

    Returns immediately. The send happens on a background greenlet, so a slow
    or unreachable FCM costs the caller nothing.

    `data` is the same dict stored on the notification row. Send it unchanged
    and a tap lands on the same screen the in-app list would open — the app
    reads `chat_id`, `request_id`, `order_id` / `family_pack_order_id` and
    `product_id`, in that order, and falls back to the notification list when
    none is present.
    """
    if not has_app_context():
        logger.warning('dispatch_to_user called with no application context; skipping.')
        return

    app = current_app._get_current_object()
    socketio.start_background_task(_send_in_context, app, user_id, title, body, data or {})


def send_to_user(user_id, title, body='', data=None):
    """Blocking version of :func:`dispatch_to_user`.

    For scripts and tests, where there is no request to keep short and the
    caller wants to know the outcome. Returns the number of devices reached.
    """
    return _send(user_id, title, body, data or {})


# ── The send itself ───────────────────────────────────────────────────────────

def _send_in_context(app, user_id, title, body, data):
    """Background-greenlet entry point.

    The greenlet has no Flask context of its own and no database session, so it
    pushes one. Because the session is fresh it also sees the just-committed
    notification row, which is what makes the badge count come out right.
    """
    with app.app_context():
        try:
            _send(user_id, title, body, data)
        except Exception:
            logger.exception('Push to user %s failed', user_id)
        finally:
            # A background task gets no automatic teardown, so the connection
            # would otherwise be held for the life of the greenlet's thread.
            db.session.remove()


def _send(user_id, title, body, data):
    if _firebase_app() is None:
        return 0

    # 500 is FCM's per-call ceiling. Nobody legitimately has that many devices;
    # the limit is here so a runaway row count degrades instead of erroring.
    rows = (DeviceToken.query
            .filter_by(user_id=user_id)
            .order_by(DeviceToken.last_seen_at.desc())
            .limit(500)
            .all())
    if not rows:
        # Logged rather than returned quietly. A user with no registered device
        # is the most common reason "push doesn't work", and it looks identical
        # from the outside to a broken key: the notification row is created, the
        # in-app list updates, and the phone stays silent. Without this line
        # there is nothing anywhere that says the send had no destination.
        logger.info('No device token for user %s — nothing to push to. The app '
                    'registers one at sign-in via POST /api/devices; an APK '
                    'built without google-services.json never will.', user_id)
        return 0

    tokens = [row.token for row in rows]

    try:
        response = messaging.send_each_for_multicast(
            _build_message(tokens, title, body, data, user_id),
            app=_firebase_app(),
        )
    except Exception:
        logger.exception('FCM rejected the whole batch for user %s', user_id)
        return 0

    _prune(rows, response)
    return response.success_count


def _build_message(tokens, title, body, data, user_id):
    """The one place a push is assembled.

    Extracted so the self-test in [diagnose] sends a byte-identical message to
    the real thing. A test that builds its own payload can pass while every
    genuine notification is dropped — the channel id below is exactly the sort
    of field that would differ — which makes the test worse than none at all.
    """
    return messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body or None),
        # Every value has to be a string: FCM's data payload is string-to-string
        # and the app parses the numbers back out (`"482"` and `482` both have
        # to work, which is what its `asIntOrNull` helpers are for).
        data={key: str(value) for key, value in (data or {}).items() if value is not None},
        android=messaging.AndroidConfig(
            # Order updates are time-sensitive and Doze will sit on a normal
            # -priority message for minutes.
            priority='high',
            notification=messaging.AndroidNotification(
                # Must match `default_notification_channel_id` in the app's
                # res/values/strings.xml, which is the channel MainActivity
                # creates on launch. Android 8+ silently drops a notification
                # posted to a channel that does not exist — no error, nothing
                # in the shade — so a typo here looks exactly like FCM being
                # broken.
                channel_id=ANDROID_CHANNEL_ID,
            ),
        ),
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(
                aps=messaging.Aps(sound='default', badge=_unread_count(user_id)),
            ),
        ),
    )


def background_tasks_run(timeout=4.0):
    """Whether `socketio.start_background_task` actually executes anything.

    This is the blind spot in every other check on this page. `dispatch_to_user`
    hands the real send to a background greenlet and returns; nothing waits on
    it and nothing reports if it never runs. So the credential can be valid, the
    tokens current, and FCM perfectly reachable, while not one notification is
    ever sent — and every diagnostic short of this one says the system is
    healthy.

    It happens when the process serving the app is not the one
    `SOCKETIO_ASYNC_MODE` names. `async_mode` defaults to ``eventlet``, and
    ``eventlet.monkey_patch()`` is called in `run.py` — so ``python run.py``
    is fine. Serve the same app through a gunicorn sync worker, or any entry
    point that skips that patch, and `start_background_task` spawns a greenlet
    onto a hub that never gets control. It waits there forever. No exception,
    no log line, nothing in the shade of anybody's phone.

    Tests the mechanism rather than a push: spawn a task whose whole job is to
    set a flag, and see whether the flag gets set. Under a correctly patched
    eventlet this returns in microseconds because `Event.wait` yields to the
    hub. Under a broken one it blocks the caller for `timeout` and returns
    False, which is the answer worth waiting for.
    """
    done = threading.Event()
    try:
        socketio.start_background_task(done.set)
    except Exception:
        logger.exception('start_background_task raised outright')
        return False
    return done.wait(timeout)


def diagnose(user_id):
    """Send a real push to `user_id`'s own devices and report what happened.

    Push has four ways to fail and they all look the same from a phone that
    stays quiet: no credential on the server, no registered device, a token FCM
    has retired, or a delivery that left the server and was dropped further
    down. The notification row is written in every one of those cases, so the
    in-app list fills up normally and the only symptom is silence.

    This walks the chain in order and stops at the first broken link, so the
    answer is one specific sentence rather than four things to go and check:

      ``server``      — no usable service-account credential. Nothing is ever
                        sent to anyone; ``push_problem`` says which part.
      ``no_devices``  — the server is fine and this account has never
                        registered a phone. The app does that at sign-in, so
                        this usually means signing in again on the handset.
      ``rejected``    — FCM refused the whole batch, credential and all.
      ``all_failed``  — every token was rejected individually. ``failures``
                        carries FCM's reason per device.
      ``sent``        — FCM accepted it. If the phone still shows nothing the
                        remaining causes are on the device: notifications
                        turned off for F2H in Android settings, battery
                        optimisation, or a Do Not Disturb mode.

    Runs synchronously, unlike [dispatch_to_user] — the caller is a person
    waiting for an answer, not a customer waiting for a checkout to finish.

    Never returns the tokens themselves. They are not quite secrets, but anyone
    holding one can push to that handset, and an admin screen is no place to
    put them.
    """
    problem = push_config_problem()

    rows = (DeviceToken.query
            .filter_by(user_id=user_id)
            .order_by(DeviceToken.last_seen_at.desc())
            .limit(500)
            .all())

    result = {
        'push_problem': problem,
        'devices': [row.to_dict() for row in rows],
        'device_count': len(rows),
        'attempted': 0,
        'delivered': 0,
        'failures': [],
        # Reported alongside the verdict rather than folded into it, because
        # the two fail independently: FCM can be perfectly healthy while no
        # real notification is ever dispatched, and vice versa. A verdict of
        # `sent` with this False is the one combination that looks like success
        # and is not.
        'background_ok': background_tasks_run(),
        'async_mode': getattr(socketio, 'async_mode', None),
    }

    if problem is not None:
        result['verdict'] = 'server'
        return result

    if not rows:
        result['verdict'] = 'no_devices'
        return result

    tokens = [row.token for row in rows]

    try:
        response = messaging.send_each_for_multicast(
            _build_message(
                tokens,
                'F2H test notification',
                'Push is working — this was sent from the admin settings screen.',
                {'type': 'push_test'},
                user_id,
            ),
            app=_firebase_app(),
        )
    except Exception as error:
        logger.exception('Push self-test failed outright for user %s', user_id)
        result['verdict'] = 'rejected'
        result['push_problem'] = f'FCM rejected the request: {error}'
        return result

    result['attempted'] = len(tokens)
    result['delivered'] = response.success_count
    result['failures'] = [
        {
            'platform': row.platform,
            'last_seen_at': row.last_seen_at.isoformat() if row.last_seen_at else None,
            'error': type(item.exception).__name__ if item.exception else 'unknown',
            'detail': str(item.exception) if item.exception else None,
            # Named so the screen can say "this phone reinstalled the app" —
            # which is normal and self-healing — rather than reporting it as a
            # fault. The row is deleted just below.
            'dead': _is_dead_token(item.exception),
        }
        for row, item in zip(rows, response.responses)
        if not item.success
    ]

    # Same pruning the real send does, so a self-test also cleans up after an
    # uninstall rather than reporting the same dead token every time.
    _prune(rows, response)

    result['verdict'] = 'sent' if response.success_count else 'all_failed'
    return result


def _unread_count(user_id):
    """What iOS should show on the app icon.

    iOS displays exactly this number and nothing else — omit it and the badge
    never changes, which is worse than a number that is occasionally one stale.
    It is deliberately the same figure `/api/notifications/unread-count`
    returns, so the badge and the list never disagree.
    """
    from .notification_service import get_unread_count
    try:
        return get_unread_count(user_id)
    except Exception:
        logger.exception('Could not read the unread count for user %s', user_id)
        return None


def _prune(rows, response):
    """Delete the tokens FCM just told us are dead.

    Tokens rotate on reinstall and on an app-data clear, and the old one is not
    reported until something tries to use it — so this is the only moment the
    information exists.
    """
    dead = [
        row for row, result in zip(rows, response.responses)
        if not result.success and _is_dead_token(result.exception)
    ]
    if not dead:
        return

    for row in dead:
        db.session.delete(row)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('Could not prune %s dead device token(s)', len(dead))
    else:
        logger.info('Pruned %s dead device token(s)', len(dead))


def _is_dead_token(error):
    if error is None:
        return False
    if type(error).__name__ in _DEAD_TOKEN_ERRORS:
        return True
    # Older firebase-admin releases surface the same conditions as a generic
    # FirebaseError carrying the code, so check that too rather than pinning a
    # version.
    code = getattr(error, 'code', None)
    return code in ('UNREGISTERED', 'INVALID_ARGUMENT', 'NOT_FOUND', 'SENDER_ID_MISMATCH')
