-- A weekly basket has no farm behind it.
--
-- Baskets used to be per-farm: you picked a farm, then picked from what that
-- farm grew, and `family_pack_subscriptions.farmer_id` named the seller. That
-- step is gone — the customer builds from the whole basket catalogue and F2H
-- sources the items — so there is no single farm to record.
--
-- `create_subscription` has set `farmer_id=None` since that change, and every
-- reader already branches on it:
--
--   * the order generator falls back to the platform seller when it is NULL
--   * `get_subscriptions_for_farmer` matches on the items' farms instead
--   * the permission check treats NULL as "no owning farm" and falls through
--     to whether the actor supplies any item
--
-- Only the column definition was left behind, still NOT NULL, so every new
-- basket died on `IntegrityError (1048) Column 'farmer_id' cannot be null`.
--
-- Legacy rows keep whatever farmer they already have. This only permits NULL;
-- it does not clear anything.
--
-- Safe to run twice.
--
--   mysql -u f2h -p f2h_db < database/basket_no_farm.sql

-- The FK to users.id must keep matching users.id exactly. An INT where the
-- referenced column is INT UNSIGNED is a foreign key that silently fails to
-- create, which is how `platform_settings` went missing for weeks.
SET @sql = IF(
  (SELECT IS_NULLABLE FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'family_pack_subscriptions'
      AND COLUMN_NAME = 'farmer_id') = 'NO',
  'ALTER TABLE family_pack_subscriptions
     MODIFY COLUMN farmer_id INT UNSIGNED NULL',
  'SELECT "family_pack_subscriptions.farmer_id already allows NULL" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── Check ────────────────────────────────────────────────────────────────────
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME = 'family_pack_subscriptions'
   AND COLUMN_NAME IN ('customer_id', 'farmer_id');

-- The foreign key must still be there afterwards. MODIFY keeps it, but an
-- earlier type mismatch would show up here as a missing row.
SELECT CONSTRAINT_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
  FROM information_schema.KEY_COLUMN_USAGE
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME = 'family_pack_subscriptions'
   AND COLUMN_NAME = 'farmer_id'
   AND REFERENCED_TABLE_NAME IS NOT NULL;

SELECT
  COUNT(*)                        AS subscriptions,
  SUM(farmer_id IS NULL)          AS f2h_sourced,
  SUM(farmer_id IS NOT NULL)      AS legacy_single_farm
FROM family_pack_subscriptions;
