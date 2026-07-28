from ..extensions import db
from datetime import datetime


class Chat(db.Model):
    __tablename__ = 'chats'

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('purchase_requests.id', ondelete='CASCADE'),
                           nullable=False, unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    farmer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    last_message_at = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    request = db.relationship('PurchaseRequest', back_populates='chat')
    customer = db.relationship('User', foreign_keys=[customer_id])
    farmer = db.relationship('User', foreign_keys=[farmer_id])
    messages = db.relationship('Message', back_populates='chat', cascade='all, delete-orphan',
                               order_by='Message.created_at')

    def to_dict(self, include_last_message=True, current_user_id=None):
        farmer_profile = self.farmer.farmer_profile if self.farmer else None
        data = {
            'id': self.id,
            'request_id': self.request_id,
            'customer_id': self.customer_id,
            'farmer_id': self.farmer_id,
            'last_message_at': self.last_message_at.isoformat() if self.last_message_at else None,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'customer': {
                'id': self.customer.id,
                'full_name': self.customer.full_name,
                'avatar_url': self.customer.avatar_url,
            } if self.customer else None,
            'farmer': {
                'id': self.farmer.id,
                'full_name': self.farmer.full_name,
                'farm_name': farmer_profile.farm_name if farmer_profile else self.farmer.full_name,
                'avatar_url': farmer_profile.avatar_url if farmer_profile else self.farmer.avatar_url,
            } if self.farmer else None,
        }
        if include_last_message and self.messages:
            last_msg = self.messages[-1]
            data['last_message'] = {
                'content': last_msg.content[:100],
                'sender_id': last_msg.sender_id,
                'created_at': last_msg.created_at.isoformat(),
            }
        if current_user_id:
            unread = sum(1 for m in self.messages
                         if not m.is_read and m.sender_id != current_user_id)
            data['unread_count'] = unread
        return data


class Message(db.Model):
    __tablename__ = 'messages'

    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey('chats.id', ondelete='CASCADE'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    read_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    chat = db.relationship('Chat', back_populates='messages')
    sender = db.relationship('User', back_populates='sent_messages')

    def to_dict(self):
        return {
            'id': self.id,
            'chat_id': self.chat_id,
            'sender_id': self.sender_id,
            'content': self.content,
            'is_read': self.is_read,
            'read_at': self.read_at.isoformat() if self.read_at else None,
            'created_at': self.created_at.isoformat(),
            'sender': {
                'id': self.sender.id,
                'full_name': self.sender.full_name,
                'avatar_url': self.sender.avatar_url,
            } if self.sender else None,
        }
