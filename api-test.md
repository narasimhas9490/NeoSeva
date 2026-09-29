# NeoSeva API test report

- **Target:** https://neoseva.onrender.com (Render, `APP_ENV=production`, Supabase database and storage)
- **Run:** 2026-09-20, from a local machine, using a Python `urllib` runner (each call is recorded below with its real request and response)
- **Calls made:** 107 — **104 behaved as expected, 3 did not**
- **Auth state at test time:** `GET /health` reports `authEnabled: false`, `smsEnabled: false`

## Verdict

**All endpoints are deployed, routed and answering with the documented response envelope, except one: `POST /media` fails with HTTP 500 for every valid image upload.** That blocks request photos, partner profile photos and the identity-verification flow (which needs an uploaded document and selfie).

## Issues found

| # | Severity | Endpoint / area | What happened |
|---|---|---|---|
| 1 | **High** | `POST /media` | Valid JPEG uploads (purposes `REQUEST_PHOTO`, `IDENTITY_DOCUMENT`, `IDENTITY_SELFIE`) return `500 INTERNAL_ERROR` (3 of 3 attempts). Validation paths on the same endpoint work (bad purpose → 400). Failures in Supabase Storage itself are caught and would return `502 MEDIA_UPLOAD_FAILED`, so this is an *unhandled* exception, not a storage rejection. Most likely cause (not confirmed, I have no access to Render logs): `SUPABASE_SERVICE_ROLE_KEY` is unset or empty on Render, so `SupabaseStorage.__init__` raises `RuntimeError` (`app/core/adapters/storage.py:62`), which is not caught by the `except OSError` in `store_image`. `render.yaml` marks that key `sync: false`, so it has to be typed in the dashboard. Check the Render log for `unhandled error in POST /media`; request ids: `api_01M2YNBEX9F23KMMMS5W2K4NPA`, `api_01M2YND3DH13MJ44KZTY90DHV2`, `api_01M2YND47BSTKJ26P4B0C1FRAC`. |
| 2 | **High (security)** | All `/customer/*`, `/partner/*`, `/media`, `/devices/*` | Production is running with **auth disabled**. Every protected endpoint worked with **no token** and acted as the shared dev user `usr_dev_local` (`+919999999999`). Anyone who knows the URL can read and write as that user. The config help text says this switch is "Refused in production", yet `/health` reports `authEnabled: false` under `env: production`, so it looks like it was switched off at runtime through the admin config (or an env var). Set `AUTH_ENABLED` back to true before real users arrive. |
| 3 | Medium | Sign-in (`/auth/otp/*`) | With SMS off, production sends a *random* code to the server log only (a fixed test code is refused in production), so a full OTP sign-in could not be completed from outside. Request, resend-limit, wrong-code, malformed, refresh and logout paths were all tested and behave correctly; the successful `verify` → tokens path was **not** exercised. It also can't be used until `SMS_ENABLED` with a provider is configured. |
| 4 | Low | Performance | Free-plan latency is 0.2–2 s for most calls; `POST /customer/requests` took 4.7 s (two calls) and `PATCH /customer/requests/{id}` 3.1 s. The free Render plan also sleeps when idle, so the first call after a quiet period will be slow (not observed here; the service was awake). |

## Not covered (needs input from you)

- **Authenticated admin endpoints** (`/admin/api/*`: config, stats, partners, identity decision, ledger, resources CRUD, matching, jobs tick, media). I don't have the deployed `ADMIN_API_KEY` or admin password (they are generated / set in the Render dashboard, and the local `.env` values are for development). I verified only that each one **refuses unauthenticated calls with 401** and that login with a wrong password is 401. If you give me the admin credentials (or run with them), I can test these.
- **Offer → booking → completion flow** (`POST /partner/opportunities/{id}/offers`, `/customer/requests/{id}/book`, and all `/partner/bookings/*`, `/customer/bookings/*` actions). These need a partner that an admin has approved (identity verification, which is also blocked by issue 1) and an eligible request. Booking endpoints were tested for **routing and 404 handling only**, using non-existent ids.

## Test data left in the production database

Because auth is off, all writes went to the shared dev user. Cleaned up where the API allows it:

- Deleted: the saved place created during the test.
- Cancelled: both test requests (`API test job (safe to delete)`).
- **Left behind** (no delete endpoint): customer profile `displayName = "API Test Customer"` / default place Podalakur; partner profile `usr_dev_local` now `displayName = "API Test Partner"`, base place Podalakur, service Tractor, radius 15 km, status `ACTIVATED`; one `place-interest` row (`API-test-882894`); one OTP challenge for `+918088289499`; the two cancelled requests. Reset these from the admin console or Supabase if you want a clean slate.

## Results by endpoint

Result is **PASS** when the status matched what the endpoint is designed to return for that input (including deliberate error cases such as 400/401/404/422), **FAIL** otherwise.

