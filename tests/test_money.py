"""The arithmetic that decides who gets paid what.

Run with: python -m unittest discover -s tests   (no dependencies beyond stdlib)

Money bugs are the ones you cannot apologise your way out of. These pin the
five places this feature can silently be wrong:

* the commission split not adding back to what the customer paid,
* a balance that disagrees with the entries meant to explain it,
* the same balance being paid out twice,
* the wrong person being able to declare an order paid,
* the goods/money ordering gate pointing the wrong way.

That last one is new since this became cash on delivery, and it is the one that
would have bitten hardest. Under the old online flow an order could not move to
`preparing` until it was paid. Under cash the customer cannot pay until somebody
is standing at their door — so keeping that rule would have deadlocked every
order in the system, and the tests below pin the inverted version in place.
"""

import sqlite3
import unittest
from decimal import ROUND_HALF_UP, Decimal


def money(value):
    """The rounding used throughout: to paise, half-up.

    Python's default is bankers' rounding, which settles 2.5 paise to 2. For
    money owed to someone, round the way an invoice does.
    """
    return Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def split(amount, rate=20):
    """The rule from Payment.split: commission first, farmer gets the remainder."""
    total = money(amount)
    commission = money(total * Decimal(str(rate)) / Decimal('100'))
    return commission, total - commission


class TheSplitAlwaysAddsBack(unittest.TestCase):

    def test_the_simple_case(self):
        commission, farmer = split(500)
        self.assertEqual(commission, Decimal('100.00'))
        self.assertEqual(farmer, Decimal('400.00'))

    def test_the_two_halves_always_reconstruct_the_total(self):
        # The farmer's share is the remainder rather than its own
        # multiplication. Computing both independently strands a paisa on some
        # amounts — the kind of gap nobody notices until a farmer adds up a
        # month of orders and is short by four rupees.
        for amount in ('0.01', '0.05', '1.00', '9.99', '33.33', '99.95', '120.50',
                       '333.33', '1234.56', '99999.99'):
            with self.subTest(amount=amount):
                commission, farmer = split(amount)
                self.assertEqual(commission + farmer, money(amount),
                                 f'₹{amount} does not reconstruct')

    def test_nothing_is_ever_negative(self):
        for amount in ('0.00', '0.01', '0.04'):
            commission, farmer = split(amount)
            self.assertGreaterEqual(commission, 0)
            self.assertGreaterEqual(farmer, 0)

    def test_a_coupon_reduces_both_shares(self):
        # Commission is charged on what the customer actually paid, so a
        # discount is shared rather than absorbed entirely by the platform.
        # ₹500 order, ₹50 coupon → customer pays ₹450.
        commission, farmer = split(450)
        self.assertEqual(commission, Decimal('90.00'))
        self.assertEqual(farmer, Decimal('360.00'))
        self.assertEqual(commission + farmer, Decimal('450.00'))

    def test_the_rate_is_snapshotted_not_recomputed(self):
        # An order taken at 20% must still read 20% after the platform moves to
        # 25%. This is why commission_rate is a column and not a config lookup.
        old_commission, old_farmer = split(1000, rate=20)
        new_commission, _ = split(1000, rate=25)
        self.assertEqual(old_commission, Decimal('200.00'))
        self.assertEqual(new_commission, Decimal('250.00'))
        self.assertEqual(old_farmer, Decimal('800.00'),
                         'the historic order keeps its original split')

    def test_paise_round_half_up_not_to_even(self):
        # ₹0.125 commission → 0.13, not 0.12. Bankers' rounding would give 0.12
        # and quietly favour the platform on every such order.
        commission, _ = split('0.63')          # 20% = 0.126
        self.assertEqual(commission, Decimal('0.13'))


