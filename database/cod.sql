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
