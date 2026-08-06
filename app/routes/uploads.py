from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..utils.helpers import save_upload

uploads_bp = Blueprint('uploads', __name__)


@uploads_bp.route('/image', methods=['POST'])
@jwt_required()
def upload_image():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    subfolder = request.form.get('type', 'misc')
    # Validate subfolder
    allowed_subfolders = ['products', 'avatars', 'covers', 'banners', 'misc']
    if subfolder not in allowed_subfolders:
        subfolder = 'misc'
    try:
        url = save_upload(file, subfolder)
        return jsonify({'url': url}), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
