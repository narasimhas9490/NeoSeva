import logging
from pathlib import Path

from flask import Blueprint, current_app, g, request, send_from_directory

from app.core.adapters.storage import storage
from app.core.auth import require_user
from app.core.db import run, tx
from app.core.envelope import created
from app.core.errors import ApiError, bad_request, not_found
from app.core.ids import new_id

bp = Blueprint("media", __name__)
log = logging.getLogger("neoseva.media")

PURPOSES = ("REQUEST_PHOTO", "PARTNER_PROFILE_PHOTO", "IDENTITY_DOCUMENT", "IDENTITY_SELFIE", "COMPLAINT_EVIDENCE")
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def sniff(data):
    """Tell an image's real type from its first bytes.
    The declared content type is never trusted.
    Returns a content type or None for anything that is not JPEG, PNG or WebP."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def store_image(cfg, purpose, upload_file, user_id=None, admin=None):
    """Check one uploaded picture, store it and record it in media.
    Owned by an app user or, for the admin console, by an admin name.
    Returns the media id, the sniffed content type and the public URL (None when private)."""
    data = upload_file.read(cfg.media_max_bytes + 1)
    if len(data) > cfg.media_max_bytes:
        raise ApiError(
            413,
            "MEDIA_TOO_LARGE",
            f"The image is larger than {cfg.media_max_bytes // (1024 * 1024)} MB.",
            {"maxBytes": cfg.media_max_bytes},
        )
    content_type = sniff(data)
    if content_type is None or content_type not in cfg.media_allowed_content_types:
        raise ApiError(
            415, "MEDIA_TYPE_NOT_ALLOWED", "This kind of file is not accepted.", {"allowed": cfg.media_allowed_content_types}
        )
    media_id = new_id("med")
    key = f"{purpose.lower()}/{media_id}.{EXTENSIONS[content_type]}"
    try:
        url = storage(cfg).save(purpose, key, data)
    except OSError:
        log.exception("storing %s failed", key)
        raise ApiError(502, "MEDIA_UPLOAD_FAILED", "The image could not be stored. Try again.")
    with tx() as conn:
        run(
            conn,
            """INSERT INTO media (id, user_id, uploaded_by_admin, purpose, content_type, storage_key, url, thumbnail_url, bytes)
               VALUES (:id, :u, :a, :p, :ct, :k, :url, :url, :b)""",
            id=media_id,
            u=user_id,
            a=admin,
            p=purpose,
            ct=content_type,
            k=key,
            url=url,
            b=len(data),
        )
    return media_id, content_type, url


@bp.post("/media")
@require_user()
def upload():
    """Accept one picture on its own and return its id.
    Everything afterwards refers to the picture by id, never by bytes.
    Too large, wrong type and storage failure each have their own code."""
    cfg = current_app.config["NS"]
    purpose = request.form.get("purpose")
    upload_file = request.files.get("file")
    if purpose not in PURPOSES:
        raise bad_request("INVALID_MEDIA_PURPOSE", f"purpose must be one of {', '.join(PURPOSES)}.")
    if upload_file is None:
        raise bad_request("MEDIA_FILE_REQUIRED", "Send the image in a part named file.")
    media_id, content_type, _ = store_image(cfg, purpose, upload_file, user_id=g.user_id)
    return created({"mediaId": media_id, "contentType": content_type})


@bp.get("/media/<path:key>")
def serve_public(key):
    """Serve a public image such as a profile or request photo.
    Only the public root is reachable; identity images never are.
    Unknown files are 404."""
    root = Path(current_app.config["NS"].media_public_root).resolve()
    if not (root / key).resolve().is_relative_to(root) or not (root / key).is_file():
        raise not_found()
    return send_from_directory(root, key)
