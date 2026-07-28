from ..extensions import db
from datetime import datetime

VALID_TRANSITIONS = {
    'pending': ['accepted', 'rejected', 'cancelled', 'admin_review'],
    'admin_review': ['accepted', 'rejected', 'cancelled'],
    'accepted': ['chat_active', 'cancelled'],
    'rejected': [],
    'chat_active': ['confirmed', 'cancelled'],
    'confirmed': ['preparing', 'cancelled'],
    'preparing': ['ready_for_pickup', 'out_for_delivery', 'cancelled'],
    'ready_for_pickup': ['completed', 'cancelled'],
    'out_for_delivery': ['completed', 'cancelled'],
    'completed': [],
    'cancelled': [],
}


class PurchaseRequest(db.Model):
    __tablename__ = 'purchase_requests'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    quantity = db.Column(db.Numeric(10, 3), nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    total_price = db.Column(db.Numeric(10, 2), nullable=False)
    purchase_mode = db.Column(db.Enum('delivery', 'pickup'), nullable=False)
    status = db.Column(
        db.Enum('pending', 'admin_review', 'accepted', 'rejected', 'chat_active',
                'confirmed', 'preparing', 'ready_for_pickup', 'out_for_delivery', 'completed', 'cancelled'),
        default='pending'
    )
    delivery_address_id = db.Column(db.Integer, db.ForeignKey('addresses.id', ondelete='SET NULL'))
    delivery_notes = db.Column(db.Text)
    pickup_notes = db.Column(db.Text)
    customer_message = db.Column(db.Text)
    rejection_reason = db.Column(db.Text)
    cancellation_reason = db.Column(db.Text)
    cancelled_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = db.relationship('User', foreign_keys=[customer_id])
    farmer = db.relationship('User', foreign_keys=[farmer_id])
    product = db.relationship('Product', back_populates='requests')
    delivery_address = db.relationship('Address', foreign_keys=[delivery_address_id])
    canceller = db.relationship('User', foreign_keys=[cancelled_by])
    status_history = db.relationship('RequestStatusHistory', back_populates='request',
                                      cascade='all, delete-orphan', order_by='RequestStatusHistory.created_at')
    chat = db.relationship('Chat', back_populates='request', uselist=False)

    def can_transition_to(self, new_status):
        return new_status in VALID_TRANSITIONS.get(self.status, [])

    def to_dict(self, include_product=True, include_users=True):
        data = {
            'id': self.id,
            'customer_id': self.customer_id,
            'farmer_id': self.farmer_id,
            'product_id': self.product_id,
            'quantity': float(self.quantity),
            'unit_price': float(self.unit_price),
            'total_price': float(self.total_price),
            'purchase_mode': self.purchase_mode,
            'status': self.status,
            'delivery_address_id': self.delivery_address_id,
            'delivery_notes': self.delivery_notes,
            'pickup_notes': self.pickup_notes,
            'customer_message': self.customer_message,
            'rejection_reason': self.rejection_reason,
            'cancellation_reason': self.cancellation_reason,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'chat_id': self.chat.id if self.chat else None,
        }
        if include_product and self.product:
            data['product'] = {
                'id': self.product.id,
                'name': self.product.name,
                'unit': self.product.unit,
                'primary_image': self.product.primary_image.image_url if self.product.primary_image else None,
            }
        if include_users:
            if self.customer:
                data['customer'] = {
                    'id': self.customer.id,
                    'full_name': self.customer.full_name,
                    'avatar_url': self.customer.avatar_url,
                }
            if self.farmer:
                fp = self.farmer.farmer_profile
                data['farmer'] = {
                    'id': self.farmer.id,
                    'full_name': self.farmer.full_name,
                    'farm_name': fp.farm_name if fp else self.farmer.full_name,
                    'avatar_url': fp.avatar_url if fp else self.farmer.avatar_url,
                }
        if self.delivery_address:
            data['delivery_address'] = self.delivery_address.to_dict()
        return data


class RequestStatusHistory(db.Model):
    __tablename__ = 'request_status_history'

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('purchase_requests.id', ondelete='CASCADE'), nullable=False)
    from_status = db.Column(db.String(50))
    to_status = db.Column(db.String(50), nullable=False)
    changed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    request = db.relationship('PurchaseRequest', back_populates='status_history')
    changer = db.relationship('User', foreign_keys=[changed_by])

    def to_dict(self):
        return {
            'id': self.id,
            'from_status': self.from_status,
            'to_status': self.to_status,
            'note': self.note,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'changed_by': {
                'id': self.changer.id,
                'full_name': self.changer.full_name,
            } if self.changer else None,
        }
