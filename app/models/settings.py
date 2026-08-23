"""Platform settings an admin can change without a deploy.

One row, forced to id 1. That is not laziness — a settings table with many rows
invites two of them, and then "which one is live?" becomes a bug that only shows
up in production. `get()` below creates the row on first use and every read goes
through it, so there is exactly one.

Right now the only setting is the order minimum. To add a second, add a column
here, a line to `to_dict`, and a bounded field in the admin route — no new table
and no new migration pattern.
"""

from datetime import datetime

from flask import current_app

from ..extensions import db


class PlatformSettings(db.Model):
    __tablename__ = 'platform_settings'

    id = db.Column(db.Integer, primary_key=True)

    # Nullable on purpose: NULL means "nobody has set this, use the configured
    # default". That keeps the env var meaningful as a starting value instead of
    # being silently overwritten by whatever the column default happened to be
    # the first time the row was created.
    min_order_value = db.Column(db.Numeric(10, 2))

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    updater = db.relationship('User', foreign_keys=[updated_by])

    @classmethod
    def get(cls):
        """The single settings row, created if this is the first call."""
        row = cls.query.get(1)
        if row is None:
            row = cls(id=1)
            db.session.add(row)
            db.session.commit()
        return row

    def to_dict(self):
        return {
            'min_order_value': min_order_value(),
            'min_order_value_default': float(
                current_app.config.get('MIN_ORDER_VALUE', 300)
            ),
            'is_customised': self.min_order_value is not None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'updated_by': self.updater.full_name if self.updater else None,
        }


def min_order_value():
    """The order minimum in rupees.

    Every place that enforces the floor calls this, so an admin changing the
    figure changes all of them at once — the cart summary, cart checkout and the
    single-product path used to read `current_app.config` separately, which is
    three chances to disagree.

    Falls back to the configured value if the table has not been migrated yet or
    the database is briefly unreachable. A missing settings row must not take
    checkout down with it: refusing every order because a lookup failed is worse
    than using a slightly stale floor, and the value it falls back to is the one
    that was live before this feature existed.
    """
    default = float(current_app.config.get('MIN_ORDER_VALUE', 300))
    try:
        row = db.session.get(PlatformSettings, 1)
        if row is not None and row.min_order_value is not None:
            return float(row.min_order_value)
    except Exception:  # noqa: BLE001 — see the docstring; never fail closed here
        current_app.logger.warning(
            'platform_settings unreadable, using configured minimum %s', default,
            exc_info=True,
        )
    return default


# The admin form is bounded rather than free. ₹0 would turn the floor off
# without saying so, and a typo of 30000 instead of 300 silently closes the shop
# — both are one keystroke away and neither announces itself.
MIN_ORDER_FLOOR = 1.0
MIN_ORDER_CEILING = 10000.0
