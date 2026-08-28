-- One route for an order, and cash that completes it.
--
-- The flow an order actually follows:
--
--   pending          customer asked for it
--   confirmed        the farmer agreed to supply it
--   preparing        the farmer has it picked and ready
--   picked_up        F2H collected it and paid the farmer at the gate
--   out_for_delivery the courier is carrying it
--   cash_collected   the customer paid the courier          <- new
--   completed        the courier handed that cash to an admin
--
-- and for a customer collecting from the farm themselves:
--
--   pending → confirmed → preparing → ready_for_pickup → completed
--
-- Two changes, both about who finishes an order.
--
-- `cash_collected` is new. Delivering and settling up used to be the same
-- event: the courier pressed one button and the order was done, while the money
-- was still in their pocket. Splitting them means an order is only closed once
-- the cash has actually reached F2H, and the gap between the two is visible.
--
-- `accepted` and `chat_active` are retired. They came from a flow where a
-- farmer accepted a request and then negotiated in a chat that no longer
-- exists, so both were states an order passed through without anybody doing
-- anything. Live rows are moved to `confirmed`, which is what they meant.
--
-- `admin_review` stays. It is not part of this route: the weekly-basket
-- generator puts a basket there when stock is short, so an admin can substitute
-- items before it goes out. Removing it would let a short basket auto-confirm.
--
-- Safe to run twice.
--
--   mysql -u f2h -p f2h_db < database/order_route.sql

-- ── 1. The new status must exist before anything can be moved into it ────────
--
-- ENUM order is significant to MySQL only for sorting; the list is written in
-- flow order so it reads as the route it describes.
ALTER TABLE purchase_requests
  MODIFY COLUMN status ENUM(
    'pending', 'admin_review', 'accepted', 'rejected', 'chat_active',
    'confirmed', 'preparing', 'picked_up', 'ready_for_pickup',
    'out_for_delivery', 'cash_collected', 'completed', 'cancelled'
  ) DEFAULT 'pending';

ALTER TABLE family_pack_orders
  MODIFY COLUMN status ENUM(
    'pending', 'admin_review', 'accepted', 'rejected', 'chat_active',
    'confirmed', 'preparing', 'picked_up', 'ready_for_pickup',
    'out_for_delivery', 'cash_collected', 'completed', 'cancelled'
  ) DEFAULT 'pending';

-- ── 2. Move live orders out of the retired states ───────────────────────────
--
-- Only the two being retired. `admin_review` is deliberately untouched, and so
-- is anything already finished — a completed or cancelled order is history and
-- must not be rewritten.
UPDATE purchase_requests
   SET status = 'confirmed'
 WHERE status IN ('accepted', 'chat_active');

UPDATE family_pack_orders
   SET status = 'confirmed'
 WHERE status IN ('accepted', 'chat_active');

-- ── Check ───────────────────────────────────────────────────────────────────
SELECT 'purchase_requests' AS table_name, status, COUNT(*) AS rows_
  FROM purchase_requests GROUP BY status
UNION ALL
SELECT 'family_pack_orders', status, COUNT(*)
  FROM family_pack_orders GROUP BY status
 ORDER BY table_name, status;

-- Nothing should be left in either retired state.
SELECT
  (SELECT COUNT(*) FROM purchase_requests  WHERE status IN ('accepted','chat_active')) AS stranded_requests,
  (SELECT COUNT(*) FROM family_pack_orders WHERE status IN ('accepted','chat_active')) AS stranded_baskets;

-- Cash a courier is holding: delivered and paid for, not yet handed over.
SELECT COUNT(*) AS awaiting_handover
  FROM (
    SELECT id FROM purchase_requests  WHERE status = 'cash_collected'
    UNION ALL
    SELECT id FROM family_pack_orders WHERE status = 'cash_collected'
  ) AS x;
