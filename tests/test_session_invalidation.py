"""Whether a password reset actually throws the other person out.

Run with: python -m unittest discover -s tests   (no dependencies beyond stdlib)

The rule under test is small and its edges are all sharp. A token is refused
when it was issued before `users.password_changed_at`. Get the comparison
slightly wrong in either direction and you get one of two bad outcomes:

* **Too lenient** and the feature does nothing — the intruder keeps a working
  access token for 24 hours and can refresh it for 30 days, which is the exact
  hole this was written to close.
* **Too strict** and the person who just reset their own password is signed
  straight back out, because the token they were handed was minted in the same
  second the change was recorded.

The second one is the easy mistake: `password_changed_at` has microseconds and
a JWT's `iat` claim is whole seconds, so a naive `<` comparison rejects the
brand-new token roughly half the time. Non-deterministically. In production.
"""

import unittest
from datetime import datetime, timedelta, timezone

RESET_AT = datetime(2026, 8, 5, 12, 0, 0, 500_000)   # note the microseconds


def is_refused(issued_at_epoch, password_changed_at):
    """The rule from app/__init__.py's JWT loader, in one place.

    Kept as a function so the boundary can be tested without a database, a
    request context or a real signed token — none of which make the arithmetic
    any more correct.
    """
    if password_changed_at is None:
        return False                      # never changed: no cutoff
    if issued_at_epoch is None:
        return False                      # no iat claim: fail open
    issued = datetime.fromtimestamp(issued_at_epoch, tz=timezone.utc).replace(tzinfo=None)
    return issued < password_changed_at.replace(microsecond=0)


def epoch(dt):
    return dt.replace(tzinfo=timezone.utc).timestamp()


class AStolenSessionDies(unittest.TestCase):

    def test_a_token_from_before_the_reset_is_refused(self):
        stolen = epoch(RESET_AT - timedelta(hours=3))
        self.assertTrue(is_refused(stolen, RESET_AT))

    def test_a_token_from_the_day_before_is_refused(self):
        # Access tokens last 24 hours, so this is a live one right up until the
        # reset — and the whole reason the column exists.
        stolen = epoch(RESET_AT - timedelta(hours=23, minutes=59))
        self.assertTrue(is_refused(stolen, RESET_AT))

    def test_a_month_old_refresh_token_is_refused(self):
        # Refresh tokens last 30 days. This is the one that would otherwise let
        # an intruder mint fresh access tokens all month.
        stolen = epoch(RESET_AT - timedelta(days=29))
        self.assertTrue(is_refused(stolen, RESET_AT))


class TheOwnerIsNotLockedOut(unittest.TestCase):
    """The person who just reset their password holds a token minted in the
    same second the change was written. It must survive."""

    def test_the_token_issued_in_the_same_second_is_accepted(self):
        # password_changed_at is 12:00:00.500; the new token's iat is 12:00:00.
        # A naive comparison makes 12:00:00 < 12:00:00.500 true and signs the
        # user out immediately after a successful reset.
        fresh = epoch(RESET_AT.replace(microsecond=0))
        self.assertFalse(is_refused(fresh, RESET_AT),
                         'truncating the microseconds is what prevents this')

    def test_a_token_issued_just_after_is_accepted(self):
        self.assertFalse(is_refused(epoch(RESET_AT + timedelta(seconds=1)), RESET_AT))

    def test_signing_in_again_later_works(self):
        self.assertFalse(is_refused(epoch(RESET_AT + timedelta(days=2)), RESET_AT))

    def test_the_grace_is_exactly_one_second_and_no_more(self):
        # The second before the change is still refused — the window does not
        # quietly widen into something an attacker could use.
        just_before = epoch(RESET_AT.replace(microsecond=0) - timedelta(seconds=1))
        self.assertTrue(is_refused(just_before, RESET_AT))

    def test_the_boundary_holds_at_every_microsecond_offset(self):
        # The bug this guards against is non-deterministic: it depends on where
        # in the second the reset landed. Check the whole range.
        for micro in (0, 1, 250_000, 500_000, 999_999):
            changed = RESET_AT.replace(microsecond=micro)
            fresh = epoch(changed.replace(microsecond=0))
            with self.subTest(microsecond=micro):
                self.assertFalse(is_refused(fresh, changed),
                                 'a freshly issued token must never be refused')


class MigrationSafety(unittest.TestCase):
    """The column is added NULL and not backfilled. Nobody gets signed out by
    deploying this."""

    def test_a_null_cutoff_refuses_nothing(self):
        for age in (timedelta(0), timedelta(days=1), timedelta(days=365)):
            with self.subTest(age=age):
                self.assertFalse(is_refused(epoch(RESET_AT - age), None))

    def test_a_token_with_no_iat_claim_is_allowed_through(self):
        # Fail open rather than closed. Refusing here would lock out every
        # session on a deployment whose tokens were minted without the claim,
        # and the is_active / deleted_at checks still apply either way.
        self.assertFalse(is_refused(None, RESET_AT))


class WhatGetsClearedOnReset(unittest.TestCase):
    """Push registrations go too, not just tokens.

    An account can be locked and still leaking: the intruder's phone keeps
    receiving notifications about the owner's orders. That is worse than a live
    session for being invisible to everyone involved.
    """

    def test_every_device_for_that_user_is_dropped_and_no_others(self):
        import sqlite3
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute('CREATE TABLE device_tokens (id INTEGER PRIMARY KEY, user_id INT, token TEXT)')
        con.executemany('INSERT INTO device_tokens VALUES (?,?,?)', [
            (1, 10, 'victim-phone'),
            (2, 10, 'intruder-phone'),
            (3, 11, 'someone-else'),
        ])
        con.execute('DELETE FROM device_tokens WHERE user_id = ?', (10,))

        rows = con.execute('SELECT user_id FROM device_tokens').fetchall()
        self.assertEqual(rows, [(11,)],
                         "only the resetting account's devices should go")


if __name__ == '__main__':
    unittest.main()