| # | Group | Test | Method | Path | Expected | Actual | Result | ms |
|---|---|---|---|---|---|---|---|---|
| 1 | Health | Service and database health | GET | `/health` | 200 | 200 | PASS | 599 |
| 2 | Catalog | Catalog for an area | GET | `/catalog?areaId=geo_podalakur` | 200 | 200 | PASS | 1022 |
| 3 | Catalog | Catalog, unknown area | GET | `/catalog?areaId=geo_nowhere` | 200 | 200 | PASS | 1178 |
| 4 | Catalog | Request template for a service | GET | `/catalog/services/svc_tractor/request-template` | 200 | 200 | PASS | 923 |
| 5 | Catalog | Request template, unknown service | GET | `/catalog/services/svc_nope/request-template` | 404 | 404 SERVICE_NOT_FOUND | PASS | 777 |
| 6 | Locations | Places in an area | GET | `/locations/places?areaId=geo_podalakur` | 200 | 200 | PASS | 653 |
| 7 | Locations | Resolve coordinates inside a served area | GET | `/geographies/resolve?latitude=14.4167&longitude=79.7333` | 200 | 200 | PASS | 722 |
| 8 | Locations | Resolve coordinates outside served areas | GET | `/geographies/resolve?latitude=17.38&longitude=78.48` | 200 | 200 | PASS | 1057 |
| 9 | Locations | Resolve with invalid coordinates | GET | `/geographies/resolve?latitude=x&longitude=1` | 400 | 400 INVALID_COORDINATES | PASS | 683 |
| 10 | Locations | Register interest in an unserved place | POST | `/locations/place-interest` | 202 | 202 | PASS | 764 |
| 11 | Locations | Place interest with blank name | POST | `/locations/place-interest` | 400 | 400 INVALID_PLACE_NAME | PASS | 715 |
| 12 | Auth | OTP request, invalid phone | POST | `/auth/otp/request` | 400 | 400 INVALID_PHONE_NUMBER | PASS | 483 |
| 13 | Auth | OTP request, invalid purpose | POST | `/auth/otp/request` | 400 | 400 INVALID_PURPOSE | PASS | 407 |
| 14 | Auth | OTP request, valid phone | POST | `/auth/otp/request` | 200 | 200 | PASS | 820 |
| 15 | Auth | OTP request again immediately (resend backoff) | POST | `/auth/otp/request` | 429 | 429 OTP_RESEND_TOO_SOON | PASS | 876 |
| 16 | Auth | OTP verify, wrong code | POST | `/auth/otp/verify` | 422 | 422 OTP_INVALID | PASS | 759 |
| 17 | Auth | OTP verify, malformed body | POST | `/auth/otp/verify` | 400 | 400 INVALID_REQUEST | PASS | 232 |
| 18 | Auth | Token refresh with an invalid token | POST | `/auth/token/refresh` | 401 | 401 SESSION_EXPIRED | PASS | 509 |
| 19 | Auth | Logout with an invalid token (always 204) | POST | `/auth/logout` | 204 | 204 | PASS | 743 |
| 20 | Media | Upload a request photo | POST | `/media` | 201 | 500 INTERNAL_ERROR | **FAIL** | 564 |
| 21 | Media | Upload with a bad body | POST | `/media` | 400 | 400 INVALID_MEDIA_PURPOSE | PASS | 558 |
| 22 | Media | Fetch a missing media file | GET | `/media/request_photo/med_missing.jpg` | 404 | 404 NOT_FOUND | PASS | 263 |
| 23 | Devices | Register push token | PUT | `/devices/apitest-882894/push-token` | 204 | 204 | PASS | 1304 |
| 24 | Devices | Remove push token | DELETE | `/devices/apitest-882894/push-token` | 204 | 204 | PASS | 826 |
| 25 | Customer profile & home | Get my profile | GET | `/customer/me` | 200 | 200 | PASS | 1145 |
| 26 | Customer profile & home | Update my profile | PATCH | `/customer/me` | 200 | 200 | PASS | 1103 |
| 27 | Customer profile & home | Update profile with invalid place | PATCH | `/customer/me` | 400/404/422 | 422 PLACE_NOT_FOUND | PASS | 1111 |
| 28 | Customer profile & home | Customer home | GET | `/customer/home` | 200 | 200 | PASS | 783 |
| 29 | Customer saved places | Save a place | POST | `/customer/places` | 200/201 | 201 | PASS | 1041 |
| 30 | Customer saved places | List saved places | GET | `/customer/places` | 200 | 200 | PASS | 1054 |
| 31 | Customer saved places | Save a place with an invalid label | POST | `/customer/places` | 422 | 422 INVALID_PLACE_LABEL | PASS | 783 |
| 32 | Customer requests | Create request with stale template version | POST | `/customer/requests` | 422 | 422 TEMPLATE_VERSION_STALE | PASS | 1727 |
| 33 | Customer requests | Create request for a past date | POST | `/customer/requests` | 422 | 422 DATE_IN_PAST | PASS | 1969 |
| 34 | Customer requests | Create request without Idempotency-Key | POST | `/customer/requests` | 400/422 | 400 IDEMPOTENCY_KEY_REQUIRED | PASS | 556 |
| 35 | Customer requests | Create a request | POST | `/customer/requests` | 201 | 201 | PASS | 4745 |
| 36 | Customer requests | List active requests | GET | `/customer/requests?bucket=ACTIVE` | 200 | 200 | PASS | 1116 |
| 37 | Customer requests | Get one request | GET | `/customer/requests/rqt_01M2YNBYF92WPZJEMCTAEVN6HC` | 200 | 200 | PASS | 981 |
| 38 | Customer requests | Edit a request | PATCH | `/customer/requests/rqt_01M2YNBYF92WPZJEMCTAEVN6HC` | 200 | 200 | PASS | 3122 |
| 39 | Customer requests | List offers for a request (none yet) | GET | `/customer/requests/rqt_01M2YNBYF92WPZJEMCTAEVN6HC/offers` | 200 | 200 | PASS | 1002 |
| 40 | Customer requests | Get a missing request | GET | `/customer/requests/req_missing` | 404 | 404 NOT_FOUND | PASS | 796 |
| 41 | Customer requests | Create a second request (to cancel) | POST | `/customer/requests` | 201 | 201 | PASS | 4687 |
| 42 | Customer requests | Cancel a request | POST | `/customer/requests/rqt_01M2YNCA0WR1884AMCENWSA3MD/cancel` | 200 | 200 | PASS | 1629 |
| 43 | Customer requests | Cancel the same request again (idempotent state) | POST | `/customer/requests/rqt_01M2YNCA0WR1884AMCENWSA3MD/cancel` | 200/409 | 200 | PASS | 1802 |
| 44 | Customer requests | List past requests | GET | `/customer/requests?bucket=PAST` | 200 | 200 | PASS | 915 |
| 45 | Customer requests | Book with an unknown offer | POST | `/customer/requests/rqt_01M2YNBYF92WPZJEMCTAEVN6HC/book` | 404/409/422 | 409 OFFER_NOT_AVAILABLE | PASS | 1755 |
| 46 | Customer bookings (routing only, no booking exists) | GET /customer/bookings/bkg_missing | GET | `/customer/bookings/bkg_missing` | 404 | 404 NOT_FOUND | PASS | 797 |
| 47 | Customer bookings (routing only, no booking exists) | GET /customer/bookings/bkg_missing/completion-pin | GET | `/customer/bookings/bkg_missing/completion-pin` | 404 | 404 NOT_FOUND | PASS | 1136 |
| 48 | Customer bookings (routing only, no booking exists) | POST /customer/bookings/bkg_missing/agree-amount | POST | `/customer/bookings/bkg_missing/agree-amount` | 404 | 404 NOT_FOUND | PASS | 1344 |
| 49 | Customer bookings (routing only, no booking exists) | POST /customer/bookings/bkg_missing/confirm-arrival | POST | `/customer/bookings/bkg_missing/confirm-arrival` | 404 | 404 NOT_FOUND | PASS | 1078 |
| 50 | Customer bookings (routing only, no booking exists) | POST /customer/bookings/bkg_missing/confirm-complete | POST | `/customer/bookings/bkg_missing/confirm-complete` | 404 | 404 NOT_FOUND | PASS | 832 |
| 51 | Partner profile & onboarding | Get partner profile | GET | `/partner/me` | 200 | 200 | PASS | 910 |
| 52 | Partner profile & onboarding | Activate before profile is complete | POST | `/partner/me/activate` | 422 | 422 SETUP_INCOMPLETE | PASS | 1622 |
| 53 | Partner profile & onboarding | Update basic profile | PATCH | `/partner/me/basic` | 200 | 200 | PASS | 1170 |
| 54 | Partner profile & onboarding | Set services and equipment | PUT | `/partner/me/services` | 200 | 200 | PASS | 1755 |
| 55 | Partner profile & onboarding | Set travel radius, invalid value | PATCH | `/partner/me` | 422 | 422 INVALID_TRAVEL_RADIUS | PASS | 900 |
| 56 | Partner profile & onboarding | Set travel radius, valid value | PATCH | `/partner/me` | 200 | 200 | PASS | 1416 |
| 57 | Partner profile & onboarding | Register partner push token | PUT | `/devices/apitest-882894-p/push-token` | 204 | 204 | PASS | 881 |
| 58 | Partner profile & onboarding | Remove partner push token | DELETE | `/devices/apitest-882894-p/push-token` | 204 | 204 | PASS | 1051 |
| 59 | Partner profile & onboarding | Activate profile | POST | `/partner/me/activate` | 200/409/422 | 200 | PASS | 1526 |
| 60 | Partner profile & onboarding | Upload identity document | POST | `/media` | 201 | 500 INTERNAL_ERROR | **FAIL** | 809 |
| 61 | Partner profile & onboarding | Upload identity selfie | POST | `/media` | 201 | 500 INTERNAL_ERROR | **FAIL** | 609 |
| 62 | Partner profile & onboarding | Get identity verification (before) | GET | `/partner/me/identity-verification` | 200 | 200 | PASS | 830 |
| 63 | Partner opportunities, offers, jobs | Partner home | GET | `/partner/home` | 200 | 200 | PASS | 1648 |
| 64 | Partner opportunities, offers, jobs | List opportunities | GET | `/partner/opportunities` | 200 | 200 | PASS | 980 |
| 65 | Partner opportunities, offers, jobs | View one opportunity | GET | `/partner/opportunities/rqt_01M2YNBYF92WPZJEMCTAEVN6HC` | 200/403/404 | 404 NOT_FOUND | PASS | 1313 |
| 66 | Partner opportunities, offers, jobs | Send an offer (partner not yet admin-approved) | POST | `/partner/opportunities/rqt_01M2YNBYF92WPZJEMCTAEVN6HC/off…` | 201/403/404/409/422 | 403 NOT_ELIGIBLE | PASS | 1450 |
| 67 | Partner opportunities, offers, jobs | Decline an opportunity | POST | `/partner/opportunities/rqt_01M2YNBYF92WPZJEMCTAEVN6HC/dec…` | 204/403/404/409 | 404 NOT_FOUND | PASS | 1073 |
| 68 | Partner opportunities, offers, jobs | List my offers | GET | `/partner/offers` | 200 | 200 | PASS | 894 |
| 69 | Partner opportunities, offers, jobs | Withdraw an unknown offer | DELETE | `/partner/offers/off_missing` | 404 | 404 NOT_FOUND | PASS | 1415 |
| 70 | Partner opportunities, offers, jobs | List active jobs | GET | `/partner/jobs?bucket=ACTIVE` | 200 | 200 | PASS | 899 |
| 71 | Partner opportunities, offers, jobs | List job history | GET | `/partner/jobs?bucket=HISTORY` | 200 | 200 | PASS | 864 |
| 72 | Partner opportunities, offers, jobs | GET /partner/bookings/bkg_missing (routing only) | GET | `/partner/bookings/bkg_missing` | 404/422 | 404 NOT_FOUND | PASS | 803 |
| 73 | Partner opportunities, offers, jobs | POST /partner/bookings/bkg_missing/on-my-way (routing only) | POST | `/partner/bookings/bkg_missing/on-my-way` | 404/422 | 404 NOT_FOUND | PASS | 1072 |
| 74 | Partner opportunities, offers, jobs | POST /partner/bookings/bkg_missing/arrive (routing only) | POST | `/partner/bookings/bkg_missing/arrive` | 404/422 | 404 NOT_FOUND | PASS | 1329 |
| 75 | Partner opportunities, offers, jobs | POST /partner/bookings/bkg_missing/final-amount (routing only) | POST | `/partner/bookings/bkg_missing/final-amount` | 404/422 | 404 NOT_FOUND | PASS | 1835 |
| 76 | Partner opportunities, offers, jobs | POST /partner/bookings/bkg_missing/complete (routing only) | POST | `/partner/bookings/bkg_missing/complete` | 404/422 | 404 NOT_FOUND | PASS | 1723 |
| 77 | Admin (unauthenticated: every endpoint must refuse) | Admin console page | GET | `/admin` | 200 | 200 | PASS | 245 |
| 78 | Admin (unauthenticated: every endpoint must refuse) | Admin console page (trailing slash) | GET | `/admin/` | 200 | 200 | PASS | 573 |
| 79 | Admin (unauthenticated: every endpoint must refuse) | Admin static asset | GET | `/admin/static/admin.css` | 200 | 200 | PASS | 250 |
| 80 | Admin (unauthenticated: every endpoint must refuse) | Admin login, wrong password | POST | `/admin/api/login` | 401 | 401 INVALID_CREDENTIALS | PASS | 1288 |
| 81 | Admin (unauthenticated: every endpoint must refuse) | POST /admin/api/logout | POST | `/admin/api/logout` | 200 | 200 | PASS | 541 |
| 82 | Admin (unauthenticated: every endpoint must refuse) | GET /admin/api/me without credentials | GET | `/admin/api/me` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 271 |
| 83 | Admin (unauthenticated: every endpoint must refuse) | GET /admin/api/meta without credentials | GET | `/admin/api/meta` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 274 |
| 84 | Admin (unauthenticated: every endpoint must refuse) | GET /admin/api/r/service without credentials | GET | `/admin/api/r/service` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 256 |
| 85 | Admin (unauthenticated: every endpoint must refuse) | POST /admin/api/r/service/get without credentials | POST | `/admin/api/r/service/get` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 272 |
| 86 | Admin (unauthenticated: every endpoint must refuse) | POST /admin/api/r/service without credentials | POST | `/admin/api/r/service` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 268 |
| 87 | Admin (unauthenticated: every endpoint must refuse) | PUT /admin/api/r/service without credentials | PUT | `/admin/api/r/service` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 285 |
| 88 | Admin (unauthenticated: every endpoint must refuse) | DELETE /admin/api/r/service without credentials | DELETE | `/admin/api/r/service` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 520 |
| 89 | Admin (unauthenticated: every endpoint must refuse) | GET /admin/api/stats without credentials | GET | `/admin/api/stats` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 275 |
| 90 | Admin (unauthenticated: every endpoint must refuse) | GET /admin/api/partners without credentials | GET | `/admin/api/partners` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 317 |
| 91 | Admin (unauthenticated: every endpoint must refuse) | POST /admin/api/partners/x/status without credentials | POST | `/admin/api/partners/x/status` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 264 |
| 92 | Admin (unauthenticated: every endpoint must refuse) | POST /admin/api/identity/x/decision without credentials | POST | `/admin/api/identity/x/decision` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 303 |
| 93 | Admin (unauthenticated: every endpoint must refuse) | POST /admin/api/media without credentials | POST | `/admin/api/media` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 312 |
| 94 | Admin (unauthenticated: every endpoint must refuse) | DELETE /admin/api/media/x without credentials | DELETE | `/admin/api/media/x` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 616 |
| 95 | Admin (unauthenticated: every endpoint must refuse) | GET /admin/api/media/x/file without credentials | GET | `/admin/api/media/x/file` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 203 |
| 96 | Admin (unauthenticated: every endpoint must refuse) | POST /admin/api/ledger/adjust without credentials | POST | `/admin/api/ledger/adjust` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 244 |
| 97 | Admin (unauthenticated: every endpoint must refuse) | GET /admin/api/requests/x/detail without credentials | GET | `/admin/api/requests/x/detail` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 240 |
| 98 | Admin (unauthenticated: every endpoint must refuse) | POST /admin/api/requests/x/run-matching without credentials | POST | `/admin/api/requests/x/run-matching` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 248 |
| 99 | Admin (unauthenticated: every endpoint must refuse) | POST /admin/api/jobs/tick without credentials | POST | `/admin/api/jobs/tick` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 229 |
| 100 | Admin (unauthenticated: every endpoint must refuse) | GET /admin/api/demand without credentials | GET | `/admin/api/demand` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 265 |
| 101 | Admin (unauthenticated: every endpoint must refuse) | GET /admin/api/config without credentials | GET | `/admin/api/config` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 236 |
| 102 | Admin (unauthenticated: every endpoint must refuse) | PUT /admin/api/config/AUTH_ENABLED without credentials | PUT | `/admin/api/config/AUTH_ENABLED` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 572 |
| 103 | Admin (unauthenticated: every endpoint must refuse) | DELETE /admin/api/config/AUTH_ENABLED without credentials | DELETE | `/admin/api/config/AUTH_ENABLED` | 401 | 401 ADMIN_UNAUTHENTICATED | PASS | 214 |
| 104 | Cleanup and misc | Delete the saved place created above | DELETE | `/customer/places/spl_01M2YNBPZPBCE0BQ2TTNFQQCW7` | 204 | 204 | PASS | 1016 |
| 105 | Cleanup and misc | Cancel the test request created above | POST | `/customer/requests/rqt_01M2YNBYF92WPZJEMCTAEVN6HC/cancel` | 200/409 | 200 | PASS | 1805 |
| 106 | Cleanup and misc | Unknown route | GET | `/no/such/route` | 404 | 404 NOT_FOUND | PASS | 241 |
| 107 | Cleanup and misc | Wrong method on a known route | DELETE | `/health` | 405 | 405 METHOD_NOT_ALLOWED | PASS | 224 |

