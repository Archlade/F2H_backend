from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..utils.decorators import admin_required
from ..utils.helpers import paginate_response, log_audit
from ..models import (User, FarmerProfile, Product, PurchaseRequest, Review, Report,
                       FeaturedFarmer, FeaturedProduct, HomepageSection, Announcement, Category)
from ..extensions import db
from datetime import datetime
from sqlalchemy import func

admin_bp = Blueprint('admin', __name__)


def _get_admin_id():
    return int(get_jwt_identity())


# ── Dashboard ──────────────────────────────────────────────────────────────────
@admin_bp.route('/dashboard', methods=['GET'])
@jwt_required()
@admin_required
def dashboard():
    total_users = User.query.filter_by(deleted_at=None).count()
    total_farmers = (User.query.join(FarmerProfile, User.id == FarmerProfile.user_id)
                     .filter(User.deleted_at.is_(None)).count())
    total_products = Product.query.filter(Product.deleted_at.is_(None)).count()
    active_requests = PurchaseRequest.query.filter(
        PurchaseRequest.status.in_(['pending', 'chat_active', 'confirmed', 'preparing'])
    ).count()
    completed_orders = PurchaseRequest.query.filter_by(status='completed').count()
    pending_requests = PurchaseRequest.query.filter_by(status='pending').count()
    pending_reports = Report.query.filter_by(status='pending').count()

    revenue = db.session.query(func.sum(PurchaseRequest.total_price)).filter_by(status='completed').scalar() or 0

    return jsonify({
        'total_users': total_users,
        'total_farmers': total_farmers,
        'total_products': total_products,
        'active_requests': active_requests,
        'completed_orders': completed_orders,
        'pending_requests': pending_requests,
        'pending_reports': pending_reports,
        'total_revenue': float(revenue),
    }), 200


# ── Users ──────────────────────────────────────────────────────────────────────
@admin_bp.route('/users', methods=['GET'])
@jwt_required()
@admin_required
def list_users():
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
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
        from ..models import Role
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
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
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
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
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
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
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
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
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
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)

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
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
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
    new_requests = PurchaseRequest.query.filter(PurchaseRequest.created_at >= thirty_ago).count()

    revenue = db.session.query(func.sum(PurchaseRequest.total_price)).filter(
        PurchaseRequest.status == 'completed',
        PurchaseRequest.created_at >= thirty_ago
    ).scalar() or 0

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
