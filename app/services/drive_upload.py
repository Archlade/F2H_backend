"""Uploading a generated file to Google Drive, from the server.

Uses the service account already on disk for Firebase push
(`secrets/firebase-service-account.json`). A second credential would be a second
thing to rotate and a second thing to leak; the same identity can hold both
scopes.

── Why a service account needs a folder shared with it ─────────────────────────

A service account is not a person and has no Drive of its own that anybody can
open. Uploading without a parent folder succeeds and then the file is
effectively invisible — it lives in storage nobody has a UI for. So
`DRIVE_FOLDER_ID` is required rather than optional, and the folder must be
shared with the service account's email as an Editor. That share is what gives
the account somewhere real to write and gives you somewhere real to look.

── Why it replaces rather than appends ────────────────────────────────────────

Each run updates the *same* file if one with the same name is already in the
folder. Creating a new file every two days would give you a folder of near
-identical spreadsheets and a link that goes stale immediately. Updating in
place means the Drive link, any bookmark, and anything referencing the file by
id all keep working, and Drive keeps the previous versions in its own revision
history if you ever need one back.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Only what is needed to create and update the app's own files. `drive.file`
# grants access to files this credential created or that were explicitly shared
# with it — not the whole Drive, which `drive` would.
SCOPES = ['https://www.googleapis.com/auth/drive.file']

XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class DriveNotConfigured(RuntimeError):
    """Raised when the pieces are not in place, with the missing piece named."""


def _service(credentials_path):
    # Imported here, not at module scope: google-api-python-client is only
    # needed by this one job, and a missing dependency must not stop the whole
    # app from booting. The error below says exactly what to install.
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - depends on deployment
        raise DriveNotConfigured(
            'google-api-python-client and google-auth are not installed. '
            'Run: pip install google-api-python-client google-auth'
        ) from exc

    credentials = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES)
    # cache_discovery=False because the default file cache warns loudly under
    # any non-writable working directory, which is most containers.
    return build('drive', 'v3', credentials=credentials, cache_discovery=False)


def upload(data, filename, folder_id, credentials_path):
    """Create or update `filename` in `folder_id`. Returns the file's metadata.

    `data` is the file's bytes. Raises `DriveNotConfigured` when something is
    missing, so the caller can skip cleanly and say why rather than failing the
    whole cron run.
    """
    from googleapiclient.http import MediaIoBaseUpload  # local, see _service
    import io

    if not folder_id:
        raise DriveNotConfigured('DRIVE_FOLDER_ID is not set')
    if not credentials_path or not os.path.isfile(credentials_path):
        raise DriveNotConfigured(
            f'Service account file not found at {credentials_path!r}')

    service = _service(credentials_path)
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=XLSX_MIME, resumable=False)

    # `name = '...'` needs the quotes escaped, and `trashed = false` matters:
    # without it a file the user deleted still matches, and the update puts the
    # new content straight back into the bin.
    safe = filename.replace("'", "\\'")
    query = (f"name = '{safe}' and '{folder_id}' in parents and trashed = false")
    existing = service.files().list(
        q=query, fields='files(id, name)', pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get('files', [])

    if existing:
        file_id = existing[0]['id']
        result = service.files().update(
            fileId=file_id, media_body=media,
            fields='id, name, webViewLink, modifiedTime',
            supportsAllDrives=True,
        ).execute()
        logger.info('Drive: updated %s (%s)', result.get('name'), file_id)
    else:
        result = service.files().create(
            body={'name': filename, 'parents': [folder_id]},
            media_body=media,
            fields='id, name, webViewLink, modifiedTime',
            supportsAllDrives=True,
        ).execute()
        logger.info('Drive: created %s (%s)', result.get('name'), result.get('id'))

    return result
