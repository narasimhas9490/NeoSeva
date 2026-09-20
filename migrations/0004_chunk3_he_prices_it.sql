CREATE TABLE partner_profile (
    user_id                TEXT PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    display_name           TEXT NOT NULL,
    profile_photo_media_id TEXT REFERENCES media(id),
    base_place_id          TEXT REFERENCES place(id),
    experience_range_code  TEXT REFERENCES experience_range(code),
    status                 TEXT NOT NULL CHECK (status IN
                             ('REGISTERED','ACTIVATED','ACTIVE','SUSPENDED')),
    status_note            TEXT,
    accepting_new_jobs     BOOLEAN NOT NULL DEFAULT true,
    travel_radius_km       INT,
    next_step              TEXT NOT NULL CHECK (next_step IN
                             ('BASIC_PROFILE','SERVICES','TRAVEL_RADIUS','NOTIFICATIONS','COMPLETE')),
    accepted_terms_version TEXT,
    accepted_terms_at      TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX partner_matchable_idx ON partner_profile (base_place_id)
    WHERE status = 'ACTIVE' AND accepting_new_jobs = true;

CREATE TABLE partner_service (
    partner_id    TEXT NOT NULL REFERENCES partner_profile(user_id) ON DELETE CASCADE,
    service_id    TEXT NOT NULL REFERENCES service(id),
    equipment_ids TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (partner_id, service_id)
);
CREATE INDEX partner_service_service_idx ON partner_service (service_id);

CREATE TABLE identity_verification (
    partner_id        TEXT PRIMARY KEY REFERENCES partner_profile(user_id) ON DELETE CASCADE,
    status            TEXT NOT NULL CHECK (status IN
                        ('PENDING','VERIFIED','REJECTED','NEEDS_ACTION')),
    document_type     TEXT CHECK (document_type IN
                        ('MASKED_AADHAAR','VOTER_ID','DRIVING_LICENCE','PASSPORT')),
    document_media_id TEXT REFERENCES media(id),
    selfie_media_id   TEXT REFERENCES media(id),
    submitted_at      TIMESTAMPTZ,
    reviewed_at       TIMESTAMPTZ,
    reviewed_by       TEXT,
    review_note       TEXT
);

CREATE TABLE offer (
    id                      TEXT PRIMARY KEY,
    request_id              TEXT NOT NULL REFERENCES request(id) ON DELETE CASCADE,
    partner_id              TEXT NOT NULL REFERENCES partner_profile(user_id),
    status                  TEXT NOT NULL CHECK (status IN
                              ('ACTIVE','BOOKED','WITHDRAWN','NOT_SELECTED','CLOSED')),
    pricing_type            TEXT NOT NULL CHECK (pricing_type IN
                              ('FIXED','UNIT_RATE','INSPECTION')),
    exact_amount_minor      BIGINT,
    rate_minor              BIGINT,
    unit_code               TEXT,
    inspection_charge_minor BIGINT,
    comparable_cost_minor   BIGINT,
    offered_day_part        TEXT CHECK (offered_day_part IN ('MORNING','AFTERNOON','EVENING')),
    note                    TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    withdrawn_at            TIMESTAMPTZ,
    UNIQUE (request_id, partner_id),
    CONSTRAINT offer_pricing_shape CHECK (
        (pricing_type = 'FIXED'
            AND exact_amount_minor IS NOT NULL
            AND rate_minor IS NULL AND unit_code IS NULL
            AND inspection_charge_minor IS NULL)
     OR (pricing_type = 'UNIT_RATE'
            AND rate_minor IS NOT NULL AND unit_code IS NOT NULL
            AND exact_amount_minor IS NULL AND inspection_charge_minor IS NULL)
     OR (pricing_type = 'INSPECTION'
            AND inspection_charge_minor IS NOT NULL
            AND exact_amount_minor IS NULL AND rate_minor IS NULL AND unit_code IS NULL)
    )
);
CREATE INDEX offer_request_idx ON offer (request_id) WHERE status = 'ACTIVE';

CREATE TABLE opportunity_notification (
    request_id  TEXT NOT NULL REFERENCES request(id) ON DELETE CASCADE,
    partner_id  TEXT NOT NULL REFERENCES partner_profile(user_id),
    batch       SMALLINT NOT NULL,
    notified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    declined_at TIMESTAMPTZ,
    PRIMARY KEY (request_id, partner_id)
);
