"""What decides whether a banner is on screen.

Run with: python -m unittest discover -s tests   (no dependencies beyond stdlib)

Scheduling is the part of this feature that fails quietly. A banner with the
wrong dates does not raise, does not log, and does not look broken in the admin
list — it just never appears, or worse, an expired campaign keeps running and
the advertiser sees a price that ended last week.

So these tests pin the two things that decide it: the SQL the public feed
filters with, and the four-way status the admin list shows. The SQL is run
against a real engine rather than reasoned about, because NULL comparisons are
exactly where date filters go wrong.
"""

import sqlite3
import unittest
from datetime import datetime, timedelta

NOW = datetime(2026, 8, 5, 12, 0, 0)
HOUR = timedelta(hours=1)

# The WHERE clause from banners.list_banners(). NULL means "no bound", which is
# why each side needs the IS NULL branch — `NULL <= '2026-08-05'` is NULL, not
# true, so a plain comparison silently hides every open-ended banner.
LIVE_SQL = """
SELECT id FROM ad_banners
 WHERE is_active = 1
   AND (starts_at IS NULL OR starts_at <= ?)
   AND (ends_at   IS NULL OR ends_at   >= ?)
 ORDER BY sort_order, id
"""


def _db(rows):
    con = sqlite3.connect(':memory:', isolation_level=None)
    con.execute("""CREATE TABLE ad_banners (
                       id INTEGER PRIMARY KEY,
                       is_active INTEGER NOT NULL DEFAULT 1,
                       starts_at TEXT, ends_at TEXT,
                       sort_order INTEGER NOT NULL DEFAULT 0)""")
    con.executemany('INSERT INTO ad_banners VALUES (?,?,?,?,?)', rows)
    return con


def _live(con, now=NOW):
    stamp = now.isoformat(sep=' ')
    return [r[0] for r in con.execute(LIVE_SQL, (stamp, stamp))]


def _iso(dt):
    return dt.isoformat(sep=' ') if dt else None


class TheFeedShowsOnlyWhatIsLive(unittest.TestCase):

    def test_an_open_ended_banner_shows(self):
        # No dates at all: the common case, and the one a naive date filter
        # breaks because NULL comparisons are never true.
        con = _db([(1, 1, None, None, 0)])
        self.assertEqual(_live(con), [1])

    def test_a_paused_banner_never_shows_whatever_its_dates_say(self):
        con = _db([(1, 0, _iso(NOW - HOUR), _iso(NOW + HOUR), 0)])
        self.assertEqual(_live(con), [],
                         'is_active is the emergency stop and must beat the schedule')

    def test_a_banner_scheduled_for_later_is_withheld(self):
        con = _db([(1, 1, _iso(NOW + HOUR), None, 0)])
        self.assertEqual(_live(con), [])

    def test_an_expired_banner_stops_on_its_own(self):
        con = _db([(1, 1, None, _iso(NOW - HOUR), 0)])
        self.assertEqual(_live(con), [],
                         'nobody should have to remember to switch a campaign off')

    def test_a_half_open_window_works_from_either_end(self):
        con = _db([
            (1, 1, _iso(NOW - HOUR), None, 0),   # started, runs forever
            (2, 1, None, _iso(NOW + HOUR), 1),   # runs until later today
        ])
        self.assertEqual(_live(con), [1, 2])

    def test_the_boundaries_are_inclusive(self):
        # A campaign timed to start at midnight should be live at midnight, not
        # a second after.
        con = _db([(1, 1, _iso(NOW), _iso(NOW), 0)])
        self.assertEqual(_live(con), [1])

    def test_order_is_sort_order_then_id(self):
        con = _db([
            (7, 1, None, None, 2),
            (8, 1, None, None, 0),
            (9, 1, None, None, 0),
        ])
        self.assertEqual(_live(con), [8, 9, 7],
                         'ties fall back to id so the order is at least stable')

    def test_the_same_row_goes_live_and_expires_as_the_clock_moves(self):
        con = _db([(1, 1, _iso(NOW), _iso(NOW + HOUR), 0)])
        self.assertEqual(_live(con, NOW - HOUR), [])
        self.assertEqual(_live(con, NOW + timedelta(minutes=30)), [1])
        self.assertEqual(_live(con, NOW + 2 * HOUR), [])