## Request and response detail

### Health

#### 1. Service and database health — PASS

`GET /health` → **200** in 599 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": {"status": "ok", "env": "production", "authEnabled": false, "smsEnabled": false, "smsProvider": "console", "schedulerEnabled": true}, "meta": {"requestId": "api_01M2YNB1CX71ZWNQXAJ9D4TG4G", "serverTime": "2026-09-20T11:11:33", "timezone": "Asia/Kolkata"}}
```

### Catalog

#### 2. Catalog for an area — PASS

`GET /catalog?areaId=geo_podalakur` → **200** in 1022 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": {"services": [{"id": "svc_tractor", "name": "Tractor", "iconUrl": null, "category": "agriculture_heavy", "categoryLabel": "Agriculture & Heavy Machinery", "commonOnHome": true, "equipment": [{"id": "ROTAVATOR", "label": "Rotavator"}, {"id": "CULTIVATOR", "label": "Cultivator"}, {"id": "PLOUGH", "label": "Plough"}, {"id": "TRAILER", "label": "Trailer"}, {"id": "SEED_DRILL", "label": "Seed drill"}]}, {"id": "svc_jcb", "name": "JCB", "iconU …(truncated)
```

#### 3. Catalog, unknown area — PASS

`GET /catalog?areaId=geo_nowhere` → **200** in 1178 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": {"services": [], "helpMeChoose": {"templateVersion": 1, "prompt": "What do you need done?", "options": []}, "reasons": {"CUSTOMER_CANCEL_REQUEST": [{"code": "PLANS_CHANGED", "label": "Plans changed"}, {"code": "FOUND_SOMEONE_ELSE", "label": "I found somebody else"}, {"code": "PRICES_TOO_HIGH", "label": "The prices are too high"}, {"code": "NO_OFFERS", "label": "Nobody sent a price"}, {"code": "POSTED_BY_MISTAKE", "label": "I posted this  …(truncated)
```

#### 4. Request template for a service — PASS

`GET /catalog/services/svc_tractor/request-template` → **200** in 923 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": {"serviceId": "svc_tractor", "templateVersion": 3, "questions": [{"id": "q_work_type", "type": "SINGLE_CHOICE", "label": "What work do you need?", "required": true, "allowNotSure": false, "options": [{"id": "rotavator", "label": "Rotavator"}, {"id": "ploughing", "label": "Ploughing"}, {"id": "cultivation", "label": "Cultivation"}]}, {"id": "q_acres", "type": "NUMBER_WITH_UNIT", "label": "How many acres?", "required": true, "allowNotSure" …(truncated)
```

#### 5. Request template, unknown service — PASS

`GET /catalog/services/svc_nope/request-template` → **404** in 777 ms (expected 404)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"error": {"code": "SERVICE_NOT_FOUND", "message": "No such service.", "details": {}}, "meta": {"requestId": "api_01M2YNB4XBQA701AQB55TDGQYE", "serverTime": "2026-09-20T11:11:37", "timezone": "Asia/Kolkata"}}
```

### Locations

#### 6. Places in an area — PASS

`GET /locations/places?areaId=geo_podalakur` → **200** in 653 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": [{"id": "plc_althurthi", "areaId": "geo_podalakur", "name": "Althurthi", "mandal": "Podalakur", "district": "SPSR Nellore", "state": "Andhra Pradesh", "latitude": 14.467477, "longitude": 79.752438}, {"id": "plc_ankupalle", "areaId": "geo_podalakur", "name": "Ankupalle", "mandal": "Podalakur", "district": "SPSR Nellore", "state": "Andhra Pradesh", "latitude": 14.451407, "longitude": 79.776088}, {"id": "plc_ayyagaripalem", "areaId": "geo_p …(truncated)
```

#### 7. Resolve coordinates inside a served area — PASS

`GET /geographies/resolve?latitude=14.4167&longitude=79.7333` → **200** in 722 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": {"geography": {"id": "geo_podalakur", "label": "Podalakur Mandal"}, "place": {"id": "plc_podalakur", "name": "Podalakur"}}, "meta": {"requestId": "api_01M2YNB6FJ6CWCJZMHW167KKZ4", "serverTime": "2026-09-20T11:11:39", "timezone": "Asia/Kolkata"}}
```

#### 8. Resolve coordinates outside served areas — PASS

`GET /geographies/resolve?latitude=17.38&longitude=78.48` → **200** in 1057 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": {"geography": null, "place": null}, "meta": {"requestId": "api_01M2YNB74Z09JF7VJHPT8HW2CH", "serverTime": "2026-09-20T11:11:40", "timezone": "Asia/Kolkata"}}
```

#### 9. Resolve with invalid coordinates — PASS

`GET /geographies/resolve?latitude=x&longitude=1` → **400** in 683 ms (expected 400)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"error": {"code": "INVALID_COORDINATES", "message": "latitude and longitude are required numbers.", "details": {}}, "meta": {"requestId": "api_01M2YNB8DYX1W3HCPTTNDZXTD7", "serverTime": "2026-09-20T11:11:40", "timezone": "Asia/Kolkata"}}
```

#### 10. Register interest in an unserved place — PASS

`POST /locations/place-interest` → **202** in 764 ms (expected 202)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"placeName": "API-test-882894", "phoneNumber": null}
Response: null
```

#### 11. Place interest with blank name — PASS

`POST /locations/place-interest` → **400** in 715 ms (expected 400)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"placeName": " "}
Response: {"error": {"code": "INVALID_PLACE_NAME", "message": "placeName must not be blank.", "details": {}}, "meta": {"requestId": "api_01M2YNB9PN4QGP4ZZ5VNR1D6R6", "serverTime": "2026-09-20T11:11:42", "timezone": "Asia/Kolkata"}}
```

### Auth

#### 12. OTP request, invalid phone — PASS

`POST /auth/otp/request` → **400** in 483 ms (expected 400)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"phoneNumber": "123"}
Response: {"error": {"code": "INVALID_PHONE_NUMBER", "message": "phoneNumber must be E.164, e.g. +919876543210.", "details": {}}, "meta": {"requestId": "api_01M2YNBAD4VJXCQZFRK2Q3KDMP", "serverTime": "2026-09-20T11:11:42", "timezone": "Asia/Kolkata"}}
```

