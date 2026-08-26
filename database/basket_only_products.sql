-- Items that exist only inside a weekly basket.
--
-- An admin creates these; F2H sources them against the baskets actually
-- ordered. They are ordinary `products` rows so that categories, images, the
-- basket builders on both clients and the buying-plan report all keep working
-- — what makes one different is three flags set together:
--
--   basket_eligible = 1   may go in a basket
--   basket_only     = 1   may go nowhere else
--   farmer_id             the platform seller account, not a real farm
--
-- `basket_only` is the new one. It means three things, all the same idea:
--   * hidden from the marketplace listing (product_service excludes it unless
--     the caller asked for basket_eligible);
--   * refused for a one-off purchase request;
--   * exempt from stock checks — available_quantity on one of these is not a
--     number anybody maintains, and stock_service skips it in both directions.
--
-- Safe to run twice.
--
--   mysql -u f2h -p f2h_db < database/basket_only_products.sql

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'products'
      AND COLUMN_NAME = 'basket_only') = 0,
  'ALTER TABLE products
     ADD COLUMN basket_only TINYINT(1) NOT NULL DEFAULT 0 AFTER basket_eligible,
     ADD INDEX idx_products_basket_only (basket_only)',
  'SELECT "products.basket_only already exists" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── Check ────────────────────────────────────────────────────────────────────
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME = 'products'
   AND COLUMN_NAME IN ('basket_eligible', 'basket_only')
 ORDER BY COLUMN_NAME;

-- Every existing product defaults to basket_only = 0, which is correct: they
-- are all farm listings and all still for sale individually.
SELECT
  COUNT(*)                                   AS total_products,
  SUM(basket_eligible = 1)                   AS marked_basket_eligible,
  SUM(basket_only = 1)                       AS basket_only_items
FROM products
WHERE deleted_at IS NULL;


-- ── Optional: take farmers' listings out of weekly baskets ───────────────────
--
-- Run this only if baskets should contain *nothing but* admin-created items.
--
-- Deliberately not part of the migration above. It changes what customers can
-- put in a basket, and any subscription already containing one of these
-- products keeps it — `update_subscription` re-validates on the next edit and
-- would then refuse. Look at what you have first:
--
--   SELECT p.id, p.name, u.email AS farm
--     FROM products p JOIN users u ON u.id = p.farmer_id
--    WHERE p.basket_eligible = 1 AND p.basket_only = 0 AND p.deleted_at IS NULL;
--
-- Then, if that list should no longer be basket produce:
--
--   UPDATE products SET basket_eligible = 0
--    WHERE basket_only = 0 AND deleted_at IS NULL;