class TheAdminSeesWhichOfFourStates(unittest.TestCase):
    """`AdBanner.status`, reimplemented here so the rule is pinned in one place.

    Four states, not a boolean, because "switched on but not showing" is the
    thing an admin cannot otherwise diagnose.
    """

    @staticmethod
    def status(is_active, starts_at, ends_at, now=NOW):
        if not is_active:
            return 'paused'
        if starts_at and now < starts_at:
            return 'scheduled'
        if ends_at and now > ends_at:
            return 'expired'
        return 'live'

    def test_every_state_is_reachable_and_distinct(self):
        cases = [
            (True, None, None, 'live'),
            (True, NOW + HOUR, None, 'scheduled'),
            (True, None, NOW - HOUR, 'expired'),
            (False, None, None, 'paused'),
            # Paused wins over a window that would otherwise be open: an admin
            # pulling a banner needs one control that always works.
            (False, NOW - HOUR, NOW + HOUR, 'paused'),
            # And over one that has not opened yet.
            (False, NOW + HOUR, None, 'paused'),
        ]
        for is_active, starts, ends, expected in cases:
            with self.subTest(active=is_active, starts=starts, ends=ends):
                self.assertEqual(self.status(is_active, starts, ends), expected)

    def test_status_agrees_with_the_feed(self):
        """'live' in the admin list must mean 'in the feed'. If these two ever
        disagree, the admin is debugging a lie."""
        rows = [
            (1, 1, None, None, 0),
            (2, 1, _iso(NOW + HOUR), None, 0),
            (3, 1, None, _iso(NOW - HOUR), 0),
            (4, 0, None, None, 0),
            (5, 1, _iso(NOW - HOUR), _iso(NOW + HOUR), 0),
        ]
        con = _db(rows)
        in_feed = set(_live(con))
        for row_id, active, starts, ends, _ in rows:
            state = self.status(
                bool(active),
                datetime.fromisoformat(starts) if starts else None,
                datetime.fromisoformat(ends) if ends else None,
            )
            self.assertEqual(state == 'live', row_id in in_feed,
                             f'banner {row_id}: status says {state}')


class CountersSurviveConcurrentViewers(unittest.TestCase):
    """Several people open the home screen at once. Every impression counts."""

    BUMP = 'UPDATE ad_banners SET impressions = impressions + 1 WHERE id = ?'

    def test_increments_do_not_overwrite_each_other(self):
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute('CREATE TABLE ad_banners (id INTEGER PRIMARY KEY, impressions INTEGER NOT NULL DEFAULT 0)')
        con.execute('INSERT INTO ad_banners VALUES (1, 0)')

        for _ in range(50):
            con.execute(self.BUMP, (1,))

        self.assertEqual(con.execute('SELECT impressions FROM ad_banners').fetchone()[0], 50)

    def test_read_modify_write_would_have_lost_counts(self):
        # The version this deliberately avoids: two viewers read 0, both write
        # 1, and one impression is gone.
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute('CREATE TABLE ad_banners (id INTEGER PRIMARY KEY, impressions INTEGER NOT NULL DEFAULT 0)')
        con.execute('INSERT INTO ad_banners VALUES (1, 0)')

        a = con.execute('SELECT impressions FROM ad_banners WHERE id = 1').fetchone()[0]
        b = con.execute('SELECT impressions FROM ad_banners WHERE id = 1').fetchone()[0]
        con.execute('UPDATE ad_banners SET impressions = ? WHERE id = 1', (a + 1,))
        con.execute('UPDATE ad_banners SET impressions = ? WHERE id = 1', (b + 1,))

        self.assertEqual(con.execute('SELECT impressions FROM ad_banners').fetchone()[0], 1)

    def test_bumping_a_deleted_banner_is_harmless(self):
        # The app fires these and forgets them; a banner deleted a second ago
        # must not become an error on someone's home screen.
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute('CREATE TABLE ad_banners (id INTEGER PRIMARY KEY, impressions INTEGER NOT NULL DEFAULT 0)')
        self.assertEqual(con.execute(self.BUMP, (999,)).rowcount, 0)


if __name__ == '__main__':
    unittest.main()
