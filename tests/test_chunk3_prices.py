import io
import json
from datetime import timedelta

from app.core.db import tx
from tests.conftest import active_partner, at_local, sign_in, tractor_request

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
FORBIDDEN = ("landmark", "latitude", "longitude", "accuracyMeters", "phoneNumber", "displayName", "Near the canal bridge", "+9191")


def new_request(client, today, phone="+919200000001", **kw):
    """Sign a customer in and post a tractor request for tomorrow.
    Matching runs as part of posting.
    Returns (customer_api, request_id)."""
    customer = sign_in(client, phone)
    status, body = customer.write("POST", "/customer/requests", tractor_request(today + timedelta(days=1), **kw))
    assert status == 201, body
    return customer, body["data"]["id"]


def rate(amount, unit="ACRE"):
    """Build a UNIT_RATE pricing body.
    amount is the rate in paise.
    unit is the unit code."""
    return {"type": "UNIT_RATE", "rate": {"amountMinor": amount, "currency": "INR"}, "unit": {"code": unit}}


def fixed(amount):
    """Build a FIXED pricing body.
    amount is the whole job in paise.
    Currency is always INR."""
    return {"type": "FIXED", "exactAmount": {"amountMinor": amount, "currency": "INR"}}


def test_onboarding_steps_and_activation(client):
    """nextStep advances with each save and never goes backwards.
    Activation before setup is SETUP_INCOMPLETE with the step to return to.
    A finished partner becomes ACTIVATED, not ACTIVE."""
    at_local(7)
    p = sign_in(client, "+919200000010", "PARTNER")
    assert p.get("/partner/me")[1]["data"]["nextStep"] == "BASIC_PROFILE"
    status, body = p.write("POST", "/partner/me/activate")
    assert body["error"]["code"] == "SETUP_INCOMPLETE" and body["error"]["details"]["nextStep"] == "BASIC_PROFILE"
    assert p.call("PATCH", "/partner/me/basic", {"fullName": "Ravi", "basePlaceId": "plc_podalakur"})[1]["data"]["nextStep"] == "SERVICES"
    bad = p.call("PUT", "/partner/me/services", {"services": [{"serviceId": "svc_tractor", "equipmentIds": ["LORRY"]}]})
    assert bad[1]["error"]["code"] == "INVALID_EQUIPMENT"
    assert p.call("PUT", "/partner/me/services", {"services": [{"serviceId": "svc_tractor", "equipmentIds": ["ROTAVATOR"]}]})[1]["data"]["nextStep"] == "TRAVEL_RADIUS"
    assert p.call("PATCH", "/partner/me", {"travelRadiusKm": 7})[1]["error"]["code"] == "INVALID_TRAVEL_RADIUS"
    assert p.call("PATCH", "/partner/me", {"travelRadiusKm": 15})[1]["data"]["nextStep"] == "NOTIFICATIONS"
    assert p.call("PATCH", "/partner/me/basic", {"fullName": "Ravi K", "basePlaceId": "plc_podalakur"})[1]["data"]["nextStep"] == "NOTIFICATIONS"
    status, body = p.write("POST", "/partner/me/activate")
    assert body["data"]["status"] == "ACTIVATED" and body["data"]["nextStep"] == "COMPLETE" and body["data"]["workBlock"] is None


def test_identity_submit_and_admin_decision_deletes_images(app, client):
    """He sends a document and a selfie and sees PENDING.
    NEEDS_ACTION shows the note and lets him send again; VERIFIED deletes both images.
    The decision and timestamps are kept."""
    at_local(7)
    p = sign_in(client, "+919200000011", "PARTNER")
    headers = {"Authorization": f"Bearer {p.token}"}

    def upload(purpose):
        """Upload a tiny JPEG for one identity purpose.
        Returns the new media id.
        Uses the multipart endpoint like the app."""
        return client.post("/media", data={"purpose": purpose, "file": (io.BytesIO(JPEG), "x.jpg")}, headers=headers).get_json()["data"]["mediaId"]

    doc, selfie = upload("IDENTITY_DOCUMENT"), upload("IDENTITY_SELFIE")
    body = {"documentType": "MASKED_AADHAAR", "documentMediaId": doc, "selfieMediaId": selfie}
    assert p.call("POST", "/partner/me/identity-verification/initiate", body)[1]["data"]["status"] == "PENDING"
    admin = {"X-Admin-Key": app.config["NS"].admin_api_key}
    r = client.post(f"/admin/api/identity/{p.user_id}/decision", json={"status": "NEEDS_ACTION", "reviewNote": "Too blurry"}, headers=admin)
    assert r.status_code == 200
    assert p.get("/partner/me/identity-verification")[1]["data"]["reviewNote"] == "Too blurry"
    doc2, selfie2 = upload("IDENTITY_DOCUMENT"), upload("IDENTITY_SELFIE")
    body = {"documentType": "VOTER_ID", "documentMediaId": doc2, "selfieMediaId": selfie2}
    assert p.call("POST", "/partner/me/identity-verification/initiate", body)[1]["data"]["status"] == "PENDING"
    assert client.post(f"/admin/api/identity/{p.user_id}/decision", json={"status": "VERIFIED"}, headers=admin).status_code == 200
    with app.app_context():
        with tx() as conn:
            deleted = conn.exec_driver_sql(
                f"SELECT count(*) FROM media WHERE id IN ('{doc}','{selfie}','{doc2}','{selfie2}') AND deleted_at IS NOT NULL"
            ).scalar()
    assert deleted == 4
    assert p.get("/partner/me")[1]["data"]["identityStatus"] == "VERIFIED"