#### 13. OTP request, invalid purpose — PASS

`POST /auth/otp/request` → **400** in 407 ms (expected 400)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"phoneNumber": "+918088289499", "purpose": "SIGNUP"}
Response: {"error": {"code": "INVALID_PURPOSE", "message": "purpose must be LOGIN.", "details": {}}, "meta": {"requestId": "api_01M2YNBATCXNVY9SZDVWF5T8PS", "serverTime": "2026-09-20T11:11:43", "timezone": "Asia/Kolkata"}}
```

#### 14. OTP request, valid phone — PASS

`POST /auth/otp/request` → **200** in 820 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"phoneNumber": "+918088289499", "purpose": "LOGIN", "language": "te"}
Response: {"data": {"challengeId": "otp_01M2YNBBG3S77MSQKADZK7RHE6", "resendAfterSeconds": 30, "expiresInSeconds": 300}, "meta": {"requestId": "api_01M2YNBB6670GZ4VQ4CCD4H73W", "serverTime": "2026-09-20T11:11:44", "timezone": "Asia/Kolkata"}}
```

#### 15. OTP request again immediately (resend backoff) — PASS

`POST /auth/otp/request` → **429** in 876 ms (expected 429)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"phoneNumber": "+918088289499", "purpose": "LOGIN"}
Response: {"error": {"code": "OTP_RESEND_TOO_SOON", "message": "Wait before asking again.", "details": {"retryAfterSeconds": 30}}, "meta": {"requestId": "api_01M2YNBBVTR6K6FBH0BNBPSZ87", "serverTime": "2026-09-20T11:11:45", "timezone": "Asia/Kolkata"}}
```

#### 16. OTP verify, wrong code — PASS

`POST /auth/otp/verify` → **422** in 759 ms (expected 422)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"challengeId": "otp_01M2YNBBG3S77MSQKADZK7RHE6", "code": "000000"}
Response: {"error": {"code": "OTP_INVALID", "message": "That code is not right.", "details": {}}, "meta": {"requestId": "api_01M2YNBCRE2VEKX2S41AXX4ZSQ", "serverTime": "2026-09-20T11:11:45", "timezone": "Asia/Kolkata"}}
```

#### 17. OTP verify, malformed body — PASS

`POST /auth/otp/verify` → **400** in 232 ms (expected 400)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"challengeId": 1}
Response: {"error": {"code": "INVALID_REQUEST", "message": "challengeId and code are required strings.", "details": {}}, "meta": {"requestId": "api_01M2YNBDE942K0Y9Q54Y1RJHAD", "serverTime": "2026-09-20T11:11:46", "timezone": "Asia/Kolkata"}}
```

#### 18. Token refresh with an invalid token — PASS

`POST /auth/token/refresh` → **401** in 509 ms (expected 401)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"refreshToken": "bogus"}
Response: {"error": {"code": "SESSION_EXPIRED", "message": "Sign in again.", "details": {}}, "meta": {"requestId": "api_01M2YNBDPJMK4RBX4WQK9Z86XV", "serverTime": "2026-09-20T11:11:46", "timezone": "Asia/Kolkata"}}
```

#### 19. Logout with an invalid token (always 204) — PASS

`POST /auth/logout` → **204** in 743 ms (expected 204)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"refreshToken": "bogus"}
Response: null
```

### Media

#### 20. Upload a request photo — FAIL

`POST /media` → **500** in 564 ms (expected 201)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "multipart/form-data; boundary=d70793da52184ba781baebb15fe2d9eb"}
Body: multipart upload
Response: {"error": {"code": "INTERNAL_ERROR", "message": "Something went wrong on our side.", "details": {}}, "meta": {"requestId": "api_01M2YNBEX9F23KMMMS5W2K4NPA", "serverTime": "2026-09-20T11:11:47", "timezone": "Asia/Kolkata"}}
```

#### 21. Upload with a bad body — PASS

`POST /media` → **400** in 558 ms (expected 400)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "multipart/form-data; boundary=x"}
Body: multipart upload
Response: {"error": {"code": "INVALID_MEDIA_PURPOSE", "message": "purpose must be one of REQUEST_PHOTO, PARTNER_PROFILE_PHOTO, IDENTITY_DOCUMENT, IDENTITY_SELFIE, COMPLAINT_EVIDENCE.", "details": {}}, "meta": {"requestId": "api_01M2YNBFESQYR31R2BPEGTMWXQ", "serverTime": "2026-09-20T11:11:48", "timezone": "Asia/Kolkata"}}
```

#### 22. Fetch a missing media file — PASS

`GET /media/request_photo/med_missing.jpg` → **404** in 263 ms (expected 404)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNBG0AB7S6MN6BY5MJ6E2P", "serverTime": "2026-09-20T11:11:48", "timezone": "Asia/Kolkata"}}
```

### Devices

#### 23. Register push token — PASS

`PUT /devices/apitest-882894/push-token` → **204** in 1304 ms (expected 204)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"token": "tok-882894", "app": "CUSTOMER", "platform": "ANDROID", "language": "te"}
Response: null
```

#### 24. Remove push token — PASS

`DELETE /devices/apitest-882894/push-token` → **204** in 826 ms (expected 204)

```
Headers: {"X-App": "CUSTOMER"}
Response: null
```

### Customer profile & home

#### 25. Get my profile — PASS

`GET /customer/me` → **200** in 1145 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": {"id": "usr_dev_local", "displayName": null, "phoneNumber": "+919999999999", "defaultPlaceId": null, "completedJobs": 0}, "meta": {"requestId": "api_01M2YNBJBAKR2BXZ7AZZ4F63GE", "serverTime": "2026-09-20T11:11:51", "timezone": "Asia/Kolkata"}}
```

#### 26. Update my profile — PASS

`PATCH /customer/me` → **200** in 1103 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"displayName": "API Test Customer", "defaultPlaceId": "plc_podalakur"}
Response: {"data": {"id": "usr_dev_local", "displayName": "API Test Customer", "phoneNumber": "+919999999999", "defaultPlaceId": "plc_podalakur", "completedJobs": 0}, "meta": {"requestId": "api_01M2YNBKE6V923AJYBCDN7RJ2R", "serverTime": "2026-09-20T11:11:52", "timezone": "Asia/Kolkata"}}
```

#### 27. Update profile with invalid place — PASS

`PATCH /customer/me` → **422** in 1111 ms (expected 400/404/422)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"defaultPlaceId": "plc_nope"}
Response: {"error": {"code": "PLACE_NOT_FOUND", "message": "No such village.", "details": {}}, "meta": {"requestId": "api_01M2YNBMHHR8T629X1Z1FW3WDC", "serverTime": "2026-09-20T11:11:54", "timezone": "Asia/Kolkata"}}
```

#### 28. Customer home — PASS

`GET /customer/home` → **200** in 783 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": {"activeRequest": null, "savedPartners": [], "savedPartnersTotal": 0}, "meta": {"requestId": "api_01M2YNBNKCKDF7GAYEBFPDRA1E", "serverTime": "2026-09-20T11:11:54", "timezone": "Asia/Kolkata"}}
```

### Customer saved places

#### 29. Save a place — PASS

`POST /customer/places` → **201** in 1041 ms (expected 200/201)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"labelCode": "FIELD", "placeId": "plc_podalakur", "landmark": "API test landmark", "latitude": 14.4168, "longitude": 79.7334, "accuracyMeters": 15}
Response: {"data": {"id": "spl_01M2YNBPZPBCE0BQ2TTNFQQCW7", "labelCode": "FIELD", "label": "My field", "placeId": "plc_podalakur", "placeName": "Podalakur", "landmark": "API test landmark", "latitude": 14.4168, "longitude": 79.7334, "accuracyMeters": 15.0}, "meta": {"requestId": "api_01M2YNBPBXAGT6QHJ26ENGYHSD", "serverTime": "2026-09-20T11:11:55", "timezone": "Asia/Kolkata"}}
```

#### 30. List saved places — PASS

`GET /customer/places` → **200** in 1054 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": [{"id": "spl_01M2YNBPZPBCE0BQ2TTNFQQCW7", "labelCode": "FIELD", "label": "My field", "placeId": "plc_podalakur", "placeName": "Podalakur", "landmark": "API test landmark", "latitude": 14.4168, "longitude": 79.7334, "accuracyMeters": 15.0}], "meta": {"requestId": "api_01M2YNBQCYZ76TAP9VMSZ4EG56", "serverTime": "2026-09-20T11:11:57", "timezone": "Asia/Kolkata"}}
```

#### 31. Save a place with an invalid label — PASS

`POST /customer/places` → **422** in 783 ms (expected 422)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"labelCode": "NOPE", "placeId": "plc_podalakur"}
Response: {"error": {"code": "INVALID_PLACE_LABEL", "message": "That place name is not offered.", "details": {}}, "meta": {"requestId": "api_01M2YNBRDD38MGXPKVZGG4GTXQ", "serverTime": "2026-09-20T11:11:57", "timezone": "Asia/Kolkata"}}
```

### Customer requests

#### 32. Create request with stale template version — PASS