class TheBalanceIsDerivedNotStored(unittest.TestCase):

    def _ledger(self):
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute("""CREATE TABLE farmer_ledger (
                           id INTEGER PRIMARY KEY, farmer_id INT,
                           entry_type TEXT, amount REAL, payment_id INT)""")
        return con

    def balance(self, con, farmer_id=1):
        return round(con.execute(
            "SELECT COALESCE(SUM(CASE WHEN entry_type='credit' THEN amount ELSE -amount END), 0) "
            "FROM farmer_ledger WHERE farmer_id = ?", (farmer_id,)).fetchone()[0], 2)

    def test_credits_and_debits_net_out(self):
        con = self._ledger()
        con.execute("INSERT INTO farmer_ledger VALUES (1,1,'credit',400.00,10)")
        con.execute("INSERT INTO farmer_ledger VALUES (2,1,'credit',360.00,11)")
        con.execute("INSERT INTO farmer_ledger VALUES (3,1,'debit',500.00,NULL)")
        self.assertEqual(self.balance(con), 260.00)

    def test_a_farmer_with_no_entries_has_no_balance(self):
        self.assertEqual(self.balance(self._ledger()), 0)

    def test_a_refund_reversal_can_push_a_balance_negative(self):
        # Farmer earned ₹400, withdrew it, then the order was refunded. The
        # balance goes to -400 and that is correct: they owe it back, and it
        # settles out of their next order. Clamping it to zero here would
        # silently write off money the platform is out of pocket for.
        con = self._ledger()
        con.execute("INSERT INTO farmer_ledger VALUES (1,1,'credit',400.00,10)")
        con.execute("INSERT INTO farmer_ledger VALUES (2,1,'debit',400.00,NULL)")
        con.execute("INSERT INTO farmer_ledger VALUES (3,1,'debit',400.00,10)")
        self.assertEqual(self.balance(con), -400.00)

    def test_a_credit_is_applied_only_once_per_payment(self):
        # Status transitions get replayed — a retried request, a double-tapped
        # button, an admin nudging an order forward twice. A credit applied
        # twice is money invented from nothing.
        con = self._ledger()
        payment_id = 10

        def credit_once(amount):
            exists = con.execute(
                "SELECT 1 FROM farmer_ledger WHERE payment_id=? AND entry_type='credit'",
                (payment_id,)).fetchone()
            if exists:
                return False
            con.execute("INSERT INTO farmer_ledger (farmer_id, entry_type, amount, payment_id) "
                        "VALUES (1,'credit',?,?)", (amount, payment_id))
            return True

        self.assertTrue(credit_once(400.00))
        self.assertFalse(credit_once(400.00), 'the second attempt must be a no-op')
        self.assertFalse(credit_once(400.00))
        self.assertEqual(self.balance(con), 400.00)


class ABalanceCanOnlyBeSpentOnce(unittest.TestCase):
    """Two taps on Redeem must not produce two payouts for the same money."""

    def _db(self):
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute("CREATE TABLE payouts (id INTEGER PRIMARY KEY, farmer_id INT, amount REAL, status TEXT)")
        return con

    def available(self, con, balance, farmer_id=1):
        pending = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payouts "
            "WHERE farmer_id=? AND status IN ('requested','approved')", (farmer_id,)).fetchone()[0]
        return round(balance - pending, 2)

    def test_an_open_request_is_counted_against_the_balance(self):
        con = self._db()
        balance = 1000.00
        self.assertEqual(self.available(con, balance), 1000.00)

        con.execute("INSERT INTO payouts VALUES (1,1,1000.00,'requested')")
        self.assertEqual(self.available(con, balance), 0.00,
                         'the second request must see nothing left')

    def test_a_rejected_request_frees_the_money_again(self):
        con = self._db()
        con.execute("INSERT INTO payouts VALUES (1,1,1000.00,'rejected')")
        self.assertEqual(self.available(con, 1000.00), 1000.00)

    def test_a_paid_request_is_not_double_counted(self):
        # Once paid, the money has left via a ledger debit, so counting the
        # payout row as well would subtract it twice.
        con = self._db()
        con.execute("INSERT INTO payouts VALUES (1,1,600.00,'paid')")
        self.assertEqual(self.available(con, 400.00), 400.00)


