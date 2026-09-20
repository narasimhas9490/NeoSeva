import threading
import uuid
from datetime import timedelta

from app.core.db import tx
from tests.conftest import active_partner, at_local, sign_in, tractor_request


def setup_offer(app, client, today, pricing, phone_suffix="1", not_sure=False, day_part=None):
    """Create a customer request and one partner offer on it.
    pricing is the offer's pricing body; day_part optionally differs from hers.
    Returns (customer, partner, request_id, offer_id)."""
    partner = active_partner(app, client, f"+91930000{phone_suffix}0")
    customer = sign_in(client, f"+91930000{phone_suffix}1")
    rid = customer.write("POST", "/customer/requests", tractor_request(today + timedelta(days=1), not_sure=not_sure))[1]["data"]["id"]
    body = {"pricing": pricing}
    if day_part:
        body["offeredDaypart"] = day_part
    status, offer = partner.write("POST", f"/partner/opportunities/{rid}/offers", body)
    assert status == 201, offer
    return customer, partner, rid, offer["data"]["id"]


def money(amount):
    """Build a money object in paise.
    Currency is always INR.
    Used in pricing bodies."""
    return {"amountMinor": amount, "currency": "INR"}


def test_full_job_with_pin(app, client):
    """Book, on my way, arrive, read the PIN, type it: the job completes.
    Her exact spot and number unlock for him only after booking; the fee shows as ₹120.
    Afternoon offered on a morning request books the afternoon, expected by 17:30."""
    today = at_local(7)
    customer, ravi, rid, oid = setup_offer(app, client, today, {"type": "FIXED", "exactAmount": money(240000)}, day_part="AFTERNOON")
    status, booking = customer.write("POST", f"/customer/requests/{rid}/book", {"offerId": oid})
    assert status == 201, booking
    booking = booking["data"]
    assert booking["schedule"]["dayPart"] == "AFTERNOON" and booking["arrivalExpectedBy"].endswith("T17:30:00")
    assert booking["exactLocation"]["landmark"] == "Near the canal bridge" and booking["partner"]["phoneNumber"]
    bid = booking["id"]
    job = ravi.get(f"/partner/bookings/{bid}")[1]["data"]
    assert job["customer"]["phoneNumber"] and job["exactLocation"]["latitude"] == 14.4171
    assert job["introduction"] == {"isNewCustomerPair": True, "amount": money(12000)}
    assert ravi.get("/partner/offers")[1]["data"][0]["status"] == "BOOKED"
    assert ravi.call("POST", f"/partner/bookings/{bid}/on-my-way")[0] == 204
    assert ravi.get("/partner/jobs?bucket=ACTIVE")[1]["data"][0]["isOnMyWay"] is True
    evidence = {"latitude": 14.4172, "longitude": 79.7335, "accuracyMeters": 6, "capturedAt": "2026-09-19T09:14:00Z"}
    assert ravi.write("POST", f"/partner/bookings/{bid}/arrive", {"evidence": evidence})[1]["data"]["state"] == "ARRIVED"
    pin = customer.get(f"/customer/bookings/{bid}/completion-pin")[1]["data"]
    assert pin == customer.get(f"/customer/bookings/{bid}/completion-pin")[1]["data"]
    assert "Ravi" in pin["instruction"]
    wrong = "0000" if pin["pin"] != "0000" else "1111"
    assert ravi.write("POST", f"/partner/bookings/{bid}/complete", {"pin": wrong})[1]["error"]["code"] == "WRONG_PIN"
    status, done = ravi.write("POST", f"/partner/bookings/{bid}/complete", {"pin": pin["pin"]})
    assert status == 200 and done["data"]["state"] == "COMPLETED"
    assert customer.get(f"/customer/bookings/{bid}/completion-pin")[1]["error"]["code"] == "BOOKING_ALREADY_COMPLETED"
    assert customer.get(f"/customer/requests/{rid}")[1]["data"]["state"] == "COMPLETED"
    with app.app_context():
        with tx() as conn:
            assert conn.exec_driver_sql("SELECT count(*) FROM credit_ledger_transaction").scalar() == 0


