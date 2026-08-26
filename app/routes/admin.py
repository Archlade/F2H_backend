from flask import Blueprint, request, jsonify, make_response, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..utils.decorators import admin_required
from ..utils.helpers import paginate_response, log_audit
from ..models import (User, Role, FarmerProfile, Product, PurchaseRequest, Review, Report,
                       FeaturedFarmer, FeaturedProduct, HomepageSection, Announcement, Category,
                       FamilyPackOrder, FamilyPackSubscription, PlatformSettings)
from ..models.settings import (MIN_ORDER_FLOOR, MIN_ORDER_CEILING,
                               DELIVERY_CHARGE_FLOOR, DELIVERY_CHARGE_CEILING)

from ..extensions import db, socketio
from ..services.notification_service import create_notification
from ..services.product_service import set_product_images, unique_slug
from datetime import datetime
from urllib.parse import quote
from sqlalchemy import func
from ..utils.validators import clamp_page

admin_bp = Blueprint('admin', __name__)


def _get_admin_id():
    return int(get_jwt_identity())


# ── Dashboard ──────────────────────────────────────────────────────────────────
@admin_bp.route('/dashboard', methods=['GET'])
@jwt_required()
@admin_required
def dashboard():
    """Platform totals for the admin landing page.

    Every order figure here counts both kinds of order. The previous version
    summed only PurchaseRequest, so a platform selling mostly weekly baskets
    reported a fraction of its real revenue and completed orders.
    """
    total_users = User.query.filter_by(deleted_at=None).count()

    # Count farmers by their current role, not by "has a profile row" — a
    # profile outlives a role change, so the old join over-counted.
    total_farmers = (User.query.join(Role, User.role_id == Role.id)
                     .filter(Role.name == 'farmer', User.deleted_at.is_(None)).count())
    total_products = Product.query.filter(Product.deleted_at.is_(None)).count()

    # In-flight work, across both order tables. Both share one status enum, so
    # one list covers them. The old version stopped at 'preparing' and so lost
    # sight of anything already packed or on the road.
    open_statuses = ['pending', 'admin_review', 'accepted', 'chat_active',
                     'confirmed', 'preparing', 'ready_for_pickup', 'out_for_delivery']
    active_requests = (
        PurchaseRequest.query.filter(PurchaseRequest.status.in_(open_statuses)).count()
        + FamilyPackOrder.query.filter(FamilyPackOrder.status.in_(open_statuses)).count()
    )
    completed_orders = (
        PurchaseRequest.query.filter_by(status='completed').count()
        + FamilyPackOrder.query.filter_by(status='completed').count()
    )
    pending_requests = (
        PurchaseRequest.query.filter_by(status='pending').count()
        + FamilyPackOrder.query.filter_by(status='pending').count()
    )
    pending_reports = Report.query.filter_by(status='pending').count()

    # Queues an admin is expected to act on, so the dashboard can link to them.
    pending_farmers = (FarmerProfile.query.join(User, User.id == FarmerProfile.user_id)
                       .filter(FarmerProfile.is_verified.is_(False),
                               User.deleted_at.is_(None)).count())
    pending_products = Product.query.filter(Product.is_approved.is_(False),
                                            Product.deleted_at.is_(None)).count()
    # Weekly baskets waiting on an admin, not curated packs — that feature is
    # gone. This is the queue somebody actually has to work: a basket sits at
    # 'pending' doing nothing until it is approved.
    pending_baskets = FamilyPackSubscription.query.filter(
        FamilyPackSubscription.status == 'pending').count()
    pending_reviews = Review.query.filter(Review.is_approved.is_(False)).count()

    # total_price is already the amount charged, so discounts are accounted for.
    request_revenue = db.session.query(func.sum(PurchaseRequest.total_price)).filter(
        PurchaseRequest.status == 'completed').scalar() or 0
    pack_revenue = db.session.query(func.sum(FamilyPackOrder.total_price)).filter(
        FamilyPackOrder.status == 'completed').scalar() or 0

    return jsonify({
        'total_users': total_users,
        'total_farmers': total_farmers,
        'total_products': total_products,
        'active_requests': active_requests,
        'completed_orders': completed_orders,
        'pending_requests': pending_requests,
        'pending_reports': pending_reports,
        'pending_farmers': pending_farmers,
        'pending_products': pending_products,
        'pending_baskets': pending_baskets,
        'pending_reviews': pending_reviews,
        'total_revenue': float(request_revenue) + float(pack_revenue),
        # Split out so the analytics page can show where the money comes from.
        'product_revenue': float(request_revenue),
        'basket_revenue': float(pack_revenue),
    }), 200


# ── Users ──────────────────────────────────────────────────────────────────────
@admin_bp.route('/users', methods=['GET'])
@jwt_required()
@admin_required
def list_users():
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=100)
    search = request.args.get('q', '').strip()
    role = request.args.get('role')

    query = User.query.filter(User.deleted_at.is_(None))
    if search:
        query = query.filter(db.or_(
            User.email.ilike(f'%{search}%'),
            User.first_name.ilike(f'%{search}%'),
            User.last_name.ilike(f'%{search}%'),
        ))
    if role:
        r = Role.query.filter_by(name=role).first()
        if r:
            query = query.filter_by(role_id=r.id)

    total = query.count()
    users = query.order_by(User.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    items = [u.to_dict(include_private=True) for u in users]
    return jsonify(paginate_response(items, total, page, per_page)), 200


@admin_bp.route('/users/<int:user_id>', methods=['GET'])
@jwt_required()
@admin_required
def get_user(user_id):
    user = User.query.get_or_404(user_id)
    data = user.to_dict(include_private=True)
    if user.farmer_profile:
        data['farmer_profile'] = user.farmer_profile.to_dict()
    return jsonify(data), 200


@admin_bp.route('/users/<int:user_id>/activate', methods=['PATCH'])
@jwt_required()
@admin_required
def toggle_user(user_id):
    admin_id = _get_admin_id()
    user = User.query.get_or_404(user_id)
    user.is_active = not user.is_active
    log_audit(admin_id, 'toggle_user_active', 'user', user_id,
              {'is_active': not user.is_active}, {'is_active': user.is_active})
    db.session.commit()
    return jsonify({'is_active': user.is_active}), 200


# ── Farmers ────────────────────────────────────────────────────────────────────
@admin_bp.route('/farmers', methods=['GET'])
@jwt_required()
@admin_required
def list_farmers():
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=100)
    search = request.args.get('q', '').strip()

    query = (User.query
             .join(FarmerProfile, User.id == FarmerProfile.user_id)
             .filter(User.deleted_at.is_(None)))
    if search:
        query = query.filter(db.or_(
            User.email.ilike(f'%{search}%'),
            FarmerProfile.farm_name.ilike(f'%{search}%'),
        ))

    total = query.count()
    farmers = query.offset((page-1)*per_page).limit(per_page).all()
    items = []
    for f in farmers:
        d = f.to_dict(include_private=True)
        d['farmer_profile'] = f.farmer_profile.to_dict(include_user=False)
        items.append(d)
    return jsonify(paginate_response(items, total, page, per_page)), 200


@admin_bp.route('/farmers/<int:farmer_id>/verify', methods=['PATCH'])
@jwt_required()
@admin_required
def verify_farmer(farmer_id):
    admin_id = _get_admin_id()
    fp = FarmerProfile.query.filter_by(user_id=farmer_id).first_or_404()
    fp.is_verified = not fp.is_verified
    fp.verification_date = datetime.utcnow() if fp.is_verified else None
    fp.verified_by = admin_id if fp.is_verified else None
    log_audit(admin_id, 'verify_farmer', 'farmer_profile', fp.id)
    db.session.commit()
    return jsonify({'is_verified': fp.is_verified}), 200


