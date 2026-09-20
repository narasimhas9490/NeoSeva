import hmac
import secrets
from datetime import timedelta
from functools import wraps

from flask import current_app, g, request
from werkzeug.security import check_password_hash, generate_password_hash

from app.core import clock
from app.core.auth import hash_secret
from app.core.db import one, run, tx
from app.core.errors import ApiError
from app.core.ids import new_id

COOKIE = "nsv_admin"


def create_admin(conn, username, password):
    """Create an admin user with a hashed password.
    Does nothing when the username already exists.
    Returns the admin id, new or existing."""
    existing = one(conn, "SELECT id FROM admin_user WHERE username = :u", u=username)
    if existing:
        return existing["id"]
    admin_id = new_id("adm")
    run(
        conn,
        "INSERT INTO admin_user (id, username, password_hash) VALUES (:id, :u, :h)",
        id=admin_id,
        u=username,
        h=generate_password_hash(password),
    )
    return admin_id


def login(username, password):
    """Check an admin's password and open a session.
    Returns the session token for the cookie.
    A wrong name or password is one indistinguishable 401."""
    cfg = current_app.config["NS"]
    with tx() as conn:
        admin = one(conn, "SELECT * FROM admin_user WHERE username = :u AND is_active", u=username)
        if admin is None or not check_password_hash(admin["password_hash"], password or ""):
            raise ApiError(401, "INVALID_CREDENTIALS", "Wrong username or password.")
        token = secrets.token_urlsafe(32)
        run(
            conn,
            "INSERT INTO admin_session (id, admin_user_id, token_hash, expires_at) VALUES (:id, :a, :h, :exp)",
            id=new_id("ads"),
            a=admin["id"],
            h=hash_secret(token),
            exp=clock.now_utc() + timedelta(hours=cfg.admin_session_hours),
        )
    return token, admin["username"]


def logout(token):
    """Revoke the admin session behind a cookie token.
    An unknown token is ignored.
    The cookie itself is cleared by the caller."""
    if token:
        with tx() as conn:
            run(conn, "UPDATE admin_session SET revoked_at = now() WHERE token_hash = :h", h=hash_secret(token))


def current_admin():
    """Resolve the admin making this request, or None.
    Accepts the session cookie, or X-Admin-Key matching ADMIN_API_KEY for scripts.
    Sets nothing; require_admin stores the result on g."""
    cfg = current_app.config["NS"]
    key = request.headers.get("X-Admin-Key")
    if key and cfg.admin_api_key and hmac.compare_digest(key, cfg.admin_api_key):
        return "api-key"
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with tx() as conn:
        row = one(
            conn,
            """SELECT u.username FROM admin_session s JOIN admin_user u ON u.id = s.admin_user_id
               WHERE s.token_hash = :h AND s.revoked_at IS NULL AND s.expires_at > :now AND u.is_active""",
            h=hash_secret(token),
            now=clock.now_utc(),
        )
    return row["username"] if row else None


def require_admin(view):
    """Protect an admin endpoint.
    Refuses everything when ADMIN_API_ENABLED is false.
    Sets g.admin to the admin's username."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_app.config["NS"].admin_api_enabled:
            raise ApiError(404, "NOT_FOUND", "Not found.")
        admin = current_admin()
        if admin is None:
            raise ApiError(401, "ADMIN_UNAUTHENTICATED", "Sign in to the admin console.")
        g.admin = admin
        return view(*args, **kwargs)

    return wrapper
