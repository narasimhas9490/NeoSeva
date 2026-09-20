# NeoSeva backend v1

NeoSeva is a two-sided marketplace for rural services, starting with Podalakur Mandal, Andhra Pradesh. A **customer** asks for work (a tractor, a plumber) by tapping through a few questions. **Partners** near her send prices. She books one. The job counts as done only when the partner types a 4-digit code that only she has.

This repository is the backend for both Android apps, plus an HTML **admin console** where the business is configured. Wording, numbers, lists and switches are all edited there, so changing them never needs an app release.

- **Stack:** Python 3.14, Flask 3, SQLAlchemy Core (plain SQL), PostgreSQL 17 + PostGIS 3.6, APScheduler, PyJWT.
- **Source of truth:** handover docs 0–4 (database, getting in, she asks, he prices, the job).
- **Full request and response examples:** `docs/api-spec.md`, generated from real calls. See [section 5](#5-api-flow-requests-and-responses).

---

## Contents

1. [Run it](#1-run-it)
2. [Code layout](#2-code-layout)
3. [Conventions every endpoint follows](#3-conventions-every-endpoint-follows)
4. [Database schema](#4-database-schema)
5. [API flow, requests and responses](#5-api-flow-requests-and-responses)
6. [Business decisions](#6-business-decisions)
7. [What the admin can configure (and which fields are required)](#7-what-the-admin-can-configure)
8. [Runtime configuration (config.py)](#8-runtime-configuration-configpy)
9. [Testing](#9-testing)
10. [Not built yet](#10-not-built-yet)

---

## 1. Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt       # Windows; use .venv/bin on Linux/macOS
cp .env.example .env                                 # then edit
python scripts/migrate.py --reset                    # rebuild the schema (drops everything)
python seed/seed.py                                  # catalog, settings, demo people and jobs (--no-demo: catalog only)
python run.py                                        # http://127.0.0.1:5000
```

| What | Where |
|---|---|
| Admin console | <http://127.0.0.1:5000/admin>. Log in with `ADMIN_BOOTSTRAP_USERNAME` / `ADMIN_BOOTSTRAP_PASSWORD` (default `admin` / `admin12345`) |
| Health | `GET /health`. Reports whether the DB is up and whether auth, SMS and the scheduler are on |
| Dev sign-in | While `SMS_ENABLED=false`, every OTP is `SMS_FIXED_OTP` (default `123456`) |
| Tests | `python -m pytest`: 47 tests against `TEST_DATABASE_URL` |
| Call every endpoint | `python scripts/smoke.py --spec docs/api-spec.md`: 135 live calls, and the spec is rewritten |

**Requirements.** The database role that runs migrations must be a superuser, because it enables the PostGIS extension. PostGIS is installed into its own schema, `postgis` (see [6.1](#61-schema-and-data)).

---

## 2. Code layout

Customer logic and partner logic live in separate packages. Rules both sides share live in `shared/`.

```
app/
  config.py        every setting, with metadata (group, type, secret, restart-only)
  core/            infrastructure, no business rules
    db.py            engine, tx(), one()/many()/scalar()/run()
    envelope.py      {data, meta} and {error, meta}, local serverTime
    errors.py        ApiError(status, code, message, details)
    auth.py          JWT access tokens, hashed refresh tokens, @require_user(app)
    idempotency.py   @idempotent: the Idempotency-Key protocol
    runtime.py       admin overrides of config.py, applied live
    clock.py         area timezone, dayparts, cutoff/notice/horizon rules
    money.py         paise, fee rounding
    adapters/        sms.py (console | 2Factor.in), push.py (log), storage.py (public | private roots)
  shared/          domain rules used by both apps
    templates.py     request questions, showIf visibility, answer validation, formatting
    pricing.py       offer validation, comparableCost, lowest-price and most-jobs claims
    matching.py      who sees a request, batches, preferred-partner head start
    bookings.py      book transaction, arrive, final amount, PIN check, completion
    presenters.py    ApproximateArea / ExactLocation, request and trust shapes
    settings.py      catalog_setting, platform_setting, display_text, places
  common/          endpoints both apps call: auth, catalog, locations, media, devices
  customer/        customer ("user") endpoints: profile, home, places, requests, offers+book, bookings
  partner/         partner ("provider") endpoints: profile/onboarding, identity, home, opportunities, offers, jobs
  admin/           console: api/registry.py (tables), api/crud.py, api/hooks.py, api/routes.py, static/ (HTML/JS/CSS)
  jobs/scheduler.py  matching batches, request expiry, idempotency cleanup (every SCHEDULER_TICK_SECONDS)
migrations/        0001 PostGIS · 0002–0006 doc 0 DDL · 0007 additions · 0008 admin · 0009 runtime settings
seed/              seed.py + catalog_data.py (36 real Podalakur villages)
scripts/           migrate.py, smoke.py (calls every endpoint, writes docs/api-spec.md)
tests/             one file per chunk + admin + runtime config
docs/api-spec.md   every endpoint with a real request and response
```

---

## 3. Conventions every endpoint follows

### 3.1 Response envelopes

```json
{ "data": { }, "meta": { "requestId": "api_01J…", "serverTime": "2026-09-19T14:32:10", "timezone": "Asia/Kolkata" } }
```

```json
{ "error": { "code": "OFFER_NOT_AVAILABLE", "message": "This offer is no longer available.", "details": { } },
  "meta": { "requestId": "api_01J…", "serverTime": "…", "timezone": "Asia/Kolkata" } }
```

- **`serverTime` is the area's local wall clock with no zone suffix.** The apps set their clock from it and never trust the phone's.
- **Apps branch on `error.code`.** `message` is for developers and is never shown to a user.
- **Lists put paging in `meta`:** `"nextCursor": "eyJ…"`, where `null` means the end. Cursors are opaque.
- **`details`** carries structured extras. For example, `REQUEST_ALREADY_BOOKED` includes `bookingId`, and `SETUP_INCOMPLETE` includes `nextStep`.

### 3.2 HTTP status codes

| Status | Meaning |
|---|---|
| 200 | Read, or a change that returns the new state |
| 201 | Created (request, offer, booking, media, saved place) |
| 202 | Accepted with empty body (place interest) |
| 204 | Done, no body (logout, push token, on-my-way, decline) |
| 304 | Catalog unchanged (`If-None-Match`) |
| 400 | Malformed input |
| 401 | No token or expired token (`UNAUTHENTICATED`, `TOKEN_EXPIRED`, `SESSION_EXPIRED`) |
| 403 | Real user, not allowed (`WRONG_APP`, `NOT_ELIGIBLE`, `CUSTOMER_RESTRICTED`) |
| 404 | Not found, **or somebody else's**. 403 would confirm the thing exists |
| 409 | Conflicts with the current state |
| 413 / 415 | Upload too large / wrong type |
| 422 | Well formed but refused by a business rule |
| 429 | Rate limited (OTP, PIN) |
| 502 | A vendor failed (SMS, storage) |

### 3.3 Headers

| Header | When |
|---|---|
| `Authorization: Bearer <accessToken>` | Every `/customer/*`, `/partner/*`, `/media` and `/devices/*` call, unless `AUTH_ENABLED=false` |
| `X-App: CUSTOMER` or `X-App: PARTNER` | Every app call. On `/auth/otp/verify` it decides which profile is created. Customer endpoints refuse a partner token with `403 WRONG_APP`, and the reverse |
| `Idempotency-Key: <uuid>` | Required on every write marked **Idem** in section 5. One key per attempt, reused on retry |
| `If-None-Match: "<etag>"` | `GET /catalog` returns `304` when nothing changed |
| `X-Admin-Key: <ADMIN_API_KEY>` or the `nsv_admin` cookie | `/admin/api/*` |

### 3.4 Idempotency

- An `IN_FLIGHT` row is written **before** the work starts, unique on `(user, endpoint pattern, key)`.
- A retry of a finished call gets **the first response again**. The data is the same; `meta` is fresh.
- The same key arriving while the first call is still running gets `409 IDEMPOTENCY_IN_PROGRESS`.
- The same key with a **different body** gets `409 IDEMPOTENCY_CONFLICT`.
- A failed attempt, meaning any 4xx or 5xx, is forgotten so it can be retried with the same key.
- Rows older than 48 hours are purged by the scheduler.

### 3.5 Money, time and ids

| Kind | Rule |
|---|---|
| Money | Always `{"amountMinor": 135000, "currency": "INR"}`. Integers in **paise**, never floats. Apps do the formatting |
| Moments | `TIMESTAMPTZ` in UTC, sent as `2026-09-18T14:32:10Z` |
| Local times | `serverTime` and `arrivalExpectedBy` are sent **zoneless**, in the area's timezone |
| Schedules | A `DATE` plus a daypart (`MORNING`, `AFTERNOON`, `EVENING`, `ANY_TIME`), never an instant |
| Ids | Type prefix + ULID: `usr_`, `rqt_`, `ofr_`, `bkg_`, `med_`, `ctx_`, `spl_` (saved place), `otp_`, `rft_`, `idm_`, `api_` |
| Catalog keys | Readable: `svc_tractor`, `plc_podalakur`, `geo_podalakur`, `ROTAVATOR` |
| Enums | Exact, case-sensitive strings |

---

## 4. Database schema

There are **44 tables** in `public`: the 36 from doc 0, 7 additions (migration 0007) and the admin and runtime tables. There is also 1 view, `partner_balance`. PostGIS types live in the `postgis` schema.

### 4.1 Rules that apply everywhere

- **Primary keys** are `TEXT` (prefix + ULID) or readable catalog keys. There are no serial integers.
- **Enumerations** are `TEXT` with a `CHECK` list, not Postgres `ENUM`. Adding a value is one `ALTER`.
- **Money** is `BIGINT` in paise, and every money column ends in `_minor`.
- **Geography:** points and areas are `postgis.geography(POINT|MULTIPOLYGON, 4326)`.
- **Nothing derived is stored.** Completed-job counts, balances, offer counts, fees and events are all computed from the rows that record them.
- **Almost nothing is deleted.** Rows gain `cancelled_at`, `withdrawn_at` or `deleted_at` instead. Only saved places are hard-deleted, and identity images are deleted from storage once decided.

### 4.2 Constraints that carry business rules

| Constraint | Rule it enforces |
|---|---|
| `booking_one_live_per_request` (partial unique on `booking(request_id) WHERE state IN ('BOOKED','ARRIVED')`) | At most one running booking per request. A rematch can still book again later |
| `offer_pricing_shape` (CHECK) | FIXED carries only `exact_amount_minor`; UNIT_RATE only `rate_minor` + `unit_code`; INSPECTION only `inspection_charge_minor` |
| `UNIQUE (request_id, partner_id)` on `offer` | One offer per partner per request. No bidding rounds |
| `credit_one_job_fee_per_booking` (partial unique) | A partner can never be charged twice for one job |
| `device_push_token_uidx` (partial unique) | One device, one push token |
| `saved_place UNIQUE (user, label, place, landmark)` | The same place is never saved twice |
| `catalog_setting CHECK (id = 1)` and `platform_setting CHECK (id = 1)` | Exactly one settings row each |
| `booking.state` CHECK omits `REQUESTED`; `offer.offered_day_part` omits `ANY_TIME` | Deliberately narrower than the app enums: a booking is never "requested", and a partner can't promise "any time" |

### 4.3 Tables by chunk

**Chunk 1: getting in**

| Table | Holds | Key columns |
|---|---|---|
| `geography` | One service area and how its day works | `id`, `timezone`, `same_day_cutoff_hour`, `book_ahead_days`, `minimum_notice_minutes`, `no_show_prompt_delay_minutes`, `preferred_partner_head_start_minutes`, `morning/afternoon/evening_ends_hour`, `boundary`* |
| `place` | Villages | `geography_id`, `name`, `mandal`, `district`, `state`, `centre` (point), `boundary` (multipolygon, decides which village a pin is in) |
| `place_interest` | Villages we don't serve yet, as typed | `name_typed`, `phone_e164` (optional), no coordinates |
| `app_user` | One phone = one person, whichever app | `phone_e164` (unique), `phone_verified_at`, `language` (`te`/`en`) |
| `otp_request` | Login challenges | `code_hash` (never the code), `expires_at`, `attempts`, `consumed_at` |
| `auth_session` | One per signed-in app | `refresh_token_hash`, `app` (`CUSTOMER`/`PARTNER`), `expires_at`, `revoked_at` |
| `device` | Push tokens | `device_id`, `user_id`, `app`, `push_token` |
| `service`, `service_geography` | Trades, and where each runs | `name`, `category`, `category_label`, `common_on_home`, `sort_order`, `is_active` |
| `service_equipment` | What a partner may own for a service (usually none) | `(service_id, id)`, `label` |
| `reason` | Cancel and report reasons, keyed by `(context, code)` | 6 contexts |
| `place_label`, `review_tag`, `experience_range` | Small served lists | `code`, `label` (`review_tag.positive`) |
| `help_me_choose_option` | "I know what's wrong, not the trade" picker | `label`, `service_id` (null = something else) |
| `catalog_setting` | One row: business numbers and every sentence the apps show | see [7.4](#74-settings-single-row-forms) |
| `media` | Uploaded images | `purpose`, `content_type`, `storage_key`, `url`, `deleted_at` |
| `idempotency_key` | Retry protection | see 3.4 |

**Chunk 2: she asks for work**

| Table | Holds |
|---|---|
| `customer_profile` | `display_name` (nullable), `default_place_id`, `is_restricted` |
| `saved_place` | `label_code`, `place_id`, `landmark`, `pin`, `accuracy_meters` |
| `request_question` | `service_id`, `template_version`, `type` (`SINGLE_CHOICE`/`MULTI_CHOICE`/`NUMBER_WITH_UNIT`), `label`, `short_label`*, `required`, `allow_not_sure`, unit words, `min/max/step/default_value`, `depends_on_question_id` + `depends_on_values` (showIf), `sort_order` |
| `request_question_option` | `question_id`, `label`, `equipment_id`* (the equipment this work type needs) |
| `request` | `customer_id`, `geography_id`, `service_id`, `work_type_id`, `template_version`, `state`, `schedule_date`, `day_part`, `place_id`, `landmark`, `pin`, `accuracy_meters`, `description`, `origin_source`, `preferred_partner_id`, `ended_reason`, `ended_at`, `cancel_reason_code`*, `cancel_note`*, `is_rematching` |
| `request_answer` | `(request_id, question_id)`, `values TEXT[]`, `unit_code`, `not_sure` |
| `request_media`* | Photos attached to a request |

**Chunk 3: he prices it**

| Table | Holds |
|---|---|
| `partner_profile` | `display_name`, `base_place_id`, `experience_range_code`, `status` (`REGISTERED`/`ACTIVATED`/`ACTIVE`/`SUSPENDED`), `status_note`, `accepting_new_jobs`, `travel_radius_km`, `next_step` (`BASIC_PROFILE`→`SERVICES`→`TRAVEL_RADIUS`→`NOTIFICATIONS`→`COMPLETE`) |
| `partner_service` | `(partner_id, service_id)`, `equipment_ids TEXT[]` |
| `identity_verification` | `status` (`PENDING`/`VERIFIED`/`REJECTED`/`NEEDS_ACTION`), `document_type`, media ids, `review_note` (written for him to read) |
| `offer` | `status` (`ACTIVE`/`BOOKED`/`WITHDRAWN`/`NOT_SELECTED`/`CLOSED`), `pricing_type`, amounts, `unit_code`, `comparable_cost_minor`, `offered_day_part` |
| `opportunity_notification` | Who was told about which request, in which `batch`, when, and `declined_at` |
| `service_offer_unit`* | Units a service may be priced in: `code`, `label` ("acres"), `per_label` ("acre") |

**Chunk 4: the job**

| Table | Holds |
|---|---|
| `booking` | `state` (`BOOKED`/`ARRIVED`/`COMPLETED`/`CANCELLED`); **frozen** `booked_*` pricing snapshot; `schedule_date`, `day_part`, `arrival_expected_by`; `final_amount_*`; `completion_pin` (clear text, never logged); `on_my_way_at`, `arrived_at`, `arrival_point`, `completed_at`, `completed_by`; cancel fields; `booked_partner_display_name`*, `arrival_accuracy_meters`* |
| `booking_pin_attempt`* | Wrong-PIN counter and lockout per booking |

**Chunks 5–7: money, when it goes wrong, after the job** (tables exist; the app endpoints are waiting on specs)

| Table | Holds |
|---|---|
| `credit_ledger_transaction` | Append-only, signed `amount_minor`. `type`: `TOP_UP`, `JOB_FEE`, `SYSTEM_RESTORE`, `REFERRAL_REWARD`, `PROMO`, `MANUAL_ADJUSTMENT` |
| `partner_balance` (view) | `SUM(amount_minor)` per partner. There is no balance column anywhere |
| `job_report` | Issues and no-show disputes, with `reason_code` (nullable) |
| `review` | One per booking: `verdict` (`NOT_GOOD`/`OKAY`/`VERY_GOOD`), `tag_codes`, `comment`. No numeric rating |
| `saved_partner`, `referral`, `complaint` | As named. Complaints carry `status` (`OPEN`/`IN_REVIEW`/`RESOLVED`) |

**Additions and system tables**

| Table | Why |
|---|---|
| `display_text`* | Served sentences the docs require but give no column for: ending labels, daypart words, "Not sure", the area-label format |
| `platform_setting`* | Operational knobs: matching batch size and interval, OTP daily cap, PIN lockout, saved-place upgrade distance, well-rated thresholds |
| `admin_user`, `admin_session` | Console login. Passwords are hashed; session tokens are hashed |
| `runtime_setting` | Admin overrides of `config.py` values (section 8) |

`*` marks something added beyond doc 0. Doc 0's own DDL is copied verbatim into migrations 0002–0006.

---

## 5. API flow, requests and responses

`docs/api-spec.md` has a **real request and response for every endpoint**, including each error. This section shows the order the apps call things and the main shapes.

Legend: **Auth** C = customer token, P = partner token, – = none. **Idem** ✔ = `Idempotency-Key` required.

### 5.1 Chunk 1: getting in

| Method & path | Auth | Idem | What it does |
|---|---|---|---|
| `GET /health` | – | | Says whether the service and its database are up, and whether auth, SMS and the scheduler are currently on. Returns 503 when the database cannot be reached |
| `GET /catalog?areaId=` | – | | Serves everything both apps display in one body: services, the guided picker, all six reason lists, small lists, business numbers, terms, support contact and the area's scheduling hours. Carries an ETag, so an unchanged catalog costs a 304 |
| `GET /catalog/services/{serviceId}/request-template` | – | | Serves the tap-only questions for one service, with units, stepper bounds and showIf rules. A service with no questions returns an empty list, which sends her straight to picking a day |
| `POST /auth/otp/request` | – | | Sends a login code by SMS and returns a challenge id, the resend countdown and how long the code lasts. The code itself is never in the response |
| `POST /auth/otp/verify` | – | | Checks the code, creates or resumes the account, creates the profile for whichever app is calling, and returns an access token, a refresh token and who the user is |
| `POST /auth/token/refresh` | refresh | | Exchanges a refresh token for a new access token, rotating the refresh token. A revoked or expired one returns 401 `SESSION_EXPIRED`, which sends the app back to the phone screen |
| `POST /auth/logout` | refresh | | Revokes the session for this app only, so the same person stays signed in on the other app. Returns 204 |
| `GET /locations/places?areaId=` | – | | Lists every village in an area, unpaged, so the app can hold it and filter as she types. An unknown area returns an empty list, not a 404 |
| `GET /geographies/resolve?latitude=&longitude=` | – | | Turns a phone's pin into the area and village it falls inside, using real village boundaries rather than the nearest centre. Inside the area but in no village returns a null village; outside everything returns two nulls |
| `POST /locations/place-interest` | – | | Records the name of a village we do not serve yet, so we learn where to open next. Stores nothing that locates a person, does not deduplicate, and returns 202 |
| `POST /media` (multipart `file`, `purpose`) | C/P | | Banks one picture on its own and returns an id that later calls refer to. The real file type is read from the bytes, and identity images go to private storage |
| `GET /media/{key}` | – | | Serves a public image, such as a request photo or profile picture. Identity documents live outside this folder and can never be fetched here |
| `PUT /devices/{deviceId}/push-token` | C/P | | Registers this installation's push token, and takes that token off any older device holding it, so a job is never sent to a phone in a drawer. Returns 204 |
| `DELETE /devices/{deviceId}/push-token` | C/P | | Stops pushes to one installation, for example on sign-out. Returns 204 |

**`POST /auth/otp/request`**

```json
// request
{ "phoneNumber": "+919876543210", "purpose": "LOGIN", "language": "te" }
// 200
{ "data": { "challengeId": "otp_01J…", "resendAfterSeconds": 30, "expiresInSeconds": 300 } }
```

Errors: `400 INVALID_PHONE_NUMBER`, `429 OTP_RESEND_TOO_SOON` (`details.retryAfterSeconds`), `429 OTP_DAILY_LIMIT`, `502 OTP_SEND_FAILED`.

**`POST /auth/otp/verify`**, sent with header `X-App: CUSTOMER|PARTNER`

```json
// request
{ "challengeId": "otp_01J…", "code": "123456" }
// 200
{ "data": {
    "accessToken": "eyJ…", "refreshToken": "rft_01J….xyz", "expiresAtEpochSeconds": 1758200000,
    "user": { "id": "usr_01J…", "phoneNumber": "+919876543210", "phoneVerified": true,
              "hasCustomerProfile": true, "hasPartnerProfile": false } } }
```

Three distinct failures: `422 OTP_INVALID` (wrong code; type again), `422 OTP_EXPIRED` (resend now), `429 OTP_TOO_MANY_ATTEMPTS` (resend now).

**`GET /catalog`** returns these 18 keys:

- lists: `services[] {id, name, iconUrl, category, categoryLabel, commonOnHome, equipment[]}`, `helpMeChoose {templateVersion, prompt, options[]}`, `reasons {6 contexts: [{code,label}]}`, `placeLabels`, `reviewTags {code,label,positive}`, `experienceRanges`
- numbers: `travelDistancesKm`, `jobsOnHomeScreen`, `maxOfferRupees`, `jobFeePercent`, `cancellationLimit`, `cancellationWindowDays`
- terms and contact: `termsVersion`, `termsUrl`, `supportContact {…}`, `bookingTerms {4 sentences}`, `partnerTerms {4 sentences}`
- `scheduling {sameDayCutoffHour, bookAheadDays, minimumNoticeMinutes, noShowPromptDelayMinutes, preferredPartnerHeadStartMinutes, morningEndsHour, afternoonEndsHour, eveningEndsHour}`

An unknown `areaId` is `200` with empty service lists, never `404`.

**`GET /geographies/resolve`**

```json
{ "data": { "geography": { "id": "geo_podalakur", "label": "Podalakur Mandal" }, "place": { "id": "plc_podalakur", "name": "Podalakur" } } }
```

- A pin in the area but in no village returns `place: null`.
- A pin outside every area returns both `null`.
- Bad input is `400 INVALID_COORDINATES`.

### 5.2 Chunk 2: she asks for work (customer)

All of these take a customer token.

| Method & path | Idem | What it does |
|---|---|---|
| `GET /customer/me` | | Returns her name, phone number, default village and how many jobs she has completed. The count is computed from her bookings, never stored |
| `PATCH /customer/me` | | Changes her display name or default village. She is never forced to give a name, so it may be cleared |
| `GET /customer/home` | | Draws her whole home screen in one call: the one request still running (in full) plus her saved partners |
| `GET /customer/places` | | Lists the places she has saved, each with the label word resolved so the app just prints it |
| `POST /customer/places` | | Saves a place under a name she tapped. The same label, village and landmark updates that place instead of making a second one, so she never ends up with four things called "My field" |
| `DELETE /customer/places/{spl_id}` | | Removes a saved place outright. A wrong one would misdirect every future job, so removing is never refused |
| `POST /customer/requests` | ✔ | **The main write.** Posts a job: service, answers, day and daypart, village and pin, photos. Re-checks every rule the app enforces, then notifies the first batch of partners. Returns the full request |
| `GET /customer/requests?bucket=ACTIVE\|PAST&cursor=` | | Lists her requests: the ones running, or her history newest first with a cursor |
| `GET /customer/requests/{id}` | | Reads one request, and while it can still be edited also returns a `draft` holding exactly what she entered, so the editor can be filled back in |
| `PATCH /customer/requests/{id}` | ✔ | Edits a request nobody has priced yet. Every posting rule runs again, because the cutoff may have passed while the screen was open |
| `POST /customer/requests/{id}/cancel` | ✔ | Calls off a request before anybody is booked, with an optional reason. Nothing about money happens. Tapping twice is fine |
| `GET /customer/requests/{id}/offers` | | Returns the request and its offers together, with each partner's trust facts, what the job would cost her, and the cheapest and most-experienced labels |

**Request template** (`GET …/request-template`)

```json
{ "data": { "serviceId": "svc_tractor", "templateVersion": 3, "questions": [
  { "id": "q_work_type", "type": "SINGLE_CHOICE", "label": "What work do you need?", "required": true, "allowNotSure": false,
    "options": [ { "id": "rotavator", "label": "Rotavator" }, { "id": "ploughing", "label": "Ploughing" } ] },
  { "id": "q_acres", "type": "NUMBER_WITH_UNIT", "label": "How many acres?", "required": true, "allowNotSure": true,
    "unit": { "code": "ACRE", "label": "acres", "perLabel": "acre" }, "min": 0.5, "max": 20, "step": 0.5, "default": 2 },
  { "id": "q_trips", "type": "NUMBER_WITH_UNIT", "label": "How many trips?", …,
    "showIf": { "questionId": "q_work_type", "answerIn": ["ploughing"] } } ] } }
```

**`POST /customer/requests`**

```json
{
  "serviceId": "svc_tractor", "workTypeId": "rotavator", "templateVersion": 3,
  "answers": [ { "questionId": "q_work_type", "values": ["rotavator"] },
               { "questionId": "q_acres", "values": ["3"], "unit": "ACRE" } ],
  "description": "Need rotavator work tomorrow morning",
  "schedule": { "date": "2026-09-20", "dayPart": "MORNING" },
  "location": { "placeId": "plc_podalakur", "landmark": "Near the canal bridge",
                "latitude": 14.367123, "longitude": 79.733456, "accuracyMeters": 18.0 },
  "mediaIds": ["med_01J…"], "preferredPartnerId": null,
  "origin": { "source": "HOME", "optionId": null, "templateVersion": null }
}
```

For "not sure", send `{"questionId": "q_acres", "values": [], "notSure": true}`. Values are always strings.

**Request shape**, returned by create, read, list and home:

```json
{ "id": "rqt_01J…", "state": "REQUESTED",
  "service": { "id": "svc_tractor", "name": "Tractor" },
  "summary": { "scheduleLabel": "20 Sep • Morning", "areaLabel": "Podalakur", "description": "…" },
  "offerCount": 0, "notifiedPartnerCount": 2, "isRematching": false, "rematch": null,
  "bookingId": null, "endedReason": null, "endedLabel": null,
  "canEdit": true, "canCancel": true, "createdAt": "2026-09-19T05:02:11Z",
  "draft": { "…what she entered…": "only on GET /customer/requests/{id} while canEdit" } }
```

Every rule is checked again on the server, each with its own error code:

| Check | Error |
|---|---|
| Service runs in her area | `422 SERVICE_NOT_AVAILABLE` |
| Village is served | `422 PLACE_NOT_SERVED` |
| Date not in the past (area calendar) | `422 DATE_IN_PAST` |
| Not past today's cutoff hour | `422 SAME_DAY_CLOSED` |
| Within the booking horizon | `422 BEYOND_BOOKING_HORIZON` |
| Daypart still has minimum notice | `422 INSUFFICIENT_NOTICE` |
| Answers match the template | `ANSWER_REQUIRED`, `ANSWER_NOT_APPLICABLE`, `NOT_SURE_NOT_ALLOWED`, `ANSWER_OUT_OF_RANGE`, `INVALID_ANSWER_VALUE`, `TEMPLATE_VERSION_STALE` |
| Photos are hers and are request photos | `422 INVALID_MEDIA_REFERENCE` |
| Edit after an offer, or after the request ended | `409 REQUEST_NOT_EDITABLE` |
| Cancel after booking | `409 REQUEST_NOT_CANCELLABLE` |

### 5.3 Chunk 3: he sees it and prices it (partner)

All of these take a partner token.

| Method & path | Idem | What it does |
|---|---|---|
| `GET /partner/me` | | Returns his whole profile in one read: name, base village, services, radius, our status and his own accepting switch, his identity state, and the setup step his app should open at |
| `PATCH /partner/me/basic` | | Saves his name, base village, experience and photo, and the language we would write to him in. Moves setup on to choosing services |
| `PUT /partner/me/services` | | Replaces his whole list of work and the equipment he owns for each. Sending the list without a service is how he drops it. Doing no work at all is allowed |
| `PATCH /partner/me` | | Sets how far he travels (one of the offered distances), or switches himself off and on. Being off is his choice and is not a suspension |
| `POST /partner/me/activate` | ✔ | He says setup is finished. Marks him waiting on us, not yet able to take work, and names the unfinished step if something is missing |
| `POST /partner/me/identity-verification/initiate` | | Sends one government document and a selfie for a person to compare. Can be sent again after we ask for a clearer photo, but not after a final decision |
| `GET /partner/me/identity-verification` | | Tells him how the check is going, and carries the note we wrote for him when something was wrong |
| `GET /partner/home` | | Draws his whole home screen in one call: profile, identity state, the first few jobs near him, his running jobs, the prices he has out, and his credit |
| `GET /partner/opportunities?cursor=` | | Lists work he has been told about and can still price. Shows the village and area only: no landmark, pin, customer name or phone number |
| `GET /partner/opportunities/{requestId}` | | Opens one job with her answers written out as sentences, the units he may price it in, and whether he can still send a price |
| `POST /partner/opportunities/{requestId}/offers` | ✔ | Sends his one price: a flat amount, a rate per unit, or a charge to come and look. We work out what it comes to for her. Sending a price is always free |
| `POST /partner/opportunities/{requestId}/decline` | | Says he is not interested. Free, with no effect on his standing; it drops off his list and tells us the job was seen and passed over |
| `GET /partner/offers` | | Lists the prices he has sent, each with the job it belongs to, so a row is never a price with no context |
| `DELETE /partner/offers/{offerId}` | ✔ | Takes a price back. If she booked him in the same moment, the booking stands and he is told it is now a job |

**Opportunity.** It is privacy-safe: there is no field for her landmark, pin, name or number.

```json
{ "requestId": "rqt_01J…", "service": { "id": "svc_tractor", "name": "Tractor" },
  "workType": { "id": "rotavator", "name": "Rotavator" },
  "approximateArea": { "placeId": "plc_podalakur", "placeName": "Podalakur", "areaLabel": "Podalakur area" },
  "schedule": { "date": "2026-09-20", "dayPart": "MORNING" },
  "answers": [ { "questionId": "q_acres", "label": "Land", "displayValue": "3 acres" } ],
  "allowedOfferUnits": [ { "code": "ACRE", "label": "acres", "perLabel": "acre" } ],
  "description": "…", "media": [ { "id": "med_…", "url": "…", "thumbnailUrl": "…" } ],
  "customerTrust": { "phoneVerified": true, "completedJobs": 3 },
  "canSendOffer": true, "postedAt": "2026-09-19T05:02:11Z" }
```

**Send an offer.** `pricing` takes exactly one of three shapes:

```json
{ "pricing": { "type": "FIXED", "exactAmount": { "amountMinor": 240000, "currency": "INR" } }, "offeredDaypart": "MORNING", "note": null }
{ "pricing": { "type": "UNIT_RATE", "rate": { "amountMinor": 80000, "currency": "INR" }, "unit": { "code": "ACRE" } }, "offeredDaypart": "AFTERNOON" }
{ "pricing": { "type": "INSPECTION", "inspectionCharge": { "amountMinor": 30000, "currency": "INR" } } }
```

Errors: `422 INVALID_PRICING`, `422 OFFER_ABOVE_MAXIMUM`, `422 INVALID_OFFER_UNIT`, `422 INVALID_OFFERED_DAYPART`, `409 OFFER_ALREADY_SENT`, `409 REQUEST_NOT_OPEN`, `403 NOT_ELIGIBLE`.

**Her offers view** (`GET /customer/requests/{id}/offers`)

```json
{ "data": { "request": { "…request shape…": "" }, "offers": [ {
  "id": "ofr_01J…", "requestId": "rqt_01J…",
  "partner": { "id": "usr_01J…", "displayName": "Ravi", "profilePhoto": null,
               "trust": { "identityVerified": true, "completedJobs": 18, "wellRated": false } },
  "pricing": { "type": "UNIT_RATE", "rate": { "amountMinor": 75000, "currency": "INR" },
               "unit": { "code": "ACRE", "label": "acres", "perLabel": "acre" } },
  "comparableCost": { "amountMinor": 225000, "currency": "INR" },
  "isLowestPrice": true, "isMostJobsCompleted": false,
  "offeredDaypart": "AFTERNOON", "note": null, "status": "ACTIVE", "createdAt": "…" } ] } }
```

**Sent offer shape** (`GET /partner/offers`):

`{id, requestId, status, pricing, offeredDaypart, note, requestContext {service, workType, approximateArea, schedule}, bookingId, createdAt}`

Withdrawing an offer she has already booked returns `409 OFFER_ALREADY_BOOKED`, and the booking stands.

### 5.4 Chunk 4: the job

| Method & path | Auth | Idem | What it does |
|---|---|---|---|
| `POST /customer/requests/{id}/book` `{offerId}` | C | ✔ | **The moment the product exists for.** In one transaction: creates the booking with the price frozen as she saw it, closes every other offer, marks the request booked, stops any further partners being told, makes her 4-digit code and works out when he is expected by. This is also where his access to her number and exact spot begins |
| `GET /customer/bookings/{id}` | C | | Her booked job: the frozen price and schedule, his name and number, the exact spot, what has happened so far, which buttons to show, and the sentence she reads before giving her code |
| `GET /customer/bookings/{id}/completion-pin` | C | | Reveals her four digits, the same ones every time she opens the screen. Refused while a visit price is waiting for her agreement, and after the job is done |
| `POST /customer/bookings/{id}/agree-amount` | C | ✔ | She says yes to the price he named after looking. Only for a visit-charge job. Until she does, her code stays hidden, because the code is her yes |
| `POST /customer/bookings/{id}/confirm-arrival` | C | | She marks him arrived when his own tap never landed, for example when he has no signal in a field. She is standing next to him and can see he is there |
| `POST /customer/bookings/{id}/confirm-complete` | C | | She closes a job that finished after he drove away, so it never hangs open. Recorded as completed by her rather than by the code |
| `GET /partner/jobs?bucket=ACTIVE\|HISTORY` | P | | His jobs as short rows: running ones, or his history. Rows carry her given name and the village, never her number or exact spot |
| `GET /partner/bookings/{id}` | P | | One job in full. Now that she has booked him: her name, her number, the landmark and the pin, her answers, and what this job will cost him in credit |
| `POST /partner/bookings/{id}/on-my-way` | P | | He says he has set off. Her app then stops asking whether he came, because a man who has just left has not failed to turn up. He can tap it more than once |
| `POST /partner/bookings/{id}/arrive` `{evidence?}` | P | ✔ | He marks himself there, with his position if the phone has a fix. The position is optional, because no fix must never block arrival, and it is later used to improve her saved pin |
| `POST /partner/bookings/{id}/final-amount` `{quantity}` or `{amountMinor}` | P | ✔ | For a job nobody could price up front: on a rate job he sends only how many and we multiply by his own rate; on a visit job he sends the total she pays, with the visit charge already inside it |
| `POST /partner/bookings/{id}/complete` `{pin}` | P | ✔ | He types the four digits she reads out, and the job is done. A wrong code is a normal answer he can try again; too many wrong ones lock the booking for a countdown he can read |

Booking outcomes:

| Outcome | Result |
|---|---|
| Booked | `201` |
| Offer gone | `409 OFFER_NOT_AVAILABLE` |
| Already booked | `409 REQUEST_ALREADY_BOOKED` + `details.bookingId` |
| Request ended, or schedule no longer possible | `422 REQUEST_NOT_BOOKABLE` |
| Partner no longer active | `422 PARTNER_NOT_BOOKABLE` |

**Customer booking shape**

```json
{ "id": "bkg_01J…", "requestId": "rqt_01J…", "state": "BOOKED",
  "partner": { "id": "usr_…", "displayName": "Ravi", "phoneNumber": "+9198…", "trust": { … } },
  "service": { "id": "svc_tractor", "name": "Tractor" },
  "bookedPricing": { "type": "FIXED", "exactAmount": { "amountMinor": 240000, "currency": "INR" } },
  "bookedCost": { "amountMinor": 240000, "currency": "INR" }, "finalAmount": null, "finalAmountAgreed": false,
  "schedule": { "date": "2026-09-20", "dayPart": "AFTERNOON" }, "arrivalExpectedBy": "2026-09-20T17:30:00",
  "exactLocation": { "placeId": "…", "placeName": "Podalakur", "landmark": "…", "latitude": 14.36, "longitude": 79.73, "accuracyMeters": 18.0 },
  "events": [ { "type": "BOOKED", "at": "…Z" }, { "type": "ON_MY_WAY", "at": "…Z" } ],
  "availableActions": ["CALL_PARTNER", "WHATSAPP_PARTNER"],
  "pinGuidance": "Give this code to Ravi only after the work is finished and you are happy with it.",
  "partnerSaved": false, "partnerFellThrough": false, "review": null, "createdAt": "…Z" }
```

**Partner job detail** adds:
- `customer {id, displayName, phoneNumber}`, `workType`, `answers` and `description`
- `introduction {isNewCustomerPair, amount}`, where `amount` is the fee he will pay: 5% rounded down to the rupee, and 0 when the job is unpriced
- `availableActions`: `CALL_CUSTOMER`, `WHATSAPP_CUSTOMER`, `ON_MY_WAY`, `ARRIVE`

**Partner job list row:**

`{bookingId, state, service, customerDisplayName, areaLabel, schedule, bookedPricing, isOnMyWay, endedReason, canDisputeNoShow}`

**PIN, final amount and completion errors**

| Where | Error |
|---|---|
| Completion PIN | `409 FINAL_AMOUNT_NOT_AGREED` (visit price not agreed yet), `409 BOOKING_ALREADY_COMPLETED` |
| Final amount | `400 INVALID_FINAL_AMOUNT`, `422 FINAL_AMOUNT_NOT_REQUIRED`, `409 BOOKING_NOT_ARRIVED`, `422 AMOUNT_ABOVE_MAXIMUM` |
| Complete | `422 WRONG_PIN`; `429 PIN_TOO_MANY_ATTEMPTS` + `details.retryAfterSeconds`; `409 FINAL_AMOUNT_REQUIRED` |
| Arrive | `409 BOOKING_NOT_ARRIVABLE` |

### 5.5 Admin API (`/admin/api/*`)

All of these take the `nsv_admin` cookie from login, or an `X-Admin-Key` header for scripts.

| Method & path | What it does |
|---|---|
| `POST /login` `{username, password}` | Signs an admin in and sets the session cookie. A wrong name and a wrong password give the same answer, so neither can be probed |
| `POST /logout` | Ends the console session and clears the cookie |
| `GET /me` | Says who is signed in, so the page knows whether to show the login screen |
| `GET /meta` | Describes every table the console may show or edit, with each column's type, whether it is required and its help text. The whole UI is built from this, so nothing outside the registry is reachable |
| `GET /r/{resource}?q=&f.{column}=&offset=` | Lists rows of one table, with a text search, one exact filter and paging |
| `POST /r/{resource}/get` `{key}` | Reads one row. The key is sent in the body because some tables have two key columns |
| `POST /r/{resource}` `{values}` | Creates a row, after the checks for that table run. Editing questions also raises the template version everywhere |
| `PUT /r/{resource}` `{key, values}` | Updates a row. Key columns and read-only columns are never touched |
| `DELETE /r/{resource}` `{key}` | Deletes a row. Anything still referenced elsewhere is refused by the database, with the reason passed back in words |
| `GET /stats` | The dashboard: what is waiting on a person, work and jobs by state, and the villages most asked for |
| `GET /partners?status=` | Every partner on one screen: status, identity state, services, base village, radius, completed jobs and credit balance |
| `POST /partners/{id}/status` `{status, statusNote?}` | Approves, suspends or reinstates a partner. Approving needs finished setup and a verified identity. The note is shown to him, so it is written for him to read |
| `POST /identity/{partnerId}/decision` `{status, reviewNote?, activate?}` | Decides an identity check. Verifying or rejecting is final and deletes both images from storage, keeping only the decision. Verifying can also make him active in the same step |
| `GET /media/{id}/file` | Shows one uploaded image to an admin. This is the only way an identity document is ever read back, and it is gone once decided |
| `POST /media` (multipart `file`, `purpose`) | Uploads a picture from the console, such as a service icon, and returns its public URL to put on a row |
| `DELETE /media/{id}` | Removes an image the console uploaded, for example when replacing an icon |
| `POST /ledger/adjust` `{partnerId, type, amountMinor, note}` + `Idempotency-Key` | Adds one credit row by hand, such as a top-up or a correction. The ledger is append-only, so a correction is another row; job fees can never be written here |
| `GET /requests/{id}/detail` | The whole story of one request: answers, who was told and in which batch, every offer and every booking. The first place to look when matching misbehaves. Never shows a code |
| `POST /requests/{id}/run-matching` | Sends the next batch of partners for one request now, instead of waiting for the timer. Useful when trying out matching settings |
| `POST /jobs/tick` | Runs all the scheduled work once: matching batches, ending past-date requests, cleaning up retry records |
| `GET /demand` | Villages people outside the area typed, grouped and counted. This is the list that decides where to open next |
| `GET /config` | Lists every setting with its `.env` value, any override and the value in force. Secrets show only whether they are set |
| `PUT /config/{ENV_KEY}` `{value}` | Switches one setting on the running server, within seconds and with no restart |
| `DELETE /config/{ENV_KEY}` | Drops an override so the `.env` value applies again |

The console itself is served at `GET /admin` (the page) and `GET /admin/static/{file}` (its stylesheet and script).

### 5.6 End-to-end flow

```
Customer app                         Backend                                    Partner app
------------                         -------                                    -----------
GET /catalog, /geographies/resolve ─►
POST /auth/otp/request, /verify ────► user + customer_profile, JWT
                                                                     ◄─ OTP sign-in → partner_profile (REGISTERED)
                                                                     ◄─ basic → services → radius → activate (ACTIVATED)
                                                                     ◄─ POST /media ×2, identity/initiate (PENDING)
                                      ADMIN: verify identity → VERIFIED, images deleted, partner ACTIVE
GET request-template
POST /customer/requests ────────────► 8 checks → request REQUESTED
                                      matching: preferred partner (batch 0) → head start → batches of N every M min
                                      opportunity_notification rows + push ──────────────► GET /partner/opportunities
                                                                     ◄─ POST …/offers (comparableCost computed)
GET …/offers (claims computed) ◄─────
POST …/book ────────────────────────► ONE transaction: booking snapshot, other offers NOT_SELECTED,
                                      request BOOKED (matching stops), PIN, arrivalExpectedBy
                                                                     ◄─ GET /partner/bookings/{id} (her number and spot now visible)
                                                                     ◄─ on-my-way → arrive
                                      [visit charge] ◄─ final-amount; she agree-amount
GET completion-pin (reads it aloud) ─────────────────────────────────►  POST …/complete {pin}
                                      booking + request COMPLETED, saved place pin improved
Scheduler tick: past-date requests → NOT_BOOKED / NO_PARTNER_AVAILABLE, offers CLOSED
```

---

## 6. Business decisions

### 6.1 Schema and data

| Decision | Why |
|---|---|
| ULID ids with a type prefix | Readable in logs and support messages; they sort by creation time |
| Money in integer paise; the fee rounds **down** to the rupee, once | No drift between two places that round differently; rounding favours the partner (₹2,449 at 5% is ₹122, not ₹122.45) |
| TEXT + CHECK enums | Adding a value is one `ALTER`; apps match on exact strings |
| No stored derived facts (balance, counts, events, fees) | A copy of a fact is a second thing that can disagree |
| PostGIS in its own schema | Doc 0 names a table `geography`, which collides with PostGIS's `geography` type |
| Partner ids are `usr_…` | Every foreign key uses `partner_profile.user_id`; the doc's `prv_` would be a second id for one person |
| One template version for all services, raised automatically on any question edit | Answers record which wording they answered; apps holding stale questions are refused with `TEMPLATE_VERSION_STALE` |

### 6.2 Privacy and trust

- **Her landmark, pin, accuracy, name and phone never leave the server until she books.** Before booking, partners get an `approximateArea` shape, which has no field that could hold more.
- After booking, **only the booked partner** gets `exactLocation` and her number. Partners not selected keep only the village.
- **Customer trust shown to a partner:** `phoneVerified` and `completedJobs` only. No name, no score.
- **Partner trust shown to a customer:** `identityVerified`, `completedJobs`, `wellRated`. There is no rating or average anywhere. `wellRated` stays false until the founders set its thresholds.
- **Identity is checked by a person**, not an algorithm. Once the decision is VERIFIED or REJECTED, **both images are deleted** from storage (`media.deleted_at`), which is a legal requirement. Nothing in either app ever reads an identity image back.
- **The completion PIN is stored in clear** because she has to read it out. It is never logged and never shown in the admin console.
- **Request bodies are never logged**, only method, path, status and time.

### 6.3 Requests

- **The server re-checks every rule the app enforces.** The app's clock can be wrong, screens stay open for an hour, and retries arrive late.
- **Time rules use the area's clock** (`geography.timezone`):
  - no requests for today after the cutoff hour (18:00);
  - at most 14 days ahead;
  - a daypart needs 60 minutes of notice left;
  - `ANY_TIME` passes if any daypart still has notice.
- **The server walks `showIf` itself.** A hidden question's answer is refused. "Not sure" is a real answer that keeps dependent questions hidden.
- **An unknown `origin.source` is stored, not rejected.** It's a statistic, and old apps must still be able to post work.
- **A preferred partner who is no longer active is silently dropped**; the request still posts.
- **Editing stops once anyone has offered.** `canEdit` and `canCancel` are decided on the server.
- **Cancelling never moves money.** Cancelling twice returns 200 both times. Open offers become `CLOSED`.

### 6.4 Matching

A partner is eligible when all of these hold:
- he is `ACTIVE` and accepting jobs;
- he does the service;
- he owns the equipment for the work type, if the service has equipment;
- the request's village is within his travel radius of his base village (`ST_DWithin`).

How notifications go out:
- **Preferred partner first, alone (batch 0).** Everyone else waits for the area's head start (30 min).
- **Then batches of N (default 5), nearest first, every M minutes (default 10), but only while nobody has offered.**
- **Every notification is recorded** (`opportunity_notification`), even when push fails. Opening the app always shows the same list.
- **Matching stops the moment the request is booked.** Booking and batching lock the same row, so a batch can't go out after she books.
- **Declining is free and has no consequence.** It only hides the job and records the signal.

### 6.5 Pricing

- **Three pricing types only:** `FIXED`, `UNIT_RATE`, `INSPECTION`. `RANGE` and "rough total next to a rate" were removed on purpose, because they let a partner look cheap and bill high.
- **`comparableCost`** is what the job costs her: the fixed price, or rate × her quantity in the same unit. It is **null** when there's no single matching quantity, she said "not sure", or it's a visit charge. It is never a bare rate shown as a price.
- **`isLowestPrice`** is true when an offer is no more expensive than every other active offer. **If any active offer can't be priced, nobody gets it.** Ties mark everyone in the tie.
- **`isMostJobsCompleted`** goes to the most completed jobs among offerers. It is false for everyone when the top count is 0.
- **With only one offer, neither claim is made**, because saying nothing is always honest. These are platform claims covered by India's advertising rules, so they are computed on the server.
- **`maxOfferRupees` is a guard against typos**, not a price judgement. 0 or missing means no ceiling.
- **`offeredDaypart` is what he promises.** It defaults to hers and is never `ANY_TIME`. The booking uses **his** daypart.
- **Sending a price is always free.** No ledger row is ever written when an offer is sent.

### 6.6 Booking and the job

- **Booking is one transaction:**
  - four checks: request open, offer active, partner active, schedule still possible;
  - four effects: booking with a frozen price snapshot, other offers `NOT_SELECTED`, request `BOOKED`, PIN + `arrivalExpectedBy`.
- A partial unique index makes two live bookings on one request impossible, even under concurrency.
- **Booking twice** with a different key returns `409 REQUEST_ALREADY_BOOKED` with the booking id, so her app goes straight to the job.
- **A withdraw racing a booking never undoes the booking.** Whichever commits first wins.
- **`arrivalExpectedBy`** is the end of the booked daypart plus the no-show grace, in local time. For example, afternoon becomes 17:30.
- **On my way is an event, not a state.** Only the latest time is kept.
- **Arrival evidence is optional**, because a field may have no GPS fix.
- **A job nobody could price up front:**
  - With a **visit charge**, he names the inclusive total. **She must agree before her PIN appears**, and both the endpoint and the app enforce this.
  - With a **rate where she said not sure**, he sends only the quantity and the server multiplies. She isn't asked, because she already agreed the rate.
- **PIN lockout:** after 5 wrong PINs the booking locks for 300 s, and `retryAfterSeconds` is in the error. This defends against brute-forcing 10,000 codes.
- **Her overrides:** confirm-arrival and confirm-complete (`completed_by = CUSTOMER`). A partner can't complete a job that still needs its final amount.
- **Completion is transactional.** The chunk-5 `JOB_FEE` ledger row will be added inside this same transaction. Today `introduction.amount` only shows the fee.
- **Saved places get better over time.** A completed job moves her saved pin to his arrival point, but only if that point is within 300 m and more accurate.

### 6.7 Accounts and auth

- **One phone number is one person.** Customer and partner profiles hang off the same user, and each app has its own session, so logging out of one keeps the other.
- **OTP codes and refresh tokens are stored only as hashes.**
- OTP answers are distinct: wrong code (type again), expired (resend now), too many tries (resend now).
- **The resend wait counts only unused codes**, so signing in to the second app straight after the first isn't blocked. The daily cap counts every code.
- **Partner onboarding** moves forward only: `BASIC_PROFILE → SERVICES → TRAVEL_RADIUS → NOTIFICATIONS → COMPLETE`.
  - Activate makes him `ACTIVATED`; only an admin makes him `ACTIVE`.
  - Identity is asked for **after** setup, when it actually stands between him and work.
- **`status` is ours and `acceptingNewJobs` is his.** A suspended partner can't switch himself back on.
- **The scheduler ends requests whose day has passed:** `NOT_BOOKED` if there were offers, `NO_PARTNER_AVAILABLE` if not. The wording comes from `display_text`.

---

## 7. What the admin can configure

The console at `/admin` has a sidebar grouped as below. Generic table pages support search, one exact filter, paging, **New**, and click-to-edit. **Required** fields must be filled on create; everything else is optional. Key fields are locked once a row exists. The database's own constraints have the final word: violations come back as readable `422 CONSTRAINT_VIOLATION` messages.

**C** create · **U** update · **D** delete · **RO** read-only.

### 7.1 Overview pages (actions)

| Page | What you can do |
|---|---|
| Dashboard | Counts (identity checks waiting, partners awaiting approval, open requests, running jobs, offers in 24h, open complaints), top requested villages, **Run scheduled jobs now** |
| Partners | **Approve** (ACTIVATED → ACTIVE: needs finished setup and a VERIFIED identity), **Suspend** (asks for a note he will read), **Reinstate** |
| Identity queue | See document and selfie side by side. **Verify and activate**, **Ask him to send again** (note required), **Reject** (note required). Verify and Reject delete both images |
| Requests | Full story of a request: answers, who was told in which batch, offers, bookings. **Send next batch now** |
| Adjust credit | Add a ledger row. Required: partner, type (`TOP_UP`, `MANUAL_ADJUSTMENT`, `PROMO`, `SYSTEM_RESTORE`, `REFERRAL_REWARD`), non-zero amount in rupees, and a **note**. `JOB_FEE` can never be written by hand |
| Demand | Villages people outside the area asked for, grouped, with counts |
| Runtime config | Every `config.py` setting (section 8) |

### 7.2 Areas, places and services

| Resource | Access | Required fields | Optional fields | Notes |
|---|---|---|---|---|
| **Geographies** | CUD | `id`, `label`, `timezone` (IANA, e.g. `Asia/Kolkata`), `same_day_cutoff_hour`, `book_ahead_days`, `minimum_notice_minutes`, `no_show_prompt_delay_minutes`, `preferred_partner_head_start_minutes`, `morning_ends_hour`, `afternoon_ends_hour`, `evening_ends_hour` | `is_active`, `boundary` (GeoJSON Polygon/MultiPolygon) | Every scheduling rule for an area. Served in `catalog.scheduling` |
| **Villages** (`place`) | CUD | `id` (e.g. `plc_kovur`), `geography_id`, `name`, `mandal`, `district`, `state`, `centre` (lat, lng) | `boundary` (GeoJSON; decides which village a pin is in), `is_active` | No boundary means pins never resolve to it, but it can still be picked from the list |
| **Services** | CUD | `id` (permanent, e.g. `svc_tractor`), `name`, `category`, `category_label` | `icon_url`, `common_on_home`, `sort_order`, `is_active` | If nothing is `common_on_home`, the app shows all |
| **Service areas** (`service_geography`) | CD | `service_id`, `geography_id` | – | **A service with no row runs nowhere.** That's how to prepare one before launch |
| **Equipment** | CUD | `service_id`, `id` (e.g. `ROTAVATOR`), `label` | `sort_order` | Most services have none |
| **Offer units** | CUD | `service_id`, `code` (e.g. `ACRE`), `label` ("acres") | `per_label` ("acre"), `sort_order` | The units a partner may price that service in. With none, `UNIT_RATE` is impossible |

### 7.3 Request questions and catalog lists

| Resource | Access | Required fields | Optional fields | Notes |
|---|---|---|---|---|
| **Questions** | CUD | `id`, `service_id`, `type` (`SINGLE_CHOICE`/`MULTI_CHOICE`/`NUMBER_WITH_UNIT`), `label` | `short_label` (heading the partner reads, e.g. "Land"), `required`, `allow_not_sure`, `sort_order`, showIf (`depends_on_question_id` + `depends_on_values`) | **Number questions must also have** `unit_code`, `unit_label`, `min_value`, `max_value`, `step_value`, `default_value` (min ≤ default ≤ max, step > 0); `unit_per_label` is recommended. showIf must point at an **earlier** question of the **same** service. **Any change raises the template version for every service** |
| **Question options** | CUD | `id` (e.g. `rotavator`), `question_id`, `label` | `equipment_id` (equipment a partner needs for this work type), `icon_url`, `service_id`, `sort_order` | Choice questions with no options are hidden from apps. Edits also raise the template version |
| **Help me choose** | CUD | `id`, `label` | `service_id` (empty = "something else"), `icon_url`, `sort_order`, `is_active` | The service must run in some area. Edits raise `help_me_choose_version` |
| **Reasons** | CUD | `context` (one of the 6), `code`, `label` | `sort_order`, `is_active` | The same code may appear under two contexts. Empty lists never block anybody |
| **Place labels** | CUD | `code`, `label` | `sort_order` | Names she taps for a saved place ("My field") |
| **Review tags** | CUD | `code`, `label`, `positive` | `sort_order` | Positive tags show when she's happy, the others when not |
| **Experience ranges** | CUD | `code`, `label` | `sort_order` | Partner's years of experience |

### 7.4 Settings (single-row forms)

**Catalog settings** (`catalog_setting`, served to both apps):

| Field | Required | Meaning |
|---|---|---|
| `job_fee_percent` | ✔ | 5.00 today. Should only ever be cut |
| `cancellation_limit`, `cancellation_window_days` | ✔ | Partner cancellations before he's paused (3 in 30 days) |
| `max_offer_rupees` | ✔ | Typo guard for offers and final amounts; 0 means no ceiling |
| `jobs_on_home_screen` | ✔ | How many opportunities appear on his home screen |
| `travel_distances_km` | ✔ | Radius chips, comma separated (`5,10,15,25,40`). His radius must be one of these |
| `help_me_choose_prompt` | ✔ | The picker's question |
| `booking_shares_contact`, `booking_other_offers_close`, `booking_payment`, `booking_inspection_charge_adjusted` | ✔ | The four sentences on her confirm screen (`{partnerName}` is filled in by the app) |
| `partner_looking_is_free`, `partner_when_charged`, `partner_when_reporting_a_problem`, `partner_inspection_charge_adjusted` | ✔ | The four sentences for him |
| `pin_guidance` | ✔ | Shown before her PIN is revealed. `{partnerName}` is filled in by the server |
| `terms_version`, `terms_url` | | Null until the terms exist |
| `support_officer_name`, `support_officer_designation`, `support_email`, `support_phone`, `support_response_promise` | | The grievance officer (Consumer Protection Rules 2020) |
| `help_me_choose_version` | read-only | Raised automatically |

**Platform settings** (`platform_setting`, operational):

| Field | Required | Default | Meaning |
|---|---|---|---|
| `matching_batch_size` | ✔ | 5 | Partners told per batch |
| `matching_batch_interval_minutes` | ✔ | 10 | Wait before the next batch (only while nobody has offered) |
| `otp_daily_limit` | ✔ | 10 | Codes per phone per 24 h |
| `pin_max_attempts` | ✔ | 5 | Wrong PINs before lockout |
| `pin_lockout_seconds` | ✔ | 300 | Lockout length |
| `saved_place_upgrade_max_meters` | ✔ | 300 | How close his arrival must be to improve her saved pin |
| `well_rated_min_reviews`, `well_rated_min_positive_percent` | | empty | **Founder decision pending.** While empty, `wellRated` is always false |

**Display text** (`display_text`, CUD; required: `key`, `label`):

| Key | Used for |
|---|---|
| `ENDED_CANCELLED_BY_CUSTOMER`, `ENDED_NO_PARTNER_AVAILABLE`, `ENDED_NOT_BOOKED`, `ENDED_PARTNER_FELL_THROUGH` | `endedLabel` for each way a request ends |
| `DAYPART_MORNING` / `_AFTERNOON` / `_EVENING` / `_ANY_TIME` | Words in `scheduleLabel` ("20 Sep • Morning") |
| `NOT_SURE` | `displayValue` when she answered not sure |
| `AREA_LABEL_FORMAT` | `approximateArea.areaLabel`, default `{placeName} area` |

### 7.5 People, work, money, support (operational views)

| Resource | Access | Editable fields |
|---|---|---|
| Customers (`customer_profile`) | U | `display_name`, `default_place_id`, `is_restricted` (restricted customers can't post work) |
| Partners (`partner_profile`) | U | `display_name`, `base_place_id`, `experience_range_code`, `accepting_new_jobs`, `travel_radius_km`, `accepted_terms_version`. Status changes go through the Partners page |
| Complaints | U | `status` (required: `OPEN`/`IN_REVIEW`/`RESOLVED`), `resolution` |
| Users, partner services, identity checks, saved places, requests, answers, notifications, offers, **bookings (never shows the PIN)**, ledger, balances, job reports, reviews, saved partners, referrals, place interest, media, devices, sessions, OTP requests (no codes), idempotency keys | RO | – |

---

## 8. Runtime configuration (config.py)

Every setting in `app/config.py` is read from `.env` at start-up. It is also listed on **Settings → Runtime config**, with its `.env` value and its live value. Settings marked **live** can be overridden from the console and take effect within about 2 seconds, with no restart. **Reset to .env** removes the override. Overrides are stored in `runtime_setting`.

The same works over the API: `GET /admin/api/config`, `PUT /admin/api/config/<KEY> {"value": "false"}`, `DELETE /admin/api/config/<KEY>`.

| Group | Setting | Type | Live? | Meaning |
|---|---|---|---|---|
| Auth | `AUTH_ENABLED` | bool | live | Off: protected endpoints skip tokens and act as `DEV_FAKE_USER_ID`. **Refused in production** |
| | `DEV_FAKE_USER_ID`, `DEV_FAKE_USER_PHONE` | str | live | Who the apps act as while auth is off |
| JWT | `JWT_SECRET_KEY` (secret), `JWT_ALGORITHM`, `JWT_ISSUER`, `JWT_AUDIENCE` | str | live | Changing the secret signs everybody out |
| | `JWT_ACCESS_TTL_SECONDS`, `JWT_REFRESH_TTL_DAYS`, `JWT_LEEWAY_SECONDS` | int | live | Token lifetimes |
| | `JWT_ROTATE_REFRESH` | bool | live | New refresh token on every refresh |
| SMS | `SMS_ENABLED` | bool | live | Off: codes go to the log, never to a phone |
| | `SMS_PROVIDER` | str | live | `console` or `twofactor` |
| | `SMS_FIXED_OTP` (secret) | str | live | Code used while SMS is off. **Ignored and refused in production** |
| | `SMS_LOG_OTP`, `SMS_FAIL_SILENTLY` | bool | live | Print codes; issue a code even when the vendor fails |
| 2Factor.in | `TWOFACTOR_API_KEY` (secret), `TWOFACTOR_BASE_URL`, `TWOFACTOR_SENDER_ID`, `TWOFACTOR_TEMPLATE_LOGIN_EN`, `TWOFACTOR_TEMPLATE_LOGIN_TE`, `TWOFACTOR_TIMEOUT_SECONDS` | str/int | live | Vendor connection |
| OTP | `OTP_LENGTH`, `OTP_TTL_SECONDS`, `OTP_MAX_ATTEMPTS`, `OTP_RESEND_WINDOW_HOURS` | int | live | Code rules |
| | `OTP_RESEND_BACKOFF_SECONDS` | int list | live | Wait before each resend, e.g. `30,60,120,300` |
| Media | `MEDIA_MAX_BYTES`, `MEDIA_ALLOWED_CONTENT_TYPES`, `MEDIA_PUBLIC_ROOT`, `MEDIA_PRIVATE_ROOT`, `MEDIA_PUBLIC_BASE_URL` | mixed | live | Upload limits, storage folders, public URL prefix |
| | `STORAGE_BACKEND` | str | .env only | `local` |
| Catalog | `CATALOG_ETAG_ENABLED`, `CATALOG_CACHE_SECONDS` | bool/int | live | Catalog caching |
| Background jobs | `SCHEDULER_ENABLED` | bool | live | Off: matching batches and expiry pause |
| | `SCHEDULER_TICK_SECONDS` | int | .env only | Tick interval |
| Area defaults | `DEFAULT_AREA_ID`, `DEFAULT_TIMEZONE`, `DEFAULT_LANGUAGE`, `SUPPORTED_LANGUAGES` | str/list | live | Used when an app sends no area or language |
| Application | `LOG_LEVEL`, `CORS_ORIGINS` | str/list | live | |
| | `APP_ENV`, `DEBUG`, `SECRET_KEY`, `LOG_JSON` | | .env only | |
| Admin | `ADMIN_API_KEY` (secret), `ADMIN_SESSION_HOURS` | | live | |
| | `ADMIN_API_ENABLED`, `ADMIN_BOOTSTRAP_USERNAME`, `ADMIN_BOOTSTRAP_PASSWORD` | | .env only | The console can't lock itself out |
| Database | `DATABASE_URL`, `TEST_DATABASE_URL`, `SQLALCHEMY_ECHO`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_RECYCLE`, `DB_CONNECT_TIMEOUT` | | .env only | Need a restart |

Secrets are never displayed, only whether they are set.

---

## 9. Testing

| Command | What it checks |
|---|---|
| `python -m pytest` | 47 tests, one file per chunk plus admin and runtime config. They follow each handover doc's "we are done when" list: OTP outcomes, cutoff and notice with a moved clock, hidden-answer rejection, privacy of the opportunity body, lowest-price going false with an inspection offer, afternoon offer booking the afternoon (17:30 deadline), same key and different key double booking, concurrent booking vs withdraw, PIN lockout, inspection agreement gate, ₹2,400 → ₹120 fee, and live auth and SMS switching |
| `python scripts/smoke.py --spec docs/api-spec.md` | 135 real HTTP calls across all 77 endpoints; rewrites the spec. Adds test data to the dev database, so reset and reseed afterwards if needed |

---

## 10. Not built yet

These are waiting on handover specs for chunks 5–7. Their tables already exist, and the admin can view them and add ledger adjustments.

- Writing the `JOB_FEE` ledger row at completion, and the work gate (`workBlock`: owing, unfinished job, too many cancellations).
- Top-up payment gateway (an open founder decision).
- Cancelling a booked job, no-shows, disputes, rematching, rescheduling (`CANCEL` and `RESCHEDULE` are left out of `availableActions` until these exist).
- Reviews, saved partners, referrals and complaints endpoints for the apps.
- Real push delivery (it is logged today) and the real 2Factor.in SMS (it needs an API key).