@admin_bp.route('/farmers/<int:farmer_id>/suspend', methods=['PATCH'])
@jwt_required()
@admin_required
def suspend_farmer(farmer_id):
    admin_id = _get_admin_id()
    fp = FarmerProfile.query.filter_by(user_id=farmer_id).first_or_404()
    fp.is_suspended = not fp.is_suspended
    log_audit(admin_id, 'suspend_farmer', 'farmer_profile', fp.id)
    db.session.commit()
    return jsonify({'is_suspended': fp.is_suspended}), 200


# ── Products ───────────────────────────────────────────────────────────────────
@admin_bp.route('/products', methods=['GET'])
@jwt_required()
@admin_required
def list_products():
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=100)
    search = request.args.get('q', '').strip()

    query = Product.query.filter(Product.deleted_at.is_(None))
    if search:
        query = query.filter(Product.name.ilike(f'%{search}%'))

    total = query.count()
    products = query.order_by(Product.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    return jsonify(paginate_response([p.to_dict() for p in products], total, page, per_page)), 200


@admin_bp.route('/products/<int:product_id>/approve', methods=['PATCH'])
@jwt_required()
@admin_required
def approve_product(product_id):
    admin_id = _get_admin_id()
    product = Product.query.get_or_404(product_id)
    product.is_approved = not product.is_approved
    log_audit(admin_id, 'approve_product', 'product', product_id)
    db.session.commit()
    return jsonify({'is_approved': product.is_approved}), 200


@admin_bp.route('/products/<int:product_id>/feature', methods=['PATCH'])
@jwt_required()
@admin_required
def toggle_feature_product(product_id):
    admin_id = _get_admin_id()
    product = Product.query.get_or_404(product_id)
    existing = FeaturedProduct.query.filter_by(product_id=product_id).first()
    if existing:
        db.session.delete(existing)
        product.is_featured = False
        featured = False
    else:
        fp = FeaturedProduct(product_id=product_id, added_by=admin_id)
        db.session.add(fp)
        product.is_featured = True
        featured = True
    log_audit(admin_id, 'feature_product', 'product', product_id)
    db.session.commit()
    return jsonify({'featured': featured}), 200


@admin_bp.route('/products/<int:product_id>/basket', methods=['PATCH'])
@jwt_required()
@admin_required
def toggle_basket_product(product_id):
    """Add or remove a product from the weekly basket catalogue.

    Adding is inert — it only makes the product appear for anyone building a
    basket. **Removing is not.** Every live basket containing it is edited and
    those customers are notified, because the alternative is F2H promising a
    weekly item it has stopped sourcing.

    The response reports how many baskets changed so the admin screen can say
    so, rather than the admin discovering it from support messages.
    """
    from ..services.family_pack_subscription_service import (
        notify_product_removed, remove_product_from_baskets)

    admin_id = _get_admin_id()
    product = Product.query.get_or_404(product_id)

    affected = []
    if product.basket_eligible:
        product.basket_eligible = False
        # Same transaction as the flag: a product delisted but still sitting in
        # baskets is the one state that must not be reachable.
        affected = remove_product_from_baskets(product)
    else:
        product.basket_eligible = True

    log_audit(admin_id, 'basket_eligible_product', 'product', product_id,
              new_data={'basket_eligible': product.basket_eligible,
                        'baskets_affected': len(affected)})
    db.session.commit()

    # After the commit — a notification for a change that rolled back is worse
    # than a late one.
    if affected:
        notify_product_removed(affected, product)

    return jsonify({
        'basket_eligible': product.basket_eligible,
        'baskets_affected': len(affected),
        'baskets_paused': sum(1 for _, emptied in affected if emptied),
    }), 200


# ── Featured Content ───────────────────────────────────────────────────────────
@admin_bp.route('/featured-farmers', methods=['GET'])
@jwt_required()
@admin_required
def get_featured_farmers():
    items = FeaturedFarmer.query.order_by(FeaturedFarmer.sort_order).all()
    return jsonify([f.to_dict() for f in items]), 200


@admin_bp.route('/featured-farmers', methods=['PUT'])
@jwt_required()
@admin_required
def set_featured_farmers():
    admin_id = _get_admin_id()
    data = request.get_json()
    farmer_ids = data.get('farmer_ids', [])

    FeaturedFarmer.query.delete()
    for i, fid in enumerate(farmer_ids):
        ff = FeaturedFarmer(farmer_id=fid, added_by=admin_id, sort_order=i)
        db.session.add(ff)
    log_audit(admin_id, 'set_featured_farmers', 'featured_farmers', None, None, {'farmer_ids': farmer_ids})
    db.session.commit()
    return jsonify({'message': 'Featured farmers updated'}), 200


@admin_bp.route('/featured-products', methods=['GET'])
@jwt_required()
@admin_required
def get_featured_products():
    items = FeaturedProduct.query.order_by(FeaturedProduct.sort_order).all()
    return jsonify([f.to_dict() for f in items]), 200


@admin_bp.route('/featured-products', methods=['PUT'])
@jwt_required()
@admin_required
def set_featured_products():
    admin_id = _get_admin_id()
    data = request.get_json()
    product_ids = data.get('product_ids', [])

    FeaturedProduct.query.delete()
    for i, pid in enumerate(product_ids):
        fp = FeaturedProduct(product_id=pid, added_by=admin_id, sort_order=i)
        db.session.add(fp)
    log_audit(admin_id, 'set_featured_products', 'featured_products', None, None, {'product_ids': product_ids})
    db.session.commit()
    return jsonify({'message': 'Featured products updated'}), 200


# ── Homepage Content ───────────────────────────────────────────────────────────
@admin_bp.route('/homepage-content', methods=['GET'])
@jwt_required()
@admin_required
def get_homepage_content():
    sections = HomepageSection.query.order_by(HomepageSection.sort_order).all()
    return jsonify([s.to_dict() for s in sections]), 200


@admin_bp.route('/homepage-content/<section_key>', methods=['PUT'])
@jwt_required()
@admin_required
def update_homepage_section(section_key):
    admin_id = _get_admin_id()
    section = HomepageSection.query.filter_by(section_key=section_key).first_or_404()
    data = request.get_json()

    allowed = ['title', 'subtitle', 'cta_label', 'cta_url', 'is_visible', 'sort_order', 'data']
    for field in allowed:
        if field in data:
            setattr(section, field, data[field])
    section.updated_by = admin_id
    log_audit(admin_id, 'update_homepage_section', 'homepage_section', section.id)
    db.session.commit()
    return jsonify(section.to_dict()), 200


# ── Categories ─────────────────────────────────────────────────────────────────
@admin_bp.route('/categories', methods=['POST'])
@jwt_required()
@admin_required
def create_category():
    admin_id = _get_admin_id()
    data = request.get_json()
    from slugify import slugify
    cat = Category(
        name=data['name'],
        slug=slugify(data['name']),
        description=data.get('description', ''),
        icon=data.get('icon', ''),
        sort_order=data.get('sort_order', 0),
    )
    db.session.add(cat)
    log_audit(admin_id, 'create_category', 'category', None, None, {'name': data['name']})
    db.session.commit()
    return jsonify(cat.to_dict()), 201


# ── Reports ────────────────────────────────────────────────────────────────────
@admin_bp.route('/reports', methods=['GET'])
@jwt_required()
@admin_required
def list_reports():
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=100)
    status = request.args.get('status', 'pending')

    query = Report.query
    if status:
        query = query.filter_by(status=status)

    total = query.count()
    reports = query.order_by(Report.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    return jsonify(paginate_response([r.to_dict() for r in reports], total, page, per_page)), 200


@admin_bp.route('/reports/<int:report_id>', methods=['PATCH'])
@jwt_required()
@admin_required
def update_report(report_id):
    admin_id = _get_admin_id()
    report = Report.query.get_or_404(report_id)
    data = request.get_json()
    report.status = data.get('status', report.status)
    report.reviewed_by = admin_id
    report.reviewed_at = datetime.utcnow()
    db.session.commit()
    return jsonify(report.to_dict()), 200


# ── Requests (Admin view) ──────────────────────────────────────────────────────
@admin_bp.route('/requests', methods=['GET'])
@jwt_required()
@admin_required
def list_all_requests():
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=100)
    status = request.args.get('status')

    query = PurchaseRequest.query
    if status:
        query = query.filter_by(status=status)
    total = query.count()
    reqs = query.order_by(PurchaseRequest.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    return jsonify(paginate_response([r.to_dict() for r in reqs], total, page, per_page)), 200


# ── Audit Logs ─────────────────────────────────────────────────────────────────
@admin_bp.route('/audit-logs', methods=['GET'])
@jwt_required()
@admin_required
def get_audit_logs():
    from ..models import AdminAuditLog
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=100)

    total = AdminAuditLog.query.count()
    logs = (AdminAuditLog.query
            .order_by(AdminAuditLog.created_at.desc())
            .offset((page-1)*per_page)
            .limit(per_page).all())
    return jsonify(paginate_response([l.to_dict() for l in logs], total, page, per_page)), 200


