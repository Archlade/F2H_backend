from datetime import datetime
from ..extensions import db, socketio
from ..models import FamilyPack, FamilyPackOrder, RequestStatusHistory, Chat, Product
from .notification_service import create_notification

def create_family_pack_order(customer_id: int, data: dict):
    pack_id = data.get('pack_id')
    pack = FamilyPack.query.get(pack_id)
    if not pack or not pack.is_active or not pack.is_approved or pack.deleted_at:
        raise ValueError('Family pack is not available')

    # A farmer may buy packs, but not their own — see create_purchase_request.
    if pack.farmer_id == customer_id:
        raise ValueError('This is your own Family Pack')

    address_id = data.get('delivery_address_id')
    if not address_id:
        raise ValueError('Delivery address is required for Family Pack orders')
    # Must be the customer's own address — it is echoed back to both parties.
    from ..models import Address
    if not Address.query.filter_by(id=address_id, user_id=customer_id).first():
        raise ValueError('That delivery address does not belong to you')

    # Check active duplicate order
    active_statuses = ['pending', 'admin_review', 'accepted', 'chat_active', 'confirmed', 'preparing']
    existing = FamilyPackOrder.query.filter(
        FamilyPackOrder.customer_id == customer_id,
        FamilyPackOrder.pack_id == pack_id,
        FamilyPackOrder.status.in_(active_statuses)
    ).first()
    if existing:
        raise ValueError('You already have an active order for this Family Pack')

    unit_price = float(pack.price)
    subtotal = unit_price  # 1 pack order

    # Resolved before the order is created, so an invalid code fails the whole
    # request. Priced from the pack's own price, never from client input.
    from .coupon_service import apply_to_total, redeem
    coupon, discount, total_price = apply_to_total(data.get('coupon_code'), subtotal)

    order = FamilyPackOrder(
        customer_id=customer_id,
        farmer_id=pack.farmer_id,
        pack_id=pack_id,
        unit_price=unit_price,
        subtotal=subtotal,
        discount_amount=discount,
        total_price=total_price,
        coupon_id=coupon.id if coupon else None,
        purchase_mode='delivery',
        delivery_address_id=data.get('delivery_address_id'),
        delivery_notes=data.get('delivery_notes', ''),
        customer_message=data.get('customer_message', '')
    )
    db.session.add(order)
    db.session.flush()

    # Same transaction as the order: if the commit fails, the code stays free.
    if coupon:
        redeem(coupon, customer_id, subtotal, discount, family_pack_order_id=order.id)

    _add_status_history(order.id, None, 'pending', customer_id)
    db.session.commit()

    # Notify farmer
    create_notification(
        recipient_id=pack.farmer_id,
        sender_id=customer_id,
        notif_type='new_request',
        title='New Family Pack Order',
        body=f"You have a new Family Pack order for {pack.name}",
        data={'order_id': order.id, 'pack_id': pack.id}
    )

    socketio.emit('new_notification', {'type': 'new_request', 'order_id': order.id}, room=f"user_{pack.farmer_id}")
    return order


def update_family_pack_order_status(order_id: int, actor_id: int, actor_role: str, new_status: str, data: dict = None):
    order = FamilyPackOrder.query.get_or_404(order_id)
    data = data or {}

    # By side of this order, not account role: a farmer who ordered someone
    # else's pack is its buyer and may only cancel it.
    from ..models.request import party_for, party_may_set
    party = party_for(order, actor_id, actor_role)
    if party is None:
        raise PermissionError('Not authorized')

    if not party_may_set(party, new_status):
        noun = {'buyer': 'buyer', 'seller': 'seller'}.get(party, actor_role)
        raise PermissionError(f"The {noun} cannot set an order to '{new_status}'")

    if not order.can_transition_to(new_status):
        raise ValueError(f"Cannot transition from '{order.status}' to '{new_status}'")

    old_status = order.status
    order.status = new_status

    if new_status == 'rejected':
        order.rejection_reason = data.get('reason', '')
    if new_status == 'cancelled':
        order.cancellation_reason = data.get('reason', '')
        order.cancelled_by = actor_id

    # Stock deduction when confirmed
    if new_status == 'confirmed':
        for item in order.pack.items:
            prod = Product.query.get(item.product_id)
            if prod and float(prod.available_quantity) >= float(item.quantity):
                prod.available_quantity = float(prod.available_quantity) - float(item.quantity)
                prod.update_stock_status()

    _add_status_history(order.id, old_status, new_status, actor_id, data.get('note', ''))

    if new_status == 'accepted':
        chat = Chat(
            family_pack_order_id=order.id,
            customer_id=order.customer_id,
            farmer_id=order.farmer_id
        )
        db.session.add(chat)
        db.session.flush()
        order.status = 'chat_active'
        _add_status_history(order.id, 'accepted', 'chat_active', actor_id, 'Chat created')

        create_notification(order.customer_id, actor_id, 'request_accepted',
                            'Family Pack Order Accepted!',
                            f"Your order for {order.pack.name} was accepted. Chat is now open.",
                            {'order_id': order.id, 'chat_id': chat.id})
        socketio.emit('new_notification', {'type': 'request_accepted', 'order_id': order.id, 'chat_id': chat.id},
                      room=f"user_{order.customer_id}")
    elif new_status == 'rejected':
        create_notification(order.customer_id, actor_id, 'request_rejected',
                            'Family Pack Order Rejected',
                            f"Your order for {order.pack.name} was rejected. Reason: {order.rejection_reason or 'No reason given.'}",
                            {'order_id': order.id})
        socketio.emit('new_notification', {'type': 'request_rejected', 'order_id': order.id},
                      room=f"user_{order.customer_id}")
    else:
        recipient = order.customer_id if actor_id == order.farmer_id else order.farmer_id
        create_notification(recipient, actor_id, 'status_update',
                            'Family Pack Order Status Updated',
                            f"Your order status changed to {new_status.replace('_', ' ').title()}",
                            {'order_id': order.id})
        socketio.emit('new_notification', {'type': 'status_update', 'order_id': order.id, 'status': new_status},
                      room=f"user_{recipient}")

    db.session.commit()
    return order


def _add_status_history(order_id, from_status, to_status, changed_by, note=''):
    h = RequestStatusHistory(
        family_pack_order_id=order_id,
        from_status=from_status,
        to_status=to_status,
        changed_by=changed_by,
        note=note
    )
    db.session.add(h)


def get_family_pack_orders_for_customer(customer_id: int, status=None, page=1, per_page=20):
    query = FamilyPackOrder.query.filter_by(customer_id=customer_id)
    if status:
        query = query.filter(FamilyPackOrder.status == status)
    query = query.order_by(FamilyPackOrder.created_at.desc())
    total = query.count()
    orders = query.offset((page - 1) * per_page).limit(per_page).all()
    return orders, total


def get_family_pack_orders_for_farmer(farmer_id: int, status=None, page=1, per_page=20):
    query = FamilyPackOrder.query.filter_by(farmer_id=farmer_id)
    if status:
        query = query.filter(FamilyPackOrder.status == status)
    query = query.order_by(FamilyPackOrder.created_at.desc())
    total = query.count()
    orders = query.offset((page - 1) * per_page).limit(per_page).all()
    return orders, total
