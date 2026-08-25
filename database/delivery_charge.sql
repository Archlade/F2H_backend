-- Flat delivery charge, admin-editable.
--
-- Three columns: one on platform_settings holding the figure, and one on each
-- order table recording what was actually charged on that order.
--
-- The order columns are not redundant with the setting. The setting is what
-- *would* be charged today; the order column is what *was* charged then, and
-- those must be allowed to differ — otherwise raising the fee would silently
-- rewrite the total of every order ever placed, including ones already
-- collected in cash. Every money column in these tables works this way.
--
-- Safe to run twice: each statement checks first.
--
--   mysql -u f2h -p f2h_db < database/delivery_charge.sql

-- ── platform_settings.delivery_charge ────────────────────────────────────────
--
-- NULL means "nobody has set this, use the configured default", which is the
-- same convention min_order_value uses. The configured default is 0, so the
-- charge is off until an admin sets a figure — a migration must not start
-- adding money to people's bills on its own.
SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'platform_settings'
      AND COLUMN_NAME = 'delivery_charge') = 0,
  'ALTER TABLE platform_settings ADD COLUMN delivery_charge DECIMAL(10,2) NULL AFTER min_order_value',
  'SELECT "platform_settings.delivery_charge already exists" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── purchase_requests.delivery_charge ────────────────────────────────────────
--
-- NOT NULL DEFAULT 0, so every order that already exists reads as "no delivery
-- charge" — which is true, because none was charged. Included in total_price
-- for new orders; excluded from the farmer's share by payment_service.
SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'purchase_requests'
      AND COLUMN_NAME = 'delivery_charge') = 0,
  'ALTER TABLE purchase_requests ADD COLUMN delivery_charge DECIMAL(10,2) NOT NULL DEFAULT 0 AFTER discount_amount',
  'SELECT "purchase_requests.delivery_charge already exists" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── family_pack_orders.delivery_charge ───────────────────────────────────────
SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'family_pack_orders'
      AND COLUMN_NAME = 'delivery_charge') = 0,
  'ALTER TABLE family_pack_orders ADD COLUMN delivery_charge DECIMAL(10,2) NOT NULL DEFAULT 0 AFTER discount_amount',
  'SELECT "family_pack_orders.delivery_charge already exists" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── Check ────────────────────────────────────────────────────────────────────
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = DATABASE()
   AND COLUMN_NAME = 'delivery_charge'
 ORDER BY TABLE_NAME;

-- Expect three rows. The charge stays 0 everywhere until an admin sets it in
-- Admin → Settings → Delivery charge.
