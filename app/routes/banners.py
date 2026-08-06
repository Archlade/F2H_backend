"""Promotional banners: the public feed, the counters, and the admin CRUD.

Three audiences on one blueprint:

* **Anyone** gets `GET /api/banners` — only what is live right now, and only
  the fields needed to draw and route it.
* **Anyone** can `POST` an impression or a click. These are unauthenticated on
  purpose: a banner is shown to signed-out visitors too, and requiring a token
  would silently under-count exactly the audience an advertiser cares about.
* **Admins** get the rest.
"""

from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity

from ..extensions import db, limiter
from ..models import AdBanner
from ..models.ad_banner import TARGET_TYPES
from ..utils.decorators import role_required

banners_bp = Blueprint('banners', __name__)

# Enough to fill a carousel without turning the home screen into a billboard.
# Anything beyond this and later banners are seen by almost nobody anyway.
MAX_LIVE = 10


# ── Public ────────────────────────────────────────────────────────────────────

@banners_bp.route('', methods=['GET'])
def list_banners():
    """The banners to show right now, in the order the admin set.

    The schedule is filtered in SQL rather than in Python so a hundred expired
    banners cost nothing, and so the app is never handed something it would
    have to know to hide.
    """
    now = datetime.utcnow()
    banners = (AdBanner.query
               .filter(AdBanner.is_active.is_(True))
               .filter(db.or_(AdBanner.starts_at.is_(None), AdBanner.starts_at <= now))
               .filter(db.or_(AdBanner.ends_at.is_(None), AdBanner.ends_at >= now))
               .order_by(AdBanner.sort_order, AdBanner.id)
               .limit(MAX_LIVE)
               .all())
    return jsonify({'banners': [b.to_dict() for b in banners]}), 200


# Unauthenticated by necessity — banners are shown to signed-out visitors too,
# and requiring a token would under-count exactly the audience an advertiser
# cares about. The trade is that anyone can call them, so they are throttled:
# without a limit a script could inflate an advertiser's numbers to whatever it
# liked, which is billing fraud the moment a placement is sold rather than
# given away. Generous enough that a real person scrolling never notices.
@banners_bp.route('/<int:banner_id>/impression', methods=['POST'])
@limiter.limit('120 per hour')
def record_impression(banner_id):
    return _bump(banner_id, AdBanner.impressions)


@banners_bp.route('/<int:banner_id>/click', methods=['POST'])
@limiter.limit('60 per hour')
def record_click(banner_id):
    return _bump(banner_id, AdBanner.clicks)


def _bump(banner_id, column):
    """Increment a counter in SQL, without loading the row.

    `UPDATE … SET n = n + 1` rather than read-modify-write: several people see
    the same banner at the same moment, and two Python-side increments racing
    would each write the same number and lose one of the two.

    A missing banner answers 200. The app fires these and forgets them, and a
    404 on a banner that was deleted a second ago is noise, not information.
    """
    updated = (db.session.query(AdBanner)
               .filter(AdBanner.id == banner_id)
               .update({column: column + 1}, synchronize_session=False))
    db.session.commit()
    return jsonify({'success': True, 'counted': updated == 1}), 200


# ── Admin ─────────────────────────────────────────────────────────────────────

@banners_bp.route('/admin', methods=['GET'])
@role_required('admin')
def admin_list_banners():
    """Every banner, live or not, newest-relevant first."""
    banners = AdBanner.query.order_by(AdBanner.sort_order, AdBanner.id.desc()).all()
    return jsonify({'banners': [b.to_dict(admin=True) for b in banners]}), 200


@banners_bp.route('/admin', methods=['POST'])
@role_required('admin')
def create_banner():
    data = request.get_json(silent=True) or {}
    try:
        fields = _validated(data, creating=True)
    except ValueError as error:
        return jsonify({'error': str(error)}), 400

    banner = AdBanner(created_by=int(get_jwt_identity()), **fields)
    db.session.add(banner)
    # Flushed, not committed: the audit row needs the banner's id, and both
    # should land in the same transaction. log_audit leaves the commit to its
    # caller, so logging after one would write an audit row that is discarded
    # at teardown.
    db.session.flush()
    _log(banner, 'create_banner')
    db.session.commit()
    return jsonify(banner.to_dict(admin=True)), 201


@banners_bp.route('/admin/<int:banner_id>', methods=['PUT', 'PATCH'])
@role_required('admin')
def update_banner(banner_id):
    banner = AdBanner.query.get_or_404(banner_id)
    data = request.get_json(silent=True) or {}
    try:
        fields = _validated(data, creating=False)
    except ValueError as error:
        return jsonify({'error': str(error)}), 400

    for key, value in fields.items():
        setattr(banner, key, value)
    _log(banner, 'update_banner')
    db.session.commit()
    return jsonify(banner.to_dict(admin=True)), 200


