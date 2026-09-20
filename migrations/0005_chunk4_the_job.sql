CREATE TABLE booking (
    id                             TEXT PRIMARY KEY,
    request_id                     TEXT NOT NULL REFERENCES request(id),
    offer_id                       TEXT NOT NULL REFERENCES offer(id),
    customer_id                    TEXT NOT NULL REFERENCES app_user(id),
    partner_id                     TEXT NOT NULL REFERENCES partner_profile(user_id),
    state                          TEXT NOT NULL CHECK (state IN
                                     ('BOOKED','ARRIVED','COMPLETED','CANCELLED')),

    booked_pricing_type            TEXT NOT NULL,
    booked_exact_amount_minor      BIGINT,
    booked_rate_minor              BIGINT,
    booked_unit_code               TEXT,
    booked_inspection_charge_minor BIGINT,
    booked_cost_minor              BIGINT,

    schedule_date                  DATE NOT NULL,
    day_part                       TEXT NOT NULL,
    arrival_expected_by            TIMESTAMPTZ,

    final_amount_minor             BIGINT,
    final_amount_at                TIMESTAMPTZ,
    final_amount_agreed            BOOLEAN NOT NULL DEFAULT false,
    final_amount_agreed_at         TIMESTAMPTZ,

    completion_pin                 TEXT NOT NULL,
    on_my_way_at                   TIMESTAMPTZ,
    arrived_at                     TIMESTAMPTZ,
    arrival_point                  postgis.geography(POINT, 4326),
    completed_at                   TIMESTAMPTZ,
    completed_by                   TEXT CHECK (completed_by IN ('PARTNER_PIN','CUSTOMER')),

    cancelled_at                   TIMESTAMPTZ,
    cancelled_by                   TEXT CHECK (cancelled_by IN ('CUSTOMER','PARTNER','SYSTEM')),
    cancel_reason_code             TEXT,
    cancel_note                    TEXT,
    partner_fell_through           BOOLEAN NOT NULL DEFAULT false,

    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX booking_one_live_per_request
    ON booking (request_id) WHERE state IN ('BOOKED','ARRIVED');

CREATE INDEX booking_partner_state_idx ON booking (partner_id, state);
CREATE INDEX booking_customer_idx ON booking (customer_id, created_at DESC);
