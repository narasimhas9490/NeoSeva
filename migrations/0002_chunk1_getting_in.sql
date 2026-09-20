CREATE TABLE geography (
    id                                   TEXT PRIMARY KEY,
    label                                TEXT NOT NULL,
    timezone                             TEXT NOT NULL,
    same_day_cutoff_hour                 SMALLINT NOT NULL,
    book_ahead_days                      SMALLINT NOT NULL,
    minimum_notice_minutes               SMALLINT NOT NULL,
    no_show_prompt_delay_minutes         SMALLINT NOT NULL,
    preferred_partner_head_start_minutes SMALLINT NOT NULL,
    morning_ends_hour                    SMALLINT NOT NULL,
    afternoon_ends_hour                  SMALLINT NOT NULL,
    evening_ends_hour                    SMALLINT NOT NULL,
    is_active                            BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE place (
    id           TEXT PRIMARY KEY,
    geography_id TEXT NOT NULL REFERENCES geography(id),
    name         TEXT NOT NULL,
    mandal       TEXT NOT NULL,
    district     TEXT NOT NULL,
    state        TEXT NOT NULL,
    centre       postgis.geography(POINT, 4326) NOT NULL,
    boundary     postgis.geography(MULTIPOLYGON, 4326),
    is_active    BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (geography_id, name)
);
CREATE INDEX place_boundary_gix ON place USING GIST (boundary);
CREATE INDEX place_centre_gix   ON place USING GIST (centre);

CREATE TABLE place_interest (
    id          TEXT PRIMARY KEY,
    name_typed  TEXT NOT NULL,
    phone_e164  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
    id                TEXT PRIMARY KEY,
    phone_e164        TEXT NOT NULL UNIQUE,
    phone_verified_at TIMESTAMPTZ,
    language          TEXT NOT NULL DEFAULT 'te' CHECK (language IN ('te','en')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE idempotency_key (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES app_user(id),
    endpoint        TEXT NOT NULL,
    key             TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('IN_FLIGHT','DONE')),
    status_code     INT,
    response_body   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    UNIQUE (user_id, endpoint, key)
);
CREATE INDEX idempotency_created_idx ON idempotency_key (created_at);

CREATE TABLE otp_request (
    id          TEXT PRIMARY KEY,
    phone_e164  TEXT NOT NULL,
    purpose     TEXT NOT NULL CHECK (purpose IN ('LOGIN')),
    code_hash   TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    attempts    SMALLINT NOT NULL DEFAULT 0,
    consumed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX otp_phone_created_idx ON otp_request (phone_e164, created_at DESC);

CREATE TABLE auth_session (
    id                 TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    refresh_token_hash TEXT NOT NULL UNIQUE,
    app                TEXT NOT NULL CHECK (app IN ('CUSTOMER','PARTNER')),
    issued_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ NOT NULL,
    revoked_at         TIMESTAMPTZ
);

CREATE TABLE device (
    device_id  TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    app        TEXT NOT NULL CHECK (app IN ('CUSTOMER','PARTNER')),
    platform   TEXT NOT NULL DEFAULT 'ANDROID' CHECK (platform IN ('ANDROID')),
    push_token TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX device_push_token_uidx
    ON device (push_token) WHERE push_token IS NOT NULL;

CREATE TABLE service (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    icon_url       TEXT,
    category       TEXT NOT NULL,
    category_label TEXT NOT NULL,
    common_on_home BOOLEAN NOT NULL DEFAULT false,
    sort_order     INT NOT NULL DEFAULT 0,
    is_active      BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE service_geography (
    service_id   TEXT NOT NULL REFERENCES service(id),
    geography_id TEXT NOT NULL REFERENCES geography(id),
    PRIMARY KEY (service_id, geography_id)
);

CREATE TABLE service_equipment (
    id         TEXT NOT NULL,
    service_id TEXT NOT NULL REFERENCES service(id),
    label      TEXT NOT NULL,
    sort_order INT NOT NULL DEFAULT 0,
    PRIMARY KEY (service_id, id)
);

CREATE TABLE reason (
    context    TEXT NOT NULL CHECK (context IN (
                 'CUSTOMER_CANCEL_REQUEST','CUSTOMER_CANCEL_BOOKING','CUSTOMER_REPORT_ISSUE',
                 'PARTNER_CANCEL_BOOKING','PARTNER_REPORT_ISSUE','PARTNER_DISPUTE_NO_SHOW')),
    code       TEXT NOT NULL,
    label      TEXT NOT NULL,
    sort_order INT NOT NULL DEFAULT 0,
    is_active  BOOLEAN NOT NULL DEFAULT true,
    PRIMARY KEY (context, code)
);

CREATE TABLE place_label      (code TEXT PRIMARY KEY, label TEXT NOT NULL, sort_order INT NOT NULL DEFAULT 0);
CREATE TABLE review_tag       (code TEXT PRIMARY KEY, label TEXT NOT NULL, positive BOOLEAN NOT NULL, sort_order INT NOT NULL DEFAULT 0);
CREATE TABLE experience_range (code TEXT PRIMARY KEY, label TEXT NOT NULL, sort_order INT NOT NULL DEFAULT 0);

CREATE TABLE help_me_choose_option (
    id         TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    icon_url   TEXT,
    service_id TEXT REFERENCES service(id),
    sort_order INT NOT NULL DEFAULT 0,
    is_active  BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE catalog_setting (
    id                       SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    job_fee_percent          NUMERIC(5,2) NOT NULL,
    cancellation_limit       SMALLINT NOT NULL,
    cancellation_window_days SMALLINT NOT NULL,
    max_offer_rupees         BIGINT NOT NULL,
    jobs_on_home_screen      SMALLINT NOT NULL,
    travel_distances_km      INT[] NOT NULL,
    help_me_choose_prompt    TEXT NOT NULL,
    help_me_choose_version   INT NOT NULL DEFAULT 1,
    terms_version            TEXT,
    terms_url                TEXT,
    support_officer_name     TEXT,
    support_officer_designation TEXT,
    support_email            TEXT,
    support_phone            TEXT,
    support_response_promise TEXT,
    booking_shares_contact             TEXT NOT NULL,
    booking_other_offers_close         TEXT NOT NULL,
    booking_payment                    TEXT NOT NULL,
    booking_inspection_charge_adjusted TEXT NOT NULL,
    pin_guidance                       TEXT NOT NULL,
    partner_looking_is_free            TEXT NOT NULL,
    partner_when_charged               TEXT NOT NULL,
    partner_when_reporting_a_problem   TEXT NOT NULL,
    partner_inspection_charge_adjusted TEXT NOT NULL
);

CREATE TABLE media (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES app_user(id),
    purpose       TEXT NOT NULL CHECK (purpose IN (
                    'REQUEST_PHOTO','PARTNER_PROFILE_PHOTO','IDENTITY_DOCUMENT',
                    'IDENTITY_SELFIE','COMPLAINT_EVIDENCE')),
    content_type  TEXT NOT NULL,
    storage_key   TEXT NOT NULL,
    url           TEXT,
    thumbnail_url TEXT,
    bytes         BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);