# ── Announcements ──────────────────────────────────────────────────────────────
@admin_bp.route('/announcements', methods=['POST'])
@jwt_required()
@admin_required
def create_announcement():
    admin_id = _get_admin_id()
    data = request.get_json()
    from ..models import Announcement
    ann = Announcement(
        created_by=admin_id,
        title=data['title'],
        content=data['content'],
        type=data.get('type', 'info'),
        target_role=data.get('target_role', 'all'),
    )
    db.session.add(ann)
    db.session.commit()
    return jsonify(ann.to_dict()), 201


@admin_bp.route('/announcements', methods=['GET'])
@jwt_required()
@admin_required
def list_announcements():
    from ..models import Announcement
    items = Announcement.query.order_by(Announcement.created_at.desc()).limit(50).all()
    return jsonify([a.to_dict() for a in items]), 200


# ── Reviews moderation ─────────────────────────────────────────────────────────
@admin_bp.route('/reviews', methods=['GET'])
@jwt_required()
@admin_required
def list_reviews():
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=100)
    is_flagged = request.args.get('flagged', type=lambda x: x == 'true')

    query = Review.query
    if is_flagged:
        query = query.filter_by(is_flagged=True)
    total = query.count()
    reviews = query.order_by(Review.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    return jsonify(paginate_response([r.to_dict() for r in reviews], total, page, per_page)), 200


@admin_bp.route('/reviews/<int:review_id>/approve', methods=['PATCH'])
@jwt_required()
@admin_required
def approve_review(review_id):
    admin_id = _get_admin_id()
    review = Review.query.get_or_404(review_id)
    review.is_approved = not review.is_approved
    log_audit(admin_id, 'approve_review', 'review', review_id)
    db.session.commit()
    return jsonify({'is_approved': review.is_approved}), 200


# ── Analytics ──────────────────────────────────────────────────────────────────
@admin_bp.route('/analytics', methods=['GET'])
@jwt_required()
@admin_required
def analytics():
    from sqlalchemy import extract
    from datetime import date, timedelta

    # Users over last 30 days
    thirty_ago = datetime.utcnow() - timedelta(days=30)
    new_users = User.query.filter(User.created_at >= thirty_ago, User.deleted_at.is_(None)).count()
    new_requests = (
        PurchaseRequest.query.filter(PurchaseRequest.created_at >= thirty_ago).count()
        + FamilyPackOrder.query.filter(FamilyPackOrder.created_at >= thirty_ago).count()
    )

    # Both order tables, so this agrees with total_revenue on the dashboard.
    revenue = (db.session.query(func.sum(PurchaseRequest.total_price)).filter(
        PurchaseRequest.status == 'completed',
        PurchaseRequest.created_at >= thirty_ago
    ).scalar() or 0) + (db.session.query(func.sum(FamilyPackOrder.total_price)).filter(
        FamilyPackOrder.status == 'completed',
        FamilyPackOrder.created_at >= thirty_ago
    ).scalar() or 0)

    # Top categories
    from ..models import Category
    top_categories = (
        db.session.query(Category.name, func.count(Product.id).label('count'))
        .join(Product, Product.category_id == Category.id)
        .filter(Product.deleted_at.is_(None))
        .group_by(Category.id)
        .order_by(func.count(Product.id).desc())
        .limit(5).all()
    )

    return jsonify({
        'new_users_30d': new_users,
        'new_requests_30d': new_requests,
        'revenue_30d': float(revenue),
        'top_categories': [{'name': n, 'count': c} for n, c in top_categories],
    }), 200


# The curated family-pack admin screens are gone with the feature. What
# remains under these names — family_pack_orders and
# family_pack_subscriptions — is the weekly basket, which shares the tables.


def _contact(user):
    """Name and phone, for an admin who needs to ring somebody.

    Deliberately not part of any order's normal `to_dict`. A customer must not
    receive the farmer's number in an API response and vice versa — this is
    admin-only, which is why it lives here rather than on the model.
    """
    if user is None:
        return None
    return {'id': user.id, 'name': user.full_name, 'phone': user.phone}


def _place(address):
    """Where to go, and a link that will actually open a map.

    Coordinates when the address has them, falling back to the written address.
    Built server-side so both the app and the web panel get the same link, and
    so a missing address is None rather than a URL that opens an empty map.
    """
    if address is None:
        return None

    lat = float(address.latitude) if address.latitude is not None else None
    lon = float(address.longitude) if address.longitude is not None else None
    written = ', '.join(p for p in (
        getattr(address, 'address_line1', None),
        getattr(address, 'city', None),
        getattr(address, 'postal_code', None),
    ) if p)

    if lat is not None and lon is not None:
        query = f'{lat},{lon}'
    elif written:
        query = written
    else:
        return None

    return {
        'address': written or None,
        'latitude': lat,
        'longitude': lon,
        # The universal form — opens the Maps app on Android and iOS, and the
        # website on desktop, without needing platform-specific URLs.
        'maps_url': f'https://www.google.com/maps/search/?api=1&query={quote(query)}',
    }


def _farmer_payment_map(requests_, pack_orders):
    """{(order_type, id): farmer payment facts} for a page of orders, in 2 queries.

    Farmers are paid in cash at stock pickup, so "who still needs paying" is the
    question this screen answers. Looking it up per row would be a query per
    order; both sets are fetched by id instead.
    """
    from ..models.payment import Payment

    out = {}
    req_ids = [r.id for r in requests_]
    pack_ids = [o.id for o in pack_orders]

    def pack(p):
        return {
            'due': float(p.farmer_amount or 0),
            'paid_at': p.farmer_paid_at.isoformat() if p.farmer_paid_at else None,
            'paid_amount': (float(p.farmer_paid_amount)
                            if p.farmer_paid_amount is not None else None),
            'note': p.farmer_paid_note,
        }

    if req_ids:
        for p in Payment.query.filter(Payment.request_id.in_(req_ids)).all():
            out[('request', p.request_id)] = pack(p)
    if pack_ids:
        for p in Payment.query.filter(Payment.family_pack_order_id.in_(pack_ids)).all():
            out[('pack-order', p.family_pack_order_id)] = pack(p)
    return out


@admin_bp.route('/orders', methods=['GET'])
@jwt_required()
@admin_required
def list_all_orders():
    """Every order in the platform, both kinds, newest first.

    Purchase requests and family pack orders live in separate tables with no
    shared key, so they are merged in Python rather than in SQL. That is fine at
    the volumes an admin browses and keeps the two models independent — but it
    does mean the whole result set is read before slicing, so the page size is
    capped rather than trusted.

    Carries the contact numbers and a map link for each side, which is the point
    of the screen: an admin chasing a late delivery needs to phone someone and
    know where they are going.
    """
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'),
                                max_per_page=100)
    status = request.args.get('status')

    rows = []

    req_query = PurchaseRequest.query
    if status:
        req_query = req_query.filter_by(status=status)
    requests_ = req_query.order_by(PurchaseRequest.created_at.desc()).limit(500).all()

    pack_query = FamilyPackOrder.query
    if status:
        pack_query = pack_query.filter_by(status=status)
    pack_orders = pack_query.order_by(FamilyPackOrder.created_at.desc()).limit(500).all()

    # Farmer payment facts for every row, in two queries rather than a thousand.
    # This screen is where an admin decides who still needs paying at pickup, so
    # it cannot be the one place that omits it — but looking each one up in the
    # loop would issue a query per order, twice over.
    farmer_pay = _farmer_payment_map(requests_, pack_orders)

    for r in requests_:
        rows.append({
            'order_type': 'request',
            'id': r.id,
            'title': r.product.name if r.product else f'Request #{r.id}',
            'quantity': float(r.quantity),
            'unit': r.product.unit if r.product else None,
            'status': r.status,
            'payment_status': r.payment_status,
            'total_price': float(r.total_price),
            'purchase_mode': r.purchase_mode,
            'created_at': r.created_at.isoformat() if r.created_at else None,
            'customer': _contact(r.customer),
            'farmer': _contact(r.farmer),
            'delivery': _place(r.delivery_address),
            'farmer_payment': farmer_pay.get(('request', r.id)),
            # Who is carrying it. Present on both order shapes so the assign
            # control does not have to know which table a row came from.
            'assigned_delivery_id': r.assigned_delivery_id,
            'courier_name': r.courier.full_name if r.courier else None,
        })

    for o in pack_orders:
        rows.append({
            'order_type': 'pack-order',
            'id': o.id,
            'title': (o.pack.name if o.pack else None) or f'Weekly basket #{o.id}',
            'quantity': None,
            'unit': None,
            'status': o.status,
            'payment_status': o.payment_status,
            'total_price': float(o.total_price),
            'purchase_mode': o.purchase_mode,
            'created_at': o.created_at.isoformat() if o.created_at else None,
            'customer': _contact(o.customer),
            'farmer': _contact(o.farmer),
            'delivery': _place(o.delivery_address),
            'farmer_payment': farmer_pay.get(('pack-order', o.id)),
            'assigned_delivery_id': o.assigned_delivery_id,
            'courier_name': o.courier.full_name if o.courier else None,
        })

    # Sorted on the ISO string, which orders correctly because the format is
    # fixed-width and zero-padded. Nulls last so a row with no timestamp does
    # not sort to the top.
    rows.sort(key=lambda r: r['created_at'] or '', reverse=True)

    total = len(rows)
    start = (page - 1) * per_page
    return jsonify(paginate_response(rows[start:start + per_page], total, page, per_page)), 200


