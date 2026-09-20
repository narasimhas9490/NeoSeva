from flask import Blueprint

from app.common.auth import service
from app.core.auth import app_header, refresh_token_from_request
from app.core.envelope import body, no_content, ok

bp = Blueprint("auth", __name__, url_prefix="/auth")


@bp.post("/otp/request")
def otp_request():
    """Send a login code by SMS.
    The code is never in the response, only the challenge id.
    Resend and daily limits are separate 429 codes."""
    return ok(service.request_otp(body()))


@bp.post("/otp/verify")
def otp_verify():
    """Check a login code and sign in.
    X-App says which app is signing in, and so which profile to create.
    Wrong, expired and too many tries each have their own code."""
    return ok(service.verify_otp(body(), app_header()))


@bp.post("/token/refresh")
def token_refresh():
    """Get a new access token with a refresh token.
    Same shape as a verified code.
    401 SESSION_EXPIRED sends the app back to the phone screen."""
    return ok(service.refresh(refresh_token_from_request(body())))


@bp.post("/logout")
def logout():
    """Sign out of one app by revoking its refresh token.
    The other app's session is untouched.
    Always 204."""
    service.logout(refresh_token_from_request(body()))
    return no_content()
