import io
import os
import uuid
from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError
from ..extensions import db
from ..models.audit import AdminAuditLog

# iPhones shoot HEIC by default. Pillow can't read it without this plugin, so
# register it when available and fall back to a clear error when it isn't.
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover - depends on the deployment image
    HEIF_SUPPORTED = False


ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'heic', 'heif'}

# The one list. The upload route used to keep its own copy that had 'banners'
# in it while this one did not, so every admin banner image was quietly filed
# under 'misc' — the upload "succeeded", returned a URL, and displayed fine, so
# nothing ever looked broken. Two allow-lists for one decision will always drift;
# the route imports this now.
ALLOWED_SUBFOLDERS = {'products', 'avatars', 'covers', 'banners', 'misc'}

# Long edge in pixels. A 12MP phone photo is far larger than anything the site
# displays, and storing the original wastes disk and bandwidth.
MAX_DIMENSION = 2560
JPEG_QUALITY = 85


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# Leading bytes of the formats we accept — an extension alone proves nothing.
MAGIC_PREFIXES = (
    b'\xff\xd8\xff',                    # JPEG
    b'\x89PNG\r\n\x1a\n',               # PNG
)

# ISO base-media brands used by HEIC/HEIF (and AVIF, which Pillow reads too).
HEIF_BRANDS = {
    b'heic', b'heix', b'hevc', b'hevx', b'heim', b'heis', b'hevm', b'hevs',
    b'mif1', b'msf1', b'avif', b'avis',
}


def _is_heif(head: bytes) -> bool:
    return head[4:8] == b'ftyp' and head[8:12] in HEIF_BRANDS


def _looks_like_image(file):
    head = file.stream.read(12)
    file.stream.seek(0)
    if head.startswith(MAGIC_PREFIXES):
        return True
    # WEBP: 'RIFF' .... 'WEBP'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return True
    return _is_heif(head)


def _encode(image, source_format):
    """Re-encode to a web format, keeping PNG/WebP but converting HEIC to JPEG."""
    # EXIF orientation is why iPhone portraits show up rotated; bake it into the
    # pixels, then drop the metadata (it also carries the camera's GPS fix).
    image = ImageOps.exif_transpose(image)

    if max(image.size) > MAX_DIMENSION:
        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    has_alpha = image.mode in ('RGBA', 'LA') or (
        image.mode == 'P' and 'transparency' in image.info
    )
    out = io.BytesIO()

    if source_format == 'PNG' and has_alpha:
        image.convert('RGBA').save(out, format='PNG', optimize=True)
        return out.getvalue(), 'png'
    if source_format == 'WEBP':
        image.convert('RGBA' if has_alpha else 'RGB').save(out, format='WEBP', quality=JPEG_QUALITY)
        return out.getvalue(), 'webp'

    # Everything else — JPEG, HEIC, flat PNG — becomes a JPEG.
    if has_alpha:
        background = Image.new('RGB', image.size, (255, 255, 255))
        background.paste(image.convert('RGBA'), mask=image.convert('RGBA').split()[-1])
        image = background
    image.convert('RGB').save(out, format='JPEG', quality=JPEG_QUALITY, optimize=True,
                              progressive=True)
    return out.getvalue(), 'jpg'


def save_upload(file, subfolder='misc'):
    if not file or not file.filename or not allowed_file(file.filename):
        raise ValueError('Invalid file type. Allowed: JPG, PNG, WebP, HEIC')
    # Never trust a caller-supplied directory name.
    if subfolder not in ALLOWED_SUBFOLDERS:
        subfolder = 'misc'
    if not _looks_like_image(file):
        raise ValueError('That file is not a valid JPG, PNG, WebP or HEIC image')

    head = file.stream.read(12)
    file.stream.seek(0)
    if _is_heif(head) and not HEIF_SUPPORTED:
        raise ValueError(
            'HEIC photos are not supported on this server yet. Please install '
            'pillow-heif, or set your iPhone to Settings › Camera › Formats › '
            '"Most Compatible" and take the photo again.'
        )

    try:
        with Image.open(file.stream) as image:
            source_format = image.format
            data, ext = _encode(image, source_format)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError('That image could not be read. Please try another photo.') from exc

    filename = f"{uuid.uuid4().hex}.{ext}"
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], subfolder)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, filename), 'wb') as fh:
        fh.write(data)
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
