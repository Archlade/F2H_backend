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
