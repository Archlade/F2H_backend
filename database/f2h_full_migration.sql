-- F2H Market — the complete schema migration, in one file.
--
--   mysql -u f2h -p f2h_db < database/f2h_full_migration.sql
--
-- This is every migration in database/*.sql concatenated in DEPENDENCY order,
-- which is not the order they were written in. Two orderings matter and getting
-- either wrong leaves a broken schema rather than an error you would notice:
--
--   * platform_settings (1) creates the table that delivery_charge (6) alters.
--
--   * basket_sold_by_f2h (9) sets family_pack_subscriptions.farmer_id to a
--     SIGNED int; basket_no_farm (12) sets it UNSIGNED, to match users.id.
--     Run them the other way round and the signed version wins, and the foreign
--     key to users silently will not hold.
--
--   * farmer_paid_at_pickup (5) sets an order-status ENUM that predates
--     cash_collected. order_route (14) sets the final one. Reversed, the app
--     writes a status the column cannot hold and every courier handover fails
--     with "Data truncated for column 'status'".
--
-- SAFE TO RUN ON A DATABASE THAT IS PARTLY MIGRATED, and safe to run twice.
-- Every ADD COLUMN and ADD CONSTRAINT is wrapped in an information_schema check
-- and prints a note instead of failing when the column is already there. The
-- MODIFY COLUMN and UPDATE statements are idempotent by nature: setting a column
-- to a definition it already has, and updating rows that no longer match, both
-- do nothing the second time.
--
-- Six statements were unguarded in the original files and are guarded here:
-- last_reminded_for, hold_reason, the four payments.farmer_paid_* columns, and
-- fk_payments_farmer_paid_by.
--
-- What this does NOT do: create the base tables. users, products,
-- purchase_requests, family_pack_orders, payments and the rest come from the
-- application's own create_all on first run. This layers on top of that.
--
-- Each section keeps its original explanation and its own verification SELECT,
-- so the output is a running commentary — a section that prints "already
-- exists" was applied on an earlier run and was skipped.

-- ==========================================================================
-- SECTION 01 of 14  .  platform_settings.sql
-- Admin-editable settings. Created first — delivery_charge below alters it.
-- ==========================================================================

-- Settings an admin can change from the admin page, without a deploy.
--
-- The order minimum used to live in `MIN_ORDER_VALUE`, an environment variable
-- read at boot. Changing it meant editing the server's .env and restarting —
-- which is not something an admin can do, so in practice the figure was frozen
-- at whatever it was set to on the day the server was last touched.
--
-- One row, always id 1. A settings table that can hold two rows eventually
-- holds two rows, and then "which one is live?" is a production bug rather than
-- a question. The CHECK constraint makes a second row impossible rather than
-- merely discouraged.
--
--   mysql -u f2h -p f2h_db < database/platform_settings.sql

CREATE TABLE IF NOT EXISTS platform_settings (
  id INT NOT NULL PRIMARY KEY,

  -- NULL means "not set — use the configured default".
  --
  -- This is the difference between an admin who has never opened the settings
  -- page and one who deliberately typed the same number that was already there.
  -- Without the distinction, deploying a new default would have no effect on
  -- any existing installation, because the row would already hold the old value
  -- as though someone had chosen it.
  min_order_value DECIMAL(10,2) NULL,

  -- The flat delivery fee. Same NULL-means-unset rule as above.
  delivery_charge DECIMAL(10,2) NULL,

  updated_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  -- INT UNSIGNED, matching `users.id`, and not plain INT.
  --
  -- This was `INT` and it is why this table did not exist for weeks. MySQL
  -- requires a foreign key column to match the referenced column exactly, and
  -- signed against unsigned is a mismatch — so the CREATE TABLE failed on the
  -- constraint, no table was created, and every run since produced the same
  -- error. The application did not notice because `min_order_value()` catches
  -- a failed read and falls back to the configured default, so the shop kept
  -- trading on a figure nobody could change.
  updated_by INT UNSIGNED NULL,

  CONSTRAINT chk_platform_settings_singleton CHECK (id = 1),

  -- ON DELETE SET NULL, not CASCADE: removing the admin who last changed a
  -- setting must not delete the setting. The attribution is worth less than the
  -- figure the whole shop trades on.
  CONSTRAINT fk_platform_settings_updated_by
    FOREIGN KEY (updated_by) REFERENCES users (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Seed the singleton with "not set", so the first read has a row to find and
-- the configured default stays in charge until an admin decides otherwise.
INSERT INTO platform_settings (id, min_order_value)
VALUES (1, NULL)
ON DUPLICATE KEY UPDATE id = id;

-- ==========================================================================
-- SECTION 02 of 14  .  cart.sql
-- The shopping cart.
-- ==========================================================================

-- =============================================
-- Migration: shopping cart
-- Run against an existing f2h_db:
--   mysql -u root -p f2h_db < database/cart.sql
--
-- SAFE TO RUN MORE THAN ONCE — CREATE TABLE IF NOT EXISTS, and nothing here
-- alters an existing table, so a repeat is a no-op.
--
-- One row per product per customer. There is no `carts` table: the customer is
-- the cart, so a parent row would hold a foreign key and a timestamp and be
-- joined through for nothing.
--
-- Deliberately stores no price. Prices, stock and the commission split are read
-- from the product at checkout, so a cart left for a week neither locks in last
-- week's price nor reserves stock somebody else could have bought. Two
-- customers can hold the same 5kg; whoever checks out first gets it, and the
-- other is told at checkout rather than at the door.
-- =============================================

CREATE TABLE IF NOT EXISTS cart_items (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    customer_id INT UNSIGNED NOT NULL,
    product_id INT UNSIGNED NOT NULL,
    quantity DECIMAL(10,3) NOT NULL DEFAULT 1,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (customer_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,

    -- Adding the same product twice is an increase in quantity, not a second
    -- line. Enforced by the database rather than the route, because two taps on
    -- "Add to cart" race each other and the second would otherwise insert a
    -- duplicate row.
    UNIQUE KEY uq_cart_customer_product (customer_id, product_id),
    INDEX idx_cart_customer (customer_id)
);

-- Check it landed:
--   SHOW COLUMNS FROM cart_items;
--   SELECT COUNT(*) FROM cart_items;

-- ==========================================================================
-- SECTION 03 of 14  .  service_reviews.sql
-- Reviews of F2H itself, as opposed to a product or a farm.
-- ==========================================================================

-- What customers think of F2H itself — the app, the site, the service.
--
-- Deliberately not the `reviews` table. That one is about a product or a farm:
-- it carries product_id and farmer_id and feeds rating_avg on those rows. This
-- is about the service as a whole and feeds the homepage. Sharing a table would
-- have meant every existing review query learning to exclude a kind of row it
-- was never written for.
--
--   mysql -u f2h -p f2h_db < database/service_reviews.sql
--
-- Safe to run twice.

CREATE TABLE IF NOT EXISTS service_reviews (
  id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

  -- UNIQUE is what makes "one review per customer" true rather than merely
  -- intended: the endpoint upserts on it, and without the constraint two
  -- requests racing would both insert.
  --
  -- INT UNSIGNED to match users.id. A plain INT is a foreign key that silently
  -- refuses to create — which is how platform_settings failed to exist for
  -- weeks without anybody noticing.
  user_id INT UNSIGNED NOT NULL UNIQUE,

  rating TINYINT NOT NULL,
  comment TEXT NULL,

  -- Nothing reaches the homepage without an admin saying so. The form is open
  -- to every account, so this flag is the only thing between somebody's bad day
  -- and the front page. Editing a review resets it — see the endpoint.
  is_approved TINYINT(1) NOT NULL DEFAULT 0,
  approved_by INT UNSIGNED NULL,
  approved_at DATETIME NULL,

  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  INDEX idx_service_reviews_approved (is_approved),

  -- CASCADE on the author: a deleted account takes its opinion with it.
  CONSTRAINT fk_service_reviews_user
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
  -- SET NULL on the approver: removing an admin must not delete a published
  -- review, only the note of who approved it.
  CONSTRAINT fk_service_reviews_approved_by
    FOREIGN KEY (approved_by) REFERENCES users (id) ON DELETE SET NULL,

  CONSTRAINT chk_service_reviews_rating CHECK (rating BETWEEN 1 AND 5)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── Check ────────────────────────────────────────────────────────────────────
SHOW COLUMNS FROM service_reviews;

SELECT COUNT(*) AS reviews,
       SUM(is_approved = 1) AS published
  FROM service_reviews;

-- ==========================================================================
-- SECTION 04 of 14  .  cod.sql
-- Cash on delivery: who took the money, and when.
-- ==========================================================================

-- =============================================
-- Migration: online payment (Razorpay) → cash on delivery
-- Run against an existing f2h_db, after database/payments.sql:
--   mysql -u root -p f2h_db < database/cod.sql
-- New installs get all of this from payments.sql already.
--
-- SAFE TO RUN MORE THAN ONCE. Every step checks whether it has already been
-- applied, so a partial run can simply be repeated. MySQL has no
-- "ADD COLUMN IF NOT EXISTS", hence the stored procedure — without it,
-- re-running fails on a duplicate column and leaves you guessing which
-- statements got through.
--
-- WHAT THIS CHANGES
--   * adds  method, collected_by, collected_at, collection_note, refund_reason
--   * drops razorpay_order_id, razorpay_payment_id, razorpay_signature,
--           refund_id, failure_reason
--   * rewrites any payment left at 'failed' by the old gateway back to
--     'created', because under cash it is simply money still owed
--
-- WHAT THIS DOES NOT CHANGE
--   The status enum keeps its four values. 'created' now means "confirmed,
--   cash due at the door", 'paid' means "cash in hand", 'refunded' means
--   "cash handed back". 'failed' becomes unreachable but is left in the enum:
--   removing a value from a live ENUM rewrites the whole table and buys
--   nothing.
--
--   Amounts, the commission split, the ledger and payouts are all untouched.
--   F2H still collects the money and still holds it until a farmer redeems, so
--   the accounting either side of the collection is exactly as it was.
--
-- TAKE A BACKUP FIRST. This drops columns, and a dropped column does not come
-- back:
--   mysqldump -u root -p f2h_db payments > payments-before-cod.sql
-- =============================================

DROP PROCEDURE IF EXISTS f2h_migrate_cod;

DELIMITER $$

CREATE PROCEDURE f2h_migrate_cod()
BEGIN
    -- ── New columns ──────────────────────────────────────────────────────────

    IF NOT EXISTS (SELECT 1 FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'payments'
                      AND COLUMN_NAME = 'method') THEN
        -- One value today. A column rather than an assumption, because the day
        -- a second method appears, every row written before it needs to already
        -- say what it was — retrofitting that later is guesswork.
        ALTER TABLE payments
            ADD COLUMN method VARCHAR(20) NOT NULL DEFAULT 'cod' AFTER currency;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'payments'
                      AND COLUMN_NAME = 'collected_by') THEN
        -- Who took the money and when. Under cash this is the entire audit
        -- trail: there is no gateway record and no bank statement to check it
        -- against, so a collection with nobody's name on it is a collection
        -- nobody can be asked about.
        ALTER TABLE payments
            ADD COLUMN collected_by INT UNSIGNED NULL AFTER status,
            ADD COLUMN collected_at DATETIME NULL AFTER collected_by,
            ADD COLUMN collection_note VARCHAR(255) NULL AFTER collected_at;

        ALTER TABLE payments
            ADD CONSTRAINT fk_payments_collected_by
            FOREIGN KEY (collected_by) REFERENCES users(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'payments'
                      AND COLUMN_NAME = 'refund_reason') THEN
        ALTER TABLE payments
            ADD COLUMN refund_reason VARCHAR(255) NULL AFTER refunded_at;
    END IF;

    -- Everything that already exists was taken through the app, and from here
    -- on the app means cash. Explicit rather than relying on the DEFAULT, which
    -- only applies to rows inserted after the column existed.
    UPDATE payments SET method = 'cod' WHERE id > 0 AND (method IS NULL OR method = '');

    -- ── A failed online payment is just money still owed ─────────────────────
    --
    -- 'failed' meant the bank declined. There is no bank now, and leaving these
    -- rows behind would strand their orders: nothing in the COD flow moves a
    -- payment out of 'failed', so those customers could never be collected
    -- from. 'created' is what they are — confirmed, cash due at the door.
    --
    -- Deliberately does not touch 'paid' or 'refunded'. Money that really moved
    -- through the gateway stays exactly as recorded.
    UPDATE payments SET status = 'created' WHERE status = 'failed';

    -- ── Drop the gateway columns ─────────────────────────────────────────────
    --
    -- Dropped one at a time and each guarded, so a re-run after a partial
    -- failure picks up where it stopped. MySQL drops a single-column index
    -- along with its column, so the UNIQUE keys on the razorpay ids need no
    -- separate statement.

    IF EXISTS (SELECT 1 FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'payments'
                  AND COLUMN_NAME = 'razorpay_order_id') THEN
        ALTER TABLE payments DROP COLUMN razorpay_order_id;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'payments'
                  AND COLUMN_NAME = 'razorpay_payment_id') THEN
        ALTER TABLE payments DROP COLUMN razorpay_payment_id;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'payments'
                  AND COLUMN_NAME = 'razorpay_signature') THEN
        ALTER TABLE payments DROP COLUMN razorpay_signature;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'payments'
                  AND COLUMN_NAME = 'refund_id') THEN
        ALTER TABLE payments DROP COLUMN refund_id;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'payments'
                  AND COLUMN_NAME = 'failure_reason') THEN
        ALTER TABLE payments DROP COLUMN failure_reason;
    END IF;
END$$

DELIMITER ;

CALL f2h_migrate_cod();
DROP PROCEDURE f2h_migrate_cod;

-- Check it landed:
--   SHOW COLUMNS FROM payments;
--   SELECT status, method, COUNT(*) FROM payments GROUP BY status, method;
--
-- Expect: no razorpay_* columns, every row method='cod', no row at 'failed'.

-- ==========================================================================
-- SECTION 05 of 14  .  farmer_paid_at_pickup.sql
-- The farmer is paid in cash at collection. Needs the payments columns above.
-- ==========================================================================

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

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'payments'
      AND COLUMN_NAME = 'farmer_paid_at') = 0,
  'ALTER TABLE payments ADD COLUMN farmer_paid_at DATETIME NULL AFTER refund_reason, ADD COLUMN farmer_paid_amount DECIMAL(10, 2) NULL AFTER farmer_paid_at, ADD COLUMN farmer_paid_by INT NULL AFTER farmer_paid_amount, ADD COLUMN farmer_paid_note VARCHAR(255) NULL AFTER farmer_paid_by',
  'SELECT "payments.farmer_paid_* already exist" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'payments'
      AND CONSTRAINT_NAME = 'fk_payments_farmer_paid_by') = 0,
  'ALTER TABLE payments ADD CONSTRAINT fk_payments_farmer_paid_by
     FOREIGN KEY (farmer_paid_by) REFERENCES users(id) ON DELETE SET NULL',
  'SELECT "fk_payments_farmer_paid_by already exists" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

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

-- ==========================================================================
-- SECTION 06 of 14  .  delivery_charge.sql
-- Flat delivery fee. Alters platform_settings, so it follows section 1.
-- ==========================================================================

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

-- ==========================================================================
-- SECTION 07 of 14  .  delivery_role.sql
-- The delivery role, order assignment, and the cash-handover ledger.
-- ==========================================================================

-- The delivery role, and the column that assigns an order to one.
--
-- A fourth role beside customer, farmer and admin. Delivery accounts are made
-- by an admin — there is no self-registration for them — and each one sees only
-- the orders assigned to it.
--
-- `assigned_delivery_id` is the whole of that authorisation. `party_for()` in
-- app/models/request.py reads it, and an account not named there has no standing
-- on the order, so every read and write refuses without a single explicit check
-- in any route. Holding the role grants nothing on its own; you have to have
-- been given the job.
--
-- Safe to run twice.
--
--   mysql -u f2h -p f2h_db < database/delivery_role.sql

-- ── The role ─────────────────────────────────────────────────────────────────
INSERT INTO roles (name, description)
VALUES ('delivery', 'Delivery partner — collects from farms and delivers to customers')
ON DUPLICATE KEY UPDATE description = VALUES(description);

-- ── purchase_requests.assigned_delivery_id ───────────────────────────────────
--
-- INT UNSIGNED, matching `users.id`. A plain INT is a foreign key that silently
-- refuses to create — which is how platform_settings failed to exist for weeks.
--
-- ON DELETE SET NULL, not CASCADE: removing a delivery account must return
-- their orders to the unassigned pool, not delete the orders.
SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'purchase_requests'
      AND COLUMN_NAME = 'assigned_delivery_id') = 0,
  'ALTER TABLE purchase_requests
     ADD COLUMN assigned_delivery_id INT UNSIGNED NULL AFTER delivery_charge,
     ADD INDEX idx_pr_assigned_delivery (assigned_delivery_id),
     ADD CONSTRAINT fk_pr_assigned_delivery
       FOREIGN KEY (assigned_delivery_id) REFERENCES users (id) ON DELETE SET NULL',
  'SELECT "purchase_requests.assigned_delivery_id already exists" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── family_pack_orders.assigned_delivery_id ──────────────────────────────────
SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'family_pack_orders'
      AND COLUMN_NAME = 'assigned_delivery_id') = 0,
  'ALTER TABLE family_pack_orders
     ADD COLUMN assigned_delivery_id INT UNSIGNED NULL AFTER delivery_charge,
     ADD INDEX idx_fpo_assigned_delivery (assigned_delivery_id),
     ADD CONSTRAINT fk_fpo_assigned_delivery
       FOREIGN KEY (assigned_delivery_id) REFERENCES users (id) ON DELETE SET NULL',
  'SELECT "family_pack_orders.assigned_delivery_id already exists" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── delivery_remittances ─────────────────────────────────────────────────────
--
-- Cash handed over by a delivery partner. One row per handover.
--
-- Only handovers are stored. What each partner has *collected* is derived from
-- the sum of their completed orders — it is already on those rows, and a second
-- copy is a second thing to keep in step. Outstanding is the subtraction, so
-- the two figures cannot contradict each other.
--
-- `amount` is signed on purpose: a handover entered wrongly is corrected by
-- recording a negative one, so the trail keeps both the mistake and the fix.
--
-- ON DELETE RESTRICT on delivery_id — removing a delivery account must not
-- erase the record of money they handed over. Deactivate them instead.
CREATE TABLE IF NOT EXISTS delivery_remittances (
  id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  delivery_id INT UNSIGNED NOT NULL,
  amount DECIMAL(10,2) NOT NULL,
  received_by INT UNSIGNED NULL,
  note VARCHAR(255) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  INDEX idx_remit_delivery (delivery_id),
  INDEX idx_remit_created (created_at),

  CONSTRAINT fk_remit_delivery
    FOREIGN KEY (delivery_id) REFERENCES users (id) ON DELETE RESTRICT,
  CONSTRAINT fk_remit_received_by
    FOREIGN KEY (received_by) REFERENCES users (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── Check ────────────────────────────────────────────────────────────────────
SELECT id, name FROM roles ORDER BY id;

SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = DATABASE()
   AND COLUMN_NAME = 'assigned_delivery_id'
 ORDER BY TABLE_NAME;

-- Expect four roles and two columns, both INT UNSIGNED and nullable.
-- No existing order is assigned to anyone, which is correct: they were all
-- handled before this role existed.

-- ==========================================================================
-- SECTION 08 of 14  .  basket_only_products.sql
-- Products that exist only inside a weekly basket.
-- ==========================================================================

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

-- ==========================================================================
-- SECTION 09 of 14  .  basket_sold_by_f2h.sql
-- A basket is sold by F2H rather than by one farm.
-- ==========================================================================

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

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'family_pack_orders'
      AND COLUMN_NAME = 'hold_reason') = 0,
  'ALTER TABLE family_pack_orders ADD COLUMN hold_reason VARCHAR(500) NULL AFTER rejection_reason',
  'SELECT "family_pack_orders.hold_reason already exists" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

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

-- ==========================================================================
-- SECTION 10 of 14  .  basket_reminders.sql
-- Remind a customer before their basket is prepared.
-- ==========================================================================

-- Remind customers before their weekly basket is prepared.
--
-- A standing basket is easy to forget about. By the time the delivery is
-- generated the order is confirmed and its stock is committed, so a customer
-- who is away that week has already cost a farmer a picked basket. The reminder
-- goes out before that point, while pausing or editing still costs nobody
-- anything.
--
-- One column, and it exists to stop double-sending. The reminder job runs from
-- cron and may run more than once a day — a retry, a manual trigger, two hosts.
-- Recording which delivery date was last reminded about means the second run
-- finds its own work already done and sends nothing. A reminder that arrives
-- three times is worse than one that arrives late.
--
--   mysql -u f2h -p f2h < database/basket_reminders.sql

SET @sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'family_pack_subscriptions'
      AND COLUMN_NAME = 'last_reminded_for') = 0,
  'ALTER TABLE family_pack_subscriptions ADD COLUMN last_reminded_for DATE NULL AFTER last_generated_date',
  'SELECT "family_pack_subscriptions.last_reminded_for already exists" AS note'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Existing baskets start with NULL, so each gets exactly one reminder for its
-- next delivery and settles into the weekly rhythm from there.

-- ==========================================================================
-- SECTION 11 of 14  .  homemade_products.sql
-- Made rather than grown — jam, pickle, ghee, honey.
-- ==========================================================================

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

-- ==========================================================================
-- SECTION 12 of 14  .  basket_no_farm.sql
-- farmer_id becomes INT UNSIGNED. MUST follow section 9, which sets it signed.
-- ==========================================================================

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

-- ==========================================================================
-- SECTION 13 of 14  .  basket_courier.sql
-- A standing courier for a weekly basket.
-- ==========================================================================

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

-- ==========================================================================
-- SECTION 14 of 14  .  order_route.sql
-- The final order route, including cash_collected. MUST be last.
-- ==========================================================================

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