def test_request_reaches_partner_without_exact_location(app, client):
    """A matching partner is notified and sees the job with only the approximate area.
    The opportunity body carries no landmark, pin, accuracy, name or number.
    A partner without the equipment is never told."""
    today = at_local(7)
    ravi = active_partner(app, client, "+919200000020")
    no_rotavator = active_partner(app, client, "+919200000021", services=(("svc_tractor", ["PLOUGH"]),), name="Plough only")
    customer, rid = new_request(client, today)
    status, body = ravi.get("/partner/opportunities")
    assert status == 200 and [o["requestId"] for o in body["data"]] == [rid]
    opportunity = ravi.get(f"/partner/opportunities/{rid}")[1]["data"]
    text = json.dumps(opportunity)
    assert not any(word in text for word in FORBIDDEN), text
    assert opportunity["approximateArea"]["areaLabel"] == "Podalakur area"
    assert {"questionId": "q_acres", "label": "Land", "displayValue": "3 acres"} in opportunity["answers"]
    assert opportunity["customerTrust"] == {"phoneVerified": True, "completedJobs": 0}
    assert no_rotavator.get("/partner/opportunities")[1]["data"] == []
    assert customer.get(f"/customer/requests/{rid}")[1]["data"]["notifiedPartnerCount"] == 1


def test_offer_rules_and_comparable_cost(app, client):
    """A rate times her acres becomes the comparable cost; a second offer is refused.
    Above the maximum, a foreign unit and ANY_TIME each have their own code.
    Offering the afternoon on a morning request records the afternoon."""
    today = at_local(7)
    ravi = active_partner(app, client, "+919200000030")
    customer, rid = new_request(client, today)
    path = f"/partner/opportunities/{rid}/offers"
    assert ravi.write("POST", path, {"pricing": fixed(20000000)})[1]["error"]["code"] == "OFFER_ABOVE_MAXIMUM"
    assert ravi.write("POST", path, {"pricing": rate(80000, "TRIP")})[1]["error"]["code"] == "INVALID_OFFER_UNIT"
    assert ravi.write("POST", path, {"pricing": rate(80000), "offeredDaypart": "ANY_TIME"})[1]["error"]["code"] == "INVALID_OFFERED_DAYPART"
    assert ravi.write("POST", path, {"pricing": {"type": "RANGE"}})[1]["error"]["code"] == "INVALID_PRICING"
    status, body = ravi.write("POST", path, {"pricing": rate(80000), "offeredDaypart": "AFTERNOON"})
    assert status == 201 and body["data"]["offeredDaypart"] == "AFTERNOON" and body["data"]["status"] == "ACTIVE"
    assert "landmark" not in json.dumps(body["data"]["requestContext"])
    assert ravi.write("POST", path, {"pricing": fixed(1000)})[1]["error"]["code"] == "OFFER_ALREADY_SENT"
    offer = customer.get(f"/customer/requests/{rid}/offers")[1]["data"]["offers"][0]
    assert offer["comparableCost"] == {"amountMinor": 240000, "currency": "INR"}
    assert "phoneNumber" not in offer["partner"]
    assert customer.write("PATCH", f"/customer/requests/{rid}", {})[1]["error"]["code"] == "REQUEST_NOT_EDITABLE"


def test_not_sure_rate_has_no_comparable_cost(app, client):
    """A rate on a request where she said not sure has no comparable cost.
    Her card then shows his own wording instead of a made-up total.
    Absent is sent as null."""
    today = at_local(7)
    ravi = active_partner(app, client, "+919200000040")
    customer, rid = new_request(client, today, not_sure=True)
    ravi.write("POST", f"/partner/opportunities/{rid}/offers", {"pricing": rate(80000)})
    offer = customer.get(f"/customer/requests/{rid}/offers")[1]["data"]["offers"][0]
    assert offer["comparableCost"] is None and offer["pricing"]["unit"]["perLabel"] == "acre"


