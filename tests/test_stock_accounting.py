"""What stops a farmer's 5kg being sold twice.

Run with: python -m unittest discover -s tests   (no dependencies beyond stdlib)

These tests exercise the *SQL contract* that `app/services/stock_service.py`
depends on, against a real database engine, rather than mocking it out. The
engine here is sqlite because it is in the standard library; the statements
under test are plain SQL and behave identically on MySQL, which is what the
deduction's correctness actually rests on.

The bug they exist to prevent: a farmer lists 5kg, one customer's order is
confirmed for 3kg, and a second order for 3kg is confirmed too. Before this,
purchase requests never decremented stock at all, and the two paths that did
decrement checked the quantity in Python and wrote the result back — which two
concurrent transactions can both pass.
"""

import sqlite3
import unittest


def _db():
    con = sqlite3.connect(':memory:', isolation_level=None)
    con.execute("""CREATE TABLE products (
                       id INTEGER PRIMARY KEY,
                       name TEXT,
                       available_quantity REAL NOT NULL)""")
    con.execute("INSERT INTO products (id, name, available_quantity) VALUES (1, 'Tomatoes', 5.0)")
    return con


def _stock(con):
    return con.execute('SELECT available_quantity FROM products WHERE id = 1').fetchone()[0]


# The statement stock_service.commit() issues. The condition is inside the
# UPDATE rather than in a preceding SELECT, which is the entire point.
COMMIT = 'UPDATE products SET available_quantity = available_quantity - ? ' \
         'WHERE id = ? AND available_quantity >= ?'
RESTORE = 'UPDATE products SET available_quantity = available_quantity + ? WHERE id = ?'


class TheOldWayOversells(unittest.TestCase):
    """A read, a check in Python, then a write — the pattern that was there."""

    def test_two_confirmations_both_pass_the_check(self):
        con = _db()
        # Both farmers' confirmations read the stock before either writes,
        # which is all it takes.
        seen_by_first = _stock(con)
        seen_by_second = _stock(con)

        self.assertGreaterEqual(seen_by_first, 3.0)
        self.assertGreaterEqual(seen_by_second, 3.0)   # both checks pass

        con.execute('UPDATE products SET available_quantity = ? WHERE id = 1', (seen_by_first - 3,))
        con.execute('UPDATE products SET available_quantity = ? WHERE id = 1', (seen_by_second - 3,))

        # 6kg sold out of 5, and the number left even looks plausible.
        self.assertEqual(_stock(con), 2.0)


class TheNewWayRefuses(unittest.TestCase):

    def test_the_second_confirmation_is_refused(self):
        con = _db()
        first = con.execute(COMMIT, (3.0, 1, 3.0))
        self.assertEqual(first.rowcount, 1, 'the first 3kg should come off')
        self.assertEqual(_stock(con), 2.0)

        second = con.execute(COMMIT, (3.0, 1, 3.0))
        self.assertEqual(second.rowcount, 0, 'the second 3kg must be refused')
        self.assertEqual(_stock(con), 2.0, 'and must not have moved the stock')

    def test_stock_never_goes_negative_however_many_try(self):
        con = _db()
        taken = sum(con.execute(COMMIT, (2.0, 1, 2.0)).rowcount for _ in range(10))
        self.assertEqual(taken, 2, 'only two 2kg orders fit in 5kg')
        self.assertEqual(_stock(con), 1.0)
        self.assertGreaterEqual(_stock(con), 0)

    def test_exactly_the_remaining_amount_still_fits(self):
        con = _db()
        self.assertEqual(con.execute(COMMIT, (5.0, 1, 5.0)).rowcount, 1)
        self.assertEqual(_stock(con), 0.0)
        # …and nothing more.
        self.assertEqual(con.execute(COMMIT, (0.5, 1, 0.5)).rowcount, 0)

    def test_concurrent_writers_are_serialised_not_interleaved(self):
        # Two connections, each in its own transaction, both trying to take 3
        # of 5. The second must see the first's committed value, not the value
        # it read when it started.
        path = 'file:stocktest?mode=memory&cache=shared'
        keep = sqlite3.connect(path, uri=True)
        keep.execute('CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, available_quantity REAL NOT NULL)')
        keep.execute("INSERT INTO products VALUES (1, 'Tomatoes', 5.0)")
        keep.commit()

        a = sqlite3.connect(path, uri=True)
        b = sqlite3.connect(path, uri=True)
        try:
            a.execute('BEGIN IMMEDIATE')
            self.assertEqual(a.execute(COMMIT, (3.0, 1, 3.0)).rowcount, 1)
            a.commit()

            b.execute('BEGIN IMMEDIATE')
            self.assertEqual(b.execute(COMMIT, (3.0, 1, 3.0)).rowcount, 0,
                             'the second transaction must re-read the committed 2.0')
            b.commit()

            self.assertEqual(keep.execute('SELECT available_quantity FROM products WHERE id=1')
                             .fetchone()[0], 2.0)
        finally:
            a.close(); b.close(); keep.close()


class CancellingGivesItBack(unittest.TestCase):

    def test_a_confirmed_then_cancelled_order_returns_its_stock(self):
        con = _db()
        con.execute(COMMIT, (3.0, 1, 3.0))
        self.assertEqual(_stock(con), 2.0)

        con.execute(RESTORE, (3.0, 1))
        self.assertEqual(_stock(con), 5.0, 'cancelling a confirmed order restores it')

        # And the 3kg is now available to somebody else.
        self.assertEqual(con.execute(COMMIT, (3.0, 1, 3.0)).rowcount, 1)

    def test_restore_is_not_capped_by_the_original_quantity(self):
        # The farmer may have restocked in the meantime; giving back 3 must add
        # 3, not clamp to whatever the listing was when the order was placed.
        con = _db()
        con.execute(COMMIT, (5.0, 1, 5.0))
        con.execute(RESTORE, (2.0, 1))          # farmer harvests more
        con.execute(RESTORE, (5.0, 1))          # order cancelled
        self.assertEqual(_stock(con), 7.0)


class MultiItemOrdersAreAllOrNothing(unittest.TestCase):
    """A family pack short on one product must not take the others."""

    def _pack_db(self):
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute('CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, available_quantity REAL NOT NULL)')
        con.executemany('INSERT INTO products VALUES (?,?,?)',
                        [(1, 'Tomatoes', 5.0), (2, 'Onions', 5.0), (3, 'Carrots', 1.0)])
        return con

    def test_a_shortfall_on_one_item_puts_the_others_back(self):
        con = self._pack_db()
        wanted = [(1, 2.0), (2, 2.0), (3, 2.0)]   # carrots only have 1kg

        taken = []
        failed = False
        for pid, qty in wanted:
            if con.execute(COMMIT, (qty, pid, qty)).rowcount == 1:
                taken.append((pid, qty))
            else:
                failed = True
                break
        if failed:
            for pid, qty in taken:
                con.execute(RESTORE, (qty, pid))

        self.assertTrue(failed)
        rows = dict(con.execute('SELECT id, available_quantity FROM products').fetchall())
        self.assertEqual(rows, {1: 5.0, 2: 5.0, 3: 1.0},
                         'nothing should have left the shelf')


if __name__ == '__main__':
    unittest.main()
