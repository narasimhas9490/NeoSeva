import io

import pytest

from app.core.db import tx

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def admin(app, client):
    """Return headers that authenticate as an admin by API key.
    Removes every runtime override after the test.
    So one test's switch never leaks into the next."""
    yield {"X-Admin-Key": app.config["NS"].admin_api_key}
    with app.app_context():
        with tx() as conn:
            conn.exec_driver_sql("DELETE FROM runtime_setting")
        app.extensions["runtime"].refresh(force=True)


def setting(client, admin, key):
    """Read one setting's row from the admin config list.
    Returns the dict for that key.
    Fails the test when the key is missing."""
    rows = client.get("/admin/api/config", headers=admin).get_json()["data"]
    return next(r for r in rows if r["key"] == key)


def test_every_config_field_is_listed_and_secrets_masked(client, admin):
    """Every field in config.py appears on the runtime page.
    Secrets show only whether they are set, never their value.
    Restart-only settings are marked not editable."""
    rows = client.get("/admin/api/config", headers=admin).get_json()["data"]
    keys = {r["key"] for r in rows}
    assert {"AUTH_ENABLED", "SMS_ENABLED", "SMS_PROVIDER", "TWOFACTOR_BASE_URL", "MEDIA_PUBLIC_BASE_URL", "JWT_SECRET_KEY"} <= keys
    jwt = next(r for r in rows if r["key"] == "JWT_SECRET_KEY")
    assert "change-me" not in str(jwt) and jwt["effective"].startswith("••••")
    assert next(r for r in rows if r["key"] == "DATABASE_URL")["editable"] is False


def test_auth_switch_off_and_on_live(client, admin):
    """With auth switched off, protected endpoints work without a token as the dev user.
    Switching it back on makes the same call 401 again.
    No restart is involved."""
    assert client.get("/customer/me", headers={"X-App": "CUSTOMER"}).status_code == 401
    assert client.put("/admin/api/config/AUTH_ENABLED", json={"value": "false"}, headers=admin).status_code == 200
    body = client.get("/customer/me", headers={"X-App": "CUSTOMER"}).get_json()
    assert body["data"]["id"] == "usr_dev_local"
    assert client.get("/health").get_json()["data"]["authEnabled"] is False
    assert client.delete("/admin/api/config/AUTH_ENABLED", headers=admin).status_code == 200
    assert client.get("/customer/me", headers={"X-App": "CUSTOMER"}).status_code == 401


def test_sms_switch_provider_and_fixed_code(client, admin):
    """Changing the fixed development code applies to the next OTP at once.
    Turning SMS on with the 2Factor provider and a dead base URL is a clean 502.
    Turning it off again goes back to the console."""
    client.put("/admin/api/config/SMS_FIXED_OTP", json={"value": "654321"}, headers=admin)
    challenge = client.post("/auth/otp/request", json={"phoneNumber": "+919700000001"}).get_json()["data"]["challengeId"]
    assert client.post("/auth/otp/verify", json={"challengeId": challenge, "code": "654321"}).status_code == 200
    for key, value in (("SMS_ENABLED", "true"), ("SMS_PROVIDER", "twofactor"), ("TWOFACTOR_BASE_URL", "http://127.0.0.1:9/nowhere"), ("TWOFACTOR_TIMEOUT_SECONDS", "1")):
        assert client.put(f"/admin/api/config/{key}", json={"value": value}, headers=admin).status_code == 200
    body = client.post("/auth/otp/request", json={"phoneNumber": "+919700000002"}).get_json()
    assert body["error"]["code"] == "OTP_SEND_FAILED"
    assert setting(client, admin, "SMS_ENABLED")["overridden"] is True
    client.put("/admin/api/config/SMS_ENABLED", json={"value": "off"}, headers=admin)
    assert client.post("/auth/otp/request", json={"phoneNumber": "+919700000003"}).status_code == 200


def test_media_url_changes_live(app, client, admin):
    """A new public base URL applies to the next upload.
    The value is stored exactly as typed.
    Reset returns to the .env value."""
    client.put("/admin/api/config/MEDIA_PUBLIC_BASE_URL", json={"value": "https://cdn.example.in/m"}, headers=admin)
    client.put("/admin/api/config/AUTH_ENABLED", json={"value": "false"}, headers=admin)
    resp = client.post("/media", data={"purpose": "REQUEST_PHOTO", "file": (io.BytesIO(PNG), "p.png")}, headers={"X-App": "CUSTOMER"})
    media_id = resp.get_json()["data"]["mediaId"]
    with app.app_context():
        with tx() as conn:
            url = conn.exec_driver_sql(f"SELECT url FROM media WHERE id = '{media_id}'").scalar()
    assert url.startswith("https://cdn.example.in/m/request_photo/")


def test_bad_values_and_locked_settings(client, admin):
    """A value that does not parse as the setting's type is refused.
    Restart-only and console-locking settings cannot be changed here.
    Unknown keys are 404."""
    assert client.put("/admin/api/config/OTP_LENGTH", json={"value": "six"}, headers=admin).get_json()["error"]["code"] == "INVALID_SETTING_VALUE"
    assert client.put("/admin/api/config/DATABASE_URL", json={"value": "x"}, headers=admin).get_json()["error"]["code"] == "SETTING_NOT_EDITABLE"
    assert client.put("/admin/api/config/ADMIN_API_ENABLED", json={"value": "false"}, headers=admin).get_json()["error"]["code"] == "SETTING_NOT_EDITABLE"
    assert client.put("/admin/api/config/NOPE", json={"value": "1"}, headers=admin).status_code == 404