@admin_bp.route('/family-pack-subscriptions/<int:subscription_id>/assign', methods=['PATCH'])
@jwt_required()
@admin_required
def assign_basket_courier(subscription_id):
    """Give a weekly basket a standing courier, or take it back.

    Applies to deliveries generated *from now on*. Deliveries already created
    keep whoever they were assigned at the time — some of them are out on a van,
    and silently moving those would change who is accountable for cash already
    collected. Reassign an individual week on the order itself.

    A null `delivery_id` clears it, which is how a basket returns to the pool.
    """
    data = request.get_json(silent=True) or {}
    sub = FamilyPackSubscription.query.get_or_404(subscription_id)
    raw = data.get('delivery_id')

    if raw is None:
        sub.assigned_delivery_id = None
        assignee = None
    else:
        # Same check as the per-order assign: without it an admin could name any
        # user id at all, and `party_for` would then hand that account delivery
        # powers over every basket this subscription generates.
        assignee = (User.query.join(Role, User.role_id == Role.id)
                    .filter(User.id == raw, Role.name == 'delivery',
                            User.deleted_at.is_(None))
                    .first())
        if assignee is None:
            return jsonify({'error': 'That is not a delivery account'}), 400
        if not assignee.is_active:
            return jsonify({'error': 'That delivery account is deactivated'}), 400
        sub.assigned_delivery_id = assignee.id

    log_audit(_get_admin_id(), 'assign_basket_courier', 'family_pack_subscription',
              sub.id, new_data={'delivery_id': sub.assigned_delivery_id})
    db.session.commit()

    return jsonify({
        'id': sub.id,
        'assigned_delivery_id': sub.assigned_delivery_id,
        'courier_name': assignee.full_name if assignee else None,
    }), 200


@admin_bp.route('/orders/<string:kind>/<int:order_id>/items', methods=['GET'])
@jwt_required()
@admin_required
def order_items(kind, order_id):
    """What is actually in an order: products and quantities.

    Its own endpoint rather than a field on `/admin/orders`, because that list
    reads up to 500 rows of each kind before slicing to a page. Attaching lines
    there would fetch the contents of five hundred orders to display twenty, and
    every one of them a separate query for its items.

    Both spellings of the basket are accepted, matching `/assign` — the list
    labels these rows `pack-order` and the rest of this file says basket.

    A request is a single product, so it has exactly one line. A basket has no
    lines of its own: the row links to the subscription it was generated from,
    or to a fixed pack on the older ones, and the items live there.
    """
    if kind == 'request':
        req = PurchaseRequest.query.get_or_404(order_id)
        product = req.product
        return jsonify({
            'id': req.id,
            'kind': 'request',
            'items': [{
                'product_id': req.product_id,
                'name': product.name if product else f'Product #{req.product_id}',
                'unit': product.unit if product else None,
                'primary_image': (product.primary_image.image_url
                                  if product and product.primary_image else None),
                'quantity': float(req.quantity),
                'unit_price': float(req.unit_price),
                'line_total': float(req.total_price),
            }],
        }), 200

    if kind not in ('basket', 'pack-order'):
        return jsonify({'error': "kind must be 'request' or 'pack-order'"}), 400

    order = FamilyPackOrder.query.get_or_404(order_id)

    # Subscription first, pack second. A basket generated from a subscription is
    # the normal case now; `pack_id` is only set on rows from the fixed-pack
    # days, and those still have to open.
    source = order.subscription or order.pack
    rows = list(source.items) if source is not None else []

    items = []
    for line in rows:
        product = line.product
        price = float(product.effective_price) if product else 0.0
        quantity = float(line.quantity)
        items.append({
            'product_id': line.product_id,
            'name': product.name if product else f'Product #{line.product_id}',
            'unit': line.unit or (product.unit if product else None),
            'primary_image': (product.primary_image.image_url
                              if product and product.primary_image else None),
            'quantity': quantity,
            'unit_price': price,
            'line_total': round(price * quantity, 2),
        })

    return jsonify({
        'id': order.id,
        'kind': 'pack-order',
        # Said plainly rather than left to be inferred from an empty list. A
        # basket whose subscription was deleted is not the same thing as a
        # basket with nothing in it, and an admin chasing a delivery needs to
        # know which they are looking at.
        'source': ('subscription' if order.subscription is not None
                   else 'pack' if order.pack is not None else None),
        'items': items,
    }), 200


@admin_bp.route('/family-pack-subscriptions', methods=['GET'])
@jwt_required()
@admin_required
def list_admin_family_pack_subscriptions():
    """Weekly baskets, newest first, filterable by status.

    Exists because a basket sits at 'pending' until the farmer accepts it, and
    a farmer who never opens the app leaves a customer waiting with nobody able
    to see it, let alone act. `counts` is returned alongside so the screen can
    show how many are waiting without a second request — it is the number an
    admin actually opens this page for.
    """
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'),
                                max_per_page=100)
    status = request.args.get('status')

    query = FamilyPackSubscription.query
    if status:
        query = query.filter(FamilyPackSubscription.status == status)

    total = query.count()
    subs = (query.order_by(FamilyPackSubscription.created_at.desc())
            .offset((page - 1) * per_page).limit(per_page).all())

    payload = paginate_response(
        # Items and suppliers both included: this screen is where an admin
        # decides whether to approve a basket, and that decision is "can we
        # actually source this" — which needs the items and the farms they come
        # from. Fetching them on open instead would mean opening every one.
        [s.to_dict(include_items=True, include_suppliers=True) for s in subs],
        total, page, per_page)
    payload['counts'] = {
        state: db.session.query(func.count(FamilyPackSubscription.id))
        .filter(FamilyPackSubscription.status == state).scalar()
        for state in ('pending', 'active', 'paused', 'cancelled')
    }
    return jsonify(payload), 200