`POST /customer/requests` → **422** in 1727 ms (expected 422)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "236495f9-f3a7-4bbb-afbd-71d7c6cb5409", "Content-Type": "application/json"}
Body: {"serviceId": "svc_tractor", "workTypeId": "rotavator", "templateVersion": 1, "answers": [{"questionId": "q_work_type", "values": ["rotavator"]}, {"questionId": "q_acres", "values": ["3"], "unit": "ACRE"}], "description": "API test job (safe to delete)", "schedule": {"date": "2026-09-21", "dayPart": "MORNING"}, "location": {"placeId": "plc_podalakur", "landmark": "API test landmark", "latitude": 1 …(truncated)
Response: {"error": {"code": "TEMPLATE_VERSION_STALE", "message": "The questions have changed.", "details": {"currentTemplateVersion": 3}}, "meta": {"requestId": "api_01M2YNBS7DV3FW3AKWK1A1CTVW", "serverTime": "2026-09-20T11:11:59", "timezone": "Asia/Kolkata"}}
```

#### 33. Create request for a past date — PASS

`POST /customer/requests` → **422** in 1969 ms (expected 422)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "12940a45-d605-4674-9d9d-7e9266b4c75a", "Content-Type": "application/json"}
Body: {"serviceId": "svc_tractor", "workTypeId": "rotavator", "templateVersion": 3, "answers": [{"questionId": "q_work_type", "values": ["rotavator"]}, {"questionId": "q_acres", "values": ["3"], "unit": "ACRE"}], "description": "API test job (safe to delete)", "schedule": {"date": "2026-09-19", "dayPart": "MORNING"}, "location": {"placeId": "plc_podalakur", "landmark": "API test landmark", "latitude": 1 …(truncated)
Response: {"error": {"code": "DATE_IN_PAST", "message": "That day or time can no longer be booked.", "details": {}}, "meta": {"requestId": "api_01M2YNBTWAHYZE362RBSQBB0HD", "serverTime": "2026-09-20T11:12:01", "timezone": "Asia/Kolkata"}}
```

#### 34. Create request without Idempotency-Key — PASS

`POST /customer/requests` → **400** in 556 ms (expected 400/422)

```
Headers: {"X-App": "CUSTOMER", "Content-Type": "application/json"}
Body: {"serviceId": "svc_tractor", "workTypeId": "rotavator", "templateVersion": 3, "answers": [{"questionId": "q_work_type", "values": ["rotavator"]}, {"questionId": "q_acres", "values": ["3"], "unit": "ACRE"}], "description": "API test job (safe to delete)", "schedule": {"date": "2026-09-21", "dayPart": "MORNING"}, "location": {"placeId": "plc_podalakur", "landmark": "API test landmark", "latitude": 1 …(truncated)
Response: {"error": {"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "This endpoint needs an Idempotency-Key header.", "details": {}}, "meta": {"requestId": "api_01M2YNBWT2V58JVP68MR4FDDE6", "serverTime": "2026-09-20T11:12:02", "timezone": "Asia/Kolkata"}}
```

#### 35. Create a request — PASS

`POST /customer/requests` → **201** in 4745 ms (expected 201)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "620f9cc3-8bfd-458e-89db-cb8b4095f097", "Content-Type": "application/json"}
Body: {"serviceId": "svc_tractor", "workTypeId": "rotavator", "templateVersion": 3, "answers": [{"questionId": "q_work_type", "values": ["rotavator"]}, {"questionId": "q_acres", "values": ["3"], "unit": "ACRE"}], "description": "API test job (safe to delete)", "schedule": {"date": "2026-09-21", "dayPart": "MORNING"}, "location": {"placeId": "plc_podalakur", "landmark": "API test landmark", "latitude": 1 …(truncated)
Response: {"data": {"id": "rqt_01M2YNBYF92WPZJEMCTAEVN6HC", "state": "REQUESTED", "service": {"id": "svc_tractor", "name": "Tractor"}, "summary": {"scheduleLabel": "21 Sep • Morning", "areaLabel": "Podalakur", "description": "API test job (safe to delete)"}, "offerCount": 0, "notifiedPartnerCount": 5, "isRematching": false, "rematch": null, "bookingId": null, "endedReason": null, "endedLabel": null, "canEdit": true, "canCancel": true, "createdAt": "2026-09 …(truncated)
```

#### 36. List active requests — PASS

`GET /customer/requests?bucket=ACTIVE` → **200** in 1116 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": [{"id": "rqt_01M2YNBYF92WPZJEMCTAEVN6HC", "state": "REQUESTED", "service": {"id": "svc_tractor", "name": "Tractor"}, "summary": {"scheduleLabel": "21 Sep • Morning", "areaLabel": "Podalakur", "description": "API test job (safe to delete)"}, "offerCount": 0, "notifiedPartnerCount": 5, "isRematching": false, "rematch": null, "bookingId": null, "endedReason": null, "endedLabel": null, "canEdit": true, "canCancel": true, "createdAt": "2026-0 …(truncated)
```

#### 37. Get one request — PASS

`GET /customer/requests/rqt_01M2YNBYF92WPZJEMCTAEVN6HC` → **200** in 981 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": {"id": "rqt_01M2YNBYF92WPZJEMCTAEVN6HC", "state": "REQUESTED", "service": {"id": "svc_tractor", "name": "Tractor"}, "summary": {"scheduleLabel": "21 Sep • Morning", "areaLabel": "Podalakur", "description": "API test job (safe to delete)"}, "offerCount": 0, "notifiedPartnerCount": 5, "isRematching": false, "rematch": null, "bookingId": null, "endedReason": null, "endedLabel": null, "canEdit": true, "canCancel": true, "createdAt": "2026-09 …(truncated)
```

#### 38. Edit a request — PASS

`PATCH /customer/requests/rqt_01M2YNBYF92WPZJEMCTAEVN6HC` → **200** in 3122 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "117d0b67-e3af-48dd-b380-f897a8dc636c", "Content-Type": "application/json"}
Body: {"description": "Edited by API test"}
Response: {"data": {"id": "rqt_01M2YNBYF92WPZJEMCTAEVN6HC", "state": "REQUESTED", "service": {"id": "svc_tractor", "name": "Tractor"}, "summary": {"scheduleLabel": "21 Sep • Morning", "areaLabel": "Podalakur", "description": "Edited by API test"}, "offerCount": 0, "notifiedPartnerCount": 5, "isRematching": false, "rematch": null, "bookingId": null, "endedReason": null, "endedLabel": null, "canEdit": true, "canCancel": true, "createdAt": "2026-09-20T05:42:0 …(truncated)
```

#### 39. List offers for a request (none yet) — PASS

`GET /customer/requests/rqt_01M2YNBYF92WPZJEMCTAEVN6HC/offers` → **200** in 1002 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": {"request": {"id": "rqt_01M2YNBYF92WPZJEMCTAEVN6HC", "state": "REQUESTED", "service": {"id": "svc_tractor", "name": "Tractor"}, "summary": {"scheduleLabel": "21 Sep • Morning", "areaLabel": "Podalakur", "description": "Edited by API test"}, "offerCount": 0, "notifiedPartnerCount": 5, "isRematching": false, "rematch": null, "bookingId": null, "endedReason": null, "endedLabel": null, "canEdit": true, "canCancel": true, "createdAt": "2026-0 …(truncated)
```

#### 40. Get a missing request — PASS

`GET /customer/requests/req_missing` → **404** in 796 ms (expected 404)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNC81T9R7VH2V3MWRBSZZH", "serverTime": "2026-09-20T11:12:13", "timezone": "Asia/Kolkata"}}
```

#### 41. Create a second request (to cancel) — PASS

`POST /customer/requests` → **201** in 4687 ms (expected 201)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "bdb0abdc-e61a-4446-adcd-282f302c5e43", "Content-Type": "application/json"}
Body: {"serviceId": "svc_tractor", "workTypeId": "rotavator", "templateVersion": 3, "answers": [{"questionId": "q_work_type", "values": ["rotavator"]}, {"questionId": "q_acres", "values": ["3"], "unit": "ACRE"}], "description": "API test job (safe to delete)", "schedule": {"date": "2026-09-21", "dayPart": "MORNING"}, "location": {"placeId": "plc_podalakur", "landmark": "API test landmark", "latitude": 1 …(truncated)
Response: {"data": {"id": "rqt_01M2YNCA0WR1884AMCENWSA3MD", "state": "REQUESTED", "service": {"id": "svc_tractor", "name": "Tractor"}, "summary": {"scheduleLabel": "21 Sep • Morning", "areaLabel": "Podalakur", "description": "API test job (safe to delete)"}, "offerCount": 0, "notifiedPartnerCount": 5, "isRematching": false, "rematch": null, "bookingId": null, "endedReason": null, "endedLabel": null, "canEdit": true, "canCancel": true, "createdAt": "2026-09 …(truncated)
```

#### 42. Cancel a request — PASS

`POST /customer/requests/rqt_01M2YNCA0WR1884AMCENWSA3MD/cancel` → **200** in 1629 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "e88a54d7-286b-4149-a1dd-0c484d723b59", "Content-Type": "application/json"}
Body: {"reasonCode": "PLANS_CHANGED", "note": null}
Response: {"data": {"id": "rqt_01M2YNCA0WR1884AMCENWSA3MD", "state": "CANCELLED", "service": {"id": "svc_tractor", "name": "Tractor"}, "summary": {"scheduleLabel": "21 Sep • Morning", "areaLabel": "Podalakur", "description": "API test job (safe to delete)"}, "offerCount": 0, "notifiedPartnerCount": 5, "isRematching": false, "rematch": null, "bookingId": null, "endedReason": "CANCELLED_BY_CUSTOMER", "endedLabel": "You cancelled this request.", "canEdit": fa …(truncated)
```

#### 43. Cancel the same request again (idempotent state) — PASS

`POST /customer/requests/rqt_01M2YNCA0WR1884AMCENWSA3MD/cancel` → **200** in 1802 ms (expected 200/409)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "000f55d6-9af7-445b-a65e-94b3fc1526b0", "Content-Type": "application/json"}
Body: {"reasonCode": null}
Response: {"data": {"id": "rqt_01M2YNCA0WR1884AMCENWSA3MD", "state": "CANCELLED", "service": {"id": "svc_tractor", "name": "Tractor"}, "summary": {"scheduleLabel": "21 Sep • Morning", "areaLabel": "Podalakur", "description": "API test job (safe to delete)"}, "offerCount": 0, "notifiedPartnerCount": 5, "isRematching": false, "rematch": null, "bookingId": null, "endedReason": "CANCELLED_BY_CUSTOMER", "endedLabel": "You cancelled this request.", "canEdit": fa …(truncated)
```

#### 44. List past requests — PASS

`GET /customer/requests?bucket=PAST` → **200** in 915 ms (expected 200)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"data": [{"id": "rqt_01M2YNCA0WR1884AMCENWSA3MD", "state": "CANCELLED", "service": {"id": "svc_tractor", "name": "Tractor"}, "summary": {"scheduleLabel": "21 Sep • Morning", "areaLabel": "Podalakur", "description": "API test job (safe to delete)"}, "offerCount": 0, "notifiedPartnerCount": 5, "isRematching": false, "rematch": null, "bookingId": null, "endedReason": "CANCELLED_BY_CUSTOMER", "endedLabel": "You cancelled this request.", "canEdit": f …(truncated)
```

#### 45. Book with an unknown offer — PASS

`POST /customer/requests/rqt_01M2YNBYF92WPZJEMCTAEVN6HC/book` → **409** in 1755 ms (expected 404/409/422)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "4ad146c5-973e-4c21-9ecf-dcf8a59a3211", "Content-Type": "application/json"}
Body: {"offerId": "off_missing"}
Response: {"error": {"code": "OFFER_NOT_AVAILABLE", "message": "This offer is no longer available.", "details": {}}, "meta": {"requestId": "api_01M2YNCHQC38BSSRNPN7M8HCNM", "serverTime": "2026-09-20T11:12:24", "timezone": "Asia/Kolkata"}}
```

### Customer bookings (routing only, no booking exists)

#### 46. GET /customer/bookings/bkg_missing — PASS

`GET /customer/bookings/bkg_missing` → **404** in 797 ms (expected 404)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNCKC26NY0Z2H6QDXVFQAT", "serverTime": "2026-09-20T11:12:25", "timezone": "Asia/Kolkata"}}
```

#### 47. GET /customer/bookings/bkg_missing/completion-pin — PASS

`GET /customer/bookings/bkg_missing/completion-pin` → **404** in 1136 ms (expected 404)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNCM5Q991ZZ97G75GKRWK5", "serverTime": "2026-09-20T11:12:26", "timezone": "Asia/Kolkata"}}
```

#### 48. POST /customer/bookings/bkg_missing/agree-amount — PASS

`POST /customer/bookings/bkg_missing/agree-amount` → **404** in 1344 ms (expected 404)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "89e1329c-ed35-4301-b7c2-f1f31b71edba"}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNCN9VF66WTC1RAV9894KT", "serverTime": "2026-09-20T11:12:27", "timezone": "Asia/Kolkata"}}
```

#### 49. POST /customer/bookings/bkg_missing/confirm-arrival — PASS

`POST /customer/bookings/bkg_missing/confirm-arrival` → **404** in 1078 ms (expected 404)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "39aa89b8-6dfb-416a-afdd-7fe120d72efa"}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNCPKJVDFT4PTTWHTD60BP", "serverTime": "2026-09-20T11:12:29", "timezone": "Asia/Kolkata"}}
```

#### 50. POST /customer/bookings/bkg_missing/confirm-complete — PASS

`POST /customer/bookings/bkg_missing/confirm-complete` → **404** in 832 ms (expected 404)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "1eed09c6-6965-48ff-9705-5453c301aaaa"}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNCQMXENH0J9BZ0HT37302", "serverTime": "2026-09-20T11:12:29", "timezone": "Asia/Kolkata"}}
```

### Partner profile & onboarding

#### 51. Get partner profile — PASS

`GET /partner/me` → **200** in 910 ms (expected 200)

```
Headers: {"X-App": "PARTNER"}
Response: {"data": {"id": "usr_dev_local", "displayName": null, "profilePhotoUrl": null, "profilePhotoMediaId": null, "basePlaceId": null, "basePlaceName": null, "experienceRange": null, "status": "REGISTERED", "statusNote": null, "identityStatus": null, "acceptingNewJobs": true, "travelRadiusKm": null, "services": [], "nextStep": "BASIC_PROFILE", "workBlock": null, "acceptedTermsVersion": null, "acceptedTermsAt": null}, "meta": {"requestId": "api_01M2YNCR …(truncated)
```

#### 52. Activate before profile is complete — PASS

`POST /partner/me/activate` → **422** in 1622 ms (expected 422)

```
Headers: {"X-App": "PARTNER", "Idempotency-Key": "8245342f-f7ca-48ee-bdd2-d13b27b89e8f"}
Response: {"error": {"code": "SETUP_INCOMPLETE", "message": "A setup step is unfinished.", "details": {"nextStep": "BASIC_PROFILE"}}, "meta": {"requestId": "api_01M2YNCSCRKFT3Y3524DTCHE4T", "serverTime": "2026-09-20T11:12:32", "timezone": "Asia/Kolkata"}}
```

#### 53. Update basic profile — PASS

`PATCH /partner/me/basic` → **200** in 1170 ms (expected 200)

```
Headers: {"X-App": "PARTNER", "Content-Type": "application/json"}
Body: {"fullName": "API Test Partner", "basePlaceId": "plc_podalakur", "experienceRange": "5_TO_10_YEARS", "language": "te"}
Response: {"data": {"id": "usr_dev_local", "displayName": "API Test Partner", "profilePhotoUrl": null, "profilePhotoMediaId": null, "basePlaceId": "plc_podalakur", "basePlaceName": "Podalakur", "experienceRange": "5_TO_10_YEARS", "status": "REGISTERED", "statusNote": null, "identityStatus": null, "acceptingNewJobs": true, "travelRadiusKm": null, "services": [], "nextStep": "SERVICES", "workBlock": null, "acceptedTermsVersion": null, "acceptedTermsAt": null …(truncated)
```

#### 54. Set services and equipment — PASS

`PUT /partner/me/services` → **200** in 1755 ms (expected 200)

```
Headers: {"X-App": "PARTNER", "Content-Type": "application/json"}
Body: {"services": [{"serviceId": "svc_tractor", "equipmentIds": ["ROTAVATOR", "PLOUGH"]}]}
Response: {"data": {"id": "usr_dev_local", "displayName": "API Test Partner", "profilePhotoUrl": null, "profilePhotoMediaId": null, "basePlaceId": "plc_podalakur", "basePlaceName": "Podalakur", "experienceRange": "5_TO_10_YEARS", "status": "REGISTERED", "statusNote": null, "identityStatus": null, "acceptingNewJobs": true, "travelRadiusKm": null, "services": [{"id": "svc_tractor", "name": "Tractor"}], "nextStep": "TRAVEL_RADIUS", "workBlock": null, "accepte …(truncated)
```

#### 55. Set travel radius, invalid value — PASS

`PATCH /partner/me` → **422** in 900 ms (expected 422)

```
Headers: {"X-App": "PARTNER", "Content-Type": "application/json"}
Body: {"travelRadiusKm": 7}
Response: {"error": {"code": "INVALID_TRAVEL_RADIUS", "message": "Pick one of the offered distances.", "details": {}}, "meta": {"requestId": "api_01M2YNCXRJ7VGBYKER9ZZ3QEDH", "serverTime": "2026-09-20T11:12:36", "timezone": "Asia/Kolkata"}}
```

#### 56. Set travel radius, valid value — PASS

`PATCH /partner/me` → **200** in 1416 ms (expected 200)

```
Headers: {"X-App": "PARTNER", "Content-Type": "application/json"}
Body: {"travelRadiusKm": 15}
Response: {"data": {"id": "usr_dev_local", "displayName": "API Test Partner", "profilePhotoUrl": null, "profilePhotoMediaId": null, "basePlaceId": "plc_podalakur", "basePlaceName": "Podalakur", "experienceRange": "5_TO_10_YEARS", "status": "REGISTERED", "statusNote": null, "identityStatus": null, "acceptingNewJobs": true, "travelRadiusKm": 15, "services": [{"id": "svc_tractor", "name": "Tractor"}], "nextStep": "NOTIFICATIONS", "workBlock": null, "acceptedT …(truncated)
```

#### 57. Register partner push token — PASS

`PUT /devices/apitest-882894-p/push-token` → **204** in 881 ms (expected 204)

```
Headers: {"X-App": "PARTNER", "Content-Type": "application/json"}
Body: {"token": "ptok-882894", "app": "PARTNER"}
Response: null
```

#### 58. Remove partner push token — PASS

`DELETE /devices/apitest-882894-p/push-token` → **204** in 1051 ms (expected 204)

```
Headers: {"X-App": "PARTNER"}
Response: null
```

#### 59. Activate profile — PASS

`POST /partner/me/activate` → **200** in 1526 ms (expected 200/409/422)

```
Headers: {"X-App": "PARTNER", "Idempotency-Key": "9a5c5056-5291-4901-87bb-b6e4f5979cc2"}
Response: {"data": {"id": "usr_dev_local", "displayName": "API Test Partner", "profilePhotoUrl": null, "profilePhotoMediaId": null, "basePlaceId": "plc_podalakur", "basePlaceName": "Podalakur", "experienceRange": "5_TO_10_YEARS", "status": "ACTIVATED", "statusNote": null, "identityStatus": null, "acceptingNewJobs": true, "travelRadiusKm": 15, "services": [{"id": "svc_tractor", "name": "Tractor"}], "nextStep": "COMPLETE", "workBlock": null, "acceptedTermsVe …(truncated)
```

#### 60. Upload identity document — FAIL

`POST /media` → **500** in 809 ms (expected 201)

```
Headers: {"X-App": "PARTNER", "Content-Type": "multipart/form-data; boundary=75daf8579a104b659b5a4081e2d1b8ab"}
Body: multipart upload
Response: {"error": {"code": "INTERNAL_ERROR", "message": "Something went wrong on our side.", "details": {}}, "meta": {"requestId": "api_01M2YND3DH13MJ44KZTY90DHV2", "serverTime": "2026-09-20T11:12:41", "timezone": "Asia/Kolkata"}}
```

#### 61. Upload identity selfie — FAIL

`POST /media` → **500** in 609 ms (expected 201)

```
Headers: {"X-App": "PARTNER", "Content-Type": "multipart/form-data; boundary=36e0aec300034ce99c4cd9ff74c26ebd"}
Body: multipart upload
Response: {"error": {"code": "INTERNAL_ERROR", "message": "Something went wrong on our side.", "details": {}}, "meta": {"requestId": "api_01M2YND47BSTKJ26P4B0C1FRAC", "serverTime": "2026-09-20T11:12:42", "timezone": "Asia/Kolkata"}}
```

#### 62. Get identity verification (before) — PASS

`GET /partner/me/identity-verification` → **200** in 830 ms (expected 200)

```
Headers: {"X-App": "PARTNER"}
Response: {"data": null, "meta": {"requestId": "api_01M2YND4SRPPQB2H83RDJ7BFG4", "serverTime": "2026-09-20T11:12:43", "timezone": "Asia/Kolkata"}}
```

### Partner opportunities, offers, jobs

#### 63. Partner home — PASS

`GET /partner/home` → **200** in 1648 ms (expected 200)

```
Headers: {"X-App": "PARTNER"}
Response: {"data": {"profile": {"id": "usr_dev_local", "displayName": "API Test Partner", "profilePhotoUrl": null, "profilePhotoMediaId": null, "basePlaceId": "plc_podalakur", "basePlaceName": "Podalakur", "experienceRange": "5_TO_10_YEARS", "status": "ACTIVATED", "statusNote": null, "identityStatus": null, "acceptingNewJobs": true, "travelRadiusKm": 15, "services": [{"id": "svc_tractor", "name": "Tractor"}], "nextStep": "COMPLETE", "workBlock": null, "acc …(truncated)
```

#### 64. List opportunities — PASS

`GET /partner/opportunities` → **200** in 980 ms (expected 200)

```
Headers: {"X-App": "PARTNER"}
Response: {"data": [], "meta": {"requestId": "api_01M2YND78XZPMJ2VR866BPTRJ8", "serverTime": "2026-09-20T11:12:45", "timezone": "Asia/Kolkata", "nextCursor": null}}
```

#### 65. View one opportunity — PASS

`GET /partner/opportunities/rqt_01M2YNBYF92WPZJEMCTAEVN6HC` → **404** in 1313 ms (expected 200/403/404)

```
Headers: {"X-App": "PARTNER"}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YND86TB0MZX9P0NY2FGQCN", "serverTime": "2026-09-20T11:12:47", "timezone": "Asia/Kolkata"}}
```

#### 66. Send an offer (partner not yet admin-approved) — PASS

`POST /partner/opportunities/rqt_01M2YNBYF92WPZJEMCTAEVN6HC/offers` → **403** in 1450 ms (expected 201/403/404/409/422)

_Full happy path needs the partner to be VERIFIED/ACTIVE by an admin._

```
Headers: {"X-App": "PARTNER", "Idempotency-Key": "63546318-200a-4ab7-b5c4-daa28bf5b6de", "Content-Type": "application/json"}
Body: {"pricing": {"type": "FIXED", "exactAmount": {"amountMinor": 200000, "currency": "INR"}}}
Response: {"error": {"code": "NOT_ELIGIBLE", "message": "You cannot send prices right now.", "details": {}}, "meta": {"requestId": "api_01M2YND9F9Z6C2DE83RDDY78GQ", "serverTime": "2026-09-20T11:12:48", "timezone": "Asia/Kolkata"}}
```

#### 67. Decline an opportunity — PASS

`POST /partner/opportunities/rqt_01M2YNBYF92WPZJEMCTAEVN6HC/decline` → **404** in 1073 ms (expected 204/403/404/409)

```
Headers: {"X-App": "PARTNER"}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNDAX08B9WZ5TGGD00ZP0A", "serverTime": "2026-09-20T11:12:49", "timezone": "Asia/Kolkata"}}
```

#### 68. List my offers — PASS

`GET /partner/offers` → **200** in 894 ms (expected 200)

```
Headers: {"X-App": "PARTNER"}
Response: {"data": [], "meta": {"requestId": "api_01M2YNDBY919J4H3RS9R3DC9Z4", "serverTime": "2026-09-20T11:12:50", "timezone": "Asia/Kolkata"}}
```

#### 69. Withdraw an unknown offer — PASS

`DELETE /partner/offers/off_missing` → **404** in 1415 ms (expected 404)

```
Headers: {"X-App": "PARTNER", "Idempotency-Key": "e537d21f-c5f5-45c6-a54d-d182d6482f38"}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNDCTQTQNJ0J6KFZDV71P7", "serverTime": "2026-09-20T11:12:52", "timezone": "Asia/Kolkata"}}
```

#### 70. List active jobs — PASS

`GET /partner/jobs?bucket=ACTIVE` → **200** in 899 ms (expected 200)

```
Headers: {"X-App": "PARTNER"}
Response: {"data": [], "meta": {"requestId": "api_01M2YNDE6AWKFCE376Y6885B5D", "serverTime": "2026-09-20T11:12:52", "timezone": "Asia/Kolkata", "nextCursor": null}}
```

#### 71. List job history — PASS

`GET /partner/jobs?bucket=HISTORY` → **200** in 864 ms (expected 200)

```
Headers: {"X-App": "PARTNER"}
Response: {"data": [], "meta": {"requestId": "api_01M2YNDF1Z66T6SNRWXSZCYNZF", "serverTime": "2026-09-20T11:12:53", "timezone": "Asia/Kolkata", "nextCursor": null}}
```

#### 72. GET /partner/bookings/bkg_missing (routing only) — PASS

`GET /partner/bookings/bkg_missing` → **404** in 803 ms (expected 404/422)

```
Headers: {"X-App": "PARTNER"}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNDFX4MQZGBVN3C3QANQT8", "serverTime": "2026-09-20T11:12:54", "timezone": "Asia/Kolkata"}}
```

#### 73. POST /partner/bookings/bkg_missing/on-my-way (routing only) — PASS

`POST /partner/bookings/bkg_missing/on-my-way` → **404** in 1072 ms (expected 404/422)

```
Headers: {"X-App": "PARTNER", "Idempotency-Key": "9a11bc93-4f97-474f-86f7-8b32ad157ecd", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNDGPR6577P3QCG0GPRSG3", "serverTime": "2026-09-20T11:12:55", "timezone": "Asia/Kolkata"}}
```

#### 74. POST /partner/bookings/bkg_missing/arrive (routing only) — PASS

`POST /partner/bookings/bkg_missing/arrive` → **404** in 1329 ms (expected 404/422)

```
Headers: {"X-App": "PARTNER", "Idempotency-Key": "001a0866-7011-4e4b-b71d-66ec4de1ba26", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNDHR6FYA8RWXBCRTZP5ZE", "serverTime": "2026-09-20T11:12:57", "timezone": "Asia/Kolkata"}}
```

#### 75. POST /partner/bookings/bkg_missing/final-amount (routing only) — PASS

`POST /partner/bookings/bkg_missing/final-amount` → **404** in 1835 ms (expected 404/422)

```
Headers: {"X-App": "PARTNER", "Idempotency-Key": "9d62fa8f-fa5a-4227-877c-8d0c5d2e3763", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNDK1C9K13RA5WZK5DTJZA", "serverTime": "2026-09-20T11:12:58", "timezone": "Asia/Kolkata"}}
```

#### 76. POST /partner/bookings/bkg_missing/complete (routing only) — PASS

`POST /partner/bookings/bkg_missing/complete` → **404** in 1723 ms (expected 404/422)

```
Headers: {"X-App": "PARTNER", "Idempotency-Key": "eaa6da5f-ad30-48a1-9b81-96bafa623bc6", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "NOT_FOUND", "message": "Not found.", "details": {}}, "meta": {"requestId": "api_01M2YNDMTGRB87HRJAVTNDTZG9", "serverTime": "2026-09-20T11:13:00", "timezone": "Asia/Kolkata"}}
```

### Admin (unauthenticated: every endpoint must refuse)

#### 77. Admin console page — PASS

`GET /admin` → **200** in 245 ms (expected 200)

```
Headers: {"X-App": "ADMIN"}
Response: <1767 bytes non-JSON>
```

#### 78. Admin console page (trailing slash) — PASS

`GET /admin/` → **200** in 573 ms (expected 200)

```
Headers: {"X-App": "ADMIN"}
Response: <1767 bytes non-JSON>
```

#### 79. Admin static asset — PASS

`GET /admin/static/admin.css` → **200** in 250 ms (expected 200)

```
Headers: {"X-App": "ADMIN"}
Response: <7606 bytes non-JSON>
```

#### 80. Admin login, wrong password — PASS

`POST /admin/api/login` → **401** in 1288 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {"username": "admin", "password": "definitely-wrong"}
Response: {"error": {"code": "INVALID_CREDENTIALS", "message": "Wrong username or password.", "details": {}}, "meta": {"requestId": "api_01M2YNDQK4AJ4ZP1D7NJWHVQD5", "serverTime": "2026-09-20T11:13:03", "timezone": "Asia/Kolkata"}}
```

#### 81. POST /admin/api/logout — PASS

`POST /admin/api/logout` → **200** in 541 ms (expected 200)

```
Headers: {"X-App": "ADMIN"}
Response: {"data": {"signedOut": true}, "meta": {"requestId": "api_01M2YNDRW4XYCWZAEMV2EGDPNK", "serverTime": "2026-09-20T11:13:03", "timezone": "Asia/Kolkata"}}
```

#### 82. GET /admin/api/me without credentials — PASS

`GET /admin/api/me` → **401** in 271 ms (expected 401)

```
Headers: {"X-App": "ADMIN"}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDSCJJ1MNB955Z5H975WN", "serverTime": "2026-09-20T11:13:03", "timezone": "Asia/Kolkata"}}
```

#### 83. GET /admin/api/meta without credentials — PASS

`GET /admin/api/meta` → **401** in 274 ms (expected 401)

```
Headers: {"X-App": "ADMIN"}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDSN667R6Y24V6GCCZBNQ", "serverTime": "2026-09-20T11:13:04", "timezone": "Asia/Kolkata"}}
```

#### 84. GET /admin/api/r/service without credentials — PASS

`GET /admin/api/r/service` → **401** in 256 ms (expected 401)

```
Headers: {"X-App": "ADMIN"}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDSWSPQ7PFAQTR5WFDAVQ", "serverTime": "2026-09-20T11:13:04", "timezone": "Asia/Kolkata"}}
```

#### 85. POST /admin/api/r/service/get without credentials — PASS

`POST /admin/api/r/service/get` → **401** in 272 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDT5RB9Q0K3Z6HS95ACB2", "serverTime": "2026-09-20T11:13:04", "timezone": "Asia/Kolkata"}}
```

#### 86. POST /admin/api/r/service without credentials — PASS

`POST /admin/api/r/service` → **401** in 268 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDTE0PQ0XMG76P62MDBFW", "serverTime": "2026-09-20T11:13:04", "timezone": "Asia/Kolkata"}}
```

#### 87. PUT /admin/api/r/service without credentials — PASS

`PUT /admin/api/r/service` → **401** in 285 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDTPVZNZAW002SNGFRFKE", "serverTime": "2026-09-20T11:13:05", "timezone": "Asia/Kolkata"}}
```

#### 88. DELETE /admin/api/r/service without credentials — PASS

`DELETE /admin/api/r/service` → **401** in 520 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDTXQ5D2WZ7J7H38HWDP4", "serverTime": "2026-09-20T11:13:05", "timezone": "Asia/Kolkata"}}
```

#### 89. GET /admin/api/stats without credentials — PASS

`GET /admin/api/stats` → **401** in 275 ms (expected 401)

```
Headers: {"X-App": "ADMIN"}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDVER5S7H3T821BTZVWN6", "serverTime": "2026-09-20T11:13:05", "timezone": "Asia/Kolkata"}}
```

#### 90. GET /admin/api/partners without credentials — PASS

`GET /admin/api/partners` → **401** in 317 ms (expected 401)

```
Headers: {"X-App": "ADMIN"}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDVRQ405KCBKFHFCV5607", "serverTime": "2026-09-20T11:13:06", "timezone": "Asia/Kolkata"}}
```

#### 91. POST /admin/api/partners/x/status without credentials — PASS

`POST /admin/api/partners/x/status` → **401** in 264 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDW20EVJNPMM10B4BEZ6D", "serverTime": "2026-09-20T11:13:06", "timezone": "Asia/Kolkata"}}
```

#### 92. POST /admin/api/identity/x/decision without credentials — PASS

`POST /admin/api/identity/x/decision` → **401** in 303 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDWB99QSD5R828THTNKAW", "serverTime": "2026-09-20T11:13:06", "timezone": "Asia/Kolkata"}}
```

#### 93. POST /admin/api/media without credentials — PASS

`POST /admin/api/media` → **401** in 312 ms (expected 401)

```
Headers: {"X-App": "ADMIN"}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDWN9XCBP3H5627XC005E", "serverTime": "2026-09-20T11:13:07", "timezone": "Asia/Kolkata"}}
```

#### 94. DELETE /admin/api/media/x without credentials — PASS

`DELETE /admin/api/media/x` → **401** in 616 ms (expected 401)

```
Headers: {"X-App": "ADMIN"}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDWYYRS0J433TC83E5FXT", "serverTime": "2026-09-20T11:13:07", "timezone": "Asia/Kolkata"}}
```

#### 95. GET /admin/api/media/x/file without credentials — PASS

`GET /admin/api/media/x/file` → **401** in 203 ms (expected 401)

```
Headers: {"X-App": "ADMIN"}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDXF3HHY048NCYCW6CQC7", "serverTime": "2026-09-20T11:13:08", "timezone": "Asia/Kolkata"}}
```

#### 96. POST /admin/api/ledger/adjust without credentials — PASS

`POST /admin/api/ledger/adjust` → **401** in 244 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDXPNCKGQWD0GTYXK6RQP", "serverTime": "2026-09-20T11:13:08", "timezone": "Asia/Kolkata"}}
```

#### 97. GET /admin/api/requests/x/detail without credentials — PASS

`GET /admin/api/requests/x/detail` → **401** in 240 ms (expected 401)

```
Headers: {"X-App": "ADMIN"}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDXY6XKEBM6SJ03PWDN0B", "serverTime": "2026-09-20T11:13:08", "timezone": "Asia/Kolkata"}}
```

#### 98. POST /admin/api/requests/x/run-matching without credentials — PASS

`POST /admin/api/requests/x/run-matching` → **401** in 248 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDY5S72VKWQQZTWDKAQ28", "serverTime": "2026-09-20T11:13:08", "timezone": "Asia/Kolkata"}}
```

#### 99. POST /admin/api/jobs/tick without credentials — PASS

`POST /admin/api/jobs/tick` → **401** in 229 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDYD2KTKFWGZ523G733MT", "serverTime": "2026-09-20T11:13:08", "timezone": "Asia/Kolkata"}}
```

#### 100. GET /admin/api/demand without credentials — PASS

`GET /admin/api/demand` → **401** in 265 ms (expected 401)

```
Headers: {"X-App": "ADMIN"}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDYNG5QTEF84D5CHKKDBQ", "serverTime": "2026-09-20T11:13:09", "timezone": "Asia/Kolkata"}}
```

#### 101. GET /admin/api/config without credentials — PASS

`GET /admin/api/config` → **401** in 236 ms (expected 401)

```
Headers: {"X-App": "ADMIN"}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDYWKF6FP8Q8TDHW6DVNS", "serverTime": "2026-09-20T11:13:09", "timezone": "Asia/Kolkata"}}
```

#### 102. PUT /admin/api/config/AUTH_ENABLED without credentials — PASS

`PUT /admin/api/config/AUTH_ENABLED` → **401** in 572 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDZ59QXBXGB1S9WXFK660", "serverTime": "2026-09-20T11:13:09", "timezone": "Asia/Kolkata"}}
```

#### 103. DELETE /admin/api/config/AUTH_ENABLED without credentials — PASS

`DELETE /admin/api/config/AUTH_ENABLED` → **401** in 214 ms (expected 401)

```
Headers: {"X-App": "ADMIN", "Content-Type": "application/json"}
Body: {}
Response: {"error": {"code": "ADMIN_UNAUTHENTICATED", "message": "Sign in to the admin console.", "details": {}}, "meta": {"requestId": "api_01M2YNDZN7N7R4KF9Y1S48XC4W", "serverTime": "2026-09-20T11:13:10", "timezone": "Asia/Kolkata"}}
```

### Cleanup and misc

#### 104. Delete the saved place created above — PASS

`DELETE /customer/places/spl_01M2YNBPZPBCE0BQ2TTNFQQCW7` → **204** in 1016 ms (expected 204)

```
Headers: {"X-App": "CUSTOMER"}
Response: null
```

#### 105. Cancel the test request created above — PASS

`POST /customer/requests/rqt_01M2YNBYF92WPZJEMCTAEVN6HC/cancel` → **200** in 1805 ms (expected 200/409)

```
Headers: {"X-App": "CUSTOMER", "Idempotency-Key": "ce33ca98-92a5-4916-a800-85eedc723ce0", "Content-Type": "application/json"}
Body: {"reasonCode": null}
Response: {"data": {"id": "rqt_01M2YNBYF92WPZJEMCTAEVN6HC", "state": "CANCELLED", "service": {"id": "svc_tractor", "name": "Tractor"}, "summary": {"scheduleLabel": "21 Sep • Morning", "areaLabel": "Podalakur", "description": "Edited by API test"}, "offerCount": 0, "notifiedPartnerCount": 5, "isRematching": false, "rematch": null, "bookingId": null, "endedReason": "CANCELLED_BY_CUSTOMER", "endedLabel": "You cancelled this request.", "canEdit": false, "canCa …(truncated)
```

#### 106. Unknown route — PASS

`GET /no/such/route` → **404** in 241 ms (expected 404)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"error": {"code": "NOT_FOUND", "message": "The requested URL was not found on the server. If you entered the URL manually please check your spelling and try again.", "details": {}}, "meta": {"requestId": "api_01M2YNE2MT71NHWQHFJQ6SWM3B", "serverTime": "2026-09-20T11:13:13", "timezone": "Asia/Kolkata"}}
```

#### 107. Wrong method on a known route — PASS

`DELETE /health` → **405** in 224 ms (expected 405)

```
Headers: {"X-App": "CUSTOMER"}
Response: {"error": {"code": "METHOD_NOT_ALLOWED", "message": "The method is not allowed for the requested URL.", "details": {}}, "meta": {"requestId": "api_01M2YNE2VZFBQR5NJTFA30KMQ7", "serverTime": "2026-09-20T11:13:13", "timezone": "Asia/Kolkata"}}
```

