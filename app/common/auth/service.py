import hmac
import logging
import re
import secrets
from datetime import timedelta

from flask import current_app

from app.config import language as config_language

from app.core import clock
from app.core.adapters.sms import SmsSendError, sms_provider
from app.core.auth import access_token, ensure_profile, find_session, hash_secret, issue_tokens
from app.core.db import many, one, run, scalar, tx
from app.core.errors import ApiError, bad_request, unprocessable
from app.core.ids import new_id
from app.shared.settings import platform_setting

log = logging.getLogger("neoseva.auth")
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def valid_phone(value):
    """Say whether a string is an E.164 phone number.
    The app sends +91 and ten digits; any E.164 form is accepted.
    Used by sign-in and by place interest."""
    return isinstance(value, str) and bool(PHONE_RE.match(value))


def _code_hash(challenge_id, code):
    """Hash a login code together with its challenge id.
    The id salts it, so equal codes never share a hash.
    Only this hash is stored, never the code."""
    return hash_secret(f"{challenge_id}:{code}")


def _new_code(cfg):
    """Pick the code to send for a login.
    With SMS switched off a configured fixed code is used, except in production.
    Otherwise OTP_LENGTH random digits."""
    if not cfg.sms_enabled and cfg.sms_fixed_otp and cfg.app_env != "production":
        return cfg.sms_fixed_otp
    return "".join(str(secrets.randbelow(10)) for _ in range(cfg.otp_length))


def request_otp(data):
    """Send a login code, enforcing the resend backoff and daily cap.
    Too soon and too many are 429s with their own codes; a vendor failure is 502.
    Returns the challenge id and the two countdowns."""
    cfg = current_app.config["NS"]
    phone, purpose = data.get("phoneNumber"), data.get("purpose", "LOGIN")
    language = config_language(data.get("language"), cfg)
    if not valid_phone(phone):
        raise bad_request("INVALID_PHONE_NUMBER", "phoneNumber must be E.164, e.g. +919876543210.")
    if purpose != "LOGIN":
        raise bad_request("INVALID_PURPOSE", "purpose must be LOGIN.")
    now = clock.now_utc()
    backoff = cfg.otp_resend_backoff_seconds or [30]
    with tx() as conn:
        daily_limit = platform_setting(conn)["otp_daily_limit"]
        today = scalar(
            conn,
            "SELECT count(*) FROM otp_request WHERE phone_e164 = :p AND created_at > :since",
            p=phone,
            since=now - timedelta(hours=24),
        )
        if today >= daily_limit:
            raise ApiError(429, "OTP_DAILY_LIMIT", "Too many codes today.", {"dailyLimit": daily_limit})
        recent = many(
            conn,
            """SELECT created_at FROM otp_request WHERE phone_e164 = :p AND consumed_at IS NULL
               AND created_at > :since ORDER BY created_at DESC""",
            p=phone,
            since=now - timedelta(hours=cfg.otp_resend_window_hours),
        )
        if recent:
            wait = backoff[min(len(recent) - 1, len(backoff) - 1)]
            elapsed = (now - recent[0]["created_at"]).total_seconds()
            if elapsed < wait:
                raise ApiError(
                    429, "OTP_RESEND_TOO_SOON", "Wait before asking again.", {"retryAfterSeconds": int(wait - elapsed) + 1}
                )
        challenge_id, code = new_id("otp"), _new_code(cfg)
        run(
            conn,
            """INSERT INTO otp_request (id, phone_e164, purpose, code_hash, expires_at, created_at)
               VALUES (:id, :p, 'LOGIN', :h, :exp, :now)""",
            id=challenge_id,
            p=phone,
            h=_code_hash(challenge_id, code),
            exp=now + timedelta(seconds=cfg.otp_ttl_seconds),
            now=now,
        )
    try:
        sms_provider(cfg).send_otp(phone, code, language)
    except SmsSendError as exc:
        log.error("SMS to %s failed: %s", phone, exc)
        if not cfg.sms_fail_silently:
            with tx() as conn:
                run(conn, "DELETE FROM otp_request WHERE id = :id", id=challenge_id)
            raise ApiError(502, "OTP_SEND_FAILED", "The code could not be sent. Try again.")
    return {
        "challengeId": challenge_id,
        "resendAfterSeconds": backoff[min(len(recent), len(backoff) - 1)],
        "expiresInSeconds": cfg.otp_ttl_seconds,
    }


