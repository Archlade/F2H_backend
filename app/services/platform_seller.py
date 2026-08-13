"""Who F2H is, when F2H is the one selling.

Weekly baskets are built from the whole catalogue and sold by the platform, not
by a farm. But `family_pack_orders.farmer_id` is NOT NULL and drives who may
move an order, who is notified, and whose name sits on the payment — so a
platform sale still needs a user row to sell *as*.

That row is an ordinary admin account, nominated by email:

    PLATFORM_SELLER_EMAIL=orders@f2hmarket.com

Deliberately configured rather than "any admin". Picking an arbitrary admin
would attach every basket order to whoever happened to be first in the table,
and that account's name appears to customers.

**This fails loudly.** A misconfigured seller cannot be papered over: falling
back to some other account would silently sell baskets under a real person's
name and pay them a farmer's share. Generation stops instead, with a message
that says exactly what to fix.
"""

import logging

from flask import current_app

from ..models import User

logger = logging.getLogger(__name__)


class NoPlatformSeller(RuntimeError):
    """No usable F2H selling account. Raised rather than guessed around."""


def _configured_email():
    return (current_app.config.get('PLATFORM_SELLER_EMAIL')
            or current_app.config.get('ADMIN_EMAIL')
            or '').strip().lower()


def platform_seller():
    """The user row F2H sells weekly baskets as.

    Raises NoPlatformSeller with an actionable message rather than returning
    None, because every caller is in the middle of creating an order and has
    nothing sensible to do with a None.
    """
    email = _configured_email()
    if not email:
        raise NoPlatformSeller(
            'No platform seller configured. Weekly baskets are sold by F2H, so '
            'one admin account has to act as the seller. Set '
            'PLATFORM_SELLER_EMAIL in the environment to an existing admin '
            "account's email address."
        )

    user = User.query.filter(db_lower(User.email) == email).first()
    if user is None:
        raise NoPlatformSeller(
            f'PLATFORM_SELLER_EMAIL is set to {email!r} but no account with '
            'that email exists. Create it, or point the setting at an admin '
            'account that does.'
        )
    if user.role_name != 'admin':
        raise NoPlatformSeller(
            f'The platform seller account {email!r} is a {user.role_name!r}, '
            'not an admin. Baskets sold under a customer or farmer account '
            'would give that person a seller\'s powers over every basket order.'
        )
    if not user.is_active or user.deleted_at:
        raise NoPlatformSeller(
            f'The platform seller account {email!r} is deactivated. Reactivate '
            'it or nominate another admin account.'
        )
    return user


def platform_seller_or_none():
    """Same, but returns None instead of raising.

    For read paths — a screen listing baskets should still render when the
    setting is wrong, and say so, rather than 500.
    """
    try:
        return platform_seller()
    except NoPlatformSeller as exc:
        logger.warning('Platform seller unavailable: %s', exc)
        return None


def db_lower(column):
    """Case-insensitive email match without importing func at module scope."""
    from sqlalchemy import func
    return func.lower(column)
