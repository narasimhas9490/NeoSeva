import hashlib
import json

from flask import Blueprint, current_app, make_response, request

from app.common.catalog import service
from app.core.db import tx
from app.core.envelope import ok, use_timezone
from app.core.errors import not_found

bp = Blueprint("catalog", __name__)


@bp.get("/catalog")
def get_catalog():
    """Serve everything both apps display, in one call with one ETag.
    If-None-Match with the current ETag gets a 304 and no body.
    The clock in meta is never cached with the body."""
    cfg = current_app.config["NS"]
    with tx() as conn:
        data, tz_name = service.build_catalog(conn, request.args.get("areaId"))
    use_timezone(tz_name)
    etag = '"' + hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16] + '"'
    if cfg.catalog_etag_enabled and etag in (request.headers.get("If-None-Match") or ""):
        response = make_response("", 304)
    else:
        response = ok(data)
    if cfg.catalog_etag_enabled:
        response.headers["ETag"] = etag
    response.headers["Cache-Control"] = f"private, max-age={cfg.catalog_cache_seconds}"
    return response


@bp.get("/catalog/services/<service_id>/request-template")
def get_request_template(service_id):
    """Serve the questions for one service.
    A service without questions is a real state and returns an empty list.
    An unknown service is 404 SERVICE_NOT_FOUND."""
    with tx() as conn:
        data = service.request_template(conn, service_id)
    if data is None:
        raise not_found("SERVICE_NOT_FOUND", "No such service.")
    return ok(data)
