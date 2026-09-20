import hashlib
import re
from datetime import timedelta
from functools import wraps

from flask import g, jsonify, make_response, request
from sqlalchemy.exc import IntegrityError

from app.core import clock
from app.core.db import as_json, one, run, tx
from app.core.envelope import meta
from app.core.errors import bad_request, conflict
from app.core.ids import new_id

SYSTEM_ADMIN_USER = "usr_system_admin"


def endpoint_pattern():
    """Describe the current endpoint as a method plus a path pattern.
    Flask's <request_id> placeholders become {request_id}.
    The pattern, not the filled path, is what a key is scoped to."""
    rule = request.url_rule.rule if request.url_rule else request.path
    return f"{request.method} {re.sub(r'<(?:[^:>]+:)?([^>]+)>', r'{\1}', rule)}"


def _replay(row):
    """Rebuild the stored first response for a retried request.
    The data is returned verbatim; meta is fresh so the clock is current.
    A stored 204 replays as an empty 204."""
    if row["status_code"] == 204 or row["response_body"] is None:
        return make_response("", row["status_code"] or 204)
    stored = row["response_body"]
    return make_response(jsonify({"data": stored.get("data"), "meta": meta()}), row["status_code"])


def idempotent(view):
    """Make a write safe to retry with the Idempotency-Key header.
    IN_FLIGHT is written first; a retry of a finished call gets the stored reply.
    A failed attempt is forgotten so it can be retried with the same key."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        key = (request.headers.get("Idempotency-Key") or "").strip()
        if not key:
            raise bad_request("IDEMPOTENCY_KEY_REQUIRED", "This endpoint needs an Idempotency-Key header.")
        user_id = getattr(g, "user_id", None) or SYSTEM_ADMIN_USER
        endpoint = endpoint_pattern()
        body_hash = hashlib.sha256(request.get_data() or b"").hexdigest()
        record_id = new_id("idm")
        try:
            with tx() as conn:
                run(
                    conn,
                    """INSERT INTO idempotency_key (id, user_id, endpoint, key, request_hash, state)
                       VALUES (:id, :u, :e, :k, :h, 'IN_FLIGHT')""",
                    id=record_id,
                    u=user_id,
                    e=endpoint,
                    k=key,
                    h=body_hash,
                )
        except IntegrityError:
            with tx() as conn:
                row = one(
                    conn,
                    "SELECT * FROM idempotency_key WHERE user_id = :u AND endpoint = :e AND key = :k",
                    u=user_id,
                    e=endpoint,
                    k=key,
                )
            if row is None:
                raise conflict("IDEMPOTENCY_IN_PROGRESS", "The first attempt is still running. Retry shortly.")
            if row["request_hash"] != body_hash:
                raise conflict("IDEMPOTENCY_CONFLICT", "This key was already used with a different body.")
            if row["state"] == "DONE":
                return _replay(row)
            raise conflict("IDEMPOTENCY_IN_PROGRESS", "The first attempt is still running. Retry shortly.")
        try:
            response = make_response(view(*args, **kwargs))
        except Exception:
            with tx() as conn:
                run(conn, "DELETE FROM idempotency_key WHERE id = :id", id=record_id)
            raise
        with tx() as conn:
            if response.status_code < 400:
                run(
                    conn,
                    """UPDATE idempotency_key SET state = 'DONE', status_code = :s,
                       response_body = CAST(:b AS jsonb), completed_at = now() WHERE id = :id""",
                    s=response.status_code,
                    b=as_json(response.get_json(silent=True)),
                    id=record_id,
                )
            else:
                run(conn, "DELETE FROM idempotency_key WHERE id = :id", id=record_id)
        return response

    return wrapper


def purge_old(hours=48):
    """Delete idempotency records older than the retry window.
    The retry window is minutes; 48 hours is generous.
    Called by the scheduler; returns how many rows went."""
    with tx() as conn:
        return run(
            conn,
            "DELETE FROM idempotency_key WHERE created_at < :cut",
            cut=clock.now_utc() - timedelta(hours=hours),
        )
