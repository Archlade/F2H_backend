from datetime import datetime
from ..extensions import db, socketio
from ..models import FamilyPack, FamilyPackOrder, RequestStatusHistory, Chat, Product
from .notification_service import create_notification
from . import order_money
from . import stock_service as stock


def _pack_items(order):
    """The (product, quantity) pairs an order takes off the farmer's listings.

    A family pack order references either a curated pack or a weekly
    subscription — never both, and the contents live on whichever it is. Items
    whose product row has gone are skipped rather than crashing the
    transition; there is no stock left to move on a deleted listing.
    """
    source = order.pack or order.subscription
    if source is None:
        return []
    return [(item.product, item.quantity) for item in source.items if item.product]

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

    # A basket is one order for one delivery, so it carries the flat fee once by
    # construction — no batching to worry about, unlike the cart. Hardcoded to
    # delivery below, so there is no pickup case to exempt here; if baskets ever
    # gain a collection option this needs the same `!= 'pickup'` guard the
    # request path has.
    from ..models import delivery_charge
    delivery = round(delivery_charge(), 2)
    total_price = round(total_price + delivery, 2)

    order = FamilyPackOrder(
        customer_id=customer_id,
        farmer_id=pack.farmer_id,
        pack_id=pack_id,
        unit_price=unit_price,
        subtotal=subtotal,
        discount_amount=discount,
        delivery_charge=delivery,
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
    # The order above is already committed, so this commit is the
    # notification's own. Without it the row is discarded when the session is
    # torn down at the end of the request — the farmer never sees the order in
    # their list, and the push that now rides on the same commit never goes out
    # either.
    db.session.commit()

    socketio.emit('new_notification', {'type': 'new_request', 'order_id': order.id}, room=f"user_{pack.farmer_id}")
    return order


def update_family_pack_order_status(order_id: int, actor_id: int, actor_role: str, new_status: str, data: dict = None):
    # Locked for the transition — see the same guard in request_service. A
    # concurrent complete/cancel would otherwise credit the farmer and refund
    # the customer for one order.
    from ..utils.locking import lock_row
    order = lock_row(FamilyPackOrder, order_id)
    if order is None:
        from flask import abort
        abort(404)
    data = data or {}

    # By side of this order, not account role: a farmer who ordered someone
    # else's pack is its buyer and may only cancel it.
    from ..models.request import (buyer_may_cancel, cancellation_refused_reason,
                                  party_for, party_may_set)
    party = party_for(order, actor_id, actor_role)
    if party is None:
        raise PermissionError('Not authorized')

    if not party_may_set(party, new_status, order.status):
        noun = {'buyer': 'buyer', 'seller': 'seller'}.get(party, actor_role)
        raise PermissionError(f"The {noun} cannot set an order to '{new_status}'")

    # Same cancellation window as purchase requests — the rule lives in
    # request.py so the two order tables cannot drift apart on it. Weekly
    # subscription deliveries are born at 'confirmed', so this closes their
    # buyer-cancellation window the moment they are generated; stopping the
    # *subscription* is the customer's control there, not cancelling a basket
    # the farmer is already picking.
    if new_status == 'cancelled' and party == 'buyer' and not buyer_may_cancel(order):
        raise PermissionError(cancellation_refused_reason(order))

    if not order.can_transition_to(new_status):
        raise ValueError(f"Cannot transition from '{order.status}' to '{new_status}'")

    # An order is not closed out before its cash is in — see order_money.
    if order_money.payment_blocks(order, new_status):
        raise ValueError(order_money.payment_block_reason(order, new_status))

    order_money.settle(order, new_status, f'family pack order #{order.id}',
                       actor_id=actor_id)

    # Stock moves first, so a shortfall aborts the whole transition rather than
    # leaving a confirmed order behind that the farmer cannot fill.
    #
    # This used to read `if prod and available >= wanted:` — when a product was
    # short it simply skipped the deduction and confirmed the order anyway, so
    # the pack was oversold and nothing recorded that it had happened. It is now
    # all or nothing across the pack's items: short on one product means none of
    # them leave the shelf and the farmer is told which.
    if new_status == 'confirmed' and not order.stock_committed:
        stock.commit_items(_pack_items(order))
        order.stock_committed = True
    elif new_status == 'cancelled' and order.stock_committed:
        stock.restore_items(_pack_items(order))
        order.stock_committed = False

    old_status = order.status
    order.status = new_status

    if new_status == 'rejected':
        order.rejection_reason = data.get('reason', '')
    if new_status == 'cancelled':
        order.cancellation_reason = data.get('reason', '')
        order.cancelled_by = actor_id

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

    # Same as a one-off order: the courier is holding the customer's cash and
    # cannot close this themselves, so every admin is told there is a handover
    # to record. Shared helper, so the two order types cannot drift.
    if new_status == 'cash_collected':
        from .request_service import _notify_admins_cash_collected
        _notify_admins_cash_collected(order, actor_id, 'Basket')

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
