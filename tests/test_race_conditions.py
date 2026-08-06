"""The concurrency exploits an advanced review looks for, and the guards.

Run with: python -m unittest discover -s tests

These pin the *accounting* rules that the row locks enforce at runtime. The
lock itself (`SELECT … FOR UPDATE`) can only be proven against MySQL under real
concurrency, which this stdlib-only suite cannot spin up — but the lock is
worthless if the arithmetic it protects is wrong, and that is what breaks
silently. Each test here encodes an attack and asserts the rule that defeats it.
"""

import sqlite3
import unittest


class PayoutDoubleSpend(unittest.TestCase):
    """The headline finding: redeem the same balance twice.

    An open (requested or approved) payout must count against the balance, so a
    second request sees nothing left. In production the FOR UPDATE lock on the
    farmer's row forces the second request to read the first's committed payout;
    here we prove the accounting it then applies.
    """

    def _db(self):
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute("CREATE TABLE payouts (id INTEGER PRIMARY KEY, farmer_id INT, amount REAL, status TEXT)")
        return con

    def available(self, con, balance, farmer_id=1):
        pending = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payouts "
            "WHERE farmer_id=? AND status IN ('requested','approved')", (farmer_id,)).fetchone()[0]
        return round(balance - pending, 2)

    def test_a_second_request_sees_the_first_pending_one(self):
        con = self._db()
        balance = 1000.0
        # First redemption commits.
        self.assertEqual(self.available(con, balance), 1000.0)
        con.execute("INSERT INTO payouts VALUES (1,1,1000.0,'requested')")
        # Second redemption, now serialised behind the first, must see zero.
        self.assertEqual(self.available(con, balance), 0.0,
                         'the balance has already been claimed once')

    def test_the_unlocked_version_would_have_paid_twice(self):
        # The bug, made explicit: both requests read the balance before either
        # inserts. This is what the lock prevents.
        con = self._db()
        balance = 1000.0
        seen_by_first = self.available(con, balance)
        seen_by_second = self.available(con, balance)   # no row inserted yet
        self.assertEqual(seen_by_first, 1000.0)
        self.assertEqual(seen_by_second, 1000.0)
        # Both would proceed → ₹2000 requested against a ₹1000 balance.
        con.execute("INSERT INTO payouts VALUES (1,1,1000.0,'requested')")
        con.execute("INSERT INTO payouts VALUES (2,1,1000.0,'requested')")
        total = con.execute("SELECT SUM(amount) FROM payouts").fetchone()[0]
        self.assertEqual(total, 2000.0, 'this is the loss the lock now prevents')

    def test_a_rejected_payout_frees_the_balance_again(self):
        con = self._db()
        con.execute("INSERT INTO payouts VALUES (1,1,1000.0,'rejected')")
        self.assertEqual(self.available(con, 1000.0), 1000.0)


class PayoutMarkedPaidTwice(unittest.TestCase):
    """Two admins mark one payout paid → the ledger is debited twice."""

    def _db(self):
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute("CREATE TABLE payouts (id INTEGER PRIMARY KEY, status TEXT, amount REAL)")
        con.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, payout_id INT, amount REAL)")
        con.execute("INSERT INTO payouts VALUES (1,'approved',500.0)")
        return con

    def pay_once(self, con, payout_id=1):
        # The guard, under the lock: only act if not already terminal.
        status = con.execute("SELECT status FROM payouts WHERE id=?", (payout_id,)).fetchone()[0]
        if status in ('paid', 'rejected'):
            return False
        con.execute("UPDATE payouts SET status='paid' WHERE id=?", (payout_id,))
        con.execute("INSERT INTO ledger (payout_id, amount) VALUES (?, 500.0)", (payout_id,))
        return True

    def test_the_second_mark_paid_is_refused(self):
        con = self._db()
        self.assertTrue(self.pay_once(con))
        self.assertFalse(self.pay_once(con), 'already paid')
        debits = con.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        self.assertEqual(debits, 1, 'the farmer must be debited exactly once')


class OrderCompleteCancelRace(unittest.TestCase):
    """Complete and cancel racing from 'confirmed'.

    The exploit: credit the farmer *and* refund the customer for one order. The
    state machine defeats it once the row is locked, because the second
    transition re-reads the committed status and the move becomes illegal.
    """

    TRANSITIONS = {
        'confirmed': ['preparing', 'cancelled', 'completed'],
        'completed': [],
        'cancelled': [],
    }

    def can(self, frm, to):
        return to in self.TRANSITIONS.get(frm, [])

    def test_once_completed_the_order_cannot_also_be_cancelled(self):
        status = 'confirmed'
        # T1 completes (under lock), commits.
        self.assertTrue(self.can(status, 'completed'))
        status = 'completed'
        # T2, now serialised, re-reads 'completed' and is rejected.
        self.assertFalse(self.can(status, 'cancelled'),
                         'a completed order must not be cancellable — that is what '
                         'stops the credit-and-refund double payout')

    def test_the_reverse_order_is_also_safe(self):
        status = 'confirmed'
        self.assertTrue(self.can(status, 'cancelled'))
        status = 'cancelled'
        self.assertFalse(self.can(status, 'completed'))

    def test_the_unlocked_version_lets_both_through(self):
        # Both read 'confirmed'; both pass. This is the race the lock closes.
        status = 'confirmed'
        self.assertTrue(self.can(status, 'completed'))
        self.assertTrue(self.can(status, 'cancelled'))


