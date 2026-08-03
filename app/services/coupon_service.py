from datetime import datetime

from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models.coupon import Coupon, CouponRedemption


def normalise(code):
    """Codes are compared upper-case and trimmed, so "save10 " matches SAVE10."""
    return (code or '').strip().upper()


def find_by_code(code):
    cleaned = normalise(code)
    if not cleaned:
        return None
    return Coupon.query.filter_by(code=cleaned).first()


def preview(code, subtotal):
    """What the customer sees while typing, before anything is committed.

    Returns `(coupon, discount, error)`. A returned coupon is *not* reserved —
    two people can preview the same code at once. The claim happens in
    `redeem`, which is where the race is actually settled.
    """
    coupon = find_by_code(code)
    if not coupon:
        return None, 0.0, 'That coupon code is not valid.'

    reason = coupon.unavailable_reason(subtotal)
    if reason:
        return coupon, 0.0, reason

    discount = coupon.discount_for(subtotal)
    if discount <= 0:
        return coupon, 0.0, 'This coupon does not apply to your order.'

    return coupon, discount, None


def apply_to_total(code, subtotal):
    """Resolve a code into `(coupon, discount, total)` or raise ValueError.

    Called by order creation before the order row exists, so the caller can
    store the discounted total. Nothing is claimed yet.
    """
    if not normalise(code):
        return None, 0.0, round(float(subtotal), 2)

    coupon, discount, error = preview(code, subtotal)
    if error:
        raise ValueError(error)

    return coupon, discount, round(float(subtotal) - discount, 2)


def redeem(coupon, customer_id, subtotal, discount, *, request_id=None, family_pack_order_id=None):
    """Claim the coupon for this order. Raises ValueError if already taken.

    Runs inside the caller's transaction and does not commit — the redemption
    and the order it belongs to must land together or not at all. If the order
    later fails to commit, the claim rolls back with it and the code stays
    available.

    The INSERT is what enforces single use: `coupon_redemptions.coupon_id` is
    unique, so a second concurrent checkout gets an IntegrityError here rather
    than a second discount.
    """
    if coupon is None:
        return None

    total_after = round(float(subtotal) - float(discount), 2)
    redemption = CouponRedemption(
        coupon_id=coupon.id,
        customer_id=customer_id,
        request_id=request_id,
        family_pack_order_id=family_pack_order_id,
        subtotal=round(float(subtotal), 2),
        discount_amount=round(float(discount), 2),
        total_after_discount=total_after,
    )
    db.session.add(redemption)

    coupon.redeemed_at = datetime.utcnow()
    coupon.redeemed_by = customer_id
    coupon.redeemed_amount = round(float(discount), 2)

    try:
        # Surfaces the duplicate now, while we can still turn it into a clear
        # message, instead of at commit time inside the route's generic handler.
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise ValueError('This coupon has just been used. Please try another code.')

    return redemption


def list_coupons(status=None, search=None, page=1, per_page=20):
    """Admin listing, filterable by the same statuses the badges show."""
    query = Coupon.query

    if search:
        like = f'%{search.strip().upper()}%'
        query = query.filter(db.or_(Coupon.code.like(like),
                                    Coupon.description.ilike(f'%{search.strip()}%')))

    now = datetime.utcnow()
    if status == 'used':
        query = query.filter(Coupon.redeemed_at.isnot(None))
    elif status == 'available':
        query = query.filter(
            Coupon.redeemed_at.is_(None),
            Coupon.is_active.is_(True),
            db.or_(Coupon.expires_at.is_(None), Coupon.expires_at >= now),
        )
    elif status == 'unused':
        # Everything never redeemed, including expired and withdrawn ones.
        query = query.filter(Coupon.redeemed_at.is_(None))
    elif status == 'expired':
        query = query.filter(
            Coupon.redeemed_at.is_(None),
            Coupon.expires_at.isnot(None),
            Coupon.expires_at < now,
        )
    elif status == 'inactive':
        query = query.filter(Coupon.redeemed_at.is_(None), Coupon.is_active.is_(False))

    total = query.count()
    items = (query.order_by(Coupon.created_at.desc())
             .offset((page - 1) * per_page)
             .limit(per_page)
             .all())
    return items, total