def user_shape(conn, user_id):
    """Describe a signed-in person for both apps.
    The two profile flags decide onboarding or home in each app.
    One phone number is one person, whichever app they hold."""
    row = one(
        conn,
        """SELECT u.id, u.phone_e164, u.phone_verified_at,
                  EXISTS (SELECT 1 FROM customer_profile c WHERE c.user_id = u.id) AS has_customer,
                  EXISTS (SELECT 1 FROM partner_profile p WHERE p.user_id = u.id) AS has_partner
           FROM app_user u WHERE u.id = :id""",
        id=user_id,
    )
    return {
        "id": row["id"],
        "phoneNumber": row["phone_e164"],
        "phoneVerified": row["phone_verified_at"] is not None,
        "hasCustomerProfile": row["has_customer"],
        "hasPartnerProfile": row["has_partner"],
    }


def verify_otp(data, app):
    """Check a login code and sign the person in to one app.
    Wrong, expired and too-many-tries are three different answers.
    Creates the account and that app's profile on first success."""
    cfg = current_app.config["NS"]
    challenge_id, code = data.get("challengeId"), data.get("code")
    if not isinstance(challenge_id, str) or not isinstance(code, str):
        raise bad_request("INVALID_REQUEST", "challengeId and code are required strings.")
    now = clock.now_utc()
    with tx() as conn:
        challenge = one(conn, "SELECT * FROM otp_request WHERE id = :id FOR UPDATE", id=challenge_id)
        if challenge is None or challenge["consumed_at"] is not None or challenge["expires_at"] <= now:
            raise unprocessable("OTP_EXPIRED", "That code has expired.")
        if challenge["attempts"] >= cfg.otp_max_attempts:
            raise ApiError(429, "OTP_TOO_MANY_ATTEMPTS", "Too many tries.")
        if not hmac.compare_digest(_code_hash(challenge_id, code), challenge["code_hash"]):
            run(conn, "UPDATE otp_request SET attempts = attempts + 1 WHERE id = :id", id=challenge_id)
            exhausted = challenge["attempts"] + 1 >= cfg.otp_max_attempts
        else:
            exhausted = None
            run(conn, "UPDATE otp_request SET consumed_at = :now WHERE id = :id", now=now, id=challenge_id)
            user_id = scalar(conn, "SELECT id FROM app_user WHERE phone_e164 = :p", p=challenge["phone_e164"])
            if user_id is None:
                user_id = new_id("usr")
                run(
                    conn,
                    "INSERT INTO app_user (id, phone_e164, phone_verified_at, language) VALUES (:id, :p, :now, :lang)",
                    id=user_id,
                    p=challenge["phone_e164"],
                    now=now,
                    lang=config_language(None, cfg),
                )
            else:
                run(
                    conn,
                    "UPDATE app_user SET phone_verified_at = COALESCE(phone_verified_at, :now) WHERE id = :id",
                    now=now,
                    id=user_id,
                )
            ensure_profile(conn, user_id, app)
            tokens = issue_tokens(conn, user_id, app)
            return {**tokens, "user": user_shape(conn, user_id)}
    if exhausted:
        raise ApiError(429, "OTP_TOO_MANY_ATTEMPTS", "Too many tries.")
    raise unprocessable("OTP_INVALID", "That code is not right.")


def refresh(refresh_token):
    """Exchange a refresh token for a new access token.
    With rotation on, the old session is revoked and a new one issued.
    An expired or revoked token is 401 SESSION_EXPIRED."""
    cfg = current_app.config["NS"]
    with tx() as conn:
        session = find_session(conn, refresh_token, lock=True)
        if session is None:
            raise ApiError(401, "SESSION_EXPIRED", "Sign in again.")
        if cfg.jwt_rotate_refresh:
            run(conn, "UPDATE auth_session SET revoked_at = now() WHERE id = :id", id=session["id"])
            tokens = issue_tokens(conn, session["user_id"], session["app"])
        else:
            tokens = {**access_token(session["user_id"], session["app"], session["id"]), "refreshToken": refresh_token}
        return {**tokens, "user": user_shape(conn, session["user_id"])}


def logout(refresh_token):
    """Revoke the session behind a refresh token.
    Only that app's session ends; the other app stays signed in.
    An unknown token is quietly accepted."""
    with tx() as conn:
        session = find_session(conn, refresh_token, lock=True)
        if session is not None:
            run(conn, "UPDATE auth_session SET revoked_at = now() WHERE id = :id", id=session["id"])