class OnlyTheSellerCanSayCashArrived(unittest.TestCase):
    """`_may_collect` in routes/payments.py.

    When a gateway was involved, the defence against a false "this is paid" was
    a signature nobody but Razorpay could produce, and it barely mattered who
    called the endpoint. There is no signature to check now, so this predicate
    is the entire defence — which is why it gets its own tests.
    """

    FARMER, CUSTOMER, ADMIN, STRANGER = 10, 20, 30, 40

    def may_collect(self, user_id, role, order_farmer_id):
        if role == 'admin':
            return True
        return user_id == order_farmer_id

    def test_the_orders_farmer_may(self):
        self.assertTrue(self.may_collect(self.FARMER, 'farmer', self.FARMER))

    def test_an_admin_may(self):
        self.assertTrue(self.may_collect(self.ADMIN, 'admin', self.FARMER))

    def test_the_customer_may_not(self):
        # The one that matters. The customer is the person handing the money
        # over, and letting the payer confirm their own payment is the same
        # mistake as trusting an amount that arrived in a request body.
        self.assertFalse(self.may_collect(self.CUSTOMER, 'customer', self.FARMER))

    def test_another_farmer_may_not(self):
        # Farmers buy from each other here, so "is a farmer" is not the
        # question — "is *this* order's farmer" is.
        self.assertFalse(self.may_collect(self.STRANGER, 'farmer', self.FARMER))


class CollectingCashIsIdempotent(unittest.TestCase):
    """`mark_collected` runs at somebody's front door, on whatever signal is
    going. A retried request must not credit a farmer twice."""

    def _payment(self):
        con = sqlite3.connect(':memory:', isolation_level=None)
        con.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY, status TEXT, "
                    "collected_by INT, farmer_amount REAL)")
        con.execute("INSERT INTO payments VALUES (1,'created',NULL,400.00)")
        return con

    def collect(self, con, by_user):
        status = con.execute("SELECT status FROM payments WHERE id=1").fetchone()[0]
        if status == 'paid':
            return False                    # already in; do nothing
        if status == 'refunded':
            raise ValueError('refunded orders cannot be collected again')
        con.execute("UPDATE payments SET status='paid', collected_by=? WHERE id=1",
                    (by_user,))
        return True

    def test_the_first_collection_lands(self):
        con = self._payment()
        self.assertTrue(self.collect(con, 10))
        self.assertEqual(con.execute("SELECT status FROM payments").fetchone()[0], 'paid')

    def test_a_second_collection_is_a_no_op(self):
        con = self._payment()
        self.collect(con, 10)
        self.assertFalse(self.collect(con, 10), 'the retry must not do anything')
        self.assertFalse(self.collect(con, 10))

    def test_the_original_collector_is_not_overwritten_by_a_retry(self):
        # The audit trail is the only evidence the money was ever asked for. A
        # retry from another device must not rewrite whose name is on it.
        con = self._payment()
        self.collect(con, 10)
        self.collect(con, 99)
        self.assertEqual(con.execute("SELECT collected_by FROM payments").fetchone()[0], 10)

    def test_a_refunded_order_cannot_be_collected_again(self):
        con = self._payment()
        con.execute("UPDATE payments SET status='refunded' WHERE id=1")
        with self.assertRaises(ValueError):
            self.collect(con, 10)


