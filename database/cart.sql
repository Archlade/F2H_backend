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
