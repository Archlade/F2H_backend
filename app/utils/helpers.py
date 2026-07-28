import os
import uuid
from flask import current_app
from werkzeug.utils import secure_filename
from ..extensions import db
from ..models.audit import AdminAuditLog


ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload(file, subfolder='misc'):
    if not file or not allowed_file(file.filename):
        raise ValueError('Invalid file type. Allowed: jpg, jpeg, png, webp')
    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], subfolder)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    file.save(path)
    return f"/uploads/{subfolder}/{filename}"


def paginate_response(items, total, page, per_page):
    return {
        'items': items,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page,
        'has_next': page * per_page < total,
        'has_prev': page > 1,
    }


def log_audit(admin_id, action, entity_type=None, entity_id=None,
              old_data=None, new_data=None, ip_address=None, user_agent=None):
    log = AdminAuditLog(
        admin_id=admin_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_data=old_data,
        new_data=new_data,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.session.add(log)
    # Caller commits
    return log
