from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..models import Location, Address
from ..extensions import db

locations_bp = Blueprint('locations', __name__)


@locations_bp.route('', methods=['GET'])
@jwt_required()
def get_locations():
    user_id = int(get_jwt_identity())
    locs = Location.query.filter_by(user_id=user_id, is_active=True).all()
    return jsonify([l.to_dict() for l in locs]), 200


# Mirrors the locations.location_type ENUM. A value outside this set reaches
# MySQL as an invalid ENUM and fails at commit with a 500, so it is caught here
# and answered as the bad request it is.
VALID_LOCATION_TYPES = ('current', 'farm', 'home', 'work', 'other')


def _clear_other_primaries(user_id, keep_id=None):
    """Only one location per user may be primary.

    `add_location` has always done this; `update_location` did not, so setting
    a location primary by PUT left the previous one primary as well. Nothing
    reads `is_primary` in a way that breaks today — the product feed asks for
    `location_type='current'` first and only falls back to the flag — but two
    rows both claiming to be the one primary is a trap for whoever writes the
    next query, and `.first()` on it is a coin flip.
    """
    query = Location.query.filter_by(user_id=user_id)
    if keep_id is not None:
        query = query.filter(Location.id != keep_id)
    query.update({'is_primary': False})


@locations_bp.route('', methods=['POST'])
@jwt_required()
def add_location():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    location_type = data.get('location_type', 'current')
    if location_type not in VALID_LOCATION_TYPES:
        return jsonify({'error': f"Unknown location type '{location_type}'. "
                                 f"Expected one of: {', '.join(VALID_LOCATION_TYPES)}."}), 400

    # If setting as primary, remove others
    if data.get('is_primary'):
        _clear_other_primaries(user_id)

    loc = Location(
        user_id=user_id,
        location_type=data.get('location_type', 'current'),
        label=data.get('label'),
        address_line1=data.get('address_line1'),
        city=data.get('city'),
        state=data.get('state'),
        postal_code=data.get('postal_code'),
        country=data.get('country', 'India'),
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
        is_primary=data.get('is_primary', False),
    )
    db.session.add(loc)
    db.session.commit()
    return jsonify(loc.to_dict()), 201


@locations_bp.route('/<int:loc_id>', methods=['PUT'])
@jwt_required()
def update_location(loc_id):
    user_id = int(get_jwt_identity())
    loc = Location.query.filter_by(id=loc_id, user_id=user_id).first_or_404()
    data = request.get_json() or {}

    if 'location_type' in data and data['location_type'] not in VALID_LOCATION_TYPES:
        return jsonify({'error': f"Unknown location type '{data['location_type']}'. "
                                 f"Expected one of: {', '.join(VALID_LOCATION_TYPES)}."}), 400

    # Same rule as creating one, which is the point — this route used to skip
    # it, so saving a farm address by PUT produced a second primary alongside
    # whichever row already held the flag.
    if data.get('is_primary'):
        _clear_other_primaries(user_id, keep_id=loc.id)

    allowed = ['location_type', 'label', 'address_line1', 'city', 'state',
               'postal_code', 'latitude', 'longitude', 'is_primary']
    for field in allowed:
        if field in data:
            setattr(loc, field, data[field])
    db.session.commit()
    return jsonify(loc.to_dict()), 200


@locations_bp.route('/<int:loc_id>', methods=['DELETE'])
@jwt_required()
def delete_location(loc_id):
    user_id = int(get_jwt_identity())
    loc = Location.query.filter_by(id=loc_id, user_id=user_id).first_or_404()
    loc.is_active = False
    db.session.commit()
    return jsonify({'message': 'Deleted'}), 200


# Addresses
@locations_bp.route('/addresses', methods=['GET'])
@jwt_required()
def get_addresses():
    user_id = int(get_jwt_identity())
    addrs = Address.query.filter_by(user_id=user_id).all()
    return jsonify([a.to_dict() for a in addrs]), 200


@locations_bp.route('/addresses', methods=['POST'])
@jwt_required()
def add_address():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required = {
        'address_line1': 'Address line 1',
        'city': 'City',
        'state': 'State',
        'postal_code': 'PIN / postal code',
    }
    for field, label in required.items():
        if not str(data.get(field) or '').strip():
            return jsonify({'error': f'{label} is required'}), 400

    # A user's first address becomes the default automatically — otherwise the
    # account can end up with saved addresses but nothing marked for checkout.
    is_default = bool(data.get('is_default'))
    if not is_default and not Address.query.filter_by(user_id=user_id).first():
        is_default = True
    if is_default:
        Address.query.filter_by(user_id=user_id).update({'is_default': False})

    def clean(field, default=None):
        value = data.get(field)
        value = value.strip() if isinstance(value, str) else value
        return value or default

    addr = Address(
        user_id=user_id,
        label=clean('label'),
        recipient_name=clean('recipient_name'),
        phone=clean('phone'),
        address_line1=clean('address_line1'),
        address_line2=clean('address_line2'),
        city=clean('city'),
        state=clean('state'),
        postal_code=clean('postal_code'),
        country=clean('country', 'India'),
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
        is_default=is_default,
    )
    db.session.add(addr)
    db.session.commit()
    return jsonify(addr.to_dict()), 201


@locations_bp.route('/addresses/<int:addr_id>', methods=['PUT'])
@jwt_required()
def update_address(addr_id):
    user_id = int(get_jwt_identity())
    addr = Address.query.filter_by(id=addr_id, user_id=user_id).first_or_404()
    data = request.get_json() or {}

    # NOT NULL in the schema — an edit must not be able to blank these out.
    for field, label in (('address_line1', 'Address line 1'), ('city', 'City'),
                         ('state', 'State'), ('postal_code', 'PIN / postal code')):
        if field in data and not str(data.get(field) or '').strip():
            return jsonify({'error': f'{label} is required'}), 400

    # Only one address can be the default, so clear the others first.
    if data.get('is_default'):
        Address.query.filter(Address.user_id == user_id,
                             Address.id != addr.id).update({'is_default': False})

    allowed = ['label', 'recipient_name', 'phone', 'address_line1', 'address_line2',
               'city', 'state', 'postal_code', 'country', 'latitude', 'longitude',
               'is_default']
    for field in allowed:
        if field in data:
            value = data[field]
            setattr(addr, field, value.strip() if isinstance(value, str) else value)
    db.session.commit()
    return jsonify(addr.to_dict()), 200


@locations_bp.route('/addresses/<int:addr_id>', methods=['DELETE'])
@jwt_required()
def delete_address(addr_id):
    user_id = int(get_jwt_identity())
    addr = Address.query.filter_by(id=addr_id, user_id=user_id).first_or_404()
    db.session.delete(addr)
    db.session.commit()
    return jsonify({'message': 'Deleted'}), 200
