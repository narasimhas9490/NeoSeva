from flask import current_app, g, jsonify, make_response, request

from app.core import clock
from app.core.ids import new_id


def request_id():
    """Return the id of the current API call, creating it once.
    Every response carries it in meta.requestId for support messages.
    Shaped api_<ULID> like every other identifier."""
    if not getattr(g, "request_id", None):
        g.request_id = new_id("api")
    return g.request_id


def use_timezone(tz_name):
    """Set which geography timezone this response reports in.
    Handlers that know the caller's area call it; others use the default.
    serverTime is always local to that area and zoneless."""
    if tz_name:
        g.tz = tz_name


def meta(**extra):
    """Build the meta block every response carries.
    serverTime is the area's local wall clock, sent without a zone.
    Extra keys such as nextCursor are merged in."""
    tz_name = getattr(g, "tz", None) or current_app.config["NS"].default_timezone
    body = {
        "requestId": request_id(),
        "serverTime": clock.zoneless_local(clock.now_utc(), tz_name),
        "timezone": tz_name,
    }
    body.update(extra)
    return body


def ok(data, status=200, **meta_extra):
    """Return a success envelope with data and meta.
    Lists put paging in meta, e.g. ok(rows, nextCursor=...).
    Status defaults to 200."""
    return make_response(jsonify({"data": data, "meta": meta(**meta_extra)}), status)


def created(data):
    """Return a 201 success envelope.
    Used when a request, offer, booking or upload was created.
    The body carries the full created shape, never just an id."""
    return ok(data, 201)


def no_content():
    """Return an empty 204 response.
    Used for logout, push tokens, on-my-way and decline.
    Nothing is written in the body."""
    return make_response("", 204)


def accepted_empty():
    """Return an empty 202 response.
    Used for place interest, which the app deliberately ignores.
    Nothing is written in the body."""
    return make_response("", 202)


def error_body(code, message, details=None):
    """Build the error envelope for a failure.
    code is what the app branches on; message is never shown to a user.
    details is always an object, possibly empty."""
    return {"error": {"code": code, "message": message, "details": details or {}}, "meta": meta()}


def body():
    """Read the JSON body of the current request as a dict.
    An absent body reads as an empty dict.
    A body that is not a JSON object is a 400 INVALID_JSON."""
    from app.core.errors import bad_request

    if not request.data:
        return {}
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise bad_request("INVALID_JSON", "The request body must be a JSON object.")
    return data
