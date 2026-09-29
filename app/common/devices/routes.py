from flask import Blueprint, current_app, g

from app.config import language
from app.core.auth import APPS, require_user
from app.core.db import run, tx
from app.core.envelope import body, no_content
from app.core.errors import bad_request

bp = Blueprint("devices", __name__)


@bp.put("/devices/<device_id>/push-token")
@require_user()
def put_push_token(device_id):
    """Register the push token for one app installation.
    A token already held by another device is taken from it: one device, one token.
    Also records the language this installation reads in, on the device itself,
    since one person may read the two apps in different languages; returns 204."""
    data = body()
    token, app = data.get("token"), data.get("app") or g.app
    if not isinstance(token, str) or not token.strip():
        raise bad_request("INVALID_PUSH_TOKEN", "token is required.")
    if app not in APPS:
        raise bad_request("INVALID_APP", "app must be CUSTOMER or PARTNER.")
    if data.get("platform", "ANDROID") != "ANDROID":
        raise bad_request("INVALID_PLATFORM", "platform must be ANDROID.")
    device_language = language(data["language"], current_app.config["NS"]) if data.get("language") else None
    with tx() as conn:
        run(conn, "DELETE FROM device WHERE push_token = :t AND device_id <> :d", t=token, d=device_id)
        run(
            conn,
            """INSERT INTO device (device_id, user_id, app, platform, push_token, language, updated_at)
               VALUES (:d, :u, :a, 'ANDROID', :t, :l, now())
               ON CONFLICT (device_id) DO UPDATE SET user_id = :u, app = :a, push_token = :t,
                   language = COALESCE(:l, device.language), updated_at = now()""",
            d=device_id,
            u=g.user_id,
            a=app,
            t=token,
            l=device_language,
        )
    return no_content()


@bp.delete("/devices/<device_id>/push-token")
@require_user()
def delete_push_token(device_id):
    """Stop pushes to one installation.
    Only the caller's own device is touched.
    Always 204, because the app ignores the answer."""
    with tx() as conn:
        run(
            conn,
            "UPDATE device SET push_token = NULL, updated_at = now() WHERE device_id = :d AND user_id = :u",
            d=device_id,
            u=g.user_id,
        )
    return no_content()
