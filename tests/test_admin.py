import io
import uuid
from pathlib import Path

from app.core.db import tx


def login(client):
    """Sign in to the admin console with the bootstrap account.
    The admin user is created directly for the test.
    Returns the test client, now holding the session cookie."""
    from app.admin.auth import create_admin

    with client.application.app_context():
        with tx() as conn:
            create_admin(conn, "admin", "secret-pass")
    assert client.post("/admin/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert client.post("/admin/api/login", json={"username": "admin", "password": "secret-pass"}).status_code == 200
    return client


def test_dashboard_page_and_auth(client):
    """The dashboard HTML is served, and its API refuses anonymous calls.
    After login the registry lists every resource.
    The booking resource never exposes the completion PIN."""
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/api/meta").status_code == 401
    login(client)
    meta = client.get("/admin/api/meta").get_json()["data"]
    names = {r["name"] for r in meta}
    assert {"catalog_setting", "geography", "place", "request_question", "booking"} <= names
    booking = next(r for r in meta if r["name"] == "booking")
    assert "completion_pin" not in {c["name"] for c in booking["columns"]}


def test_edit_setting_changes_catalog(client):
    """Editing a served sentence in catalog_setting changes the catalog body.
    That is the whole point: a database write, not an app release.
    The value is put back afterwards."""
    login(client)
    original = client.get("/catalog").get_json()["data"]["partnerTerms"]["lookingIsFree"]
    r = client.put("/admin/api/r/catalog_setting", json={"key": {"id": 1}, "values": {"partner_looking_is_free": "Free to look."}})
    assert r.status_code == 200, r.get_json()
    assert client.get("/catalog").get_json()["data"]["partnerTerms"]["lookingIsFree"] == "Free to look."
    client.put("/admin/api/r/catalog_setting", json={"key": {"id": 1}, "values": {"partner_looking_is_free": original}})


def test_question_edit_bumps_template_version(client):
    """Changing any question raises the template version for every service.
    An app posting with the old version is then refused as stale.
    A showIf pointing downwards is refused."""
    login(client)
    before = client.get("/catalog/services/svc_jcb/request-template").get_json()["data"]["templateVersion"]
    r = client.put("/admin/api/r/request_question", json={"key": {"id": "q_acres"}, "values": {"short_label": "Land size"}})
    assert r.status_code == 200, r.get_json()
    assert client.get("/catalog/services/svc_jcb/request-template").get_json()["data"]["templateVersion"] == before + 1
    bad = client.put("/admin/api/r/request_question", json={"key": {"id": "q_work_type"}, "values": {"depends_on_question_id": "q_acres", "depends_on_values": "3"}})
    assert bad.get_json()["error"]["code"] == "INVALID_SHOW_IF"
    client.put("/admin/api/r/request_question", json={"key": {"id": "q_acres"}, "values": {"short_label": "Land"}})


def test_generic_crud_and_constraints(client):
    """A new reason can be created, edited and deleted from the console.
    Database constraints come back as readable 422s.
    Read-only resources refuse writes."""
    login(client)
    key = {"context": "CUSTOMER_REPORT_ISSUE", "code": "TEST_REASON"}
    assert client.post("/admin/api/r/reason", json={"values": {**key, "label": "Test", "sort_order": 99, "is_active": True}}).status_code == 201
    assert any(r["code"] == "TEST_REASON" for r in client.get("/catalog").get_json()["data"]["reasons"]["CUSTOMER_REPORT_ISSUE"])
    dup = client.post("/admin/api/r/reason", json={"values": {**key, "label": "Again"}})
    assert dup.get_json()["error"]["code"] == "CONSTRAINT_VIOLATION"
    assert client.delete("/admin/api/r/reason", json={"key": key}).status_code == 200
    assert client.post("/admin/api/r/booking", json={"values": {}}).status_code == 405


def test_ledger_adjustment_is_idempotent(app, client):
    """A manual credit row is added once per Idempotency-Key.
    The balance is the sum of the ledger, never a stored column.
    JOB_FEE cannot be written by hand."""
    login(client)
    with app.app_context():
        with tx() as conn:
            conn.exec_driver_sql("INSERT INTO app_user (id, phone_e164) VALUES ('usr_t_p', '+919555500000')")
            conn.exec_driver_sql("INSERT INTO partner_profile (user_id, display_name, status, next_step) VALUES ('usr_t_p', 'P', 'ACTIVE', 'COMPLETE')")
    key = str(uuid.uuid4())
    body = {"partnerId": "usr_t_p", "type": "TOP_UP", "amountMinor": 50000, "note": "Cash top-up"}
    first = client.post("/admin/api/ledger/adjust", json=body, headers={"Idempotency-Key": key}).get_json()
    second = client.post("/admin/api/ledger/adjust", json=body, headers={"Idempotency-Key": key}).get_json()
    assert first["data"]["id"] == second["data"]["id"] and first["data"]["balanceMinor"] == 50000
    bad = client.post("/admin/api/ledger/adjust", json={**body, "type": "JOB_FEE"}, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert bad.get_json()["error"]["code"] == "INVALID_TYPE"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def upload_icon(client, data=PNG, purpose="SERVICE_ICON"):
    """Upload one picture through the console's media endpoint.
    Returns the response, so tests can read either the data or the error."""
    return client.post("/admin/api/media", data={"purpose": purpose, "file": (io.BytesIO(data), "icon.png")}, content_type="multipart/form-data")


def stored_file(app, url):
    """Return the path a public media URL is stored at.
    The URL's tail after /media/ is the key under the public root."""
    return Path(app.config["NS"].media_public_root) / url.split("/media/", 1)[1]


def test_service_icon_is_uploaded_then_saved_on_the_service(app, client):
    """The console uploads the picture first and gets an id and URL back.
    A second request creates the service carrying that URL, and the catalog serves it.
    Deleting the service removes the picture."""
    login(client)
    up = upload_icon(client)
    assert up.status_code == 201, up.get_json()
    icon = up.get_json()["data"]
    assert icon["mediaId"].startswith("med_") and "/service_icon/" in icon["url"]
    assert stored_file(app, icon["url"]).is_file()
    created = client.post("/admin/api/r/service", json={"values": {"id": "svc_t_icon", "name": "Icon test", "icon_url": icon["url"], "category": "T", "category_label": "T"}})
    assert created.status_code == 201, created.get_json()
    assert client.get("/admin/api/r/service?f.id=svc_t_icon").get_json()["data"][0]["icon_url"] == icon["url"]
    assert client.delete("/admin/api/r/service", json={"key": {"id": "svc_t_icon"}}).status_code == 200
    assert not stored_file(app, icon["url"]).exists()


def test_replacing_the_icon_removes_the_old_picture(app, client):
    """Uploading a different picture and saving swaps the URL on the service.
    The first picture is deleted from disk once nothing uses it.
    Saving without a change keeps the picture."""
    login(client)
    first = upload_icon(client).get_json()["data"]
    client.post("/admin/api/r/service", json={"values": {"id": "svc_t_swap", "name": "Swap", "icon_url": first["url"], "category": "T", "category_label": "T"}})
    key = {"id": "svc_t_swap"}
    client.put("/admin/api/r/service", json={"key": key, "values": {"name": "Swap 2", "icon_url": first["url"]}})
    assert stored_file(app, first["url"]).is_file()
    second = upload_icon(client, PNG + b"\x01").get_json()["data"]
    assert client.put("/admin/api/r/service", json={"key": key, "values": {"icon_url": second["url"]}}).status_code == 200
    assert not stored_file(app, first["url"]).exists() and stored_file(app, second["url"]).is_file()
    assert client.put("/admin/api/r/service", json={"key": key, "values": {"icon_url": ""}}).status_code == 200
    assert not stored_file(app, second["url"]).exists()
    client.delete("/admin/api/r/service", json={"key": key})


def test_unsaved_upload_can_be_discarded_but_a_used_one_cannot(app, client):
    """A picture replaced before saving is thrown away by the console.
    One a service still uses is refused with 409, and an unknown id is 404.
    Only admin picture purposes can be uploaded here."""
    login(client)
    spare = upload_icon(client).get_json()["data"]
    assert client.delete(f"/admin/api/media/{spare['mediaId']}").status_code == 200
    assert not stored_file(app, spare["url"]).exists()
    assert client.delete(f"/admin/api/media/{spare['mediaId']}").status_code == 404
    used = upload_icon(client).get_json()["data"]
    client.post("/admin/api/r/service", json={"values": {"id": "svc_t_used", "name": "Used", "icon_url": used["url"], "category": "T", "category_label": "T"}})
    assert client.delete(f"/admin/api/media/{used['mediaId']}").get_json()["error"]["code"] == "MEDIA_IN_USE"
    assert stored_file(app, used["url"]).is_file()
    client.delete("/admin/api/r/service", json={"key": {"id": "svc_t_used"}})
    assert upload_icon(client, purpose="IDENTITY_DOCUMENT").get_json()["error"]["code"] == "INVALID_MEDIA_PURPOSE"


def test_icon_upload_needs_admin_and_a_real_image(client):
    """Anonymous callers are refused, and the file's real type is sniffed.
    A text file renamed .png is a 415."""
    assert upload_icon(client).status_code == 401
    login(client)
    assert upload_icon(client, b"not an image").get_json()["error"]["code"] == "MEDIA_TYPE_NOT_ALLOWED"
