from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required

from ..utils.helpers import ALLOWED_SUBFOLDERS, save_upload

uploads_bp = Blueprint('uploads', __name__)


@uploads_bp.route('/image', methods=['POST'])
@jwt_required()
def upload_image():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    subfolder = request.form.get('type', 'misc')

    # Imported rather than restated. This route kept its own copy of the list,
    # and the two disagreed about 'banners' — so banner uploads passed this
    # check and were then silently re-filed as 'misc' inside save_upload.
    # `save_upload` validates it again anyway; this only turns a bad `type`
    # into an honest error instead of a wrong folder.
    if subfolder not in ALLOWED_SUBFOLDERS:
        return jsonify({
            'error': f"Unknown upload type '{subfolder}'. "
                     f"Expected one of: {', '.join(sorted(ALLOWED_SUBFOLDERS))}."
        }), 400

    try:
        url = save_upload(file, subfolder)
        return jsonify({'url': url}), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
