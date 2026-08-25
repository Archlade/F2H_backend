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