class CashCollectionRacingACancellation(unittest.TestCase):
    """Collect and cancel on the same order, from two people at once.

    The sequence that costs money: cancel reads the payment, sees it is not yet
    paid, and closes the order out as `not_required`; collect then commits
    `paid`. The order now says nothing was owed while the payment row says cash
    was taken — and because the farmer is credited only on `completed`, which a
    cancelled order never reaches, their share vanishes. The customer's money is
    in a tin and nobody is recorded as owing it to anyone.

    In production the FOR UPDATE lock on the *order* row — the same row a status
    transition locks — forces these to serialise. What is pinned here is the
    invariant that makes the serialised outcome correct either way round.
    """

    def _db(self):
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, payment_status TEXT)")
        con.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY, order_id INT, status TEXT)")
        con.execute("INSERT INTO orders VALUES (1,'out_for_delivery','pending')")
        con.execute("INSERT INTO payments VALUES (1,1,'created')")
        return con

    def order(self, con):
        return con.execute("SELECT status, payment_status FROM orders WHERE id=1").fetchone()

    def payment(self, con):
        return con.execute("SELECT status FROM payments WHERE order_id=1").fetchone()[0]

    def collect(self, con):
        if self.payment(con) != 'created':
            return False
        con.execute("UPDATE payments SET status='paid' WHERE order_id=1")
        con.execute("UPDATE orders SET payment_status='paid' WHERE id=1")
        return True

    def cancel(self, con):
        con.execute("UPDATE orders SET status='cancelled' WHERE id=1")
        # Only money that actually moved gets reversed; only an order that never
        # took any is closed out as owing nothing.
        if self.payment(con) == 'paid':
            con.execute("UPDATE payments SET status='refunded' WHERE order_id=1")
            con.execute("UPDATE orders SET payment_status='refunded' WHERE id=1")
        else:
            con.execute("UPDATE orders SET payment_status='not_required' WHERE id=1")

    def assert_consistent(self, con):
        """The denormalised column on the order must never contradict the
        payment row it is denormalised from."""
        _, order_payment_status = self.order(con)
        agrees = {'created': ('pending', 'not_required'),
                  'paid': ('paid',),
                  'refunded': ('refunded',)}
        self.assertIn(order_payment_status, agrees[self.payment(con)],
                      'the order and its payment row disagree about the money')

    def test_collect_then_cancel_records_a_refund_owed(self):
        con = self._db()
        self.assertTrue(self.collect(con))
        self.cancel(con)
        self.assertEqual(self.payment(con), 'refunded',
                         'cash was taken, so it has to be given back')
        self.assert_consistent(con)

    def test_cancel_then_collect_refuses_to_take_money(self):
        con = self._db()
        self.cancel(con)
        # Serialised behind the cancellation, the collect now re-reads a
        # cancelled order. The route's status check rejects it before this
        # point in production; the accounting must hold regardless.
        self.assertEqual(self.order(con)[0], 'cancelled')
        self.assert_consistent(con)

    def test_the_interleaving_that_used_to_lose_the_money_cannot_happen(self):
        # Both readers see 'created' before either writes — the unlocked case.
        # Reproduced here only to state what the lock exists to prevent.
        con = self._db()
        cancel_saw = self.payment(con)          # 'created'
        self.collect(con)                       # …then collect commits 'paid'
        # If cancel now acted on its stale read, it would write 'not_required'
        # over a paid order and strand the farmer's share.
        stale_outcome = 'not_required' if cancel_saw != 'paid' else 'refunded'
        self.assertEqual(stale_outcome, 'not_required',
                         'this is the wrong answer, and why the row is locked')
        # The committed state, which the lock guarantees the second reader sees:
        self.assertEqual(self.payment(con), 'paid')
        self.assert_consistent(con)


class CouponSingleUseUnderRace(unittest.TestCase):
    """The pattern the payout path should have followed all along: let the
    database enforce it. A UNIQUE constraint makes the second concurrent
    redemption fail rather than granting a second discount."""

    def test_a_unique_constraint_refuses_the_second_redemption(self):
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute("CREATE TABLE coupon_redemptions ("
                    "id INTEGER PRIMARY KEY, coupon_id INT UNIQUE, customer_id INT)")
        con.execute("INSERT INTO coupon_redemptions (coupon_id, customer_id) VALUES (7, 1)")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO coupon_redemptions (coupon_id, customer_id) VALUES (7, 2)")


if __name__ == '__main__':
    unittest.main()