@admin_bp.route('/family-pack-orders', methods=['GET'])
@jwt_required()
@admin_required
def list_admin_family_pack_orders():
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=100)
    status = request.args.get('status')

    query = FamilyPackOrder.query
    if status:
        query = query.filter_by(status=status)
    total = query.count()
    orders = query.order_by(FamilyPackOrder.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    return jsonify(paginate_response([o.to_dict() for o in orders], total, page, per_page)), 200



# ── Coupons ────────────────────────────────────────────────────────────────────
@admin_bp.route('/coupons', methods=['GET'])
@jwt_required()
@admin_required
def list_coupons():
    """Every coupon, filterable by status — this is the used/unused report."""
    from ..services import coupon_service
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=100)
    status = request.args.get('status')
    search = request.args.get('q', '').strip()

    items, total = coupon_service.list_coupons(status=status, search=search,
                                               page=page, per_page=per_page)
    payload = paginate_response([c.to_dict(include_admin=True) for c in items],
                                total, page, per_page)
    # Counts travel with the first page so the header can render without a
    # second round trip.
    payload['summary'] = coupon_service.summary()
    return jsonify(payload), 200


@admin_bp.route('/coupons', methods=['POST'])
@jwt_required()
@admin_required
def create_coupon():
    from ..services import coupon_service
    admin_id = _get_admin_id()
    try:
        coupon = coupon_service.create_coupon(admin_id, request.get_json() or {})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    log_audit(admin_id, 'create_coupon', 'coupon', coupon.id, None,
              {'code': coupon.code, 'label': coupon.label})
    db.session.commit()
    return jsonify(coupon.to_dict(include_admin=True)), 201


@admin_bp.route('/coupons/<int:coupon_id>', methods=['PUT'])
@jwt_required()
@admin_required
def update_coupon(coupon_id):
    from ..services import coupon_service
    admin_id = _get_admin_id()
    try:
        coupon = coupon_service.update_coupon(coupon_id, request.get_json() or {})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    log_audit(admin_id, 'update_coupon', 'coupon', coupon.id, None,
              {'code': coupon.code, 'label': coupon.label})
    db.session.commit()
    return jsonify(coupon.to_dict(include_admin=True)), 200


@admin_bp.route('/coupons/<int:coupon_id>', methods=['DELETE'])
@jwt_required()
@admin_required
def delete_coupon(coupon_id):
    from ..services import coupon_service
    admin_id = _get_admin_id()
    try:
        coupon_service.delete_coupon(coupon_id)
    except ValueError as e:
        # Used coupons are history, not clutter — the caller is told to
        # deactivate instead.
        return jsonify({'error': str(e)}), 400

    log_audit(admin_id, 'delete_coupon', 'coupon', coupon_id)
    db.session.commit()
    return jsonify({'message': 'Coupon deleted'}), 200


@admin_bp.route('/coupon-redemptions', methods=['GET'])
@jwt_required()
@admin_required
def list_coupon_redemptions():
    """Who redeemed what, and against which order."""
    from ..services import coupon_service
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=100)
    items, total = coupon_service.redemptions(page=page, per_page=per_page)
    return jsonify(paginate_response([r.to_dict() for r in items], total, page, per_page)), 200


# ═══════════════════════════════════════════════════════════════════════════
# PLATFORM SETTINGS
# ═══════════════════════════════════════════════════════════════════════════

@admin_bp.route('/settings', methods=['GET'])
@jwt_required()
@admin_required
def get_settings():
    """The figures an admin can change without a deploy."""
    return jsonify(PlatformSettings.get().to_dict()), 200


@admin_bp.route('/settings', methods=['PATCH'])
@jwt_required()
@admin_required
def update_settings():
    """Change the order minimum.

    Only the keys actually present in the body are touched, so a client that
    knows about one setting cannot blank the ones it has not heard of yet.

    Sending `null` is not the same as omitting the key: `null` means "go back to
    the configured default", which is the only way to undo a customisation. That
    is why the presence check below is `in data` rather than a truthiness test —
    `if data.get('min_order_value')` would treat both null *and* a deliberate
    change to a falsy number as "not supplied".
    """
    admin_id = _get_admin_id()
    data = request.get_json(silent=True) or {}
    settings = PlatformSettings.get()
    before = settings.to_dict()

    if 'min_order_value' in data:
        raw = data['min_order_value']
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            settings.min_order_value = None
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return jsonify({'error': 'Enter the minimum order as a number'}), 400
            if value != value or value in (float('inf'), float('-inf')):
                return jsonify({'error': 'Enter the minimum order as a number'}), 400
            if not (MIN_ORDER_FLOOR <= value <= MIN_ORDER_CEILING):
                # Bounded rather than free. ₹0 turns the floor off without
                # saying so, and 30000 for 300 closes the shop — both are one
                # keystroke away and neither announces itself.
                return jsonify({
                    'error': f'The minimum order must be between '
                             f'₹{MIN_ORDER_FLOOR:.0f} and ₹{MIN_ORDER_CEILING:.0f}'
                }), 400
            settings.min_order_value = round(value, 2)

    if 'delivery_charge' in data:
        raw = data['delivery_charge']
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            settings.delivery_charge = None
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return jsonify({'error': 'Enter the delivery charge as a number'}), 400
            if value != value or value in (float('inf'), float('-inf')):
                return jsonify({'error': 'Enter the delivery charge as a number'}), 400
            if not (DELIVERY_CHARGE_FLOOR <= value <= DELIVERY_CHARGE_CEILING):
                # Sending null — not 0 — is what switches the charge off. The
                # branch above handles that and never reaches here, so the
                # message can talk about the range without qualifying itself.
                return jsonify({
                    'error': f'The delivery charge must be between '
                             f'₹{DELIVERY_CHARGE_FLOOR:.0f} and ₹{DELIVERY_CHARGE_CEILING:.0f}, '
                             f'or cleared to charge nothing'
                }), 400
            settings.delivery_charge = round(value, 2)

    settings.updated_by = admin_id
    settings.updated_at = datetime.utcnow()

    after = settings.to_dict()
    # Worth an audit row: this figure decides whether every customer on the
    # platform can check out, and "who changed it and when" is the first
    # question when orders suddenly stop.
    log_audit(admin_id, 'update_platform_settings', 'platform_settings', 1,
              old_data=before, new_data=after)
    db.session.commit()

    return jsonify(after), 200


@admin_bp.route('/reports/<slug>/data', methods=['GET'])
@jwt_required()
@admin_required
def report_data(slug):
    """A report as JSON, for rendering in the admin panel.

    The same structure the spreadsheet is built from — rows, column order and
    labels, summary figures — so the table on screen and the file in Drive
    cannot show different things. The client does not decide which columns
    exist or what they are called; the report module does, and both surfaces
    read that.

    `/data` on the end rather than a bare `/reports/<slug>`, because
    `/admin/reports` already belongs to content moderation: a bare slug route
    would sit alongside `<int:report_id>` and make `/admin/reports/5` ambiguous
    between "moderation report 5" and "a report named 5". The suffix keeps the
    two namespaces from ever having to be told apart.
    """
    from ..services import report_service

    try:
        module = report_service.get(slug)
    except report_service.UnknownReport:
        return jsonify({'error': f'Unknown report {slug!r}',
                        'available': sorted(report_service.available())}), 404

    return jsonify(report_service.payload(module)), 200


