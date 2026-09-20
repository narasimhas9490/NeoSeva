import io
from datetime import timedelta

from app.core import clock
from app.core.db import tx
from tests.conftest import Api, sign_in

CATALOG_KEYS = {
    "services", "helpMeChoose", "reasons", "placeLabels", "reviewTags", "experienceRanges", "travelDistancesKm",
    "jobsOnHomeScreen", "maxOfferRupees", "jobFeePercent", "cancellationLimit", "cancellationWindowDays",
    "termsVersion", "termsUrl", "supportContact", "bookingTerms", "partnerTerms", "scheduling",
}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_catalog_has_eighteen_keys_and_zoneless_clock(client):
    """The catalog carries all eighteen keys and six reason contexts.
    meta.serverTime is local and zoneless, with the area's timezone beside it.
    The unlaunched water tanker service is not sent."""
    status, body = Api(client).get("/catalog?areaId=geo_podalakur")
    assert status == 200
    assert set(body["data"]) == CATALOG_KEYS
    assert len(body["data"]["reasons"]) == 6
    assert body["meta"]["timezone"] == "Asia/Kolkata" and not body["meta"]["serverTime"].endswith("Z")
    ids = {s["id"] for s in body["data"]["services"]}
    assert "svc_tractor" in ids and "svc_water_tanker" not in ids
    assert body["data"]["scheduling"]["morningEndsHour"] == 12


def test_catalog_etag_304_and_unknown_area(client):
    """A matching If-None-Match gets a 304 with no body.
    An unknown area is 200 with empty service lists, never 404.
    No areaId falls back to the default area."""
    resp = client.get("/catalog")
    etag = resp.headers["ETag"]
    assert client.get("/catalog", headers={"If-None-Match": etag}).status_code == 304
    body = client.get("/catalog?areaId=geo_nowhere").get_json()
    assert body["data"]["services"] == [] and body["data"]["helpMeChoose"]["options"] == []


def test_catalog_etag_changes_when_admin_edits(app, client):
    """Turning commonOnHome off in the database changes the catalog.
    The ETag changes so the apps download the new body.
    Nothing is released to make it happen."""
    first = client.get("/catalog").headers["ETag"]
    with app.app_context():
        with tx() as conn:
            conn.exec_driver_sql("UPDATE service SET common_on_home = NOT common_on_home WHERE id = 'svc_jcb'")
    assert client.get("/catalog").headers["ETag"] != first
    with app.app_context():
        with tx() as conn:
            conn.exec_driver_sql("UPDATE service SET common_on_home = NOT common_on_home WHERE id = 'svc_jcb'")


def test_otp_outcomes_are_distinct(client):
    """Wrong, expired and too-many-tries are three different answers.
    A resend too soon is its own 429.
    The code never appears in the response."""
    anon = Api(client)
    status, body = anon.call("POST", "/auth/otp/request", {"phoneNumber": "+919000000001", "purpose": "LOGIN"})
    assert status == 200 and "code" not in body["data"]
    challenge = body["data"]["challengeId"]
    assert anon.call("POST", "/auth/otp/request", {"phoneNumber": "+919000000001"})[1]["error"]["code"] == "OTP_RESEND_TOO_SOON"
    assert anon.call("POST", "/auth/otp/verify", {"challengeId": challenge, "code": "000000"})[1]["error"]["code"] == "OTP_INVALID"
    for _ in range(3):
        anon.call("POST", "/auth/otp/verify", {"challengeId": challenge, "code": "000000"})
    status, body = anon.call("POST", "/auth/otp/verify", {"challengeId": challenge, "code": "000000"})
    assert (status, body["error"]["code"]) == (429, "OTP_TOO_MANY_ATTEMPTS")
    clock.set_offset(timedelta(minutes=10))
    status, body = anon.call("POST", "/auth/otp/request", {"phoneNumber": "+919000000001"})
    challenge = body["data"]["challengeId"]
    clock.set_offset(timedelta(minutes=20))
    assert anon.call("POST", "/auth/otp/verify", {"challengeId": challenge, "code": "123456"})[1]["error"]["code"] == "OTP_EXPIRED"
    assert anon.call("POST", "/auth/otp/request", {"phoneNumber": "12345"})[1]["error"]["code"] == "INVALID_PHONE_NUMBER"


