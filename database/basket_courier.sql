-- A standing courier for a weekly basket.
--
-- A basket goes to the same door every week, so the round is usually the same
-- person's. Assigning one on the subscription means every delivery generated
-- from it arrives already allocated, instead of an admin picking the same name
-- for the same customer every week.
--
-- Any single week can still be reassigned on the order itself — that is what
-- covers illness, holidays and a one-off route change. The subscription only
-- supplies the default at generation time; it is not consulted afterwards, so
-- changing it does not retrospectively move deliveries already created.
--
-- INT UNSIGNED, matching `users.id`. A plain INT is a foreign key that silently
-- fails to create, which is how `platform_settings` went missing for weeks.
--
-- ON DELETE SET NULL, not CASCADE: retiring a delivery account must not delete
-- the customer's standing order along with it.
--
-- Safe to run twice.
--
--   mysql -u f2h -p f2h_db < database/basket_courier.sql

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'family_pack_subscriptions'
      AND COLUMN_NAME = 'assigned_delivery_id') = 0,
  'ALTER TABLE family_pack_subscriptions
     ADD COLUMN assigned_delivery_id INT UNSIGNED NULL AFTER delivery_address_id,
     ADD INDEX idx_fps_assigned_delivery (assigned_delivery_id),
     ADD CONSTRAINT fk_fps_assigned_delivery
       FOREIGN KEY (assigned_delivery_id) REFERENCES users(id) ON DELETE SET NULL',
  'SELECT "family_pack_subscriptions.assigned_delivery_id already exists" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── Check ────────────────────────────────────────────────────────────────────
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME = 'family_pack_subscriptions'
   AND COLUMN_NAME IN ('delivery_address_id', 'assigned_delivery_id');

-- The foreign key must exist. If this returns nothing the column was added but
-- the constraint was not, which means the type does not match users.id.
SELECT CONSTRAINT_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
  FROM information_schema.KEY_COLUMN_USAGE
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME = 'family_pack_subscriptions'
   AND COLUMN_NAME = 'assigned_delivery_id'
   AND REFERENCED_TABLE_NAME IS NOT NULL;

SELECT
  COUNT(*)                              AS baskets,
  SUM(assigned_delivery_id IS NOT NULL) AS with_a_standing_courier
FROM family_pack_subscriptions;
