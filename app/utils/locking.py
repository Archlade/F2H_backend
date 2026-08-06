"""Row locking for the money and state-machine paths.

Every place that reads a row, decides something from it, and writes it back is a
race unless the read takes a lock. Two requests that both read "balance ₹1000"
before either writes will both act on ₹1000. The fix is `SELECT … FOR UPDATE`:
the first reader locks the row, the second blocks until the first commits, then
re-reads the *committed* value and sees the truth.

On MySQL/InnoDB (production) this is a real lock. On SQLite (dev and the test
suite) `FOR UPDATE` is silently ignored — SQLite serialises writers at the file
level anyway — so the code is correct in both, and the guarantee that matters is
the production one.

Use `lock_row` to start any transaction that will mutate a row whose current
value it must trust.
"""

from ..extensions import db


def lock_row(model, pk):
    """Load a row with `FOR UPDATE`, or None if it does not exist.

    The returned row is locked until the surrounding transaction ends, so a
    concurrent caller cannot read it until this one commits or rolls back.
    """
    if pk is None:
        return None
    return db.session.get(model, pk, with_for_update=True)


def lock_rows(model, column, value):
    """Lock every row of `model` where `column == value`, and return them.

    For serialising on something that is not a primary key — e.g. all of a
    farmer's open payouts, or every ledger row for one account — so two
    requests touching the same owner cannot interleave.
    """
    return (db.session.query(model)
            .filter(column == value)
            .with_for_update()
            .all())
