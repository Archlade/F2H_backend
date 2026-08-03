from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from ..services.family_pack_service import (
    create_family_pack, update_family_pack, delete_family_pack, list_family_packs
)
from ..models import FamilyPack
from ..utils.helpers import paginate_response
from ..utils.validators import clamp_page
from ..utils.decorators import current_user_role

family_packs_bp = Blueprint('family_packs', __name__)

@family_packs_bp.route('', methods=['GET'])
def get_packs():
    farmer_id = request.args.get('farmer_id', type=int)
    search = request.args.get('q', '').strip()
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=50)

    packs, total = list_family_packs(
        farmer_id=farmer_id,
        is_approved=True,
        is_active=True,
        search=search,
        page=page,
        per_page=per_page
    )
    items = [p.to_dict() for p in packs]
    return jsonify(paginate_response(items, total, page, per_page)), 200


@family_packs_bp.route('/<int:pack_id>', methods=['GET'])
def get_pack(pack_id):
    pack = FamilyPack.query.filter_by(id=pack_id, deleted_at=None).first_or_404()
    return jsonify(pack.to_dict()), 200


@family_packs_bp.route('', methods=['POST'])
@jwt_required()
def create_pack():
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    if role != 'farmer':
        return jsonify({'error': 'Only farmers can create family packs'}), 403

    data = request.get_json() or {}
    try:
        pack = create_family_pack(user_id, data)
        return jsonify(pack.to_dict()), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': 'Server error'}), 500


@family_packs_bp.route('/<int:pack_id>', methods=['PUT'])
@jwt_required()
def update_pack(pack_id):
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    if role != 'farmer':
        return jsonify({'error': 'Only farmers can update family packs'}), 403

    data = request.get_json() or {}
    try:
        pack = update_family_pack(pack_id, user_id, data)
        return jsonify(pack.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': 'Server error'}), 500


@family_packs_bp.route('/<int:pack_id>', methods=['DELETE'])
@jwt_required()
def delete_pack(pack_id):
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    if role != 'farmer':
        return jsonify({'error': 'Only farmers can delete family packs'}), 403

    try:
        delete_family_pack(pack_id, user_id)
        return jsonify({'message': 'Family pack deleted successfully'}), 200
    except Exception as e:
        return jsonify({'error': 'Server error'}), 500


@family_packs_bp.route('/my-packs', methods=['GET'])
@jwt_required()
def get_my_packs():
    user_id = int(get_jwt_identity())
    _, role = current_user_role()
    if role != 'farmer':
        return jsonify({'error': 'Only farmers can view their family packs'}), 403

    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'), max_per_page=50)

    packs, total = list_family_packs(
        farmer_id=user_id,
        is_approved=None, # Show all approved/unapproved for owner
        is_active=None,
        page=page,
        per_page=per_page
    )
    items = [p.to_dict() for p in packs]
    return jsonify(paginate_response(items, total, page, per_page)), 200
