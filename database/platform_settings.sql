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

  updated_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by INT NULL,

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
