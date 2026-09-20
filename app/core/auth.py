import hashlib
import secrets
from datetime import timedelta
from functools import wraps

import jwt
from flask import current_app, g, request

from app.core import clock
from app.core.db import one, run, tx
from app.core.errors import ApiError
from app.core.ids import new_id

APPS = ("CUSTOMER", "PARTNER")


def hash_secret(value):
    """Hash a secret such as a refresh token or an OTP for storage.
    A database dump must never be a list of live credentials.
    SHA-256 hex; the secrets themselves are long and random."""
    return hashlib.sha256(value.encode()).hexdigest()


def _cfg():
    """Return the application's Config object.
    Kept in app.config['NS'] by create_app.
    Short helper so auth code stays readable."""
    return current_app.config["NS"]


def app_header(default="CUSTOMER"):
    """Read which app is calling from the X-App header.
    One phone number can hold both apps, so the app decides which profile.
    Unknown or missing values fall back to the default."""
    value = (request.headers.get("X-App") or "").strip().upper()
    return value if value in APPS else default


def issue_tokens(conn, user_id, app):
    """Create a session and return an access and a refresh token.
    The refresh token is stored hashed; the access token is a signed JWT.
    One session per app, so logging out of one keeps the other."""
    cfg = _cfg()
    now = clock.now_utc()
    session_id = new_id("ses")
    refresh = f"{new_id('rft')}.{secrets.token_urlsafe(32)}"
    run(
        conn,
        """INSERT INTO auth_session (id, user_id, refresh_token_hash, app, issued_at, expires_at)
           VALUES (:id, :uid, :h, :app, :now, :exp)""",
        id=session_id,
        uid=user_id,
        h=hash_secret(refresh),
        app=app,
        now=now,
        exp=now + timedelta(days=cfg.jwt_refresh_ttl_days),
    )
    return {**access_token(user_id, app, session_id), "refreshToken": refresh}


def access_token(user_id, app, session_id):
    """Sign a short-lived access token for a session.
    Carries the user, the app and the session id.
    Returns accessToken and expiresAtEpochSeconds, in seconds."""
    cfg = _cfg()
    now = clock.now_utc()
    expires = now + timedelta(seconds=cfg.jwt_access_ttl_seconds)
    token = jwt.encode(
        {
            "sub": user_id,
            "app": app,
            "sid": session_id,
            "iss": cfg.jwt_issuer,
            "aud": cfg.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        },
        cfg.jwt_secret_key,
        algorithm=cfg.jwt_algorithm,
    )
    return {"accessToken": token, "expiresAtEpochSeconds": int(expires.timestamp())}


def find_session(conn, refresh_token, lock=False):
    """Look up a live session by its refresh token.
    Expired or revoked sessions are treated as missing.
    Returns the session row or None."""
    if not refresh_token:
        return None
    row = one(
        conn,
        "SELECT * FROM auth_session WHERE refresh_token_hash = :h" + (" FOR UPDATE" if lock else ""),
        h=hash_secret(refresh_token),
    )
    if row is None or row["revoked_at"] is not None or row["expires_at"] <= clock.now_utc():
        return None
    return row


def refresh_token_from_request(data):
    """Find the refresh token sent with a refresh or logout call.
    Accepts it in the body as refreshToken or as a Bearer header.
    Returns None when neither is present."""
    token = data.get("refreshToken") if isinstance(data, dict) else None
    if not token:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            token = header[7:].strip()
    return token


def ensure_dev_user(conn, app):
    """Create the fake development user and its profile for an app.
    Only used when AUTH_ENABLED is false.
    Idempotent, so every request can call it."""
    cfg = _cfg()
    run(
        conn,
        """INSERT INTO app_user (id, phone_e164, phone_verified_at) VALUES (:id, :p, now())
           ON CONFLICT (id) DO NOTHING""",
        id=cfg.dev_fake_user_id,
        p=cfg.dev_fake_user_phone,
    )
    ensure_profile(conn, cfg.dev_fake_user_id, app)


def ensure_profile(conn, user_id, app):
    """Create the customer or partner profile for a user if missing.
    Verifying in an app is what turns a phone into an account there.
    A new partner starts REGISTERED at the BASIC_PROFILE step."""
    if app == "PARTNER":
        run(
            conn,
            """INSERT INTO partner_profile (user_id, display_name, status, next_step)
               VALUES (:u, '', 'REGISTERED', 'BASIC_PROFILE') ON CONFLICT (user_id) DO NOTHING""",
            u=user_id,
        )
    else:
        run(conn, "INSERT INTO customer_profile (user_id) VALUES (:u) ON CONFLICT (user_id) DO NOTHING", u=user_id)


def _authenticate(expected_app):
    """Resolve the caller from the bearer token into g.user_id and g.app.
    With auth disabled the caller is the fake development user.
    A missing, expired or foreign token is a 401; the wrong app is a 403."""
    cfg = _cfg()
    if not cfg.auth_enabled:
        app = expected_app or app_header()
        with tx() as conn:
            ensure_dev_user(conn, app)
        g.user_id, g.app = cfg.dev_fake_user_id, app
        return
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise ApiError(401, "UNAUTHENTICATED", "A bearer token is required.")
    try:
        claims = jwt.decode(
            header[7:].strip(),
            cfg.jwt_secret_key,
            algorithms=[cfg.jwt_algorithm],
            audience=cfg.jwt_audience,
            issuer=cfg.jwt_issuer,
            options={"verify_exp": False, "verify_iat": False, "require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError:
        raise ApiError(401, "UNAUTHENTICATED", "The access token is not valid.")
    if claims["exp"] + cfg.jwt_leeway_seconds < clock.now_utc().timestamp():
        raise ApiError(401, "TOKEN_EXPIRED", "The access token has expired.")
    if expected_app and claims.get("app") != expected_app:
        raise ApiError(403, "WRONG_APP", f"This endpoint belongs to the {expected_app.lower()} app.")
    g.user_id, g.app = claims["sub"], claims.get("app")


def require_user(app=None):
    """Protect an endpoint with a user access token.
    app limits it to tokens issued to the CUSTOMER or PARTNER app.
    Sets g.user_id and g.app before the view runs."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            _authenticate(app)
            return view(*args, **kwargs)

        return wrapper

    return decorator
