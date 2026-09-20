from flask import Blueprint, current_app, g

from app.core import clock
from app.core.adapters.storage import storage
from app.core.auth import require_user
from app.core.db import many, one, run, scalar, tx
from app.core.envelope import body, ok
from app.core.errors import conflict, unprocessable
from app.partner.profile.service import load

bp = Blueprint("partner_identity", __name__, url_prefix="/partner/me/identity-verification")

DOCUMENT_TYPES = ("MASKED_AADHAAR", "VOTER_ID", "DRIVING_LICENCE", "PASSPORT")


def identity_shape(conn, partner_id):
    """Describe how his identity check is going.
    reviewNote is written for him to read.
    None when he has never sent anything."""
    row = one(conn, "SELECT * FROM identity_verification WHERE partner_id = :p", p=partner_id)
    if row is None:
        return None
    return {
        "status": row["status"],
        "documentType": row["document_type"],
        "submittedAt": clock.iso_utc(row["submitted_at"]),
        "reviewedAt": clock.iso_utc(row["reviewed_at"]),
        "reviewNote": row["review_note"],
    }


def delete_identity_images(conn, media_ids):
    """Delete identity images from storage and mark them deleted.
    A legal obligation once a check is decided or replaced.
    The decision is kept; the photograph is not."""
    cfg = current_app.config["NS"]
    rows = many(conn, "SELECT id, purpose, storage_key FROM media WHERE id = ANY(:ids) AND deleted_at IS NULL", ids=[m for m in media_ids if m] or [""])
    for r in rows:
        storage(cfg).delete(r["purpose"], r["storage_key"])
        run(conn, "UPDATE media SET deleted_at = now(), url = NULL, thumbnail_url = NULL WHERE id = :id", id=r["id"])


@bp.get("")
@require_user("PARTNER")
def get_identity():
    """Read the state of his identity check.
    data is null when he has not sent a document yet.
    Nothing ever reads the images back."""
    with tx() as conn:
        return ok(identity_shape(conn, g.user_id))


@bp.post("/initiate")
@require_user("PARTNER")
def initiate():
    """Send one government document and a selfie for checking.
    Both pictures must be his, uploaded for exactly these purposes.
    VERIFIED and REJECTED are final; NEEDS_ACTION may send again."""
    data = body()
    document_type = data.get("documentType")
    if document_type not in DOCUMENT_TYPES:
        raise unprocessable("INVALID_DOCUMENT_TYPE", f"documentType must be one of {', '.join(DOCUMENT_TYPES)}.")
    with tx() as conn:
        load(conn, g.user_id)
        for field, purpose in (("documentMediaId", "IDENTITY_DOCUMENT"), ("selfieMediaId", "IDENTITY_SELFIE")):
            if not scalar(
                conn,
                "SELECT 1 FROM media WHERE id = :m AND user_id = :u AND purpose = :p AND deleted_at IS NULL",
                m=data.get(field),
                u=g.user_id,
                p=purpose,
            ):
                raise unprocessable("INVALID_MEDIA_REFERENCE", f"{field} is missing or not a {purpose}.", {"field": field})
        current = one(conn, "SELECT * FROM identity_verification WHERE partner_id = :p FOR UPDATE", p=g.user_id)
        if current and current["status"] in ("VERIFIED", "REJECTED"):
            raise conflict("IDENTITY_ALREADY_DECIDED", "This check has already been decided.")
        if current and current["status"] == "PENDING":
            raise conflict("IDENTITY_ALREADY_PENDING", "Your document is already being looked at.")
        if current:
            delete_identity_images(conn, [current["document_media_id"], current["selfie_media_id"]])
        run(
            conn,
            """INSERT INTO identity_verification (partner_id, status, document_type, document_media_id,
                   selfie_media_id, submitted_at)
               VALUES (:p, 'PENDING', :t, :d, :s, :now)
               ON CONFLICT (partner_id) DO UPDATE SET status = 'PENDING', document_type = :t,
                   document_media_id = :d, selfie_media_id = :s, submitted_at = :now,
                   reviewed_at = NULL, reviewed_by = NULL, review_note = NULL""",
            p=g.user_id,
            t=document_type,
            d=data["documentMediaId"],
            s=data["selfieMediaId"],
            now=clock.now_utc(),
        )
        return ok(identity_shape(conn, g.user_id))
