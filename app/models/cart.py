"""A customer's cart: what they intend to buy, before it becomes an order.

One row per product per customer. There is no `carts` table — the customer *is*
the cart, so a separate parent row would only ever hold a foreign key and a
timestamp, and every query would join through it for nothing.

**Nothing here is a price.** Quantity and product, and that is all. Prices,
stock and the commission split are read from the product at checkout, so a cart
left for a week does not lock in last week's price or reserve stock that
somebody else could have bought. A cart is an intention; the order is the
commitment.

That is also why a cart item is not stock-committed. Two customers can hold the
same 5kg in their carts, and whichever checks out first gets it — the other is
told at checkout rather than at delivery.
"""

from datetime import datetime

from ..extensions import db


class CartItem(db.Model):
    __tablename__ = 'cart_items'
    __table_args__ = (
        # One row per product per customer. Adding the same product twice is an
        # increase in quantity, not a second line — enforced here rather than
        # trusted to the route, because two taps on "Add" race each other.
        db.UniqueConstraint('customer_id', 'product_id', name='uq_cart_customer_product'),
    )

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'),
                           nullable=False)
    quantity = db.Column(db.Numeric(10, 3), nullable=False, default=1)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = db.relationship('User', foreign_keys=[customer_id])
    product = db.relationship('Product', foreign_keys=[product_id])

    @property
    def line_total(self):
        """What this line costs at today's price, or 0 if the product is gone."""
        if self.product is None:
            return 0.0
        return round(float(self.product.effective_price) * float(self.quantity), 2)

    def problem(self):
        """Why this line cannot be ordered right now, or None.

        Checked at display *and* at checkout. A cart can sit for days, and a
        product can sell out, be delisted or have its minimum raised in the
        meantime — so the cart screen shows the problem before the customer
        commits, and checkout refuses rather than trusting what the screen said.
        """
        p = self.product
        if p is None or p.deleted_at or not p.is_active:
            return 'no longer available'
        if p.stock_status == 'out_of_stock':
            return 'out of stock'
        qty = float(self.quantity)
        if qty > float(p.available_quantity):
            return f'only {float(p.available_quantity):g} {p.unit} left'
        if qty < float(p.min_quantity):
            return f'minimum is {float(p.min_quantity):g} {p.unit}'
        return None

    def to_dict(self):
        p = self.product
        return {
            'id': self.id,
            'product_id': self.product_id,
            'quantity': float(self.quantity),
            'line_total': self.line_total,
            'problem': self.problem(),
            'product': {
                'id': p.id,
                'name': p.name,
                'unit': p.unit,
                'price': float(p.effective_price),
                'min_quantity': float(p.min_quantity),
                'available_quantity': float(p.available_quantity),
                # `primary_image` is a ProductImage row, not a URL.
                'image_url': p.primary_image.image_url if p.primary_image else None,
                'farmer_id': p.farmer_id,
                'farmer_name': (p.farmer.farmer_profile.farm_name
                                if p.farmer and p.farmer.farmer_profile else None),
            } if p else None,
        }
