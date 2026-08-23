"""The report catalogue, and publishing a report to Drive.

Everything that is not "which HTTP route asked for it" lives here, so the cron
job and the admin button run the same code rather than two copies that drift.
`admin.py` used to import `_report_payload` and `_reports` out of `cron.py` —
one route module reaching into another — which worked and told you nothing
about where the logic belonged.

Adding a report means writing a module that exposes `build_rows`, `COLUMNS`,
`SUMMARY_ROWS`, `TITLE`, `SHEET_NAME`, `SLUG` and `summary`, then adding one
line to `available()`. Nothing else changes: the endpoints, both download
handlers and both publish buttons are all slug-driven.
"""

import logging
from datetime import datetime

from flask import current_app

logger = logging.getLogger(__name__)


class UnknownReport(KeyError):
    """Asked for a slug that is not in the catalogue."""


def available():
    """The reports, by url slug.

    Imported lazily so a broken report cannot stop the app booting, and so the
    models are not touched at import time.
    """
    from . import basket_orders_report, inventory_report, procurement_report
    return {
        'farmer-stock': inventory_report,
        'basket-orders': basket_orders_report,
        'buying-plan': procurement_report,
    }


def get(slug):
    """The report module for `slug`, or raise `UnknownReport`."""
    module = available().get(slug)
    if module is None:
        raise UnknownReport(slug)
    return module


def payload(module):
    """Turn a report module into the structure the workbook builder takes.

    Shared by the JSON endpoint, both publish paths and both download handlers,
    because all of them must produce the identical structure. Building it in
    several places is several chances for the download and the scheduled file
    to disagree about their own columns.
    """
    generated = datetime.utcnow().isoformat() + 'Z'
    rows = module.build_rows()

    data = {
        'generated_at': generated,
        'title': module.TITLE,
        'sheet_name': module.SHEET_NAME,
        'slug': module.SLUG,
        'columns': module.COLUMNS,
        'summary_rows': module.SUMMARY_ROWS,
        'summary': module.summary(rows),
        'rows': rows,
    }
    # Optional: a report may want a line under the title explaining its window.
    if hasattr(module, 'subtitle'):
        data['subtitle'] = module.subtitle(generated)
    return data


def publish(slug):
    """Build a report and put it in Google Drive. Returns a result dict.

    The same work whether a cron job or an admin button asked for it — which is
    the point of it living here. Three shapes come back, and the caller turns
    each into the right HTTP status:

        {'published': True,  'file': {...}, 'summary': {...}}
        {'skipped':   True,  'reason': '...', 'summary': {...}}
        raises — something genuinely went wrong

    `skipped` is not an error. An unconfigured Drive is a setup step nobody has
    done yet, and treating it as a failure would make `curl -f` mail about it
    every run forever, and would show an admin a red banner for something that
    is working exactly as designed.
    """
    from .drive_upload import DriveNotConfigured, upload
    from .report_workbook import build_bytes, filename

    module = get(slug)
    data = payload(module)
    counts = data['summary']

    try:
        result = upload(
            build_bytes(data), filename(data),
            folder_id=current_app.config.get('DRIVE_FOLDER_ID'),
            credentials_path=current_app.config.get('DRIVE_CREDENTIALS'),
        )
    except DriveNotConfigured as e:
        logger.warning('%s not published: %s', slug, e)
        return {'skipped': True, 'reason': str(e), 'summary': counts, 'report': slug}

    logger.info('%s published: %s', slug, counts)
    return {
        'published': True,
        'report': slug,
        'file': {
            'id': result.get('id'),
            'name': result.get('name'),
            'link': result.get('webViewLink'),
            'modified': result.get('modifiedTime'),
        },
        'summary': counts,
    }


def publish_all():
    """Publish every report, and keep going if one of them fails.

    One report failing must not stop the others: they are independent, and an
    admin who pressed one button expecting three files should get the two that
    could be built plus a clear account of the one that could not. The caller
    decides the status code from `ok`.
    """
    results, ok = [], True
    for slug in available():
        try:
            results.append(publish(slug))
        except Exception as e:  # noqa: BLE001 — recorded per report, see above
            logger.exception('%s failed to publish', slug)
            results.append({'report': slug, 'error': str(e) or e.__class__.__name__})
            ok = False
    return {'ok': ok, 'results': results}
