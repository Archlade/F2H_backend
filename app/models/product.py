from ..extensions import db
from datetime import datetime


class Product(db.Model):
    __tablename__ = 'products'

    id = db.Column(db.Integer, primary_key=True)
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(250), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    unit = db.Column(db.Enum('kg', 'gram', 'litre', 'ml', 'piece', 'bundle', 'dozen', 'box'), default='kg')
    min_quantity = db.Column(db.Numeric(10, 3), default=1.0)
    available_quantity = db.Column(db.Numeric(10, 3), default=0)
    is_organic = db.Column(db.Boolean, default=False)
    is_natural = db.Column(db.Boolean, default=False)
    is_farm_grown = db.Column(db.Boolean, default=True)
    delivery_available = db.Column(db.Boolean, default=True)
    pickup_available = db.Column(db.Boolean, default=True)
    is_active = db.Column(db.Boolean, default=True)
    is_featured = db.Column(db.Boolean, default=False)
    # Whether this product may go into a weekly basket. Curated by admins, and
    # off by default: a basket commits F2H to sourcing the same item every week,
    # which is a different promise from listing it for a one-off order.
    basket_eligible = db.Column(db.Boolean, nullable=False, default=False)

    # An item that exists *only* inside a weekly basket.
    #
    # Created by an admin, owned by the platform seller account, and sourced by
    # F2H to order rather than listed by a farm. Three consequences, all of them
    # the same idea — this is not a thing on a shelf:
    #
    #   * hidden from the marketplace listing, so it never appears next to
    #     farmers' produce;
    #   * refused for a one-off purchase request, because there is no farm to
    #     accept it and nobody to pick it;
    #   * exempt from the stock check, because F2H buys it in against the
    #     basket orders that were placed. `available_quantity` on one of these
    #     is meaningless and is not maintained.
    #
    # `basket_eligible` says "may go in a basket". This says "may go *nowhere
    # else*". They are set together on admin items, but they are not the same
    # question and collapsing them would make every farmer product that was ever
    # basket-eligible vanish from the shop.
    basket_only = db.Column(db.Boolean, nullable=False, default=False)
    is_approved = db.Column(db.Boolean, default=True)
    stock_status = db.Column(db.Enum('in_stock', 'low_stock', 'out_of_stock'), default='in_stock')
    low_stock_threshold = db.Column(db.Numeric(10, 3), default=5.0)
    rating_avg = db.Column(db.Numeric(3, 2), default=0.00)
    rating_count = db.Column(db.Integer, default=0)
    view_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = db.Column(db.DateTime)

    farmer = db.relationship('User', foreign_keys=[farmer_id])
    category = db.relationship('Category', back_populates='products')
    images = db.relationship('ProductImage', back_populates='product', cascade='all, delete-orphan',
                             order_by='ProductImage.sort_order')
    discount = db.relationship('Discount', back_populates='product', uselist=False, cascade='all, delete-orphan')
    requests = db.relationship('PurchaseRequest', foreign_keys='PurchaseRequest.product_id', back_populates='product')

    @property
    def primary_image(self):
        primary = next((img for img in self.images if img.is_primary), None)
        return primary or (self.images[0] if self.images else None)

    @property
    def effective_price(self):
        if self.discount and self.discount.is_active:
            return float(self.discount.discounted_price)
        return float(self.price)

    def update_stock_status(self):
        qty = float(self.available_quantity)
        threshold = float(self.low_stock_threshold)
        if qty <= 0:
            self.stock_status = 'out_of_stock'
        elif qty <= threshold:
            self.stock_status = 'low_stock'
        else:
            self.stock_status = 'in_stock'

    def to_dict(self, include_farmer=True, include_location=False, distance=None):
        primary_img = self.primary_image
        data = {
            'id': self.id,
            'farmer_id': self.farmer_id,
            'category_id': self.category_id,
            'name': self.name,
            'slug': self.slug,
            'description': self.description,
            'price': float(self.price),
            'unit': self.unit,
            'min_quantity': float(self.min_quantity),
            'available_quantity': float(self.available_quantity),
            'is_organic': self.is_organic,
            'is_natural': self.is_natural,
            'is_farm_grown': self.is_farm_grown,
            'delivery_available': self.delivery_available,
            'pickup_available': self.pickup_available,
            'is_active': self.is_active,
            'is_featured': self.is_featured,
            'basket_eligible': self.basket_eligible,
            # So a client can label one, and so the admin screen can tell an
            # F2H item apart from a farm listing at a glance.
            'basket_only': self.basket_only,
            'is_approved': self.is_approved,
            'stock_status': self.stock_status,
            'rating_avg': float(self.rating_avg) if self.rating_avg else 0.0,
            'rating_count': self.rating_count,
            'view_count': self.view_count,
            'primary_image': primary_img.image_url if primary_img else None,
            'images': [img.to_dict() for img in self.images],
            'category': self.category.to_dict() if self.category else None,
            'discount': self.discount.to_dict() if self.discount and self.discount.is_active else None,
            'effective_price': self.effective_price,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if distance is not None:
            data['distance_km'] = round(distance, 2)
        if include_farmer and self.farmer:
            fp = self.farmer.farmer_profile
            data['farmer'] = {
                'id': self.farmer.id,
                'full_name': self.farmer.full_name,
                'farm_name': fp.farm_name if fp else self.farmer.full_name,
                'is_verified': fp.is_verified if fp else False,
                'rating_avg': float(fp.rating_avg) if fp and fp.rating_avg else 0.0,
                'avatar_url': fp.avatar_url or self.farmer.avatar_url if fp else self.farmer.avatar_url,
            }
        return data


class ProductImage(db.Model):
    __tablename__ = 'product_images'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False)
    image_url = db.Column(db.String(500), nullable=False)
    is_primary = db.Column(db.Boolean, default=False)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship('Product', back_populates='images')

    def to_dict(self):
        return {
            'id': self.id,
            'product_id': self.product_id,
            'image_url': self.image_url,
            'is_primary': self.is_primary,
            'sort_order': self.sort_order,
        }


class Discount(db.Model):
    __tablename__ = 'discounts'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False, unique=True)
    discount_type = db.Column(db.Enum('percentage', 'fixed'), default='percentage')
    discount_value = db.Column(db.Numeric(10, 2), nullable=False)
    discounted_price = db.Column(db.Numeric(10, 2), nullable=False)
    starts_at = db.Column(db.DateTime)
    ends_at = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product = db.relationship('Product', back_populates='discount')

    def to_dict(self):
        return {
            'id': self.id,
            'product_id': self.product_id,
            'discount_type': self.discount_type,
            'discount_value': float(self.discount_value),
            'discounted_price': float(self.discounted_price),
            'starts_at': self.starts_at.isoformat() if self.starts_at else None,
            'ends_at': self.ends_at.isoformat() if self.ends_at else None,
            'is_active': self.is_active,
        }
