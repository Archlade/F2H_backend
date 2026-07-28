from datetime import datetime
from ..extensions import db
from ..models import PurchaseRequest, RequestStatusHistory, Chat, Notification
from ..models.product import Product
from .notification_service import create_notification
from ..extensions import socketio


def create_purchase_request(customer_id: int, data: dict):
    product = Product.query.get(data['product_id'])
    if not product or not product.is_active or product.deleted_at:
        raise ValueError('Product not available')

    if product.stock_status == 'out_of_stock':
        raise ValueError('Product is out of stock')

    qty = float(data['quantity'])
    if qty < float(product.min_quantity):
        raise ValueError(f"Minimum quantity is {product.min_quantity} {product.unit}")
    if qty > float(product.available_quantity):
        raise ValueError('Requested quantity exceeds available stock')

    # Check for duplicate active request
    active_statuses = ['pending', 'admin_review', 'accepted', 'chat_active', 'confirmed', 'preparing']
    existing = PurchaseRequest.query.filter(
        PurchaseRequest.customer_id == customer_id,
        PurchaseRequest.product_id == data['product_id'],
        PurchaseRequest.status.in_(active_statuses),
    ).first()
    if existing:
        raise ValueError('You already have an active request for this product')

    unit_price = float(product.effective_price)
    total = round(unit_price * qty, 2)

    req = PurchaseRequest(
        customer_id=customer_id,
        farmer_id=product.farmer_id,
        product_id=data['product_id'],
        quantity=qty,
        unit_price=unit_price,
        total_price=total,
        purchase_mode=data['purchase_mode'],
        delivery_address_id=data.get('delivery_address_id'),
        delivery_notes=data.get('delivery_notes', ''),
        pickup_notes=data.get('pickup_notes', ''),
        customer_message=data.get('customer_message', ''),
    )
    db.session.add(req)
    db.session.flush()

    _add_status_history(req.id, None, 'pending', customer_id)
    db.session.commit()

    # Notify farmer
    create_notification(
        recipient_id=product.farmer_id,
        sender_id=customer_id,
        notif_type='new_request',
        title='New Purchase Request',
        body=f"You have a new purchase request for {product.name}",
        data={'request_id': req.id, 'product_id': product.id},
    )

    # Real-time notification
    socketio.emit('new_notification', {'type': 'new_request', 'request_id': req.id},
                  room=f"user_{product.farmer_id}")

    return req


def update_request_status(request_id: int, actor_id: int, actor_role: str, new_status: str, data: dict = None):
    req = PurchaseRequest.query.get_or_404(request_id)
    data = data or {}

    # Authorization
    if actor_role == 'customer' and req.customer_id != actor_id:
        raise PermissionError('Not authorized')
    if actor_role == 'farmer' and req.farmer_id != actor_id:
        raise PermissionError('Not authorized')

    if not req.can_transition_to(new_status):
        raise ValueError(f"Cannot transition from '{req.status}' to '{new_status}'")

    old_status = req.status
    req.status = new_status

    if new_status == 'rejected':
        req.rejection_reason = data.get('reason', '')
    if new_status == 'cancelled':
        req.cancellation_reason = data.get('reason', '')
        req.cancelled_by = actor_id

    _add_status_history(req.id, old_status, new_status, actor_id, data.get('note', ''))

    # Create chat when accepted
    if new_status == 'accepted':
        chat = Chat(
            request_id=req.id,
            customer_id=req.customer_id,
            farmer_id=req.farmer_id,
        )
        db.session.add(chat)
        db.session.flush()
        req.status = 'chat_active'
        _add_status_history(req.id, 'accepted', 'chat_active', actor_id, 'Chat created')

        # Notify both
        create_notification(req.customer_id, actor_id, 'request_accepted',
                            'Request Accepted!',
                            f"Your request for {req.product.name if req.product else 'product'} was accepted. Chat is now open.",
                            {'request_id': req.id, 'chat_id': chat.id})
        socketio.emit('new_notification', {'type': 'request_accepted', 'request_id': req.id, 'chat_id': chat.id},
                      room=f"user_{req.customer_id}")
    elif new_status == 'rejected':
        create_notification(req.customer_id, actor_id, 'request_rejected',
                            'Request Rejected',
                            f"Your request was rejected. Reason: {req.rejection_reason or 'No reason given.'}",
                            {'request_id': req.id})
        socketio.emit('new_notification', {'type': 'request_rejected', 'request_id': req.id},
                      room=f"user_{req.customer_id}")
    else:
        # Notify customer of status change
        recipient = req.customer_id if actor_id == req.farmer_id else req.farmer_id
        create_notification(recipient, actor_id, 'status_update',
                            'Order Status Updated',
                            f"Your order status changed to {new_status.replace('_', ' ').title()}",
                            {'request_id': req.id})
        socketio.emit('new_notification', {'type': 'status_update', 'request_id': req.id, 'status': new_status},
                      room=f"user_{recipient}")

    db.session.commit()
    return req


def _add_status_history(request_id, from_status, to_status, changed_by, note=''):
    h = RequestStatusHistory(
        request_id=request_id,
        from_status=from_status,
        to_status=to_status,
        changed_by=changed_by,
        note=note,
    )
    db.session.add(h)


def get_requests_for_customer(customer_id: int, status=None, page=1, per_page=20):
    query = PurchaseRequest.query.filter_by(customer_id=customer_id)
    if status:
        query = query.filter(PurchaseRequest.status == status)
    query = query.order_by(PurchaseRequest.created_at.desc())
    total = query.count()
    reqs = query.offset((page - 1) * per_page).limit(per_page).all()
    return reqs, total


def get_requests_for_farmer(farmer_id: int, status=None, page=1, per_page=20):
    query = PurchaseRequest.query.filter_by(farmer_id=farmer_id)
    if status:
        query = query.filter(PurchaseRequest.status == status)
    query = query.order_by(PurchaseRequest.created_at.desc())
    total = query.count()
    reqs = query.offset((page - 1) * per_page).limit(per_page).all()
    return reqs, total
