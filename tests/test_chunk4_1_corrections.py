from datetime import timedelta

from app.core import clock
from app.core.db import run, tx
from app.core.ids import new_id
from tests.conftest import Api, active_partner, at_local, sign_in, tractor_request


def admin_headers(app):
    """Build the X-Admin-Key header for direct admin API calls."""
    return {"X-Admin-Key": app.config["NS"].admin_api_key}


def test_otp_has_no_daily_cap(app, client):
    """Fifteen unconsumed codes sit inside the last 24 hours, well past the old
    cap of ten. Asking again still succeeds; only the resend backoff applies."""
    phone = "+919400000001"
    now = clock.now_utc()
    with app.app_context():
        with tx() as conn:
            for i in range(15):
                run(
                    conn,
                    """INSERT INTO otp_request (id, phone_e164, purpose, code_hash, expires_at, consumed_at, created_at)
                       VALUES (:id, :p, 'LOGIN', 'x', :exp, :now, :created)""",
                    id=new_id("otp"),
                    p=phone,
                    exp=now - timedelta(hours=23),
                    now=now - timedelta(hours=23),
                    created=now - timedelta(hours=2, minutes=i),
                )
    status, body = Api(client).call("POST", "/auth/otp/request", {"phoneNumber": phone, "purpose": "LOGIN"})
    assert status == 200, body
    assert "dailyLimit" not in (body.get("error", {}).get("details") or {})


def test_suspend_revokes_session_and_wipes_push_token(app, client):
    """Suspending him ends his session and blanks his push token at once.
    His very next call is 401, not a business-logic refusal.
    A push after that reaches no device."""
    ravi = active_partner(app, client, "+919400000010")
    assert ravi.call("PUT", "/devices/dev-corr-1/push-token", {"token": "tok-corr-1", "app": "PARTNER", "language": "en"})[0] == 204
    assert ravi.get("/partner/me")[0] == 200
    status = client.post(f"/admin/api/partners/{ravi.user_id}/status", json={"status": "SUSPENDED"}, headers=admin_headers(app)).status_code
    assert status == 200
    assert ravi.get("/partner/me")[0] == 401
    with app.app_context():
        with tx() as conn:
            token = conn.exec_driver_sql(f"SELECT push_token FROM device WHERE device_id = 'dev-corr-1'").scalar()
    assert token is None


def test_decline_all_reopens_batch_without_waiting_interval(app, client):
    """Five partners are told and all decline within a minute.
    The next batch goes on the next tick, not after the ten minute interval."""
    today = at_local(7)
    with app.app_context():
        with tx() as conn:
            conn.exec_driver_sql("UPDATE platform_setting SET matching_batch_size = 1, matching_batch_interval_minutes = 10")
    first = active_partner(app, client, "+919400000020", name="First")
    second = active_partner(app, client, "+919400000021", name="Second")
    customer = sign_in(client, "+919400000022")
    rid = customer.write("POST", "/customer/requests", tractor_request(today + timedelta(days=1)))[1]["data"]["id"]
    assert [o["requestId"] for o in first.get("/partner/opportunities")[1]["data"]] == [rid]
    assert second.get("/partner/opportunities")[1]["data"] == []
    assert first.call("POST", f"/partner/opportunities/{rid}/decline")[0] == 204
    from app.shared.matching import advance

    with app.app_context():
        advance(rid)
    assert [o["requestId"] for o in second.get("/partner/opportunities")[1]["data"]] == [rid]


def test_pin_growing_wait_and_running_total(app, client):
    """The wait after a lockout is longer than the one before it.
    total_wrong_count never resets, even after the code is finally right.
    A second job's counters are untouched."""
    from tests.test_chunk4_the_job import money, setup_offer

    today = at_local(7)
    with app.app_context():
        with tx() as conn:
            conn.exec_driver_sql("UPDATE platform_setting SET pin_max_attempts = 2, pin_lockout_backoff_seconds = '{10,20}'")
    customer, ravi, rid, oid = setup_offer(app, client, today, {"type": "FIXED", "exactAmount": money(240000)})
    bid = customer.write("POST", f"/customer/requests/{rid}/book", {"offerId": oid})[1]["data"]["id"]
    pin = customer.get(f"/customer/bookings/{bid}/completion-pin")[1]["data"]["pin"]
    wrong = "0000" if pin != "0000" else "1111"

    assert ravi.write("POST", f"/partner/bookings/{bid}/complete", {"pin": wrong})[1]["error"]["code"] == "WRONG_PIN"
    status, body = ravi.write("POST", f"/partner/bookings/{bid}/complete", {"pin": wrong})
    assert status == 429
    first_wait = body["error"]["details"]["retryAfterSeconds"]

    with app.app_context():
        with tx() as conn:
            run(conn, "UPDATE booking_pin_attempt SET locked_until = NULL WHERE booking_id = :b", b=bid)

    assert ravi.write("POST", f"/partner/bookings/{bid}/complete", {"pin": wrong})[1]["error"]["code"] == "WRONG_PIN"
    status, body = ravi.write("POST", f"/partner/bookings/{bid}/complete", {"pin": wrong})
    assert status == 429
    second_wait = body["error"]["details"]["retryAfterSeconds"]
    assert second_wait > first_wait

    with app.app_context():
        with tx() as conn:
            run(conn, "UPDATE booking_pin_attempt SET locked_until = NULL WHERE booking_id = :b", b=bid)
            total_before = conn.exec_driver_sql(f"SELECT total_wrong_count FROM booking_pin_attempt WHERE booking_id = '{bid}'").scalar()
    assert total_before == 4

    status, done = ravi.write("POST", f"/partner/bookings/{bid}/complete", {"pin": pin})
    assert status == 200 and done["data"]["state"] == "COMPLETED"
    with app.app_context():
        with tx() as conn:
            total_after = conn.exec_driver_sql(f"SELECT total_wrong_count FROM booking_pin_attempt WHERE booking_id = '{bid}'").scalar()
    assert total_after == 4


