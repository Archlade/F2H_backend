-- Products a seller made rather than grew.
--
-- Jam, pickle, ghee, honey, baked goods, cold-pressed oil — the things a farm
-- sells that never came out of the ground as-is. It sits beside the flags
-- already on `products`:
--
--   is_organic     certified organic
--   is_natural     nothing added
--   is_farm_grown  grown on the seller's own land
--   is_homemade    made by the seller, not grown          <- new
--
-- A flag rather than a category, because these are not mutually exclusive: a
-- home-made jam can also be organic, and a category would have forced a choice
-- between "Home made" and "Fruits" for the same jar.
--
-- Defaults to 0, which is right for every row already in the table — they are
-- all produce listings, and nobody has been asked this question yet.
--
-- Safe to run twice.
--
--   mysql -u f2h -p f2h_db < database/homemade_products.sql

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'products'
      AND COLUMN_NAME = 'is_homemade') = 0,
  'ALTER TABLE products
     ADD COLUMN is_homemade TINYINT(1) NOT NULL DEFAULT 0 AFTER is_farm_grown,
     ADD INDEX idx_products_is_homemade (is_homemade)',
  'SELECT "products.is_homemade already exists" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── Check ────────────────────────────────────────────────────────────────────
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME = 'products'
   AND COLUMN_NAME IN ('is_organic', 'is_natural', 'is_farm_grown', 'is_homemade')
 ORDER BY ORDINAL_POSITION;

SELECT
  COUNT(*)                  AS total_products,
  SUM(is_organic = 1)       AS organic,
  SUM(is_natural = 1)       AS natural_,
  SUM(is_farm_grown = 1)    AS farm_grown,
  SUM(is_homemade = 1)      AS homemade
FROM products
WHERE deleted_at IS NULL;
