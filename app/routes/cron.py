"""Scheduled work, triggered from outside.

Everything recurring in this app has until now run opportunistically — weekly
deliveries were generated when somebody happened to open their subscriptions
page. That is fine for generation, which only has to happen before the delivery
date, and useless for a reminder, which has to happen on a particular day
whether or not anyone opened the app. On a quiet Sunday nobody would be
reminded of anything.

So: one endpoint, hit by cron on the VPS.

**Authenticated by a shared secret, not a JWT.** A cron job has no user and no
browser, so there is nobody to sign in as. The token lives in the environment
alongside the database password and is compared in constant time.

Add to the crontab (`crontab -e`), every morning at 6am:

    0 6 * * * curl -fsS -X POST https://api.f2hmarket.com:8443/api/cron/run \\
      -H "X-Cron-Token: $F2H_CRON_TOKEN" >> /var/log/f2h-cron.log 2>&1

`-f` makes curl exit non-zero on an HTTP error, so a silently failing job shows
up in cron's mail instead of logging "success" forever.
"""

import hmac
import logging

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)

cron_bp = Blueprint('cron', __name__)


def _authorised() -> bool:
    """Constant-time comparison of the cron token.

    `hmac.compare_digest` rather than `==` because a plain string comparison
    returns as soon as it finds a differing byte, and that timing difference is
    enough to recover a secret one byte at a time from a remote caller. The
    endpoint is public and unauthenticated by design, which makes it exactly the
    kind of target that gets probed.
    """
    expected = current_app.config.get('CRON_TOKEN') or ''
    if not expected:
        # No token configured means the endpoint is off, not open. Defaulting to
        # allow would turn a missing setting into a public trigger.
        return False
    supplied = request.headers.get('X-Cron-Token', '')
    return hmac.compare_digest(str(expected), str(supplied))


@cron_bp.route('/run', methods=['POST'])
def run_scheduled():
    """Daily maintenance: remind, then generate.

    Order matters. Reminders go out for deliveries that have *not* been created
    yet — generating first would confirm them, and the reminder would then skip
    every basket it was meant to warn, every single day.

    Returns counts so the cron log says what happened rather than just '200 OK'.
    """
    if not _authorised():
        # Deliberately identical whether the token is wrong or unset: a caller
        # probing this should not learn which.
        logger.warning('Rejected cron trigger from %s', request.remote_addr)
        return jsonify({'error': 'Forbidden'}), 403

    from ..services.family_pack_subscription_service import (
        generate_due_deliveries, send_basket_reminders)

    result = {}

    # Each step is isolated. A failure in reminders must not stop deliveries
    # being generated — that would turn a notification bug into missed produce.
    try:
        reminded = send_basket_reminders()
        result['reminded'] = len(reminded)
    except Exception:
        logger.exception('Basket reminders failed')
        result['reminded'] = None
        result['reminder_error'] = True

    try:
        created = generate_due_deliveries()
        result['deliveries_created'] = len(created)
    except Exception:
        logger.exception('Delivery generation failed')
        result['deliveries_created'] = None
        result['generation_error'] = True

    logger.info('Cron run: %s', result)
    # 200 even on a partial failure, with the flags in the body. A 500 would
    # make cron retry the whole thing, re-running the half that worked.
    return jsonify(result), 200
