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


@locations_bp.route('', methods=['POST'])
@jwt_required()
def add_location():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # If setting as primary, remove others
    if data.get('is_primary'):
        Location.query.filter_by(user_id=user_id).update({'is_primary': False})

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
    data = request.get_json()

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

    required = ['address_line1', 'city', 'state', 'postal_code']
    for f in required:
        if not data.get(f):
            return jsonify({'error': f'{f} is required'}), 400

    if data.get('is_default'):
        Address.query.filter_by(user_id=user_id).update({'is_default': False})

    addr = Address(
        user_id=user_id,
        label=data.get('label'),
        recipient_name=data.get('recipient_name'),
        phone=data.get('phone'),
        address_line1=data['address_line1'],
        address_line2=data.get('address_line2'),
        city=data['city'],
        state=data['state'],
        postal_code=data['postal_code'],
        country=data.get('country', 'India'),
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
        is_default=data.get('is_default', False),
    )
    db.session.add(addr)
    db.session.commit()
    return jsonify(addr.to_dict()), 201


@locations_bp.route('/addresses/<int:addr_id>', methods=['PUT'])
@jwt_required()
def update_address(addr_id):
    user_id = int(get_jwt_identity())
    addr = Address.query.filter_by(id=addr_id, user_id=user_id).first_or_404()
    data = request.get_json()
    allowed = ['label', 'recipient_name', 'phone', 'address_line1', 'address_line2',
               'city', 'state', 'postal_code', 'latitude', 'longitude', 'is_default']
    for field in allowed:
        if field in data:
            setattr(addr, field, data[field])
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
