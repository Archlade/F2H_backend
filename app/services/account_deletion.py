"""Deleting an account, and what that means for everything attached to it.

Google Play requires that any app offering account creation also offers account
deletion, in-app and through a public web page. That is why this exists; what it
does is a judgement call, and the judgement is:

**Personal data goes. Financial records stay, anonymised.**

A hard `DELETE FROM users` is the obvious reading of "delete my account" and the
wrong one here. Every order, payment and ledger entry points at a user row; drop
it and either the foreign keys refuse, or they cascade and take a farmer's
earnings history with them. Someone deleting their account is asking not to be
identifiable — not to erase the record that ₹450 changed hands, which the other
party to that transaction has their own claim on.

So the row survives with nothing personal in it. Name, email, phone and photo
are replaced; addresses, saved locations and device tokens are deleted outright.
What remains is an order that happened, attached to nobody.

**Deletion is refused while the account is mid-transaction.** Not to trap
anyone — because a customer with produce already picked, or a farmer holding a
balance they have not withdrawn, is in the middle of something with another
person on the other side of it. The message says what to finish first.
"""

import logging
from datetime import datetime

from ..extensions import db
from ..models import Address, DeviceToken, Location, PurchaseRequest, FamilyPackOrder
from ..models.user import User

logger = logging.getLogger(__name__)

# Orders where somebody is still owed goods or money. Deleting mid-flight leaves
# the other party with a counterparty who no longer exists.
LIVE_STATUSES = ('pending', 'admin_review', 'accepted', 'chat_active', 'confirmed',
                 'preparing', 'ready_for_pickup', 'out_for_delivery')


class DeletionRefused(ValueError):
    """Something must be settled first. The message is shown to the user."""


def _live_order_count(user_id):
    total = 0
    for model in (PurchaseRequest, FamilyPackOrder):
        total += model.query.filter(
            ((model.customer_id == user_id) | (model.farmer_id == user_id))
            & model.status.in_(LIVE_STATUSES)
        ).count()
    return total


def deletion_blockers(user):
    """Why this account cannot be deleted yet, or None."""
    live = _live_order_count(user.id)
    if live:
        return (f'You have {live} order{"s" if live != 1 else ""} still in progress. '
                'Please wait for them to be completed or cancelled before deleting '
                'your account.')

    # There is no balance to strand any more: farmers are paid in cash at
    # pickup, so an account carries no unwithdrawn earnings by the time its
    # orders are settled — and the live-order check above already covers
    # anything mid-flight. Historic ledger rows from the old wallet model are
    # left untouched by deletion; they are financial records, not personal data.
    return None


def delete_account(user, reason=None):
    """Anonymise the account and remove the personal data hanging off it.

    Not committed here — the caller owns the transaction, so the anonymisation
    and the deletions land together or not at all. A half-deleted account is
    worse than either outcome.
    """
    blocker = deletion_blockers(user)
    if blocker:
        raise DeletionRefused(blocker)

    user_id = user.id

    # Personal data with no financial meaning — deleted rather than kept.
    Address.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    Location.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    DeviceToken.query.filter_by(user_id=user_id).delete(synchronize_session=False)

    # A farmer's public profile is personal too: farm name, bio and photos
    # identify them as surely as their own name does.
    profile = user.farmer_profile
    if profile is not None:
        profile.farm_name = 'Deleted farm'
        profile.bio = None
        profile.farm_description = None
        profile.avatar_url = None
        profile.cover_image_url = None
        profile.payout_upi_id = None
        profile.payout_account_name = None
        profile.payout_account_number = None
        profile.payout_ifsc = None
        profile.is_suspended = True

    # The email has a UNIQUE constraint, so it cannot simply be blanked — and
    # keeping it would let anyone check whether an address ever had an account.
    # `.invalid` is reserved by RFC 2606 and can never be a real domain, so this
    # can never collide with a live address or be mistaken for one.
    user.email = f'deleted-{user_id}@f2h.invalid'
    user.first_name = 'Deleted'
    user.last_name = 'user'
    user.phone = None
    user.avatar_url = None
    user.is_active = False
    user.deleted_at = datetime.utcnow()

    # Invalidates every outstanding token: the JWT loader refuses anything
    # issued before this moment, so an app still holding a session is signed out
    # on its next request rather than lingering until the token expires.
    user.password_changed_at = datetime.utcnow()
    # The hash is replaced with something no password can produce, so the
    # account cannot be signed back into even with the old credentials.
    user.password_hash = '!deleted'

    logger.info('Account %s deleted%s', user_id,
                f' (reason: {reason[:100]})' if reason else '')
    return user
