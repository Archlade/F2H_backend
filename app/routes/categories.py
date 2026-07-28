from flask import Blueprint, jsonify
from ..models import Category

categories_bp = Blueprint('categories', __name__)


@categories_bp.route('', methods=['GET'])
def list_categories():
    cats = Category.query.filter_by(is_active=True, parent_id=None).order_by(Category.sort_order).all()
    return jsonify([c.to_dict() for c in cats]), 200


@categories_bp.route('/<int:cat_id>', methods=['GET'])
def get_category(cat_id):
    cat = Category.query.get_or_404(cat_id)
    return jsonify(cat.to_dict()), 200