@banners_bp.route('/admin/<int:banner_id>', methods=['DELETE'])
@role_required('admin')
def delete_banner(banner_id):
    banner = AdBanner.query.get_or_404(banner_id)
    _log(banner, 'delete_banner')
    db.session.delete(banner)
    db.session.commit()
    return jsonify({'success': True}), 200


@banners_bp.route('/admin/reorder', methods=['PUT'])
@role_required('admin')
def reorder_banners():
    """Set the running order from a list of ids.

    Position in the array is the order, so the UI sends what the admin sees
    after a drag rather than computing indices — which is the part that goes
    wrong when two banners share a sort_order.
    """
    ids = (request.get_json(silent=True) or {}).get('banner_ids')
    if not isinstance(ids, list):
        return jsonify({'error': 'banner_ids must be a list'}), 400

    for position, banner_id in enumerate(ids):
        (db.session.query(AdBanner)
         .filter(AdBanner.id == banner_id)
         .update({AdBanner.sort_order: position}, synchronize_session=False))
    db.session.commit()
    return jsonify({'success': True}), 200


# ── Validation ────────────────────────────────────────────────────────────────

def _validated(data, creating):
    """The writable fields, checked. Raises ValueError with a message for the admin.

    On update, only what was sent is touched — the admin UI sends the whole
    form, but a future caller toggling `is_active` alone should not blank
    everything else.
    """
    out = {}

    if creating or 'title' in data:
        title = (data.get('title') or '').strip()
        if not title:
            raise ValueError('Give the banner a name so you can find it later')
        out['title'] = title[:255]

    if creating or 'image_url' in data:
        image_url = (data.get('image_url') or '').strip()
        if not image_url:
            raise ValueError('Upload a poster image first')
        out['image_url'] = image_url[:500]

    if 'alt_text' in data:
        out['alt_text'] = (data.get('alt_text') or '').strip()[:255] or None

    if creating or 'target_type' in data:
        target_type = (data.get('target_type') or 'none').strip()
        if target_type not in TARGET_TYPES:
            raise ValueError(f"Unknown target type '{target_type}'")
        out['target_type'] = target_type

        if target_type == 'url':
            url = (data.get('target_url') or '').strip()
            # Anything but http(s) is either a mistake or an attempt to fire an
            # intent / custom scheme from a tap the user did not understand.
            if not url.startswith(('http://', 'https://')):
                raise ValueError('The link must start with http:// or https://')
            out['target_url'] = url[:500]
            out['target_id'] = None
        elif target_type == 'none':
            out['target_url'] = None
            out['target_id'] = None
        else:
            target_id = data.get('target_id')
            try:
                out['target_id'] = int(target_id)
            except (TypeError, ValueError):
                raise ValueError(f'Choose which {target_type.replace("_", " ")} this banner opens')
            out['target_url'] = None

    if 'is_active' in data:
        out['is_active'] = bool(data.get('is_active'))

    if 'sort_order' in data:
        try:
            out['sort_order'] = int(data.get('sort_order') or 0)
        except (TypeError, ValueError):
            raise ValueError('Sort order must be a whole number')

    starts_at = _parse_date(data, 'starts_at', out)
    ends_at = _parse_date(data, 'ends_at', out)
    # Compared against whatever is already stored when only one end was sent,
    # so editing the end date of a live banner still gets checked.
    if starts_at and ends_at and ends_at <= starts_at:
        raise ValueError('The end date has to come after the start date')

    return out


def _parse_date(data, key, out):
    if key not in data:
        return None
    raw = data.get(key)
    if raw in (None, ''):
        out[key] = None
        return None
    try:
        # Accepts both '2026-08-10T09:00:00' and a trailing Z, which is what
        # a browser's toISOString() produces.
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        raise ValueError(f'{key.replace("_", " ")} is not a valid date')
    out[key] = parsed
    return parsed


def _log(banner, action):
    """Queue an audit row. The caller commits it alongside the change itself.

    Banner changes are visible to every user of the app, so who made one is
    worth keeping — but never at the cost of the change. A failure here is
    swallowed rather than 500ing an edit that was otherwise fine.
    """
    try:
        from ..utils.helpers import log_audit
        log_audit(int(get_jwt_identity()), action, 'ad_banner', banner.id,
                  new_data={'title': banner.title, 'is_active': banner.is_active})
    except Exception:
        pass
