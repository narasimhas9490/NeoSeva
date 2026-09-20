import logging

from flask import current_app

from app.admin.api.crud import _key_where
from app.admin.api.registry import RESOURCES
from app.core.adapters.storage import storage
from app.core.db import one, run, scalar, tx

log = logging.getLogger("neoseva.admin.images")

ADMIN_PURPOSES = ("SERVICE_ICON",)


def in_use(conn, url):
    """Say whether any image column still holds this URL.
    A picture in use must never be deleted from disk.
    Columns come from the registry: every column of type image."""
    if not url:
        return False
    return any(
        scalar(conn, f'SELECT 1 FROM {r.table} WHERE "{c.name}" = :u LIMIT 1', u=url)
        for r in RESOURCES
        for c in r.columns
        if c.type == "image"
    )


def discard(conn, media_id):
    """Soft-delete one admin upload and return its (purpose, key) for file removal.
    Returns None when it is unknown, already gone or not an admin purpose.
    Raises LookupError when a row still points at it."""
    row = one(conn, "SELECT * FROM media WHERE id = :id AND deleted_at IS NULL", id=media_id)
    if row is None or row["purpose"] not in ADMIN_PURPOSES:
        return None
    if in_use(conn, row["url"]):
        raise LookupError(media_id)
    run(conn, "UPDATE media SET deleted_at = now() WHERE id = :id", id=media_id)
    return row["purpose"], row["storage_key"]


def remove_file(purpose, key):
    """Delete a stored file after its media row is retired.
    A disk failure is logged, not raised; the row is already gone.
    Call this after the transaction commits."""
    try:
        storage(current_app.config["NS"]).delete(purpose, key)
    except OSError:
        log.exception("removing %s failed", key)


def old_urls(conn, res, key, new_values=None):
    """Read the image URLs a row holds now that a write would drop.
    With new_values, only image columns being changed to something else count.
    With none (a delete), every image column counts. Call before the write."""
    cols = [c.name for c in res.columns if c.type == "image" and (new_values is None or c.name in new_values)]
    if not cols or not key:
        return []
    where, params = _key_where(res, key)
    select = ", ".join(f'"{c}"' for c in cols)
    row = one(conn, f"SELECT {select} FROM {res.table} WHERE {where}", **params)
    if row is None:
        return []
    return [row[c] for c in cols if row[c] and (new_values is None or (new_values[c] or None) != row[c])]


def retire(urls):
    """Remove the admin uploads behind URLs no row points at any more.
    URLs that match no admin upload, such as ones typed by hand, are left alone.
    Call after the transaction that dropped them has committed."""
    for url in urls:
        with tx() as conn:
            media = one(conn, "SELECT id FROM media WHERE url = :u AND purpose = ANY(:p) AND deleted_at IS NULL", u=url, p=list(ADMIN_PURPOSES))
            try:
                gone = discard(conn, media["id"]) if media else None
            except LookupError:
                gone = None
        if gone:
            remove_file(*gone)
