import logging

from app.core.db import many, tx

log = logging.getLogger("neoseva.push")


def notify_user(user_id, app, kind, payload):
    """Send a push to every device of a user in one app.
    Push is how somebody finds out quickly, never the only way.
    Failures are logged and swallowed; the caller never depends on delivery."""
    try:
        with tx() as conn:
            devices = many(
                conn,
                "SELECT device_id, push_token FROM device WHERE user_id = :u AND app = :a AND push_token IS NOT NULL",
                u=user_id,
                a=app,
            )
        for device in devices:
            log.info("push %s to %s/%s: %s", kind, user_id, device["device_id"], payload)
    except Exception:
        log.exception("push %s to %s failed", kind, user_id)
