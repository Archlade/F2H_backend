import math
from ..extensions import db
from ..models import Product, ProductImage, Discount, Location, RecentlyViewed
from ..models.user import User
from slugify import slugify


def calculate_distance(lat1, lon1, lat2, lon2):
    """Haversine formula — returns distance in km."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    R = 6371
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lon2) - float(lon1))
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_products(filters: dict, customer_lat=None, customer_lon=None, page=1, per_page=20):
    query = Product.query.filter(
        Product.deleted_at.is_(None),
        Product.is_active == True,
        Product.is_approved == True,
    )

    if filters.get('category_id'):
        query = query.filter(Product.category_id == filters['category_id'])
    if filters.get('category_slug'):
        from ..models.category import Category
        cat = Category.query.filter_by(slug=filters['category_slug']).first()
        if cat:
            query = query.filter(Product.category_id == cat.id)
    if filters.get('farmer_id'):
        query = query.filter(Product.farmer_id == filters['farmer_id'])
    if filters.get('min_price') is not None:
        query = query.filter(Product.price >= filters['min_price'])
    if filters.get('max_price') is not None:
        query = query.filter(Product.price <= filters['max_price'])
    if filters.get('is_organic'):
        query = query.filter(Product.is_organic == True)
    if filters.get('delivery_available'):
        query = query.filter(Product.delivery_available == True)
    if filters.get('pickup_available'):
        query = query.filter(Product.pickup_available == True)
    if filters.get('stock_status'):
        query = query.filter(Product.stock_status == filters['stock_status'])
    if filters.get('search'):
        search_term = f"%{filters['search']}%"
        query = query.filter(
            db.or_(Product.name.ilike(search_term), Product.description.ilike(search_term))
        )
    if filters.get('has_discount'):
        query = query.join(Discount, Product.id == Discount.product_id).filter(Discount.is_active == True)

    sort = filters.get('sort', 'newest')
    if sort == 'newest':
        query = query.order_by(Product.created_at.desc())
    elif sort == 'price_asc':
        query = query.order_by(Product.price.asc())
    elif sort == 'price_desc':
        query = query.order_by(Product.price.desc())
    elif sort == 'rating':
        query = query.order_by(Product.rating_avg.desc())
    elif sort == 'popular':
        query = query.order_by(Product.view_count.desc())
    # distance sort is handled after fetching

    total = query.count()
    products = query.offset((page - 1) * per_page).limit(per_page).all()

    result = []
    for p in products:
        dist = None
        if customer_lat and customer_lon:
            farm_loc = Location.query.filter_by(user_id=p.farmer_id, location_type='farm', is_active=True).first()
            if farm_loc:
                dist = calculate_distance(customer_lat, customer_lon,
                                          farm_loc.latitude, farm_loc.longitude)
        result.append({'product': p, 'distance': dist})

    if sort == 'distance' and customer_lat and customer_lon:
        result.sort(key=lambda x: (x['distance'] is None, x['distance'] or 9999))

    return result, total


def get_product_by_id(product_id: int):
    return Product.query.filter_by(id=product_id, deleted_at=None).first()


def image_urls_from(data: dict) -> list:
    """Pull a plain list of image URLs out of whatever the client sent.

    Three shapes are in circulation and all of them have to work:

        image_urls: ["https://…", …]          the documented contract (website)
        images:     ["https://…", …]          older clients
        images:     [{"image_url": "…", …}]   the Flutter app, which mirrors the
                                              shape the API *returns*

    The third one is why publishing a listing with photos used to fail: the
    dict was assigned straight to ProductImage.image_url, which is a String
    column, and the insert blew up at commit time. Normalising here means one
    rule for create and update instead of update quietly being the only one
    that got it right.

    Precedence is on presence, not truthiness: an explicit `image_urls: []`
    means "remove every photo" and must not fall through to `images`, or a
    farmer could never delete the last picture on a listing.
    """
    raw = data.get('image_urls')
    if raw is None:
        raw = data.get('images')
    if not raw:
        return []

    urls = []
    for item in raw:
        if isinstance(item, str):
            url = item
        elif isinstance(item, dict):
            url = item.get('image_url') or item.get('url')
        else:
            url = None
        if url and isinstance(url, str) and url.strip():
            urls.append(url.strip())
    return urls


def create_product(farmer_id: int, data: dict):
    slug = slugify(data['name'])
    existing = Product.query.filter_by(slug=slug, farmer_id=farmer_id).first()
    if existing:
        slug = f"{slug}-{farmer_id}"

    product = Product(
        farmer_id=farmer_id,
        category_id=data['category_id'],
        name=data['name'].strip(),
        slug=slug,
        description=data.get('description', ''),
        price=data['price'],
        unit=data.get('unit', 'kg'),
        min_quantity=data.get('min_quantity', 1),
        available_quantity=data.get('available_quantity', 0),
        is_organic=data.get('is_organic', False),
        is_natural=data.get('is_natural', False),
        is_farm_grown=data.get('is_farm_grown', True),
        delivery_available=data.get('delivery_available', True),
        pickup_available=data.get('pickup_available', True),
        low_stock_threshold=data.get('low_stock_threshold', 5),
    )
    product.update_stock_status()
    db.session.add(product)
    db.session.flush()

    for idx, url in enumerate(image_urls_from(data)):
        img = ProductImage(
            product_id=product.id,
            image_url=url,
            is_primary=(idx == 0),
            sort_order=idx,
        )
        db.session.add(img)

    db.session.commit()
    return product


def update_product(product_id: int, farmer_id: int, data: dict):
    product = Product.query.filter_by(id=product_id, farmer_id=farmer_id, deleted_at=None).first()
    if not product:
        return None

    allowed = ['name', 'description', 'price', 'unit', 'min_quantity', 'available_quantity',
               'is_organic', 'is_natural', 'is_farm_grown', 'delivery_available',
               'pickup_available', 'is_active', 'category_id', 'low_stock_threshold']
    for field in allowed:
        if field in data:
            setattr(product, field, data[field])

    # Replace the image set when the client sends one
    if 'image_urls' in data or 'images' in data:
        # delete-orphan cascade removes the rows that are no longer listed
        product.images = [
            ProductImage(image_url=url, is_primary=(idx == 0), sort_order=idx)
            for idx, url in enumerate(image_urls_from(data))
        ]

    product.update_stock_status()
    db.session.commit()
    return product


def delete_product(product_id: int, farmer_id: int):
    product = Product.query.filter_by(id=product_id, farmer_id=farmer_id, deleted_at=None).first()
    if not product:
        return False
    from datetime import datetime
    product.deleted_at = datetime.utcnow()
    product.is_active = False
    db.session.commit()
    return True


def apply_discount(product_id: int, farmer_id: int, data: dict):
    product = Product.query.filter_by(id=product_id, farmer_id=farmer_id, deleted_at=None).first()
    if not product:
        return None

    # Calculate discounted price on backend
    if data['discount_type'] == 'percentage':
        disc_value = min(float(data['discount_value']), 99.0)
        discounted = float(product.price) * (1 - disc_value / 100)
    else:
        discounted = max(0, float(product.price) - float(data['discount_value']))

    if product.discount:
        product.discount.discount_type = data['discount_type']
        product.discount.discount_value = data['discount_value']
        product.discount.discounted_price = round(discounted, 2)
        product.discount.is_active = True
    else:
        disc = Discount(
            product_id=product.id,
            discount_type=data['discount_type'],
            discount_value=data['discount_value'],
            discounted_price=round(discounted, 2),
        )
        db.session.add(disc)

    db.session.commit()
    return product


def remove_discount(product_id: int, farmer_id: int):
    product = Product.query.filter_by(id=product_id, farmer_id=farmer_id, deleted_at=None).first()
    if not product or not product.discount:
        return False
    product.discount.is_active = False
    db.session.commit()
    return True


def track_view(product_id: int, user_id: int):
    product = Product.query.get(product_id)
    if product:
        product.view_count = (product.view_count or 0) + 1
        if user_id:
            rv = RecentlyViewed.query.filter_by(user_id=user_id, product_id=product_id).first()
            if rv:
                from datetime import datetime
                rv.viewed_at = datetime.utcnow()
            else:
                rv = RecentlyViewed(user_id=user_id, product_id=product_id)
                db.session.add(rv)
        db.session.commit()
