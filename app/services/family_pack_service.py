from datetime import datetime
from slugify import slugify
from ..extensions import db
from ..models import FamilyPack, FamilyPackItem, Product

def create_family_pack(farmer_id: int, data: dict):
    name = data.get('name')
    if not name:
        raise ValueError('Pack name is required')
    
    price = data.get('price')
    if price is None or float(price) <= 0:
        raise ValueError('Valid price is required')

    items_data = data.get('items', [])
    if not items_data or len(items_data) == 0:
        raise ValueError('Family pack must contain at least one item')

    base_slug = slugify(name)
    slug = base_slug
    counter = 1
    while FamilyPack.query.filter_by(slug=slug).first():
        slug = f"{base_slug}-{counter}"
        counter += 1

    pack = FamilyPack(
        farmer_id=farmer_id,
        name=name,
        slug=slug,
        description=data.get('description', ''),
        banner_image=data.get('banner_image', ''),
        price=float(price),
        is_active=data.get('is_active', True),
        is_approved=False # Requires admin approval
    )
    db.session.add(pack)
    db.session.flush()

    for item in items_data:
        product_id = item.get('product_id')
        qty = item.get('quantity')
        if not product_id or not qty or float(qty) <= 0:
            continue
        prod = Product.query.get(product_id)
        if not prod or prod.farmer_id != farmer_id or prod.deleted_at:
            raise ValueError(f"Invalid product ID {product_id} for this farmer")
        
        pack_item = FamilyPackItem(
            pack_id=pack.id,
            product_id=product_id,
            quantity=float(qty),
            unit=prod.unit
        )
        db.session.add(pack_item)

    db.session.commit()
    return pack

def update_family_pack(pack_id: int, farmer_id: int, data: dict):
    pack = FamilyPack.query.filter_by(id=pack_id, farmer_id=farmer_id, deleted_at=None).first_or_404()

    if 'name' in data and data['name']:
        pack.name = data['name']
        base_slug = slugify(pack.name)
        slug = base_slug
        counter = 1
        while FamilyPack.query.filter(FamilyPack.slug == slug, FamilyPack.id != pack.id).first():
            slug = f"{base_slug}-{counter}"
            counter += 1
        pack.slug = slug

    if 'description' in data:
        pack.description = data['description']
    if 'banner_image' in data:
        pack.banner_image = data['banner_image']
    if 'price' in data and float(data['price']) > 0:
        pack.price = float(data['price'])
    if 'is_active' in data:
        pack.is_active = bool(data['is_active'])

    if 'items' in data:
        items_data = data['items']
        if not items_data or len(items_data) == 0:
            raise ValueError('Family pack must contain at least one item')

        # Remove old items
        FamilyPackItem.query.filter_by(pack_id=pack.id).delete()

        for item in items_data:
            product_id = item.get('product_id')
            qty = item.get('quantity')
            if not product_id or not qty or float(qty) <= 0:
                continue
            prod = Product.query.get(product_id)
            if not prod or prod.farmer_id != farmer_id or prod.deleted_at:
                raise ValueError(f"Invalid product ID {product_id}")
            
            pack_item = FamilyPackItem(
                pack_id=pack.id,
                product_id=product_id,
                quantity=float(qty),
                unit=prod.unit
            )
            db.session.add(pack_item)

    pack.updated_at = datetime.utcnow()
    db.session.commit()
    return pack

def delete_family_pack(pack_id: int, farmer_id: int):
    pack = FamilyPack.query.filter_by(id=pack_id, farmer_id=farmer_id, deleted_at=None).first_or_404()
    pack.deleted_at = datetime.utcnow()
    pack.is_active = False
    db.session.commit()
    return True

def list_family_packs(farmer_id=None, is_approved=True, is_active=True, search=None, page=1, per_page=20):
    query = FamilyPack.query.filter(FamilyPack.deleted_at.is_(None))
    if farmer_id:
        query = query.filter(FamilyPack.farmer_id == farmer_id)
    if is_approved is not None:
        query = query.filter(FamilyPack.is_approved == is_approved)
    if is_active is not None:
        query = query.filter(FamilyPack.is_active == is_active)
    if search:
        query = query.filter(FamilyPack.name.ilike(f'%{search}%'))

    total = query.count()
    packs = query.order_by(FamilyPack.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return packs, total
