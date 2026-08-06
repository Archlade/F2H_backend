# Payment, commission and payouts

Payment is **cash on delivery**. The customer pays the person who brings the
produce; F2H collects the money, keeps 20%, and the farmer's 80% lands in their
balance when the order is delivered. They redeem it to UPI or a bank account.

There is no payment gateway, no API keys and no webhook. If you are looking for
the Razorpay integration, it was removed — see
[Migrating from Razorpay](#migrating-from-razorpay) at the bottom.

## Setup

Add to `backend/.env`:

```
PLATFORM_COMMISSION_RATE=20
MIN_PAYOUT_AMOUNT=200
```

Then:

```bash
mysql -u root -p f2h_db < database/payments.sql
```

That is the whole setup. Cash needs no keys, so there is nothing to
misconfigure and nothing to rotate — `/api/health` reports
`"payments": "cash_on_delivery"` and always will.

## Who holds the money

This is the decision everything else follows from, so it is worth being
explicit: **F2H collects the cash at the door and holds it until the farmer
redeems.** The farmer does not keep the money from their own sales.

That keeps the ledger pointing the way it always did — the platform owes the
farmer, and `payouts` is how that debt is settled. Had the farmer kept the cash
instead, everything would invert: the balance would become *commission the
farmer owes F2H*, and "redeem" would become "settle up". If the operating model
ever changes that way, `wallet_service.py` and `routes/payouts.py` are where it
lands.

## How the money flows

```
customer places order
        ↓
farmer confirms          → stock committed, total final, split frozen
                           payments row: status=created
        ↓
farmer prepares/delivers → NOT blocked; this is how payment becomes possible
        ↓
cash collected at door   → payments row: status=paid, collected_by recorded
                           order.payment_status=paid
                           customer gets a receipt, farmer told their share
        ↓
order completed          → BLOCKED until the cash is recorded
                           farmer credited 80% in farmer_ledger
        ↓
farmer requests payout   → admin pays by UPI/bank, records the reference
                           → ledger debited
```

Four decisions in there are load-bearing.

**The amount is read from the order row, never from the request body.** An
amount that arrives from a client is an amount the payer chose. Under cash this
matters *more*, not less: the figure the server produces is the figure a person
reads off a screen at a doorstep and asks for.

**The split is frozen at confirmation.** Commission rate included. Changing
`PLATFORM_COMMISSION_RATE` next month does not rewrite what a farmer was owed
last month — and under cash the farmer may well have the printed slip.

**Only the farmer or an admin can say the cash arrived.** There is no signature
to check because there is no third party to sign anything, so authority is the
whole of the security model. It is enforced in `_may_collect`.

**The farmer is credited on completion, not on collection.** Crediting when the
money is taken would let a farmer bank the proceeds of an order they then never
close out. The platform holds it in the meantime precisely so that cannot
happen.

### Goods move *before* money now

Under the old online flow an order could not reach `preparing` until it was
paid. Under cash that rule would deadlock every order in the system — the
customer cannot pay until somebody is standing at their door, and nobody gets to
their door without the order first moving through `preparing` and
`out_for_delivery`.

So the gate moved to the end. `payment_blocks()` now refuses exactly one
transition: **an order cannot be marked `completed` until the cash is
recorded.** That is the one that matters, because completing is what credits the
farmer. `tests/test_money.py::GoodsMoveBeforeMoneyNow` pins it in place.

## Endpoints

### `GET /api/payments/status/<order_type>/<order_id>`

What the order owes and whether it has been collected. `order_type` is
`request` or `pack-order`. Readable by the customer, the farmer, or an admin.

```json
{
  "order_payment_status": "pending",
  "amount_due": 450.00,
  "payment_method": "cod",
  "payments_available": true,
  "can_collect": true,
  "payment": { "...": "..." }
}
```

`can_collect` is the server telling the app whether *this* caller may record the
cash. The app draws its button from that rather than guessing at authority the
backend is going to check anyway.

### `POST /api/payments/collect`

```json
{ "order_type": "request", "order_id": 42, "note": "optional" }
```

Records that the customer handed cash over. Notice what is **not** in that body:
an amount. The server charges what it froze at confirmation, so a tampered
client cannot record a ₹500 order as ₹5 collected.

Three rules this endpoint enforces:

- **Only the farmer whose order it is, or an admin.** Not the customer — they
  are the one paying, and letting the payer confirm their own payment is the
  same mistake as trusting an amount from a request body.
- **Only once the order has reached the customer** (`ready_for_pickup`,
  `out_for_delivery`, `completed`). Before that there is nobody to take money
  from, and a collect button would invite recording a payment on the strength
  of an intention.
- **Idempotent.** A second call answers 200 with the same payment rather than
  crediting anyone twice. This button gets pressed at somebody's front door on
  whatever signal is going, and a retried request must not cost the platform a
  farmer's share.

On success both sides are notified: the customer gets a receipt they did not
have to ask for — under cash there is no card statement, so that notification is
the only record on their side — and the farmer is told their share.

## Cancellations and refunds

There is no refund API to call. Cancelling does one of two things depending on
whether money actually moved:

- **Never collected** (`payments.status = 'created'`) — nothing to return and no
  credit to reverse. The order closes out as `not_required`.
- **Already collected** (`'paid'`) — the farmer's credit is reversed in the
  ledger and the payment is marked `refunded`. **Somebody has to physically hand
  the money back**; the row only records that this is owed.

The credit is reversed *before* the refund is recorded, deliberately. If
anything downstream fails, the farmer's balance is still correct and an admin
returns the money by hand. The other order leaves a farmer holding a credit for
an order the customer has already been refunded for.

## Commission

`Payment.split()` computes the commission first and gives the farmer the
remainder, rather than multiplying twice:

```python
commission = money(total * rate / 100)
farmer_share = total - commission
```

Computing both independently strands a paisa on some amounts — the kind of gap
nobody notices until a farmer adds up a month of orders and is short by four
rupees. `tests/test_money.py` checks that the two halves reconstruct the total
across a spread of awkward amounts.

Commission is charged on what the customer actually paid, so a coupon discount
is shared rather than absorbed entirely by the platform.

## The audit trail

Under a gateway, a disputed payment could be checked against Razorpay's records.
There is nothing to check against now, so `payments.collected_by` and
`collected_at` **are** the record — the only evidence the money was ever asked
for. Both are written on collection and never overwritten by a retry.

Two queries worth watching in production:

```sql
-- Delivered but never collected. Every row is money someone walked away from,
-- and the farmer cannot close the order until it is resolved.
SELECT id, total_price, updated_at
  FROM purchase_requests
 WHERE status = 'out_for_delivery'
   AND payment_status = 'pending'
   AND updated_at < NOW() - INTERVAL 2 DAY;

-- Refunds owed back to customers in cash.
SELECT id, amount, refunded_at, refund_reason
  FROM payments WHERE status = 'refunded' ORDER BY refunded_at DESC;
```

## Payouts

Unchanged by the move to cash. Farmers see a balance, request it, and an admin
pays by UPI or bank transfer and records the reference. The balance is never
stored — it is always `SUM(credit) - SUM(debit)` over `farmer_ledger`, so the
number and its explanation cannot disagree.

`MIN_PAYOUT_AMOUNT` is the floor below which a bank transfer is not worth the
effort. A balance can only be spent once: the request endpoint re-reads it
inside the transaction that creates the payout and counts already-requested
money against it, so two taps on Redeem cannot produce two payouts.

## Migrating from Razorpay

If your database ran the earlier online-payment version:

```bash
mysqldump -u root -p f2h_db payments > payments-before-cod.sql   # do this first
mysql -u root -p f2h_db < database/cod.sql
pip uninstall razorpay        # optional; nothing imports it any more
```

`cod.sql` is safe to run more than once. It adds the collection columns, drops
the `razorpay_*` ones, and rewrites any payment left at `'failed'` back to
`'created'` — under cash a declined card is simply money still owed, and leaving
those rows behind would strand their orders where nothing could ever collect
from them. Rows at `'paid'` and `'refunded'` are not touched: money that really
moved through the gateway stays exactly as recorded.

Afterwards, delete `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and
`RAZORPAY_WEBHOOK_SECRET` from `.env`, and remove the webhook from the Razorpay
dashboard so it stops posting to an endpoint that no longer exists.