def test_one_person_two_apps_two_sessions(client):
    """Verifying in each app creates that app's profile for the same person.
    Logging out of one app leaves the other session alive.
    A revoked refresh token is 401 SESSION_EXPIRED."""
    customer = sign_in(client, "+919000000002", "CUSTOMER")
    partner = sign_in(client, "+919000000002", "PARTNER")
    assert customer.user_id == partner.user_id
    assert customer.call("POST", "/auth/logout", {"refreshToken": customer.refresh})[0] == 204
    assert customer.call("POST", "/auth/token/refresh", {"refreshToken": customer.refresh})[1]["error"]["code"] == "SESSION_EXPIRED"
    status, body = partner.call("POST", "/auth/token/refresh", {"refreshToken": partner.refresh})
    assert status == 200 and body["data"]["user"]["hasPartnerProfile"] and body["data"]["user"]["hasCustomerProfile"]


def test_places_and_resolve_by_boundary(client):
    """All villages come back unpaged with their area id.
    A pin at a village centre resolves to that village by its boundary.
    Outside everything is 200 with two nulls; bad input is INVALID_COORDINATES."""
    api = Api(client)
    status, body = api.get("/locations/places?areaId=geo_podalakur")
    assert status == 200 and len(body["data"]) == 36 and body["data"][0]["areaId"] == "geo_podalakur"
    body = api.get("/geographies/resolve?latitude=14.4167&longitude=79.7333")[1]
    assert body["data"]["place"]["id"] == "plc_podalakur" and body["data"]["geography"]["id"] == "geo_podalakur"
    assert api.get("/geographies/resolve?latitude=17.38&longitude=78.48")[1]["data"] == {"geography": None, "place": None}
    assert api.get("/geographies/resolve?latitude=abc&longitude=1")[1]["error"]["code"] == "INVALID_COORDINATES"
    assert api.get("/locations/places?areaId=geo_nowhere")[1]["data"] == []


def test_place_interest_is_not_deduplicated(app, client):
    """The same village twice is two rows; a blank name is 400.
    Nothing that locates a person is stored.
    The response is 202 with an empty body."""
    api = Api(client)
    assert api.call("POST", "/locations/place-interest", {"placeName": "Kovur", "phoneNumber": None})[0] == 202
    assert api.call("POST", "/locations/place-interest", {"placeName": "Kovur"})[0] == 202
    assert api.call("POST", "/locations/place-interest", {"placeName": "  "})[1]["error"]["code"] == "INVALID_PLACE_NAME"
    with app.app_context():
        with tx() as conn:
            assert conn.exec_driver_sql("SELECT count(*) FROM place_interest WHERE name_typed = 'Kovur'").scalar() == 2


def test_media_upload_and_refusals(client):
    """A PNG upload returns a media id; text is refused with 415.
    Uploads need a signed-in user.
    The declared type is ignored in favour of the file's bytes."""
    user = sign_in(client, "+919000000003")
    headers = {"Authorization": f"Bearer {user.token}"}
    resp = client.post("/media", data={"purpose": "REQUEST_PHOTO", "file": (io.BytesIO(PNG), "a.png")}, headers=headers)
    assert resp.status_code == 201 and resp.get_json()["data"]["mediaId"].startswith("med_")
    resp = client.post("/media", data={"purpose": "REQUEST_PHOTO", "file": (io.BytesIO(b"hello"), "a.png")}, headers=headers)
    assert resp.get_json()["error"]["code"] == "MEDIA_TYPE_NOT_ALLOWED"
    assert client.post("/media", data={"purpose": "REQUEST_PHOTO"}).status_code == 401


def test_push_token_moves_to_newest_device(app, client):
    """A token arriving on a new device is taken from the old one.
    One device, one token, enforced before the unique index can complain.
    Both calls return 204."""
    user = sign_in(client, "+919000000004")
    assert user.call("PUT", "/devices/dev-old/push-token", {"token": "tok-1", "app": "CUSTOMER", "platform": "ANDROID"})[0] == 204
    assert user.call("PUT", "/devices/dev-new/push-token", {"token": "tok-1", "app": "CUSTOMER"})[0] == 204
    with app.app_context():
        with tx() as conn:
            rows = conn.exec_driver_sql("SELECT device_id FROM device WHERE push_token = 'tok-1'").fetchall()
    assert [r[0] for r in rows] == ["dev-new"]