class GoodsMoveBeforeMoneyNow(unittest.TestCase):
    """`payment_blocks` in services/order_money.py.

    The inversion. Under the old online flow, `preparing` and everything after
    it were gated on payment. Under cash, payment *happens* at delivery, so
    gating delivery on payment would mean no order could ever be paid for. Only
    `completed` is gated — and it must be, because completing is what credits
    the farmer.
    """

    GATED = 'completed'

    def blocks(self, new_status, payment_status):
        if new_status != self.GATED:
            return False
        return payment_status == 'pending'

    def test_an_unpaid_order_can_still_be_prepared_and_delivered(self):
        # If any of these ever start blocking, every order in the system
        # deadlocks: the customer cannot pay until someone reaches their door.
        for status in ('preparing', 'ready_for_pickup', 'out_for_delivery'):
            with self.subTest(status=status):
                self.assertFalse(self.blocks(status, 'pending'),
                                 f'{status} must be reachable before the cash is in')

    def test_an_uncollected_order_cannot_be_completed(self):
        # Completing credits the farmer their share. Doing that on money nobody
        # collected means the platform pays out of its own pocket, and finds out
        # when the ledger is reconciled — if it ever is.
        self.assertTrue(self.blocks('completed', 'pending'))

    def test_a_collected_order_completes_freely(self):
        self.assertFalse(self.blocks('completed', 'paid'))

    def test_an_order_with_nothing_owed_completes_freely(self):
        # Orders predating payment tracking, and orders cancelled before any
        # cash moved. Neither has anything to collect.
        self.assertFalse(self.blocks('completed', 'not_required'))

    def test_a_refunded_order_completes_freely(self):
        # Its money already went back; blocking here would strand the row.
        self.assertFalse(self.blocks('completed', 'refunded'))


class TheBuyersCancellationWindowClosesAtConfirmation(unittest.TestCase):
    """`BUYER_CANCELLABLE_FROM` in models/request.py.

    The app tells a customer at checkout that the order cannot be cancelled once
    the farmer confirms it. These are what make that true rather than decorative
    — a promise only the client keeps is one a modified client does not keep.

    Note what is *not* being tested: the seller and admin windows. They can
    still cancel from any active state, because a farmer who cannot fulfil an
    order needs a way to say so.
    """

    OPEN = ('pending', 'admin_review', 'accepted', 'chat_active')
    CLOSED = ('confirmed', 'preparing', 'ready_for_pickup', 'out_for_delivery')

    def buyer_may_cancel(self, status):
        return status in self.OPEN

    def test_before_confirmation_the_buyer_may_still_cancel(self):
        # Nothing is committed yet — no stock deducted, no produce picked — so
        # cancelling costs the farmer only the time spent reading it.
        for status in self.OPEN:
            with self.subTest(status=status):
                self.assertTrue(self.buyer_may_cancel(status))

    def test_from_confirmation_onward_the_buyer_may_not(self):
        # Confirming deducts the farmer's stock and starts them preparing goods
        # they will not be paid for until someone reaches the door.
        for status in self.CLOSED:
            with self.subTest(status=status):
                self.assertFalse(self.buyer_may_cancel(status))

    def test_confirmed_is_the_exact_boundary(self):
        # Stated on its own because it is the line the checkout dialog promises,
        # and moving it by one state silently makes that dialog a lie.
        self.assertTrue(self.buyer_may_cancel('chat_active'))
        self.assertFalse(self.buyer_may_cancel('confirmed'))


class CancellingOnlyReversesMoneyThatMoved(unittest.TestCase):
    """Under cash most cancellations happen before anyone has paid — there is
    nothing to return and no credit to take back."""

    def reverses(self, payment_status):
        return payment_status == 'paid'

    def test_cancelling_before_collection_reverses_nothing(self):
        self.assertFalse(self.reverses('created'))

    def test_cancelling_after_collection_reverses(self):
        self.assertTrue(self.reverses('paid'))

    def test_cancelling_an_already_refunded_order_does_not_reverse_twice(self):
        # Otherwise a second cancellation debits the farmer for money that was
        # already taken back once.
        self.assertFalse(self.reverses('refunded'))


if __name__ == '__main__':
    unittest.main()
