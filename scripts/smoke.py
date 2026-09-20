import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
RESULTS = []
EXCHANGES = []


class Client:
    def __init__(self, base, token=None, app="CUSTOMER", admin_key=None):
        """Hold what every call needs: server, token, app and admin key.
        One client per signed-in person.
        Calls go over real HTTP, exactly like the apps."""
        self.base, self.token, self.app, self.admin_key = base.rstrip("/"), token, app, admin_key

    def call(self, method, path, body=None, expect=200, idem=False, headers=None, raw=None, content_type=None):
        """Make one HTTP call, record it, and check the status.
        idem adds a fresh Idempotency-Key; raw sends bytes instead of JSON.
        Returns the decoded JSON body, or None for empty bodies."""
        h = {"X-App": self.app}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.admin_key:
            h["X-Admin-Key"] = self.admin_key
        if idem:
            h["Idempotency-Key"] = str(uuid.uuid4())
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if content_type:
            h["Content-Type"] = content_type
        h.update(headers or {})
        if getattr(self, "cookie", None):
            h["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, payload = resp.status, resp.read()
                cookie = resp.headers.get("Set-Cookie")
                if cookie and cookie.startswith("nsv_admin=") and "Max-Age=0" not in cookie:
                    self.cookie = cookie.split(";")[0]
        except urllib.error.HTTPError as err:
            status, payload = err.code, err.read()
        try:
            parsed = json.loads(payload) if payload else None
        except ValueError:
            parsed = None
        code = (parsed or {}).get("error", {}).get("code") if isinstance(parsed, dict) else None
        expected = expect if isinstance(expect, tuple) else (expect,)
        ok = status in expected
        RESULTS.append((ok, method, path.split("?")[0], status, code or ""))
        EXCHANGES.append({
            "method": method, "path": path, "status": status,
            "headers": {k: v for k, v in h.items() if k in ("X-App", "Idempotency-Key", "If-None-Match", "Content-Type", "Authorization", "X-Admin-Key", "Cookie")},
            "body": body if body is not None else (multipart_summary(raw) if raw and content_type and "multipart" in content_type else None),
            "response": parsed if parsed is not None else (None if not payload else f"<{len(payload)} bytes, non-JSON>"),
        })
        print(f"{'PASS' if ok else 'FAIL'}  {status}  {method:6} {path[:80]}{('  ' + code) if code else ''}")
        return parsed


def multipart_summary(raw):
    """Describe a multipart body for the spec without dumping bytes.
    Shows the purpose field and the file part as the app sends them.
    Bodies that are not real multipart are shown as raw text."""
    import re

    match = re.search(rb'name="purpose"\s+([A-Z_]+)', raw)
    if not match:
        return f"<{len(raw)} raw bytes: {raw[:40]!r}>"
    return f"multipart/form-data\n  purpose = {match.group(1).decode()}\n  file    = photo.jpg (image/jpeg, {len(raw)} bytes total)"


def multipart(purpose, filename="photo.jpg"):
    """Build a multipart body holding one image and its purpose.
    Returns (bytes, content_type) for Client.call.
    The image is a tiny JPEG header, enough for the type check."""
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\n{purpose}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: image/jpeg\r\n\r\n'.encode() + JPEG + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def sign_in(base, phone, app, code):
    """Sign a phone in through the OTP endpoints.
    Returns a Client with the new access token and the refresh token.
    Uses the fixed development code."""
    anon = Client(base, app=app)
    challenge = anon.call("POST", "/auth/otp/request", {"phoneNumber": phone, "purpose": "LOGIN", "language": "te"})["data"]["challengeId"]
    data = anon.call("POST", "/auth/otp/verify", {"challengeId": challenge, "code": code})["data"]
    client = Client(base, data["accessToken"], app)
    client.user_id, client.refresh = data["user"]["id"], data["refreshToken"]
    return client


def request_body(day, place="plc_podalakur", work="rotavator", not_sure=False, service="svc_tractor", preferred=None):
    """Build a tractor request for a given day.
    not_sure leaves the acreage to the partner.
    Everything else matches what the customer app sends."""
    acres = {"questionId": "q_acres", "values": [], "notSure": True} if not_sure else {"questionId": "q_acres", "values": ["3"], "unit": "ACRE"}
    return {
        "serviceId": service, "workTypeId": work, "templateVersion": 3,
        "answers": [{"questionId": "q_work_type", "values": [work]}, acres],
        "description": "Smoke test job", "schedule": {"date": day.isoformat(), "dayPart": "MORNING"},
        "location": {"placeId": place, "landmark": "Smoke landmark", "latitude": 14.4168, "longitude": 79.7334, "accuracyMeters": 15},
        "mediaIds": [], "preferredPartnerId": preferred, "origin": {"source": "SAVED_PARTNER" if preferred else "HOME"},
    }


SECRET_KEYS = ("accessToken", "refreshToken")


def scrub(value):
    """Shorten tokens and long lists so the examples stay readable.
    Access and refresh tokens keep only their first characters.
    Passwords and completion PINs are replaced outright; long lists keep three items."""
    if isinstance(value, dict):
        masked = {"password": "<password>", "pin": "••••"}
        return {
            k: masked[k] if k in masked and isinstance(v, str)
            else (v[:12] + "…" if k in SECRET_KEYS and isinstance(v, str) else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub(v) for v in value[:3]] + ([f"… {len(value) - 3} more"] if len(value) > 3 else [])
    return value


def error_code(ex):
    """Pull the error code out of a recorded response.
    Returns None for successes and non-JSON bodies.
    Used to pick one example per distinct outcome."""
    return ex["response"].get("error", {}).get("code") if isinstance(ex["response"], dict) else None


def write_spec(target, base):
    """Write every endpoint with the requests and responses recorded this run.
    Routes come from the app's own URL map, so nothing is missed or invented.
    Each endpoint shows its success example and each distinct error seen."""
    from app import create_app

    app = create_app(Config(), start_scheduler=False)
    adapter = app.url_map.bind("localhost")
    routes = []
    for rule in app.url_map.iter_rules():
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            routes.append((rule.rule, method))
    order = [("/health", 0), ("/catalog", 1), ("/auth", 1), ("/locations", 1), ("/geographies", 1), ("/media", 1),
             ("/devices", 1), ("/customer", 2), ("/partner", 3), ("/admin", 4)]
    group_of = lambda path: next((v for k, v in order if path.startswith(k)), 9)
    routes.sort(key=lambda r: (group_of(r[0]), r[0], r[1]))
    by_route = {}
    for ex in EXCHANGES:
        try:
            rule, _ = adapter.match(ex["path"].split("?")[0], method=ex["method"], return_rule=True)
        except Exception:
            continue
        by_route.setdefault((rule.rule, ex["method"]), []).append(ex)
    sections = {0: "Health", 1: "Common (chunk 1): both apps", 2: "Customer app", 3: "Partner app", 4: "Admin console"}
    lines = [
        "# NeoSeva API spec",
        "",
        f"Generated by `python scripts/smoke.py --spec docs/api-spec.md` against {base}.",
        "Every example below is a real call made during that run; tokens are shortened and long lists trimmed to three items.",
        "",
        "Conventions:",
        "",
        "- Success is `{data, meta}`; failure is `{error: {code, message, details}, meta}`. Apps branch on `error.code`, never on `message`.",
        "- Money is `{amountMinor, currency}` in paise. `meta.serverTime` is the area's local time without a zone, with `meta.timezone` beside it.",
        "- Customer and partner endpoints need `Authorization: Bearer <accessToken>` (unless `AUTH_ENABLED` is off) and `X-App: CUSTOMER|PARTNER`.",
        "- Writes that show an `Idempotency-Key` header require one; send a fresh key per attempt and reuse it on retry.",
        "- Admin endpoints take the `nsv_admin` session cookie from `/admin/api/login`, or `X-Admin-Key: <ADMIN_API_KEY>`.",
        "",
        "## Endpoint index",
        "",
        "| Method | Path | Called | Statuses seen |",
        "|---|---|---|---|",
    ]
    for path, method in routes:
        seen = by_route.get((path, method), [])
        statuses = ", ".join(str(x) for x in sorted({e["status"] for e in seen})) or "-"
        lines.append(f"| {method} | `{path}` | {'yes' if seen else 'no'} | {statuses} |")
    current = None
    for path, method in routes:
        group = group_of(path)
        if group != current:
            current = group
            lines += ["", f"## {sections.get(group, 'Other')}"]
        seen = by_route.get((path, method), [])
        lines += ["", f"### {method} `{path}`", ""]
        if not seen:
            lines.append("_Not called in this run._")
            continue
        picked, keys = [], set()
        for ex in seen:
            key = (ex["status"], error_code(ex))
            if key not in keys:
                keys.add(key)
                picked.append(ex)
        picked.sort(key=lambda e: e["status"] >= 400)
        for ex in picked[:5]:
            code = error_code(ex)
            lines += [f"**{'Example' if ex['status'] < 400 else 'Error'}: {ex['status']}**" + (f" `{code}`" if code else ""), ""]
            masks = {"Authorization": "Bearer eyJ…", "X-Admin-Key": "<ADMIN_API_KEY>", "Cookie": "nsv_admin=<session>"}
            headers = {k: masks.get(k, v) for k, v in ex["headers"].items()}
            text = f"{ex['method']} {ex['path']}\n" + "\n".join(f"{k}: {v}" for k, v in headers.items())
            if ex["body"] is not None:
                text += "\n\n" + (ex["body"] if isinstance(ex["body"], str) else json.dumps(scrub(ex["body"]), indent=2, ensure_ascii=False))
            lines += ["Request:", "", "```http", text, "```", "", "Response:", ""]
            if ex["response"] is None:
                lines += ["```", "(empty body)", "```", ""]
            elif isinstance(ex["response"], str):
                lines += ["```", ex["response"], "```", ""]
            else:
                lines += ["```json", json.dumps(scrub(ex["response"]), indent=2, ensure_ascii=False), "```", ""]
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")
    called = sum(1 for route in routes if route in by_route)
    print(f"\nwrote {target}: {called}/{len(routes)} endpoints with recorded examples")
    for route in routes:
        if route not in by_route:
            print("  not called:", route[1], route[0])


def main():
    """Call every app and admin endpoint against a running server, in the order the product uses them.
    Runtime switches (auth, SMS, URLs) are flipped through the admin API and restored at the end.
    Exits non-zero when any call returns a status other than the expected one."""
    parser = argparse.ArgumentParser(description="Call every NeoSeva endpoint against a running server.")
    parser.add_argument("--base", default="http://127.0.0.1:5000")
    parser.add_argument("--spec", help="write request/response examples as Markdown to this file")
    args = parser.parse_args()
    cfg = Config()
    base = args.base
    admin = Client(base, admin_key=cfg.admin_api_key)
    anon = Client(base)
    stamp = str(int(time.time()))[-6:]
    tomorrow = date.today() + timedelta(days=1)

    print("\n== Health, runtime config and switches ==")
    anon.call("GET", "/health")
    admin.call("GET", "/admin/api/config")
    admin.call("PUT", "/admin/api/config/SMS_FIXED_OTP", {"value": "246810"})
    admin.call("PUT", "/admin/api/config/AUTH_ENABLED", {"value": "false"})
    anon.call("GET", "/customer/me")
    admin.call("PUT", "/admin/api/config/AUTH_ENABLED", {"value": "true"})
    anon.call("GET", "/customer/me", expect=401)
    admin.call("PUT", "/admin/api/config/DATABASE_URL", {"value": "x"}, expect=409)
    admin.call("PUT", "/admin/api/config/OTP_LENGTH", {"value": "six"}, expect=422)

    print("\n== Chunk 1: getting in ==")
    anon.call("GET", "/catalog?areaId=geo_podalakur")
    etag = urllib.request.urlopen(base + "/catalog?areaId=geo_podalakur").headers["ETag"]
    anon.call("GET", "/catalog?areaId=geo_podalakur", headers={"If-None-Match": etag}, expect=304)
    anon.call("GET", "/catalog?areaId=geo_nowhere")
    anon.call("GET", "/locations/places?areaId=geo_podalakur")
    anon.call("GET", "/geographies/resolve?latitude=14.4167&longitude=79.7333")
    anon.call("GET", "/geographies/resolve?latitude=17.38&longitude=78.48")
    anon.call("GET", "/geographies/resolve?latitude=x&longitude=1", expect=400)
    anon.call("POST", "/locations/place-interest", {"placeName": "Kovur", "phoneNumber": None}, expect=202)
    anon.call("POST", "/locations/place-interest", {"placeName": " "}, expect=400)
    anon.call("POST", "/auth/otp/request", {"phoneNumber": "123"}, expect=400)
    bad = anon.call("POST", "/auth/otp/request", {"phoneNumber": f"+9180{stamp}99", "purpose": "LOGIN"})["data"]["challengeId"]
    anon.call("POST", "/auth/otp/verify", {"challengeId": bad, "code": "000000"}, expect=422)
    anon.call("POST", "/auth/otp/request", {"phoneNumber": f"+9180{stamp}99", "purpose": "LOGIN"}, expect=429)
    customer = sign_in(base, f"+9181{stamp}01", "CUSTOMER", "246810")
    refreshed = customer.call("POST", "/auth/token/refresh", {"refreshToken": customer.refresh})["data"]
    customer.token, customer.refresh = refreshed["accessToken"], refreshed["refreshToken"]
    customer.call("PUT", f"/devices/smoke-{stamp}/push-token", {"token": f"tok-{stamp}", "app": "CUSTOMER", "platform": "ANDROID", "language": "te"}, expect=204)
    raw, ctype = multipart("REQUEST_PHOTO")
    photo = customer.call("POST", "/media", raw=raw, content_type=ctype, expect=201)["data"]["mediaId"]
    anon.call("GET", f"/media/request_photo/{photo}.jpg")
    raw, ctype = multipart("REQUEST_PHOTO")
    customer.call("POST", "/media", raw=b"not an image", content_type="multipart/form-data; boundary=x", expect=400)

    print("\n== Chunk 3 setup: a partner onboards and is approved ==")
    partner = sign_in(base, f"+9182{stamp}01", "PARTNER", "246810")
    partner.call("GET", "/partner/me")
    partner.call("POST", "/partner/me/activate", idem=True, expect=422)
    partner.call("PATCH", "/partner/me/basic", {"fullName": "Smoke Partner", "basePlaceId": "plc_podalakur", "experienceRange": "5_TO_10_YEARS", "language": "te"})
    partner.call("PUT", "/partner/me/services", {"services": [{"serviceId": "svc_tractor", "equipmentIds": ["ROTAVATOR", "PLOUGH"]}]})
    partner.call("PATCH", "/partner/me", {"travelRadiusKm": 7}, expect=422)
    partner.call("PATCH", "/partner/me", {"travelRadiusKm": 15})
    partner.call("PUT", f"/devices/smoke-p-{stamp}/push-token", {"token": f"ptok-{stamp}", "app": "PARTNER"}, expect=204)
    partner.call("POST", "/partner/me/activate", idem=True)
    raw, ctype = multipart("IDENTITY_DOCUMENT")
    doc = partner.call("POST", "/media", raw=raw, content_type=ctype, expect=201)["data"]["mediaId"]
    raw, ctype = multipart("IDENTITY_SELFIE")
    selfie = partner.call("POST", "/media", raw=raw, content_type=ctype, expect=201)["data"]["mediaId"]
    partner.call("POST", "/partner/me/identity-verification/initiate", {"documentType": "MASKED_AADHAAR", "documentMediaId": doc, "selfieMediaId": selfie})
    partner.call("GET", "/partner/me/identity-verification")
    admin.call("GET", f"/admin/api/media/{doc}/file")
    admin.call("POST", f"/admin/api/identity/{partner.user_id}/decision", {"status": "VERIFIED", "activate": True})
    admin.call("GET", f"/admin/api/media/{doc}/file", expect=404)
    partner.call("PATCH", "/partner/me", {"acceptingNewJobs": True})

    print("\n== Chunk 2: she asks for work ==")
    anon.call("GET", "/catalog/services/svc_tractor/request-template")
    anon.call("GET", "/catalog/services/svc_nope/request-template", expect=404)
    customer.call("GET", "/customer/me")
    customer.call("PATCH", "/customer/me", {"displayName": "Smoke Customer", "defaultPlaceId": "plc_podalakur"})
    customer.call("GET", "/customer/home")
    place = customer.call("POST", "/customer/places", {"labelCode": "FIELD", "placeId": "plc_podalakur", "landmark": "Smoke landmark", "latitude": 14.4168, "longitude": 79.7334, "accuracyMeters": 15}, expect=(200, 201))["data"]
    customer.call("POST", "/customer/places", {"labelCode": "FIELD", "placeId": "plc_podalakur", "landmark": "Smoke landmark"}, expect=200)
    customer.call("GET", "/customer/places")
    customer.call("POST", "/customer/places", {"labelCode": "NOPE", "placeId": "plc_podalakur"}, expect=422)
    customer.call("POST", "/customer/requests", {**request_body(tomorrow), "templateVersion": 1}, idem=True, expect=422)
    customer.call("POST", "/customer/requests", request_body(date.today() - timedelta(days=1)), idem=True, expect=422)
    rid = customer.call("POST", "/customer/requests", {**request_body(tomorrow, preferred=partner.user_id), "mediaIds": [photo]}, idem=True, expect=201)["data"]["id"]
    customer.call("GET", "/customer/requests?bucket=ACTIVE")
    customer.call("GET", f"/customer/requests/{rid}")
    customer.call("PATCH", f"/customer/requests/{rid}", {"description": "Edited by smoke test"}, idem=True)
    cancel_me = customer.call("POST", "/customer/requests", request_body(tomorrow), idem=True, expect=201)["data"]["id"]
    customer.call("POST", f"/customer/requests/{cancel_me}/cancel", {"reasonCode": "PLANS_CHANGED", "note": None}, idem=True)
    customer.call("POST", f"/customer/requests/{cancel_me}/cancel", {"reasonCode": None}, idem=True)
    customer.call("GET", "/customer/requests?bucket=PAST")

    print("\n== Chunk 3: he sees it and prices it ==")
    partner.call("GET", "/partner/home")
    partner.call("GET", "/partner/opportunities")
    partner.call("GET", f"/partner/opportunities/{rid}")
    path = f"/partner/opportunities/{rid}/offers"
    partner.call("POST", path, {"pricing": {"type": "FIXED", "exactAmount": {"amountMinor": 999999999, "currency": "INR"}}}, idem=True, expect=422)
    partner.call("POST", path, {"pricing": {"type": "UNIT_RATE", "rate": {"amountMinor": 80000, "currency": "INR"}, "unit": {"code": "TRIP"}}}, idem=True, expect=422)
    partner.call("POST", path, {"pricing": {"type": "FIXED", "exactAmount": {"amountMinor": 200000, "currency": "INR"}}, "offeredDaypart": "ANY_TIME"}, idem=True, expect=422)
    offer = partner.call("POST", path, {"pricing": {"type": "UNIT_RATE", "rate": {"amountMinor": 80000, "currency": "INR"}, "unit": {"code": "ACRE"}}, "offeredDaypart": "AFTERNOON"}, idem=True, expect=201)["data"]
    partner.call("POST", path, {"pricing": {"type": "FIXED", "exactAmount": {"amountMinor": 1000, "currency": "INR"}}}, idem=True, expect=409)
    partner.call("GET", "/partner/offers")
    customer.call("GET", f"/customer/requests/{rid}/offers")
    customer.call("PATCH", f"/customer/requests/{rid}", {"description": "x"}, idem=True, expect=409)

    print("\n== Chunk 4: the job ==")
    booking = customer.call("POST", f"/customer/requests/{rid}/book", {"offerId": offer["id"]}, idem=True, expect=201)["data"]
    bid = booking["id"]
    again = customer.call("POST", f"/customer/requests/{rid}/book", {"offerId": offer["id"]}, idem=True, expect=409)
    assert again["error"]["details"]["bookingId"] == bid
    partner.call("DELETE", f"/partner/offers/{offer['id']}", idem=True, expect=409)
    customer.call("GET", f"/customer/bookings/{bid}")
    partner.call("GET", "/partner/jobs?bucket=ACTIVE")
    partner.call("GET", f"/partner/bookings/{bid}")
    partner.call("POST", f"/partner/bookings/{bid}/on-my-way", expect=204)
    partner.call("POST", f"/partner/bookings/{bid}/arrive", {"evidence": {"latitude": 14.4169, "longitude": 79.7335, "accuracyMeters": 6}}, idem=True)
    pin = customer.call("GET", f"/customer/bookings/{bid}/completion-pin")["data"]["pin"]
    partner.call("POST", f"/partner/bookings/{bid}/final-amount", {"quantity": 3}, idem=True, expect=422)
    partner.call("POST", f"/partner/bookings/{bid}/complete", {"pin": "0000" if pin != "0000" else "1111"}, idem=True, expect=422)
    partner.call("POST", f"/partner/bookings/{bid}/complete", {"pin": pin}, idem=True)
    customer.call("GET", f"/customer/bookings/{bid}/completion-pin", expect=409)
    partner.call("GET", "/partner/jobs?bucket=HISTORY")

    print("\n== Chunk 4: inspection job with agreement, and her overrides ==")
    rid2 = customer.call("POST", "/customer/requests", request_body(tomorrow, not_sure=True, preferred=partner.user_id), idem=True, expect=201)["data"]["id"]
    visit = partner.call("POST", f"/partner/opportunities/{rid2}/offers", {"pricing": {"type": "INSPECTION", "inspectionCharge": {"amountMinor": 30000, "currency": "INR"}}}, idem=True, expect=201)["data"]
    bid2 = customer.call("POST", f"/customer/requests/{rid2}/book", {"offerId": visit["id"]}, idem=True, expect=201)["data"]["id"]
    customer.call("POST", f"/customer/bookings/{bid2}/confirm-arrival")
    partner.call("POST", f"/partner/bookings/{bid2}/final-amount", {"amountMinor": 500000}, idem=True)
    customer.call("GET", f"/customer/bookings/{bid2}/completion-pin", expect=409)
    customer.call("POST", f"/customer/bookings/{bid2}/agree-amount", idem=True)
    customer.call("GET", f"/customer/bookings/{bid2}/completion-pin")
    customer.call("POST", f"/customer/bookings/{bid2}/confirm-complete")

    print("\n== Decline and withdraw ==")
    rid3 = customer.call("POST", "/customer/requests", request_body(tomorrow, preferred=partner.user_id), idem=True, expect=201)["data"]["id"]
    offer3 = partner.call("POST", f"/partner/opportunities/{rid3}/offers", {"pricing": {"type": "FIXED", "exactAmount": {"amountMinor": 210000, "currency": "INR"}}}, idem=True, expect=201)["data"]
    partner.call("DELETE", f"/partner/offers/{offer3['id']}", idem=True)
    partner.call("POST", f"/partner/opportunities/{rid3}/decline", expect=204)

    print("\n== Admin console API ==")
    console = Client(base, app="ADMIN")
    console.call("GET", "/admin")
    console.call("GET", "/admin/")
    console.call("GET", "/admin/static/admin.css")
    console.call("POST", "/admin/api/login", {"username": cfg.admin_bootstrap_username, "password": "wrong"}, expect=401)
    console.call("POST", "/admin/api/login", {"username": cfg.admin_bootstrap_username, "password": cfg.admin_bootstrap_password})
    console.call("GET", "/admin/api/me")
    admin.call("GET", "/admin/api/me")
    admin.call("GET", "/admin/api/meta")
    admin.call("GET", "/admin/api/stats")
    admin.call("GET", "/admin/api/partners")
    admin.call("GET", "/admin/api/demand")
    admin.call("GET", f"/admin/api/requests/{rid}/detail")
    admin.call("POST", f"/admin/api/requests/{rid3}/run-matching")
    admin.call("POST", "/admin/api/jobs/tick")
    admin.call("GET", "/admin/api/r/service?q=tractor")
    admin.call("POST", "/admin/api/r/service/get", {"key": {"id": "svc_tractor"}})
    admin.call("PUT", "/admin/api/r/service", {"key": {"id": "svc_tractor"}, "values": {"common_on_home": True}})
    admin.call("POST", "/admin/api/r/reason", {"values": {"context": "CUSTOMER_REPORT_ISSUE", "code": f"SMOKE_{stamp}", "label": "Smoke", "is_active": True}}, expect=201)
    admin.call("DELETE", "/admin/api/r/reason", {"key": {"context": "CUSTOMER_REPORT_ISSUE", "code": f"SMOKE_{stamp}"}})
    admin.call("POST", "/admin/api/ledger/adjust", {"partnerId": partner.user_id, "type": "TOP_UP", "amountMinor": 50000, "note": "Smoke top-up"}, idem=True, expect=201)
    admin.call("POST", f"/admin/api/partners/{partner.user_id}/status", {"status": "SUSPENDED", "statusNote": "Smoke pause"})
    partner.call("PATCH", "/partner/me", {"acceptingNewJobs": True}, expect=409)
    admin.call("POST", f"/admin/api/partners/{partner.user_id}/status", {"status": "ACTIVE"})

    print("\n== Sign out and clean up ==")
    customer.call("DELETE", f"/customer/places/{place['id']}", expect=204)
    customer.call("DELETE", f"/devices/smoke-{stamp}/push-token", expect=204)
    customer.call("POST", "/auth/logout", {"refreshToken": customer.refresh}, expect=204)
    customer.call("POST", "/auth/token/refresh", {"refreshToken": customer.refresh}, expect=401)
    admin.call("DELETE", "/admin/api/config/SMS_FIXED_OTP")
    admin.call("DELETE", "/admin/api/config/AUTH_ENABLED")
    console.call("POST", "/admin/api/logout")
    console.call("GET", "/admin/api/me", expect=401)

    if args.spec:
        write_spec(args.spec, base)
    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS)} calls, {len(RESULTS) - len(failed)} as expected, {len(failed)} unexpected")
    for r in failed:
        print("  UNEXPECTED", r[1], r[2], r[3], r[4])
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