def test_other_partner_not_selected_keeps_only_village(app, client):
    """When she books one partner the other offer becomes NOT_SELECTED.
    The loser still sees only the approximate area in his sent offers.
    His opportunity list no longer shows the booked job."""
    today = at_local(7)
    other = active_partner(app, client, "+919300009990", name="Other")
    customer, ravi, rid, oid = setup_offer(app, client, today, {"type": "FIXED", "exactAmount": money(240000)})
    other_offer = other.write("POST", f"/partner/opportunities/{rid}/offers", {"pricing": {"type": "FIXED", "exactAmount": money(260000)}})[1]["data"]
    customer.write("POST", f"/customer/requests/{rid}/book", {"offerId": oid})
    sent = other.get("/partner/offers")[1]["data"][0]
    assert sent["id"] == other_offer["id"] and sent["status"] == "NOT_SELECTED"
    assert "landmark" not in str(sent) and other.get("/partner/opportunities")[1]["data"] == []
    assert other.get(f"/partner/opportunities/{rid}")[0] == 200


def test_double_book_same_and_different_keys(app, client):
    """The same key replays the first booking; a different key is REQUEST_ALREADY_BOOKED.
    The error carries the booking id so her app can go to the job.
    Only one booking exists."""
    today = at_local(7)
    customer, _, rid, oid = setup_offer(app, client, today, {"type": "FIXED", "exactAmount": money(240000)})
    first = customer.write("POST", f"/customer/requests/{rid}/book", {"offerId": oid}, key="book-1")
    again = customer.write("POST", f"/customer/requests/{rid}/book", {"offerId": oid}, key="book-1")
    assert first[0] == again[0] == 201 and first[1]["data"]["id"] == again[1]["data"]["id"]
    status, body = customer.write("POST", f"/customer/requests/{rid}/book", {"offerId": oid}, key="book-2")
    assert status == 409 and body["error"]["code"] == "REQUEST_ALREADY_BOOKED"
    assert body["error"]["details"]["bookingId"] == first[1]["data"]["id"]


