from datetime import timedelta

from app.core.db import tx
from tests.conftest import at_local, sign_in, tractor_request


def post(customer, body, key=None):
    """Post a request with an idempotency key.
    Returns (status, json).
    key reuses a previous attempt's key."""
    return customer.write("POST", "/customer/requests", body, key)


def test_template_shape(client):
    """The tractor template carries three questions with units and bounds.
    The trips question depends on ploughing.
    An unknown service is SERVICE_NOT_FOUND; one without questions is an empty list."""
    body = client.get("/catalog/services/svc_tractor/request-template").get_json()["data"]
    assert body["templateVersion"] == 3 and [q["id"] for q in body["questions"]] == ["q_work_type", "q_acres", "q_trips"]
    acres = body["questions"][1]
    assert acres["unit"] == {"code": "ACRE", "label": "acres", "perLabel": "acre"} and (acres["min"], acres["step"]) == (0.5, 0.5)
    assert body["questions"][2]["showIf"] == {"questionId": "q_work_type", "answerIn": ["ploughing"]}
    assert client.get("/catalog/services/svc_carpenter/request-template").get_json()["data"]["questions"] == []
    assert client.get("/catalog/services/svc_nope/request-template").get_json()["error"]["code"] == "SERVICE_NOT_FOUND"


def test_post_request_end_to_end(app, client):
    """A valid request lands with its answers and shows on her home screen.
    Not sure is stored as not_sure true with no values, never null.
    The response is the full request shape with a formatted schedule label."""
    today = at_local(7)
    customer = sign_in(client, "+919100000001")
    status, body = post(customer, tractor_request(today + timedelta(days=1), not_sure=True))
    assert status == 201, body
    req = body["data"]
    assert req["state"] == "REQUESTED" and req["canEdit"] and req["canCancel"] and "•" in req["summary"]["scheduleLabel"]
    with app.app_context():
        with tx() as conn:
            row = conn.exec_driver_sql(
                f"""SELECT not_sure, "values" FROM request_answer WHERE request_id = '{req["id"]}' AND question_id = 'q_acres'"""
            ).first()
    assert row == (True, [])
    home = customer.get("/customer/home")[1]["data"]
    assert home["activeRequest"]["id"] == req["id"] and home["savedPartners"] == []


def test_show_if_rules(client):
    """Ploughing needs the trips answer; rotavator must not send it.
    A not-sure parent hides its child.
    Hidden answers are ANSWER_NOT_APPLICABLE; missing required ones ANSWER_REQUIRED."""
    today = at_local(7)
    customer = sign_in(client, "+919100000002")
    tomorrow = today + timedelta(days=1)
    assert post(customer, tractor_request(tomorrow, work="ploughing"))[0] == 201
    body = tractor_request(tomorrow)
    body["answers"].append({"questionId": "q_trips", "values": ["2"], "unit": "TRIP"})
    assert post(customer, body)[1]["error"]["code"] == "ANSWER_NOT_APPLICABLE"
    body = tractor_request(tomorrow, work="ploughing")
    body["answers"] = body["answers"][:2]
    assert post(customer, body)[1]["error"]["code"] == "ANSWER_REQUIRED"


def test_schedule_rules(client):
    """Past dates, the same-day cutoff, the horizon and short notice each have a code.
    ANY_TIME passes while any daypart of the day still has notice.
    All are checked in the area's own clock. There is no evening."""
    today = at_local(16, 5)
    customer = sign_in(client, "+919100000003")
    assert post(customer, tractor_request(today, "AFTERNOON"))[1]["error"]["code"] == "SAME_DAY_CLOSED"
    assert post(customer, tractor_request(today - timedelta(days=1)))[1]["error"]["code"] == "DATE_IN_PAST"
    assert post(customer, tractor_request(today + timedelta(days=20)))[1]["error"]["code"] == "BEYOND_BOOKING_HORIZON"
    today = at_local(11, 40)
    assert post(customer, tractor_request(today, "MORNING"))[1]["error"]["code"] == "INSUFFICIENT_NOTICE"
    assert post(customer, tractor_request(today, "ANY_TIME"))[0] == 201


