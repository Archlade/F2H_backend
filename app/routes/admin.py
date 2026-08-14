from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..utils.decorators import admin_required
from ..utils.helpers import paginate_response, log_audit
from ..models import (User, Role, FarmerProfile, Product, PurchaseRequest, Review, Report,
                       FeaturedFarmer, FeaturedProduct, HomepageSection, Announcement, Category,
                       FamilyPackOrder, FamilyPackSubscription)

from ..extensions import db
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
        })

    # Sorted on the ISO string, which orders correctly because the format is
    # fixed-width and zero-padded. Nulls last so a row with no timestamp does
    # not sort to the top.
    rows.sort(key=lambda r: r['created_at'] or '', reverse=True)

    total = len(rows)
    start = (page - 1) * per_page
    return jsonify(paginate_response(rows[start:start + per_page], total, page, per_page)), 200


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