def test_customer_booking_carries_profile_photo(app, client):
    """Her booked job's partner block carries profilePhoto, null when he has none."""
    from tests.test_chunk4_the_job import money, setup_offer

    today = at_local(7)
    customer, ravi, rid, oid = setup_offer(app, client, today, {"type": "FIXED", "exactAmount": money(240000)})
    status, booking = customer.write("POST", f"/customer/requests/{rid}/book", {"offerId": oid})
    assert status == 201, booking
    assert "profilePhoto" in booking["data"]["partner"]
    assert booking["data"]["partner"]["profilePhoto"] is None


def test_catalog_has_no_evening_and_sixteen_hour_cutoff(client):
    """No eveningEndsHour is served and the same-day cutoff is sixteen."""
    scheduling = client.get("/catalog").get_json()["data"]["scheduling"]
    assert "eveningEndsHour" not in scheduling
    assert scheduling["sameDayCutoffHour"] == 16


def test_push_token_records_device_language(app, client):
    """The device's own language is stored on the device row.
    Registering with no language leaves an existing one untouched."""
    user = sign_in(client, "+919400000030")
    assert user.call("PUT", "/devices/dev-corr-lang/push-token", {"token": "tok-corr-lang", "app": "CUSTOMER", "language": "te"})[0] == 204
    with app.app_context():
        with tx() as conn:
            lang = conn.exec_driver_sql("SELECT language FROM device WHERE device_id = 'dev-corr-lang'").scalar()
    assert lang == "te"
    assert user.call("PUT", "/devices/dev-corr-lang/push-token", {"token": "tok-corr-lang-2", "app": "CUSTOMER"})[0] == 204
    with app.app_context():
        with tx() as conn:
            lang = conn.exec_driver_sql("SELECT language FROM device WHERE device_id = 'dev-corr-lang'").scalar()
    assert lang == "te"


def test_short_label_and_presets_served(app, client):
    """Every question carries shortLabel; the acres question carries its presets.
    A preset that sits on the question's step is accepted and served back;
    one that does not is refused."""
    template = client.get("/catalog/services/svc_tractor/request-template").get_json()["data"]
    acres = next(q for q in template["questions"] if q["id"] == "q_acres")
    assert acres["shortLabel"] == "Land"
    assert acres["presets"] == []

    from app.admin.auth import create_admin

    with app.app_context():
        with tx() as conn:
            create_admin(conn, "admin-corr", "secret-pass")
    assert client.post("/admin/api/login", json={"username": "admin-corr", "password": "secret-pass"}).status_code == 200
    off_step = client.put("/admin/api/r/request_question", json={"key": {"id": "q_acres"}, "values": {"presets": [1.25]}})
    assert off_step.status_code == 422, off_step.get_json()
    on_step = client.put("/admin/api/r/request_question", json={"key": {"id": "q_acres"}, "values": {"presets": [1, 2, 5, 10]}})
    assert on_step.status_code == 200, on_step.get_json()

    template = client.get("/catalog/services/svc_tractor/request-template").get_json()["data"]
    acres = next(q for q in template["questions"] if q["id"] == "q_acres")
    assert acres["presets"] == [1, 2, 5, 10]


def test_schedule_label_full_month_and_telugu_daypart(client):
    """The schedule label carries the full month name and, for a Telugu
    account, the daypart word in Telugu."""
    today = at_local(7)
    tomorrow = today + timedelta(days=1)
    te_customer = sign_in(client, "+919400000040")
    with client.application.app_context():
        with tx() as conn:
            run(conn, "UPDATE app_user SET language = 'te' WHERE id = :u", u=te_customer.user_id)
    body = tractor_request(tomorrow)
    status, resp = te_customer.write("POST", "/customer/requests", body)
    assert status == 201, resp
    label = resp["data"]["summary"]["scheduleLabel"]
    assert clock.MONTH_NAMES["te"][tomorrow.month - 1] in label
    assert "ఉదయం" in label

