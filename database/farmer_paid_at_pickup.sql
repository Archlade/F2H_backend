-- Farmers are paid in cash when F2H collects the produce, not from a wallet.
--
-- Before this, the platform held a farmer's earnings in a ledger and the farmer
-- redeemed a balance by UPI or bank transfer. Now F2H hands over cash at the
-- farm gate at stock pickup, so the payment to the farmer happens *before* the
-- payment from the customer rather than long after it.
--
-- Two changes:
--   1. A `picked_up` status, so the moment of collection is a real event that
--      can be timestamped and reported on, rather than being inferred.
--   2. Columns on `payments` recording what was handed over, when, and by whom.
--
-- Safe to run on a live database. Both statements are additive: the enum gains
-- a value and no existing row changes, and the new columns are nullable. Rows
-- created before this migration keep NULL in `farmer_paid_at`, which every
-- screen reads as "not paid at pickup" — correct for orders settled the old way.
--
-- MySQL rewrites the table for an ENUM change, so on a large `purchase_requests`
-- this takes a lock. Run it during a quiet period.
--
--   mysql -u f2h -p f2h < database/farmer_paid_at_pickup.sql

-- ── 1. The new status ────────────────────────────────────────────────────────
-- Order matters: `picked_up` sits between `preparing` and `ready_for_pickup` so
-- the enum's natural ordering still matches the lifecycle. MySQL sorts ENUMs by
-- declaration order, so appending it at the end would sort a picked-up order
-- after a completed one in any ORDER BY status.

ALTER TABLE purchase_requests
  MODIFY COLUMN status ENUM(
    'pending', 'admin_review', 'accepted', 'rejected', 'chat_active',
    'confirmed', 'preparing', 'picked_up', 'ready_for_pickup',
    'out_for_delivery', 'completed', 'cancelled'
  ) NOT NULL DEFAULT 'pending';

ALTER TABLE family_pack_orders
  MODIFY COLUMN status ENUM(
    'pending', 'admin_review', 'accepted', 'rejected', 'chat_active',
    'confirmed', 'preparing', 'picked_up', 'ready_for_pickup',
    'out_for_delivery', 'completed', 'cancelled'
  ) NOT NULL DEFAULT 'pending';

-- ── 2. What the farmer was handed ────────────────────────────────────────────
-- `farmer_paid_amount` is stored rather than derived from `farmer_amount`,
-- because a short pickup or an agreed quality deduction makes them differ, and
-- the amount that actually changed hands is the one worth keeping.

ALTER TABLE payments
  ADD COLUMN farmer_paid_at     DATETIME       NULL AFTER refund_reason,
  ADD COLUMN farmer_paid_amount DECIMAL(10, 2) NULL AFTER farmer_paid_at,
  ADD COLUMN farmer_paid_by     INT            NULL AFTER farmer_paid_amount,
  ADD COLUMN farmer_paid_note   VARCHAR(255)   NULL AFTER farmer_paid_by;

ALTER TABLE payments
  ADD CONSTRAINT fk_payments_farmer_paid_by
    FOREIGN KEY (farmer_paid_by) REFERENCES users(id) ON DELETE SET NULL;

-- Reporting reads "what did we pay out, and what is still owed at pickup",
-- which is a scan over this column filtered by farmer.
CREATE INDEX idx_payments_farmer_paid_at ON payments (farmer_id, farmer_paid_at);

-- ── Not dropped, deliberately ────────────────────────────────────────────────
-- `ledger_entries` and `payouts` are left in place. They hold the real history
-- of every payout made under the old model, and dropping them would destroy the
-- only record that those transfers happened. Nothing writes to them any more.
-- Drop them once you are satisfied that history is no longer needed:
--
--   DROP TABLE payouts;
--   DROP TABLE ledger_entries;
