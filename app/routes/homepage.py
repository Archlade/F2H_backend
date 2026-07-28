from flask import Blueprint, request, jsonify
from ..models import HomepageSection, FeaturedFarmer, FeaturedProduct
from ..extensions import db

homepage_bp = Blueprint('homepage', __name__)


@homepage_bp.route('', methods=['GET'])
def get_homepage():
    sections = HomepageSection.query.filter_by(is_visible=True).order_by(HomepageSection.sort_order).all()
    sections_dict = {s.section_key: s.to_dict() for s in sections}

    # Featured farmers
    featured_farmers = (FeaturedFarmer.query
                        .filter_by(is_active=True)
                        .order_by(FeaturedFarmer.sort_order)
                        .limit(6).all())
    sections_dict['featured_farmers_data'] = [f.to_dict() for f in featured_farmers]

    # Featured products
    featured_products = (FeaturedProduct.query
                         .filter_by(is_active=True)
                         .order_by(FeaturedProduct.sort_order)
                         .limit(8).all())
    sections_dict['featured_products_data'] = [f.to_dict() for f in featured_products]

    return jsonify(sections_dict), 200