def summary():
    """Counts for the header of the admin coupon screen."""
    now = datetime.utcnow()
    total = Coupon.query.count()
    used = Coupon.query.filter(Coupon.redeemed_at.isnot(None)).count()
    available = Coupon.query.filter(
        Coupon.redeemed_at.is_(None),
        Coupon.is_active.is_(True),
        db.or_(Coupon.expires_at.is_(None), Coupon.expires_at >= now),
    ).count()
    discount_given = db.session.query(
        db.func.coalesce(db.func.sum(CouponRedemption.discount_amount), 0)
    ).scalar()

    return {
        'total': total,
        'used': used,
        'available': available,
        # Never redeemed but not currently usable — expired or withdrawn.
        'unavailable': total - used - available,
        'total_discount_given': float(discount_given or 0),
    }


def create_coupon(admin_id, data):
    code = normalise(data.get('code'))
    if not code:
        raise ValueError('A coupon code is required')
    if len(code) < 3:
        raise ValueError('Coupon codes must be at least 3 characters')
    if Coupon.query.filter_by(code=code).first():
        raise ValueError(f'The code {code} already exists')

    discount_type = data.get('discount_type', 'percentage')
    if discount_type not in ('percentage', 'fixed'):
        raise ValueError('Discount type must be percentage or fixed')

    try:
        value = float(data.get('discount_value'))
    except (TypeError, ValueError):
        raise ValueError('Enter a discount value')
    if value <= 0:
        raise ValueError('The discount must be greater than zero')
    if discount_type == 'percentage' and value > 100:
        raise ValueError('A percentage discount cannot exceed 100')

    coupon = Coupon(
        code=code,
        description=(data.get('description') or '').strip() or None,
        discount_type=discount_type,
        discount_value=value,
        min_order_value=_optional_amount(data.get('min_order_value')),
        max_discount=_optional_amount(data.get('max_discount')),
        expires_at=_optional_date(data.get('expires_at')),
        is_active=bool(data.get('is_active', True)),
        created_by=admin_id,
    )
    db.session.add(coupon)
    db.session.commit()
    return coupon


def update_coupon(coupon_id, data):
    coupon = Coupon.query.get_or_404(coupon_id)

    # A spent coupon is a historical record. Letting an admin change its value
    # would rewrite what a customer was actually given.
    if coupon.is_redeemed:
        raise ValueError('This coupon has been used and can no longer be edited')

    if 'code' in data:
        code = normalise(data['code'])
        if not code:
            raise ValueError('A coupon code is required')
        clash = Coupon.query.filter(Coupon.code == code, Coupon.id != coupon.id).first()
        if clash:
            raise ValueError(f'The code {code} already exists')
        coupon.code = code

    if 'description' in data:
        coupon.description = (data['description'] or '').strip() or None

    if 'discount_type' in data:
        if data['discount_type'] not in ('percentage', 'fixed'):
            raise ValueError('Discount type must be percentage or fixed')
        coupon.discount_type = data['discount_type']

    if 'discount_value' in data:
        try:
            value = float(data['discount_value'])
        except (TypeError, ValueError):
            raise ValueError('Enter a discount value')
        if value <= 0:
            raise ValueError('The discount must be greater than zero')
        if coupon.discount_type == 'percentage' and value > 100:
            raise ValueError('A percentage discount cannot exceed 100')
        coupon.discount_value = value

    if 'min_order_value' in data:
        coupon.min_order_value = _optional_amount(data['min_order_value'])
    if 'max_discount' in data:
        coupon.max_discount = _optional_amount(data['max_discount'])
    if 'expires_at' in data:
        coupon.expires_at = _optional_date(data['expires_at'])
    if 'is_active' in data:
        coupon.is_active = bool(data['is_active'])

    db.session.commit()
    return coupon


def delete_coupon(coupon_id):
    coupon = Coupon.query.get_or_404(coupon_id)
    if coupon.is_redeemed:
        # Deleting would erase the discount trail behind a real order.
        raise ValueError('This coupon has been used. Deactivate it instead of deleting.')
    db.session.delete(coupon)
    db.session.commit()


def redemptions(page=1, per_page=30):
    query = CouponRedemption.query.order_by(CouponRedemption.created_at.desc())
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return items, total


def _optional_amount(value):
    if value in (None, ''):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError('Enter a valid amount')
    return amount if amount > 0 else None


def _optional_date(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace('Z', '')
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise ValueError('Enter a valid expiry date')