def test_answer_rules(app, client):
    """Out-of-range numbers, unknown options, stale templates and foreign media are refused.
    Each has its own error code.
    An unknown origin source is stored, not rejected."""
    today = at_local(7)
    customer = sign_in(client, "+919100000004")
    tomorrow = today + timedelta(days=1)
    assert post(customer, tractor_request(tomorrow, acres="25"))[1]["error"]["code"] == "ANSWER_OUT_OF_RANGE"
    assert post(customer, tractor_request(tomorrow, acres="2.3"))[1]["error"]["code"] == "ANSWER_OUT_OF_RANGE"
    assert post(customer, tractor_request(tomorrow, work="bulldozing", workTypeId=None))[1]["error"]["code"] == "INVALID_ANSWER_VALUE"
    assert post(customer, tractor_request(tomorrow, templateVersion=2))[1]["error"]["code"] == "TEMPLATE_VERSION_STALE"
    assert post(customer, tractor_request(tomorrow, mediaIds=["med_nope"]))[1]["error"]["code"] == "INVALID_MEDIA_REFERENCE"
    body = tractor_request(tomorrow)
    body["answers"][0]["notSure"] = True
    body["answers"][0]["values"] = []
    assert post(customer, body)[1]["error"]["code"] == "NOT_SURE_NOT_ALLOWED"
    status, body = post(customer, tractor_request(tomorrow, origin={"source": "SOME_OLD_SCREEN"}, serviceId="svc_water_tanker"))
    assert body["error"]["code"] == "SERVICE_NOT_AVAILABLE"
    assert post(customer, tractor_request(tomorrow, origin={"source": "SOME_OLD_SCREEN"}))[0] == 201


def test_idempotent_create(app, client):
    """The same key twice makes one request and replays the first reply.
    Two different keys make two requests.
    The same key with a different body is IDEMPOTENCY_CONFLICT."""
    today = at_local(7)
    customer = sign_in(client, "+919100000005")
    body = tractor_request(today + timedelta(days=1))
    first = post(customer, body, key="k-1")
    second = post(customer, body, key="k-1")
    assert first[0] == second[0] == 201 and first[1]["data"]["id"] == second[1]["data"]["id"]
    assert post(customer, body, key="k-2")[1]["data"]["id"] != first[1]["data"]["id"]
    assert post(customer, {**body, "description": "other"}, key="k-1")[1]["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_saved_places_upsert_and_delete(client):
    """Saving the same label, village and landmark twice returns the same id with 200.
    Delete takes the saved-place id; somebody else's is 404.
    Unknown labels are INVALID_PLACE_LABEL."""
    customer = sign_in(client, "+919100000006")
    place = {"labelCode": "FIELD", "placeId": "plc_podalakur", "landmark": "Canal"}
    first = customer.call("POST", "/customer/places", place)
    second = customer.call("POST", "/customer/places", {**place, "latitude": 14.41, "longitude": 79.73, "accuracyMeters": 9})
    assert (first[0], second[0]) == (201, 200) and first[1]["data"]["id"] == second[1]["data"]["id"]
    assert second[1]["data"]["label"] == "My field"
    assert customer.call("POST", "/customer/places", {**place, "labelCode": "NOPE"})[1]["error"]["code"] == "INVALID_PLACE_LABEL"
    other = sign_in(client, "+919100000007")
    assert other.call("DELETE", f"/customer/places/{first[1]['data']['id']}")[0] == 404
    assert customer.call("DELETE", f"/customer/places/{first[1]['data']['id']}")[0] == 204


def test_edit_and_cancel(client):
    """A request can be edited while nobody has offered, with a draft to fill the form.
    Cancelling twice is 200 both times; editing a cancelled request is REQUEST_NOT_EDITABLE.
    The ended label is served text."""
    today = at_local(7)
    customer = sign_in(client, "+919100000008")
    req = post(customer, tractor_request(today + timedelta(days=1)))[1]["data"]
    draft = customer.get(f"/customer/requests/{req['id']}")[1]["data"]["draft"]
    assert draft["answers"][1]["values"] == ["3"]
    status, body = customer.write("PATCH", f"/customer/requests/{req['id']}", {"schedule": {"dayPart": "AFTERNOON"}})
    assert status == 200 and body["data"]["summary"]["scheduleLabel"].endswith("Afternoon")
    for _ in range(2):
        status, body = customer.write("POST", f"/customer/requests/{req['id']}/cancel", {"reasonCode": None})
        assert status == 200 and body["data"]["endedReason"] == "CANCELLED_BY_CUSTOMER" and body["data"]["endedLabel"]
    assert customer.write("PATCH", f"/customer/requests/{req['id']}", {})[1]["error"]["code"] == "REQUEST_NOT_EDITABLE"
    past = customer.get("/customer/requests?bucket=PAST")[1]
    assert [r["id"] for r in past["data"]] == [req["id"]] and past["meta"]["nextCursor"] is None
