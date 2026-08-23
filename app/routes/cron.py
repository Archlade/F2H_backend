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


@cron_bp.route('/reports/<slug>', methods=['GET'])
def report_json(slug):
    """A report as JSON — the same structure the spreadsheet is built from.

    `slug` is `farmer-stock` or `basket-orders`. Same shared-secret
    authentication as `/run`, and for the same reason: the caller is a scheduled
    job with no user to sign in as. Reusing the cron token rather than inventing
    a second credential also means one secret to rotate, not two.

    GET, because it reads and changes nothing — a retry is free, and it can be
    opened in a browser with the header set, which is how anyone will first
    check whether the numbers look right.
    """
    if not _authorised():
        return jsonify({'error': 'Unauthorised'}), 401

    from ..services import report_service

    try:
        module = report_service.get(slug)
    except report_service.UnknownReport:
        return jsonify({'error': f'Unknown report {slug!r}',
                        'available': sorted(report_service.available())}), 404

    return jsonify(report_service.payload(module)), 200


@cron_bp.route('/reports/<slug>/publish', methods=['POST'])
def publish_report(slug):
    """Build a report and put it in Google Drive.

    On its own schedule rather than inside `/run`: all three reports run every
    second day, and a slow upload should not sit in front of work that is
    time-sensitive. Crontab:

        # Farmers and stock, every other day at 7am
        0 7 */2 * * curl -fsS -X POST \\
          https://api.f2hmarket.com:8443/api/cron/reports/farmer-stock/publish \\
          -H "X-Cron-Token: $F2H_CRON_TOKEN" >> /var/log/f2h-report.log 2>&1

        # Upcoming weekly baskets, every other day at 7:30am
        30 7 */2 * * curl -fsS -X POST \\
          https://api.f2hmarket.com:8443/api/cron/reports/basket-orders/publish \\
          -H "X-Cron-Token: $F2H_CRON_TOKEN" >> /var/log/f2h-report.log 2>&1

        # Buying plan, every other day at 6:30am — after deliveries generate
        30 6 */2 * * curl -fsS -X POST \\
          https://api.f2hmarket.com:8443/api/cron/reports/buying-plan/publish \\
          -H "X-Cron-Token: $F2H_CRON_TOKEN" >> /var/log/f2h-report.log 2>&1

    The basket report still *covers* fourteen days; it is rebuilt every two.
    Rebuilding a fortnightly window only fortnightly meant the file was up to
    two weeks stale before it was replaced, and every basket created, paused or
    cancelled in between was invisible in it.

    6:30 for the buying plan, before the 7am pair: it has to run after the 6am
    job that generates deliveries, or it plans against a delivery that does not
    exist yet.

    Every `*/2` line restarts on the 1st, so a 31-day month gives one
    consecutive pair at the boundary. Harmless for a report, and not worth a
    state table to avoid.

    The buying plan is the one where that drift could have mattered, since it
    describes only the *next* delivery and deliveries are Saturday and Sunday.
    Checked over a full year: the plan is never more than one day old when a
    delivery comes round, which is the same worst case as pinning it to Friday
    and Saturday by hand. So the simpler line is also the correct one, and
    there is nothing to tune.

    Returns 200 with `skipped` when Drive is not configured. Deliberate: an
    unconfigured integration is not an error, and a 500 would make `curl -f`
    mail you about it every run forever.
    """
    if not _authorised():
        return jsonify({'error': 'Unauthorised'}), 401

    from ..services import report_service

    try:
        result = report_service.publish(slug)
    except report_service.UnknownReport:
        return jsonify({'error': f'Unknown report {slug!r}',
                        'available': sorted(report_service.available())}), 404
    except Exception:
        # A real failure — network, permissions, a revoked key. This one *should*
        # be noisy, so cron mails about it.
        logger.exception('%s upload failed', slug)
        return jsonify({'error': 'Upload failed — see the server log'}), 500

    return jsonify(result), 200
