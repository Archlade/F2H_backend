-- Weekly baskets are built from the whole catalogue and sold by F2H.
--
-- Before this, a basket belonged to one farm: the customer chose a farm first
-- and could only add that farm's products, and the farmer accepted the basket
-- before it started running. That made a weekly basket a standing order with a
-- single grower, which is not what it is meant to be — a household wants
-- tomatoes from whoever has good tomatoes.
--
-- Now: the customer picks anything, an admin approves it, and F2H is the seller
-- of the weekly delivery. Which farms actually supply each item is a sourcing
-- question F2H answers, not a constraint on the customer.
--
--   mysql -u f2h -p f2h < database/basket_sold_by_f2h.sql

-- ── The customer no longer chooses a farm ────────────────────────────────────
-- Nullable rather than dropped. Existing baskets keep the farm they were
-- created against, so nothing in flight loses its history, and the column still
-- answers "who was this originally with" for anyone looking at an old row.
--
-- Everything created from here on leaves it NULL.

ALTER TABLE family_pack_subscriptions
  MODIFY COLUMN farmer_id INT NULL;

-- ── Why a delivery is waiting ────────────────────────────────────────────────
-- A basket spanning several farms will eventually hit an item nobody has that
-- week. Rather than shipping a short basket or skipping the week, the delivery
-- is generated at `admin_review` and held for an admin to substitute. This
-- column is what the admin screen reads to say *which* items are missing —
-- without it the queue shows orders needing attention and no reason why.

ALTER TABLE family_pack_orders
  ADD COLUMN hold_reason VARCHAR(500) NULL AFTER rejection_reason;

-- ── Existing baskets ─────────────────────────────────────────────────────────
-- Left exactly as they are, deliberately. An active basket with a farm attached
-- keeps working: the generator still uses its farmer when one is set, so a
-- customer mid-subscription sees no change and no delivery is interrupted.
-- They convert naturally as customers edit them.
--
-- To move them all to F2H sourcing at once instead, clear the farm:
--
--   UPDATE family_pack_subscriptions SET farmer_id = NULL WHERE status = 'active';
--
-- Do that only once a platform seller account exists and PLATFORM_SELLER_EMAIL
-- is set, or the next generation run will have no seller to sell as.
