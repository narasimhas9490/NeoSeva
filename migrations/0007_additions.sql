-- Additions the handover requires but gives no table for. Doc 0 tables above are verbatim.

ALTER TABLE geography ADD COLUMN boundary postgis.geography(MULTIPOLYGON, 4326);

ALTER TABLE request_question ADD COLUMN short_label TEXT;
ALTER TABLE request_question_option ADD COLUMN equipment_id TEXT;

ALTER TABLE request ADD COLUMN cancel_reason_code TEXT;
ALTER TABLE request ADD COLUMN cancel_note TEXT;

ALTER TABLE booking ADD COLUMN booked_partner_display_name TEXT;
ALTER TABLE booking ADD COLUMN arrival_accuracy_meters DOUBLE PRECISION;

CREATE TABLE request_media (
    request_id TEXT NOT NULL REFERENCES request(id) ON DELETE CASCADE,
    media_id   TEXT NOT NULL REFERENCES media(id),
    sort_order INT NOT NULL DEFAULT 0,
    PRIMARY KEY (request_id, media_id)
);

CREATE TABLE service_offer_unit (
    service_id TEXT NOT NULL REFERENCES service(id),
    code       TEXT NOT NULL,
    label      TEXT NOT NULL,
    per_label  TEXT,
    sort_order INT NOT NULL DEFAULT 0,
    PRIMARY KEY (service_id, code)
);

CREATE TABLE booking_pin_attempt (
    booking_id   TEXT PRIMARY KEY REFERENCES booking(id),
    failed_count SMALLINT NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ
);

CREATE TABLE display_text (
    key   TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    note  TEXT
);

CREATE TABLE platform_setting (
    id                              SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    matching_batch_size             SMALLINT NOT NULL,
    matching_batch_interval_minutes SMALLINT NOT NULL,
    otp_daily_limit                 SMALLINT NOT NULL,
    pin_max_attempts                SMALLINT NOT NULL,
    pin_lockout_seconds             INT NOT NULL,
    saved_place_upgrade_max_meters  INT NOT NULL,
    well_rated_min_reviews          INT,
    well_rated_min_positive_percent SMALLINT
);

CREATE INDEX offer_partner_idx ON offer (partner_id, created_at DESC);
CREATE INDEX opportunity_partner_idx ON opportunity_notification (partner_id, notified_at DESC);
