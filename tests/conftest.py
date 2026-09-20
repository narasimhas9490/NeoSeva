import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine

from app import create_app
from app.config import Config
from app.core import clock
from app.core.db import tx
from scripts.migrate import migrate, reset
from seed.seed import seed_catalog, seed_geography

TZ = ZoneInfo("Asia/Kolkata")
TRANSACTIONAL = [
    "idempotency_key", "otp_request", "auth_session", "device", "media", "place_interest", "customer_profile",
    "saved_place", "request", "request_answer", "request_media", "partner_profile", "partner_service",
    "identity_verification", "offer", "opportunity_notification", "booking", "booking_pin_attempt",
    "credit_ledger_transaction", "job_report", "review", "saved_partner", "referral", "complaint",
    "admin_session", "admin_user", "app_user",
]


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    """Build the app against a freshly migrated test database.
    The catalog is seeded once; media goes to a temporary folder.
    The scheduler is off so tests drive matching themselves."""
    cfg = Config()
    cfg.auth_enabled = True
    cfg.sms_enabled = False
    cfg.sms_fixed_otp = "123456"
    cfg.scheduler_enabled = False
    media = tmp_path_factory.mktemp("media")
    cfg.media_public_root = str(media / "public")
    cfg.media_private_root = str(media / "private")
    engine = create_engine(cfg.test_database_url)
    reset(engine)
    migrate(engine, verbose=False)
    engine.dispose()
    application = create_app(cfg, database_url=cfg.test_database_url, start_scheduler=False)
    with application.app_context():
        with tx() as conn:
            seed_geography(conn)
            seed_catalog(conn)
    return application


@pytest.fixture(autouse=True)
def clean(app):
    """Empty every transactional table before each test.
    The catalog and settings seeded once are kept.
    The clock is put back to real time afterwards."""
    with app.app_context():
        with tx() as conn:
            conn.exec_driver_sql("TRUNCATE " + ", ".join(TRANSACTIONAL) + " CASCADE")
            conn.exec_driver_sql(
                "INSERT INTO app_user (id, phone_e164, language) VALUES ('usr_system_admin', '+0000000000', 'en')"
            )
            conn.exec_driver_sql("UPDATE platform_setting SET matching_batch_size = 5, pin_max_attempts = 5")
            conn.exec_driver_sql("UPDATE request_question SET template_version = 3")
            conn.exec_driver_sql("DELETE FROM runtime_setting")
        app.extensions["runtime"].refresh(force=True)
    yield
    clock.set_offset(timedelta(0))


@pytest.fixture
def client(app):
    """Return a Flask test client for the app.
    Each test gets its own cookie jar.
    Requests run synchronously in-process."""
    return app.test_client()


def at_local(hour, minute=0, days=0):
    """Move the application clock to a local wall-clock time.
    The date is today in Podalakur plus days; the time is exact.
    Returns the local date that is now 'today'."""
    clock.set_offset(timedelta(0))
    now = datetime.now(TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days)
    clock.set_offset(target - now)
    return target.date()


class Api:
    def __init__(self, client, token=None, app_name="CUSTOMER"):
        """Wrap the test client with a bearer token and the app header.
        Every call returns (status, json).
        write() adds a fresh Idempotency-Key unless one is given."""
        self.client, self.token, self.app_name = client, token, app_name

    def _headers(self, extra=None):
        """Build the headers for one call.
        Adds the bearer token and X-App when known.
        extra overrides anything set here."""
        headers = {"X-App": self.app_name}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(extra or {})
        return headers

    def call(self, method, path, body=None, headers=None):
        """Make one call and decode the JSON body if any.
        Returns (status_code, json_or_None).
        body is sent as JSON when given."""
        resp = self.client.open(path, method=method, json=body, headers=self._headers(headers))
        return resp.status_code, (resp.get_json(silent=True) if resp.data else None)

    def get(self, path):
        """GET a path.
        Returns (status, json).
        Shorthand for call."""
        return self.call("GET", path)

    def write(self, method, path, body=None, key=None):
        """Make a write call with an Idempotency-Key.
        A new key is generated unless one is passed.
        Returns (status, json)."""
        return self.call(method, path, body, {"Idempotency-Key": key or str(uuid.uuid4())})


def sign_in(client, phone, app_name="CUSTOMER"):
    """Sign a phone number in to one app through the real OTP flow.
    Uses the fixed development code from configuration.
    Returns an Api bound to the new access token."""
    anon = Api(client, app_name=app_name)
    status, body = anon.call("POST", "/auth/otp/request", {"phoneNumber": phone, "purpose": "LOGIN", "language": "te"})
    assert status == 200, body
    status, body = anon.call("POST", "/auth/otp/verify", {"challengeId": body["data"]["challengeId"], "code": "123456"})
    assert status == 200, body
    api = Api(client, body["data"]["accessToken"], app_name)
    api.user_id = body["data"]["user"]["id"]
    api.refresh = body["data"]["refreshToken"]
    return api


def active_partner(app, client, phone, base="plc_podalakur", services=(("svc_tractor", ["ROTAVATOR", "PLOUGH"]),), radius=15, name="Ravi"):
    """Create a partner through the real onboarding endpoints and approve him.
    Identity is marked VERIFIED and status ACTIVE directly in the database.
    Returns his Api."""
    p = sign_in(client, phone, "PARTNER")
    assert p.call("PATCH", "/partner/me/basic", {"fullName": name, "basePlaceId": base, "experienceRange": "5_TO_10_YEARS"})[0] == 200
    assert p.call("PUT", "/partner/me/services", {"services": [{"serviceId": s, "equipmentIds": e} for s, e in services]})[0] == 200
    assert p.call("PATCH", "/partner/me", {"travelRadiusKm": radius})[0] == 200
    assert p.write("POST", "/partner/me/activate")[0] == 200
    with app.app_context():
        with tx() as conn:
            conn.exec_driver_sql(
                f"INSERT INTO identity_verification (partner_id, status) VALUES ('{p.user_id}', 'VERIFIED')"
            )
            conn.exec_driver_sql(f"UPDATE partner_profile SET status = 'ACTIVE' WHERE user_id = '{p.user_id}'")
    return p


def tractor_request(on_date, day_part="MORNING", acres="3", work="rotavator", not_sure=False, **extra):
    """Build a valid tractor request body.
    acres may be replaced by not sure; ploughing adds the trips answer.
    extra keys override the top level of the body."""
    answers = [{"questionId": "q_work_type", "values": [work]}]
    answers.append({"questionId": "q_acres", "values": [], "notSure": True} if not_sure else {"questionId": "q_acres", "values": [acres], "unit": "ACRE"})
    if work == "ploughing":
        answers.append({"questionId": "q_trips", "values": ["2"], "unit": "TRIP"})
    body = {
        "serviceId": "svc_tractor",
        "workTypeId": work,
        "templateVersion": 3,
        "answers": answers,
        "description": "Need rotavator work",
        "schedule": {"date": on_date.isoformat(), "dayPart": day_part},
        "location": {"placeId": "plc_podalakur", "landmark": "Near the canal bridge", "latitude": 14.4171, "longitude": 79.7334, "accuracyMeters": 18.0},
        "mediaIds": [],
        "preferredPartnerId": None,
        "origin": {"source": "HOME", "optionId": None, "templateVersion": None},
    }
    body.update(extra)
    return body
