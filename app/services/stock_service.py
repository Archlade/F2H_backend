"""Moving stock off a farmer's listing, and putting it back.

Every order path in F2H — one-off purchase requests, family pack orders and the
weekly baskets — takes stock from the same `products.available_quantity`
column, so they all go through here. Three things this exists to get right.

**The deduction is atomic.** It is one `UPDATE … WHERE available_quantity >= n`,
not a read followed by a write. Two farmers confirming two orders for the same
product at the same instant used to both read 5kg, both pass the check and both
write 2kg — five kilos sold twice. The condition lives inside the statement, so
the database serialises them: the first takes the row lock, the second re-reads
the committed value and its `WHERE` no longer matches.

**A shortfall is an error, not a shrug.** The old code did
`if available >= wanted:` and skipped the deduction otherwise, or clamped with
`max(0, …)`. Both let an unfulfillable order through and left no trace. Here,
not enough stock raises, the caller's transaction rolls back and the farmer is
told which product is short and by how much.

**Multi-item orders are all or nothing.** A family pack that is short on one
of six products does not get to take the other five off the shelf.

Stock comes out when the *farmer confirms*, not when the customer places the
request — a request is a request, and until the farmer accepts it nothing is
promised. It goes back if a confirmed order is later cancelled, and stays gone
once the order completes.
"""

from ..extensions import db
from ..models.product import Product


class InsufficientStock(ValueError):
    """Not enough of a product to satisfy an order.

    Subclasses ValueError because every route already maps ValueError to a 400
    with the message shown to the user, and this message is written for them.
    """

    def __init__(self, product, wanted, available):
        self.product = product
        self.wanted = wanted
        self.available = available
        name = getattr(product, 'name', 'That product')
        unit = getattr(product, 'unit', '') or ''
        super().__init__(
            f"Not enough {name} left — {_trim(wanted)}{unit} needed, "
            f"{_trim(available)}{unit} in stock."
        )


def _trim(value):
    """3.000 reads badly in a sentence; 3 does."""
    text = f'{float(value):.3f}'.rstrip('0').rstrip('.')
    return text or '0'


def commit(product, quantity):
    """Take `quantity` off `product`, or raise InsufficientStock.

    Does not commit the transaction — the caller owns that, so the deduction
    lands or is rolled back together with the order that caused it.
    """
    quantity = float(quantity)
    if quantity <= 0:
        return

    # The WHERE clause is the whole point: it is evaluated by the database
    # against the latest committed row, not against whatever this session read
    # a moment ago.
    changed = (db.session.query(Product)
               .filter(Product.id == product.id,
                       Product.available_quantity >= quantity)
               .update({Product.available_quantity: Product.available_quantity - quantity},
                       synchronize_session=False))

    if changed != 1:
        # Re-read to report the real figure rather than the stale one this
        # session was holding, which is what made the shortfall confusing.
        db.session.refresh(product)
        raise InsufficientStock(product, quantity, float(product.available_quantity))

    _resync(product)


def restore(product, quantity):
    """Put `quantity` back — a confirmed order that was later cancelled.

    Deliberately unconditional: there is no upper bound to check against, and
    refusing to restore stock would lose a farmer real inventory.
    """
    quantity = float(quantity)
    if quantity <= 0:
        return

    (db.session.query(Product)
     .filter(Product.id == product.id)
     .update({Product.available_quantity: Product.available_quantity + quantity},
             synchronize_session=False))

    _resync(product)


def commit_items(items):
    """Take stock for several products at once, all or nothing.

    `items` is an iterable of `(product, quantity)`. If any one of them is
    short, everything already taken in this call is put back before raising, so
    a half-applied deduction can never survive — not even long enough for the
    caller to mishandle it.
    """
    taken = []
    try:
        for product, quantity in items:
            commit(product, quantity)
            taken.append((product, quantity))
    except InsufficientStock:
        for product, quantity in taken:
            restore(product, quantity)
        raise


def restore_items(items):
    for product, quantity in items:
        restore(product, quantity)


def _resync(product):
    """Refresh the ORM copy and recompute the in_stock / low_stock / out_of_stock label.

    The `UPDATE` above bypasses the identity map by design — the arithmetic has
    to happen in the database — which leaves the object in this session holding
    the old number. Without this refresh the next read in the same request
    reports the pre-deduction quantity, and `stock_status` is computed from it.
    """
    db.session.refresh(product)
    product.update_stock_status()