@admin_bp.route('/reports/<slug>.xlsx', methods=['GET'])
@jwt_required()
@admin_required
def export_report(slug):
    """A report as a spreadsheet, downloaded now.

    Byte-for-byte the file the scheduled job publishes to Drive — same rows,
    same builder — so nobody has to work out whether the download and the
    scheduled copy differ. The only difference is who asked: this is a
    signed-in admin, that is the cron token.

    `.xlsx` in the path rather than a query parameter because browsers and
    proxies key their download behaviour off the extension, and a URL ending
    `/reports/basket-orders` that returns a spreadsheet gets saved as a file
    with no suffix surprisingly often.
    """
    from ..services import report_service
    from ..services.report_workbook import build_bytes, download_filename

    try:
        module = report_service.get(slug)
    except report_service.UnknownReport:
        return jsonify({'error': f'Unknown report {slug!r}',
                        'available': sorted(report_service.available())}), 404

    payload = report_service.payload(module)
    response = make_response(build_bytes(payload))
    response.headers['Content-Type'] = (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response.headers['Content-Disposition'] = (
        f'attachment; filename="{download_filename(payload)}"')
    # Stock and delivery dates move; a cached copy of this is worse than a slow
    # one.
    response.headers['Cache-Control'] = 'no-store'
    return response


@admin_bp.route('/reports/publish', methods=['POST'])
@jwt_required()
@admin_required
def publish_all_reports():
    """Rebuild all three reports and push them to Drive, now.

    The same job the cron runs — `report_service.publish` is the one code path,
    so the button and the schedule cannot produce different files. This exists
    because the schedule is every two days and sometimes you want the numbers
    in Drive before then: a farmer has just restocked, a basket was cancelled,
    and the copy in the folder is a day and a half old.

    Distinct from the download buttons beside it. That gets a file onto *your*
    device; this updates the copy everyone else is looking at.

    **200 even when one of the three fails**, with the outcome of each in the
    body — the same convention as `/api/cron/run`, and for a better reason than
    consistency. A 500 makes the client's error path run instead of its success
    path, so an admin who published three reports and had one fail would be
    told only that something broke, and never that the other two are now
    current. The request succeeded; some of the work did not, and the body says
    which.

    A report that was `skipped` because Drive is not configured is reported as
    skipped, not as an error — showing a red banner for a setup step nobody has
    done yet tells the admin something is broken when nothing is.
    """
    from ..services import report_service

    outcome = report_service.publish_all()
    published = [r for r in outcome['results'] if r.get('published')]

    log_audit(_get_admin_id(), 'publish_reports', 'report', None,
              new_data={'published': [r['report'] for r in published],
                        'ok': outcome['ok']})
    db.session.commit()

    return jsonify(outcome), 200


@admin_bp.route('/reports/<slug>/publish', methods=['POST'])
@jwt_required()
@admin_required
def publish_one_report(slug):
    """Rebuild one report and push it to Drive, now."""
    from ..services import report_service

    try:
        result = report_service.publish(slug)
    except report_service.UnknownReport:
        return jsonify({'error': f'Unknown report {slug!r}',
                        'available': sorted(report_service.available())}), 404
    except Exception:
        current_app.logger.exception('%s failed to publish', slug)
        return jsonify({'error': 'Could not publish — see the server log'}), 500

    log_audit(_get_admin_id(), 'publish_report', 'report', None,
              new_data={'report': slug, 'published': bool(result.get('published'))})
    db.session.commit()

    return jsonify(result), 200


# ── Service reviews ────────────────────────────────────────────────────────────
#
# Feedback about F2H itself, as opposed to `/admin/reviews`, which moderates what
# customers said about a *product*. Two different things that both got called
# reviews; this one decides what appears on the homepage.
@admin_bp.route('/service-reviews', methods=['GET'])
@jwt_required()
@admin_required
def list_service_reviews():
    """Every service review, waiting ones first.

    Pending before approved, because this is a queue: the reason to open it is
    the thing nobody has looked at yet.
    """
    from ..models import ServiceReview

    status = request.args.get('status')
    query = ServiceReview.query
    if status == 'pending':
        query = query.filter(ServiceReview.is_approved.is_(False))
    elif status == 'approved':
        query = query.filter(ServiceReview.is_approved.is_(True))

    rows = query.order_by(ServiceReview.is_approved.asc(),
                          ServiceReview.updated_at.desc()).limit(200).all()

    pending = ServiceReview.query.filter(ServiceReview.is_approved.is_(False)).count()
    approved = ServiceReview.query.filter(ServiceReview.is_approved.is_(True)).count()

    return jsonify({
        'items': [r.to_admin_dict() for r in rows],
        'counts': {'pending': pending, 'approved': approved},
    }), 200


@admin_bp.route('/service-reviews/<int:review_id>/approve', methods=['PATCH'])
@jwt_required()
@admin_required
def approve_service_review(review_id):
    """Publish a review to the homepage, or take it back down.

    Toggles, so the same control both approves and unpublishes — an approval
    that cannot be undone is one nobody wants to make.
    """
    from ..models import ServiceReview

    row = ServiceReview.query.get_or_404(review_id)
    row.is_approved = not row.is_approved
    row.approved_by = _get_admin_id() if row.is_approved else None
    row.approved_at = datetime.utcnow() if row.is_approved else None

    log_audit(_get_admin_id(), 'approve_service_review', 'service_review', row.id,
              new_data={'is_approved': row.is_approved})
    db.session.commit()

    return jsonify(row.to_admin_dict()), 200


@admin_bp.route('/service-reviews/<int:review_id>', methods=['DELETE'])
@jwt_required()
@admin_required
def delete_service_review(review_id):
    """Dismiss a review outright.

    A real delete, not a flag. The customer can leave another whenever they
    like — the row is not a record of anything that happened, it is an opinion,
    and keeping dismissed ones forever just makes the queue harder to read.
    """
    from ..models import ServiceReview

    row = ServiceReview.query.get_or_404(review_id)
    log_audit(_get_admin_id(), 'delete_service_review', 'service_review', row.id,
              old_data={'rating': row.rating, 'user_id': row.user_id})
    db.session.delete(row)
    db.session.commit()

    return jsonify({'id': review_id, 'deleted': True}), 200


# ── Weekly basket items ────────────────────────────────────────────────────────
#
# Items F2H sells inside a weekly basket, created here rather than listed by a
# farm. They are ordinary `Product` rows so that everything already built —
# categories, images, the basket builders on both clients, the buying-plan
# report — keeps working without a second kind of thing to teach it about.
#
# What makes one different is three flags set together, and never by hand:
#   basket_eligible  may go in a basket
#   basket_only      may go nowhere else
#   farmer_id        the platform seller account, not a real farm
@admin_bp.route('/basket-items', methods=['GET'])
@jwt_required()
@admin_required
def list_basket_items():
    """Every basket item, newest first."""
    rows = (Product.query
            .filter(Product.basket_only.is_(True), Product.deleted_at.is_(None))
            .order_by(Product.created_at.desc()).all())
    return jsonify([p.to_dict() for p in rows]), 200


@admin_bp.route('/basket-items', methods=['POST'])
@jwt_required()
@admin_required
def create_basket_item():
    """Create an item that exists only inside weekly baskets.

    Owned by the platform seller rather than by the admin who typed it in. An
    admin's own user id here would put the item on that person's shopfront and
    make them the seller of record on every basket containing it — including
    the farmer's share of the money. `platform_seller()` fails loudly when
    unconfigured rather than picking someone, which is the behaviour worth
    keeping.

    `available_quantity` is accepted but not meaningful: these are sourced
    against the baskets actually ordered, and `stock_service` skips them. It is
    stored so the field is not surprising if somebody looks, and defaulted high
    so any code path that still reads it does not conclude "out of stock".
    """
    from ..services.platform_seller import platform_seller

    data = request.get_json(silent=True) or {}

    for field in ('name', 'price', 'unit', 'category_id'):
        if data.get(field) in (None, ''):
            return jsonify({'error': f'{field.replace("_", " ")} is required'}), 400

    try:
        price = round(float(data['price']), 2)
    except (TypeError, ValueError):
        return jsonify({'error': 'Enter the price as a number'}), 400
    if price <= 0:
        return jsonify({'error': 'The price must be more than zero'}), 400

    if not Category.query.get(data['category_id']):
        return jsonify({'error': 'That category does not exist'}), 400

    try:
        seller = platform_seller()
    except Exception as e:
        # The message names PLATFORM_SELLER_EMAIL and what to set it to.
        return jsonify({'error': str(e)}), 400

    name = str(data['name']).strip()

    product = Product(
        farmer_id=seller.id,
        name=name,
        # NOT NULL, and this path never set it — every basket item died on
        # `Column 'slug' cannot be null`. Every basket item belongs to the one
        # platform seller, so collisions are routine and the helper counts.
        slug=unique_slug(name, seller.id),
        description=(data.get('description') or '').strip(),
        category_id=data['category_id'],
        price=price,
        unit=str(data['unit']).strip(),
        min_quantity=float(data.get('min_quantity') or 1),
        available_quantity=float(data.get('available_quantity') or 999999),
        is_active=True,
        # Admin-created, so there is nobody to approve it and no queue to sit in.
        is_approved=True,
        basket_eligible=True,
        basket_only=True,
        delivery_available=True,
        pickup_available=False,
    )
    db.session.add(product)
    db.session.flush()
    product.update_stock_status()

    # After the flush, because ProductImage rows need the product's id. Same
    # helper the farmer product form goes through, so all three payload shapes
    # work here too and a basket item photographs like any other listing.
    set_product_images(product, data)

    log_audit(_get_admin_id(), 'create_basket_item', 'product', product.id,
              new_data={'name': product.name, 'price': float(product.price)})
    db.session.commit()

    return jsonify(product.to_dict()), 201


@admin_bp.route('/basket-items/<int:product_id>', methods=['PATCH'])
@jwt_required()
@admin_required
def update_basket_item(product_id):
    """Change a basket item's name, price, unit, minimum or category."""
    product = Product.query.filter_by(id=product_id, basket_only=True).first()
    if product is None:
        return jsonify({'error': 'That is not a basket item'}), 404

    data = request.get_json(silent=True) or {}
    before = {'name': product.name, 'price': float(product.price)}

    if 'price' in data:
        try:
            price = round(float(data['price']), 2)
        except (TypeError, ValueError):
            return jsonify({'error': 'Enter the price as a number'}), 400
        if price <= 0:
            return jsonify({'error': 'The price must be more than zero'}), 400
        product.price = price

    if 'category_id' in data and data['category_id']:
        if not Category.query.get(data['category_id']):
            return jsonify({'error': 'That category does not exist'}), 400
        product.category_id = data['category_id']

    for field in ('name', 'unit', 'description'):
        if field in data and str(data[field]).strip():
            setattr(product, field, str(data[field]).strip())

    if 'min_quantity' in data:
        try:
            product.min_quantity = float(data['min_quantity'])
        except (TypeError, ValueError):
            return jsonify({'error': 'Enter the minimum as a number'}), 400

    if 'is_active' in data:
        product.is_active = bool(data['is_active'])

    # Absent means "leave the photos alone", so editing just the price from a
    # form that does not carry them cannot wipe them.
    set_product_images(product, data)

    log_audit(_get_admin_id(), 'update_basket_item', 'product', product.id,
              old_data=before, new_data={'name': product.name, 'price': float(product.price)})
    db.session.commit()

    return jsonify(product.to_dict()), 200


@admin_bp.route('/basket-items/<int:product_id>', methods=['DELETE'])
@jwt_required()
@admin_required
def retire_basket_item(product_id):
    """Retire an item. Soft delete, because baskets already reference it.

    A hard delete would orphan every subscription line and order row pointing at
    it. Deactivating takes it out of the builder while leaving the history of
    what people were actually sold intact.
    """
    product = Product.query.filter_by(id=product_id, basket_only=True).first()
    if product is None:
        return jsonify({'error': 'That is not a basket item'}), 404

    product.is_active = False
    log_audit(_get_admin_id(), 'retire_basket_item', 'product', product.id,
              old_data={'name': product.name})
    db.session.commit()

    return jsonify({'id': product.id, 'is_active': False}), 200


# ── Delivery partners ──────────────────────────────────────────────────────────
@admin_bp.route('/delivery-partners', methods=['GET'])
@jwt_required()
@admin_required
def list_delivery_partners():
    """Every delivery account, for the assign dropdown and the manage screen."""
    rows = (User.query.join(Role, User.role_id == Role.id)
            .filter(Role.name == 'delivery', User.deleted_at.is_(None))
            .order_by(User.first_name).all())

    return jsonify([{
        'id': u.id,
        'full_name': u.full_name,
        'email': u.email,
        'phone': u.phone,
        'is_active': u.is_active,
        # What each is currently carrying, so an admin assigning work can see
        # who is already loaded rather than guessing.
        'active_orders': (
            PurchaseRequest.query.filter(
                PurchaseRequest.assigned_delivery_id == u.id,
                PurchaseRequest.status.notin_(['completed', 'cancelled', 'rejected']),
            ).count()
            + FamilyPackOrder.query.filter(
                FamilyPackOrder.assigned_delivery_id == u.id,
                FamilyPackOrder.status.notin_(['completed', 'cancelled', 'rejected']),
            ).count()
        ),
    } for u in rows]), 200


@admin_bp.route('/delivery-partners', methods=['POST'])
@jwt_required()
@admin_required
def create_delivery_partner():
    """Create a delivery account.

    There is no self-registration for this role, deliberately: a delivery
    account can read customers' addresses and phone numbers and can record that
    a farmer has been handed cash. Those are not powers to hand out on a signup
    form — somebody has to decide, in person, that this individual does the job.

    The password is set here and given to them. No email is sent because the
    mail service is not configured, and a "check your inbox" that never arrives
    would be worse than telling the admin to pass it on themselves.
    """
    data = request.get_json(silent=True) or {}

    required = ('email', 'password', 'first_name', 'last_name')
    missing = [f for f in required if not (data.get(f) or '').strip()]
    if missing:
        return jsonify({'error': f'{", ".join(missing)} required'}), 400

    from ..utils.validators import phone_problem
    problem = phone_problem(data.get('phone'))
    if problem:
        # A phone number is required here where it is optional elsewhere: this
        # is the person a customer rings when nobody answers the door, and an
        # admin rings when a round goes quiet.
        return jsonify({'error': problem}), 400

    if len((data.get('password') or '')) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    from ..services.auth_service import register_user
    try:
        user = register_user(data, role_name='delivery')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    log_audit(_get_admin_id(), 'create_delivery_partner', 'user', user.id,
              new_data={'email': user.email, 'full_name': user.full_name})
    db.session.commit()

    return jsonify({'id': user.id, 'full_name': user.full_name,
                    'email': user.email, 'phone': user.phone,
                    'is_active': user.is_active, 'active_orders': 0}), 201


@admin_bp.route('/orders/<string:kind>/<int:order_id>/assign', methods=['PATCH'])
@jwt_required()
@admin_required
def assign_delivery(kind, order_id):
    """Hand an order to a delivery account, or take it back.

    `kind` names which table the order is in, because the two order types live
    in different ones and the admin screen lists them together.

    Both spellings of the basket are accepted. `/admin/orders` labels those rows
    `pack-order` and the rest of this file calls them baskets, so a caller
    passing back the `order_type` it was given would otherwise 400 on every
    basket — which is exactly what the first version of the screen did.
    Accepting both is cheaper than renaming a field other clients already read.

    A null `delivery_id` unassigns, which is how an order moves between drivers
    or returns to the pool. Not a delete — the order is untouched, only who is
    carrying it changes.
    """
    data = request.get_json(silent=True) or {}
    model = {
        'request': PurchaseRequest,
        'basket': FamilyPackOrder,
        'pack-order': FamilyPackOrder,
    }.get(kind)
    if model is None:
        return jsonify({'error': "kind must be 'request' or 'pack-order'"}), 400

    order = model.query.get_or_404(order_id)
    raw = data.get('delivery_id')

    # Read before the write below overwrites it. Reassigning an order is two
    # events, not one — somebody gains a stop and somebody else loses it — and
    # the one losing it is the person most in need of being told.
    previous_id = order.assigned_delivery_id

    if raw is None:
        order.assigned_delivery_id = None
        assignee = None
    else:
        assignee = (User.query.join(Role, User.role_id == Role.id)
                    .filter(User.id == raw, Role.name == 'delivery',
                            User.deleted_at.is_(None))
                    .first())
        # Checked rather than assumed: without this an admin could assign an
        # order to any user id at all, and `party_for` would then hand that
        # account delivery powers over it.
        if assignee is None:
            return jsonify({'error': 'That is not a delivery account'}), 400
        if not assignee.is_active:
            return jsonify({'error': 'That delivery account is deactivated'}), 400
        order.assigned_delivery_id = assignee.id

    admin_id = _get_admin_id()
    log_audit(admin_id, 'assign_delivery', kind, order_id,
              new_data={'delivery_id': order.assigned_delivery_id})

    # Told, not left to discover it. A courier is out with the phone in a
    # pocket; an assignment nobody announces is one that sits until they happen
    # to pull the list. `create_notification` queues the push and leaves it to
    # the commit below, so nothing is sent if that commit fails.
    #
    # `order_id` rather than `request_id` for both kinds: this account has one
    # screen, so the payload only has to say which order, never where to go.
    payload = {'order_id': order.id, 'kind': kind}
    noun = 'basket' if model is FamilyPackOrder else 'order'

    # Guarded on a real change. Re-saving the same courier — which the admin
    # screen allows — should not buzz a phone for nothing.
    if assignee is not None and assignee.id != previous_id:
        create_notification(
            assignee.id, admin_id, 'delivery_assigned',
            'New delivery assigned',
            f"You have a new {noun} to deliver. Open the app for the address "
            f"and the amount to collect.",
            payload)
        socketio.emit('new_notification',
                      {'type': 'delivery_assigned', **payload},
                      room=f"user_{assignee.id}")

    if previous_id and previous_id != order.assigned_delivery_id:
        create_notification(
            previous_id, admin_id, 'delivery_unassigned',
            'A delivery was reassigned',
            f"The {noun} you were carrying has been given to someone else. "
            f"Nothing further is needed from you.",
            payload)
        socketio.emit('new_notification',
                      {'type': 'delivery_unassigned', **payload},
                      room=f"user_{previous_id}")

    db.session.commit()

    return jsonify({'id': order.id, 'kind': kind,
                    'assigned_delivery_id': order.assigned_delivery_id,
                    'courier': ({'id': assignee.id, 'full_name': assignee.full_name,
                                 'phone': assignee.phone} if assignee else None)}), 200


@admin_bp.route('/delivery-cash', methods=['GET'])
@jwt_required()
@admin_required
def delivery_cash():
    """Per delivery partner: collected, handed over, and still in their pocket.

    **Collected is derived, not stored.** It is the sum of `total_price` over
    that account's completed orders — already recorded, once, on each order. A
    second running total kept in its own column would be a second thing to keep
    in step, and the first time the two disagreed nobody could say which was
    right. Only the handovers are written down, because nothing else in the
    system knows that cash moved from a pocket to a desk.

    So `outstanding` is a subtraction of one derived figure and one stored one,
    and cannot drift.

    Completed orders only. Cash is collected at the door, so an order still out
    for delivery is money nobody is holding yet.
    """
    from ..models import DeliveryRemittance

    partners = (User.query.join(Role, User.role_id == Role.id)
                .filter(Role.name == 'delivery', User.deleted_at.is_(None))
                .order_by(User.first_name).all())

    rows = []
    for u in partners:
        requests_total = db.session.query(
            func.coalesce(func.sum(PurchaseRequest.total_price), 0)
        ).filter(PurchaseRequest.assigned_delivery_id == u.id,
                 PurchaseRequest.status == 'completed').scalar() or 0

        baskets_total = db.session.query(
            func.coalesce(func.sum(FamilyPackOrder.total_price), 0)
        ).filter(FamilyPackOrder.assigned_delivery_id == u.id,
                 FamilyPackOrder.status == 'completed').scalar() or 0

        handed = db.session.query(
            func.coalesce(func.sum(DeliveryRemittance.amount), 0)
        ).filter(DeliveryRemittance.delivery_id == u.id).scalar() or 0

        collected = float(requests_total) + float(baskets_total)
        rows.append({
            'delivery_id': u.id,
            'full_name': u.full_name,
            'phone': u.phone,
            'is_active': u.is_active,
            'collected': round(collected, 2),
            'handed_over': round(float(handed), 2),
            'outstanding': round(collected - float(handed), 2),
        })

    return jsonify({
        'partners': rows,
        'totals': {
            'collected': round(sum(r['collected'] for r in rows), 2),
            'handed_over': round(sum(r['handed_over'] for r in rows), 2),
            'outstanding': round(sum(r['outstanding'] for r in rows), 2),
        },
    }), 200


@admin_bp.route('/delivery-cash/<int:delivery_id>/remittances', methods=['GET'])
@jwt_required()
@admin_required
def list_remittances(delivery_id):
    """Every handover recorded for one partner, newest first."""
    from ..models import DeliveryRemittance
    rows = (DeliveryRemittance.query
            .filter_by(delivery_id=delivery_id)
            .order_by(DeliveryRemittance.created_at.desc())
            .limit(200).all())
    return jsonify([r.to_dict() for r in rows]), 200


@admin_bp.route('/delivery-cash/<int:delivery_id>/remittances', methods=['POST'])
@jwt_required()
@admin_required
def record_remittance(delivery_id):
    """Record cash received from a delivery partner.

    Rows are never edited or deleted. A handover entered wrongly is corrected by
    recording a negative one, so the trail shows the mistake *and* the
    correction rather than quietly becoming a different history — which is why
    a negative amount is accepted here rather than rejected as nonsense.

    Zero is refused, because it is never a real handover and is almost always a
    half-finished form.
    """
    from ..models import DeliveryRemittance

    data = request.get_json(silent=True) or {}
    partner = (User.query.join(Role, User.role_id == Role.id)
               .filter(User.id == delivery_id, Role.name == 'delivery').first())
    if partner is None:
        return jsonify({'error': 'That is not a delivery account'}), 404

    try:
        amount = round(float(data.get('amount')), 2)
    except (TypeError, ValueError):
        return jsonify({'error': 'Enter the amount as a number'}), 400
    if amount != amount or amount in (float('inf'), float('-inf')) or amount == 0:
        return jsonify({'error': 'Enter an amount other than zero'}), 400

    row = DeliveryRemittance(
        delivery_id=delivery_id,
        amount=amount,
        received_by=_get_admin_id(),
        note=(data.get('note') or '').strip()[:255] or None,
    )
    db.session.add(row)
    db.session.flush()

    log_audit(_get_admin_id(), 'record_remittance', 'delivery_remittance', row.id,
              new_data={'delivery_id': delivery_id, 'amount': amount})
    db.session.commit()

    return jsonify(row.to_dict()), 201


# ── Push diagnostics ───────────────────────────────────────────────────────────
@admin_bp.route('/push/self-test', methods=['POST'])
@jwt_required()
@admin_required
def push_self_test():
    """Send a test notification to the calling admin's own devices.

    "I am not getting notifications" has four unrelated causes that produce
    identical symptoms — see `push_service.diagnose`. This sends a real push
    down the real code path and reports which link is broken.

    Only ever to the caller's own registered devices. Aiming a test at another
    account would make this a way for an admin to make an arbitrary user's
    phone buzz, which is not a diagnostic.

    **200 even when the send fails.** The failure is the answer here; routing it
    through the client's error path would replace a specific reason with a
    generic "request failed" and lose the whole point.
    """
    from ..services.push_service import diagnose

    try:
        return jsonify(diagnose(_get_admin_id())), 200
    except Exception:
        current_app.logger.exception('Push self-test raised')
        return jsonify({
            'verdict': 'error',
            'push_problem': 'The self-test itself failed — see the server log',
            'devices': [], 'device_count': 0,
            'attempted': 0, 'delivered': 0, 'failures': [],
            'background_ok': None, 'async_mode': None,
        }), 200