def test_lowest_price_claim(app, client):
    """The cheaper of two priced offers is marked lowest.
    An inspection offer that cannot be priced turns the claim off for everyone.
    Most-jobs-completed is false when nobody has completed any."""
    today = at_local(7)
    a = active_partner(app, client, "+919200000050", name="A")
    b = active_partner(app, client, "+919200000051", name="B")
    c = active_partner(app, client, "+919200000052", name="C")
    customer, rid = new_request(client, today)
    a.write("POST", f"/partner/opportunities/{rid}/offers", {"pricing": fixed(250000)})
    b.write("POST", f"/partner/opportunities/{rid}/offers", {"pricing": rate(70000)})
    offers = customer.get(f"/customer/requests/{rid}/offers")[1]["data"]["offers"]
    assert {o["partner"]["displayName"]: o["isLowestPrice"] for o in offers} == {"A": False, "B": True}
    assert not any(o["isMostJobsCompleted"] for o in offers)
    c.write("POST", f"/partner/opportunities/{rid}/offers", {"pricing": {"type": "INSPECTION", "inspectionCharge": {"amountMinor": 30000, "currency": "INR"}}})
    offers = customer.get(f"/customer/requests/{rid}/offers")[1]["data"]["offers"]
    assert len(offers) == 3 and not any(o["isLowestPrice"] for o in offers)


def test_accepting_suspension_decline_withdraw(app, client):
    """Turning acceptingNewJobs off stops new work; a suspended partner cannot turn it on.
    Declining hides the job and changes nothing else.
    Withdrawing marks the offer WITHDRAWN."""
    today = at_local(7)
    ravi = active_partner(app, client, "+919200000060")
    assert ravi.call("PATCH", "/partner/me", {"acceptingNewJobs": False})[1]["data"]["acceptingNewJobs"] is False
    _, rid = new_request(client, today)
    assert ravi.get("/partner/opportunities")[1]["data"] == []
    assert ravi.call("PATCH", "/partner/me", {"acceptingNewJobs": True})[0] == 200
    _, rid2 = new_request(client, today, phone="+919200000061")
    assert [o["requestId"] for o in ravi.get("/partner/opportunities")[1]["data"]] == [rid2]
    offer = ravi.write("POST", f"/partner/opportunities/{rid2}/offers", {"pricing": fixed(200000)})[1]["data"]
    status, body = ravi.write("DELETE", f"/partner/offers/{offer['id']}")
    assert status == 200 and body["data"]["status"] == "WITHDRAWN"
    assert ravi.call("POST", f"/partner/opportunities/{rid2}/decline")[0] == 204
    assert ravi.get("/partner/opportunities")[1]["data"] == []
    admin = {"X-Admin-Key": app.config["NS"].admin_api_key}
    client.post(f"/admin/api/partners/{ravi.user_id}/status", json={"status": "SUSPENDED", "statusNote": "Paused"}, headers=admin)
    assert ravi.call("PATCH", "/partner/me", {"acceptingNewJobs": True})[1]["error"]["code"] == "PARTNER_SUSPENDED"


def test_batches_and_preferred_partner_head_start(app, client):
    """With a batch size of one, only the nearest partner is told first.
    A preferred partner is told alone in batch 0, before anybody else.
    The rest wait for the head start to pass."""
    today = at_local(7)
    near = active_partner(app, client, "+919200000070", name="Near")
    far = active_partner(app, client, "+919200000071", base="plc_biradavolu", radius=25, name="Far")
    with app.app_context():
        with tx() as conn:
            conn.exec_driver_sql("UPDATE platform_setting SET matching_batch_size = 1")
    _, rid = new_request(client, today, preferredPartnerId=None)
    assert len(near.get("/partner/opportunities")[1]["data"]) == 1
    assert far.get("/partner/opportunities")[1]["data"] == []
    customer = sign_in(client, "+919200000072")
    body = tractor_request(today + timedelta(days=1), preferredPartnerId=far.user_id)
    rid2 = customer.write("POST", "/customer/requests", body)[1]["data"]["id"]
    assert [o["requestId"] for o in far.get("/partner/opportunities")[1]["data"]] == [rid2]
    assert rid2 not in [o["requestId"] for o in near.get("/partner/opportunities")[1]["data"]]
    at_local(7, 45)
    from app.shared.matching import advance

    with app.app_context():
        advance(rid2)
    assert rid2 in [o["requestId"] for o in near.get("/partner/opportunities")[1]["data"]]