def test_concurrent_booking_and_withdraw_race(app, client):
    """Two bookings at the same instant produce exactly one booking.
    A withdraw racing a booking never undoes the booking.
    The loser gets a clean error code."""
    today = at_local(7)
    customer, ravi, rid, oid = setup_offer(app, client, today, {"type": "FIXED", "exactAmount": money(240000)})
    results = []

    def book():
        """Book the offer from its own client and record the status.
        Each thread uses a new idempotency key.
        Runs concurrently with the others."""
        c = app.test_client()
        r = c.post(f"/customer/requests/{rid}/book", json={"offerId": oid},
                   headers={"Authorization": f"Bearer {customer.token}", "X-App": "CUSTOMER", "Idempotency-Key": str(uuid.uuid4())})
        results.append(r.status_code)

    def withdraw():
        """Withdraw the same offer from the partner's side.
        Records the status code.
        Runs concurrently with the bookings."""
        c = app.test_client()
        r = c.delete(f"/partner/offers/{oid}", headers={"Authorization": f"Bearer {ravi.token}", "X-App": "PARTNER", "Idempotency-Key": str(uuid.uuid4())})
        results.append(("withdraw", r.status_code, r.get_json()))

    threads = [threading.Thread(target=book) for _ in range(3)] + [threading.Thread(target=withdraw)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with app.app_context():
        with tx() as conn:
            bookings = conn.exec_driver_sql(f"SELECT count(*) FROM booking WHERE request_id = '{rid}'").scalar()
            offer_status = conn.exec_driver_sql(f"SELECT status FROM offer WHERE id = '{oid}'").scalar()
    assert bookings <= 1
    booked = [r for r in results if r == 201]
    withdraw_result = [r for r in results if isinstance(r, tuple)][0]
    if booked:
        assert len(booked) == 1 and offer_status == "BOOKED"
        assert withdraw_result[1] == 409 and withdraw_result[2]["error"]["code"] == "OFFER_ALREADY_BOOKED"
    else:
        assert offer_status == "WITHDRAWN"


def test_pin_lockout(app, client):
    """Wrong PINs are 422 until the limit, then 429 with retryAfterSeconds.
    During the lockout even the right PIN is refused.
    The lock is on the booking, not the partner."""
    today = at_local(7)
    customer, ravi, rid, oid = setup_offer(app, client, today, {"type": "FIXED", "exactAmount": money(240000)})
    bid = customer.write("POST", f"/customer/requests/{rid}/book", {"offerId": oid})[1]["data"]["id"]
    pin = customer.get(f"/customer/bookings/{bid}/completion-pin")[1]["data"]["pin"]
    wrong = "0000" if pin != "0000" else "1111"
    codes = [ravi.write("POST", f"/partner/bookings/{bid}/complete", {"pin": wrong})[1]["error"]["code"] for _ in range(5)]
    assert codes == ["WRONG_PIN"] * 4 + ["PIN_TOO_MANY_ATTEMPTS"]
    status, body = ravi.write("POST", f"/partner/bookings/{bid}/complete", {"pin": pin})
    assert status == 429 and body["error"]["details"]["retryAfterSeconds"] > 0


def test_inspection_needs_agreement_before_pin(app, client):
    """A visit-charge job shows zero fee until he names the final amount.
    Her PIN is refused with FINAL_AMOUNT_NOT_AGREED until she says go ahead.
    After agreeing the PIN appears and the job can complete."""
    today = at_local(7)
    customer, ravi, rid, oid = setup_offer(app, client, today, {"type": "INSPECTION", "inspectionCharge": money(30000)})
    bid = customer.write("POST", f"/customer/requests/{rid}/book", {"offerId": oid})[1]["data"]["id"]
    assert ravi.get(f"/partner/bookings/{bid}")[1]["data"]["introduction"]["amount"] == money(0)
    assert ravi.write("POST", f"/partner/bookings/{bid}/final-amount", {"amountMinor": 500000})[1]["error"]["code"] == "BOOKING_NOT_ARRIVED"
    ravi.write("POST", f"/partner/bookings/{bid}/arrive", {"evidence": None})
    assert ravi.write("POST", f"/partner/bookings/{bid}/final-amount", {"quantity": 3})[1]["error"]["code"] == "INVALID_FINAL_AMOUNT"
    job = ravi.write("POST", f"/partner/bookings/{bid}/final-amount", {"amountMinor": 500000})[1]["data"]
    assert job["introduction"]["amount"] == money(25000)
    assert customer.get(f"/customer/bookings/{bid}/completion-pin")[1]["error"]["code"] == "FINAL_AMOUNT_NOT_AGREED"
    assert customer.write("POST", f"/customer/bookings/{bid}/agree-amount")[1]["data"]["finalAmountAgreed"] is True
    pin = customer.get(f"/customer/bookings/{bid}/completion-pin")[1]["data"]["pin"]
    assert ravi.write("POST", f"/partner/bookings/{bid}/complete", {"pin": pin})[1]["data"]["state"] == "COMPLETED"


def test_rate_not_sure_quantity_at_the_end(app, client):
    """A rate job where she said not sure gets its quantity from him at the end.
    We multiply by his rate; she is never asked to agree and her PIN works throughout.
    A job priced up front refuses a final amount."""
    today = at_local(7)
    customer, ravi, rid, oid = setup_offer(app, client, today, {"type": "UNIT_RATE", "rate": money(80000), "unit": {"code": "ACRE"}}, not_sure=True)
    bid = customer.write("POST", f"/customer/requests/{rid}/book", {"offerId": oid})[1]["data"]["id"]
    assert customer.get(f"/customer/bookings/{bid}/completion-pin")[0] == 200
    ravi.write("POST", f"/partner/bookings/{bid}/arrive")
    job = ravi.write("POST", f"/partner/bookings/{bid}/final-amount", {"quantity": 3})[1]["data"]
    assert job["finalAmount"] == money(240000) and job["introduction"]["amount"] == money(12000)
    assert customer.get(f"/customer/bookings/{bid}/completion-pin")[0] == 200
    assert customer.write("POST", f"/customer/bookings/{bid}/agree-amount")[1]["error"]["code"] == "AGREEMENT_NOT_REQUIRED"


def test_customer_overrides(app, client):
    """She can confirm arrival without his tap, and complete without a PIN.
    completed_by records the customer route.
    Arrival with no evidence is accepted."""
    today = at_local(7)
    customer, ravi, rid, oid = setup_offer(app, client, today, {"type": "FIXED", "exactAmount": money(240000)})
    bid = customer.write("POST", f"/customer/requests/{rid}/book", {"offerId": oid})[1]["data"]["id"]
    assert customer.call("POST", f"/customer/bookings/{bid}/confirm-arrival")[1]["data"]["state"] == "ARRIVED"
    assert ravi.write("POST", f"/partner/bookings/{bid}/arrive")[1]["error"]["code"] == "BOOKING_NOT_ARRIVABLE"
    assert customer.call("POST", f"/customer/bookings/{bid}/confirm-complete")[1]["data"]["state"] == "COMPLETED"
    with app.app_context():
        with tx() as conn:
            assert conn.exec_driver_sql(f"SELECT completed_by FROM booking WHERE id = '{bid}'").scalar() == "CUSTOMER"
    history = ravi.get("/partner/jobs?bucket=HISTORY")[1]["data"]
    assert history[0]["state"] == "COMPLETED"
