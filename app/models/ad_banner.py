from datetime import datetime

from ..extensions import db

# What a tap on a banner opens. Kept as an enum rather than a free URL for the
# in-app cases, so a banner cannot be pointed at a route that does not exist
# and the app never has to parse a path it did not build itself.
TARGET_TYPES = ('none', 'product', 'farmer', 'family_pack', 'category', 'url')


class AdBanner(db.Model):
    """A promotional poster shown in the app's home feed.

    Deliberately not a `HomepageSection`. Those are fixed bands keyed by name —
    one hero, one weekly-basket strip — edited in place and always present.
    A banner is inventory: many of them, ordered, scheduled, rotated, and
    counted. Squeezing that into `sections.data` as JSON would mean no
    scheduling query, no per-banner counters and no ordering the database can
    do for you.
    """

    __tablename__ = 'ad_banners'

    id = db.Column(db.Integer, primary_key=True)

    # The admin's own label. Never shown to customers — the poster carries its
    # own message — but a list of twelve identical thumbnails is unusable
    # without it.
    title = db.Column(db.String(255), nullable=False)
    image_url = db.Column(db.String(500), nullable=False)

    # Read by screen readers, and shown if the image fails to load. A banner
    # with no alt text is invisible to anyone using VoiceOver or TalkBack.
    alt_text = db.Column(db.String(255))

    target_type = db.Column(db.Enum(*TARGET_TYPES, name='banner_target_type'),
                            nullable=False, default='none')
    # Set for product / farmer / family_pack / category. No foreign key on
    # purpose: it points at four different tables depending on target_type, and
    # a banner outliving the product it advertised should degrade to "opens
    # nothing" rather than block the delete.
    target_id = db.Column(db.Integer)
    target_url = db.Column(db.String(500))          # set when target_type = 'url'

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # Both optional. Null start means "live as soon as it is active", null end
    # means "until someone turns it off" — which is what you want for a house
    # ad, and not what you want for a paid slot.
    starts_at = db.Column(db.DateTime)
    ends_at = db.Column(db.DateTime)

    sort_order = db.Column(db.Integer, nullable=False, default=0)

    # Counters rather than an events table. An advertiser asks "how many people
    # saw it", not "who saw it at 14:32" — and a row per impression on a home
    # screen that reloads on every visit would outgrow the orders table within
    # a month. If per-day breakdowns are ever needed, that is a separate
    # rollup table, not a change here.
    impressions = db.Column(db.Integer, nullable=False, default=0)
    clicks = db.Column(db.Integer, nullable=False, default=0)

    created_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by])

    # ── Scheduling ───────────────────────────────────────────────────────────

    def is_live(self, now=None):
        """Whether this banner should be served right now."""
        now = now or datetime.utcnow()
        if not self.is_active:
            return False
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True

    @property
    def status(self):
        """For the admin list, where 'active' alone is misleading.

        A banner can be switched on and still not be showing — because its
        window has not opened yet, or has closed. Three states that look
        identical in the database are the most common source of "why isn't my
        banner live".
        """
        if not self.is_active:
            return 'paused'
        now = datetime.utcnow()
        if self.starts_at and now < self.starts_at:
            return 'scheduled'
        if self.ends_at and now > self.ends_at:
            return 'expired'
        return 'live'

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self, admin=False):
        """The public shape by default; `admin=True` adds the back-office fields.

        Customers get only what the app needs to draw and route the banner.
        Schedule, counters and who created it are none of their business, and
        leaking the click count would tell a competitor how well the placement
        performs.
        """
        data = {
            'id': self.id,
            'image_url': self.image_url,
            'alt_text': self.alt_text,
            'target_type': self.target_type,
            'target_id': self.target_id,
            'target_url': self.target_url,
        }
        if not admin:
            return data

        data.update({
            'title': self.title,
            'is_active': self.is_active,
            'status': self.status,
            'starts_at': self.starts_at.isoformat() if self.starts_at else None,
            'ends_at': self.ends_at.isoformat() if self.ends_at else None,
            'sort_order': self.sort_order,
            'impressions': self.impressions,
            'clicks': self.clicks,
            # Spelled out so the admin UI does not have to divide by zero.
            'ctr': round(self.clicks / self.impressions * 100, 2) if self.impressions else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        })
        return data
