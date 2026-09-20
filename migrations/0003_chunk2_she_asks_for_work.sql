CREATE TABLE customer_profile (
    user_id          TEXT PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    display_name     TEXT,
    default_place_id TEXT REFERENCES place(id),
    is_restricted    BOOLEAN NOT NULL DEFAULT false,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE saved_place (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    label_code      TEXT NOT NULL REFERENCES place_label(code),
    place_id        TEXT NOT NULL REFERENCES place(id),
    landmark        TEXT,
    pin             postgis.geography(POINT, 4326),
    accuracy_meters DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, label_code, place_id, landmark)
);

CREATE TABLE request_question (
    id                     TEXT PRIMARY KEY,
    service_id             TEXT NOT NULL,
    template_version       INT NOT NULL,
    type                   TEXT NOT NULL CHECK (type IN
                             ('SINGLE_CHOICE','MULTI_CHOICE','NUMBER_WITH_UNIT')),
    label                  TEXT NOT NULL,
    required               BOOLEAN NOT NULL DEFAULT true,
    allow_not_sure         BOOLEAN NOT NULL DEFAULT false,
    unit_code              TEXT,
    unit_label             TEXT,
    unit_per_label         TEXT,
    min_value              NUMERIC,
    max_value              NUMERIC,
    step_value             NUMERIC,
    default_value          NUMERIC,
    depends_on_question_id TEXT REFERENCES request_question(id),
    depends_on_values      TEXT[],
    sort_order             INT NOT NULL DEFAULT 0
);
CREATE INDEX request_question_template_idx
    ON request_question (service_id, template_version, sort_order);

CREATE TABLE request_question_option (
    id          TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES request_question(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    service_id  TEXT REFERENCES service(id),
    icon_url    TEXT,
    sort_order  INT NOT NULL DEFAULT 0
);

CREATE TABLE request (
    id                   TEXT PRIMARY KEY,
    customer_id          TEXT NOT NULL REFERENCES app_user(id),
    geography_id         TEXT NOT NULL REFERENCES geography(id),
    service_id           TEXT NOT NULL REFERENCES service(id),
    work_type_id         TEXT,
    template_version     INT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN
                           ('REQUESTED','BOOKED','ARRIVED','COMPLETED','CANCELLED')),
    schedule_date        DATE NOT NULL,
    day_part             TEXT NOT NULL CHECK (day_part IN
                           ('MORNING','AFTERNOON','EVENING','ANY_TIME')),
    place_id             TEXT NOT NULL REFERENCES place(id),
    landmark             TEXT,
    pin                  postgis.geography(POINT, 4326),
    accuracy_meters      DOUBLE PRECISION,
    description          TEXT,
    origin_source        TEXT,
    preferred_partner_id TEXT REFERENCES app_user(id),
    ended_reason         TEXT CHECK (ended_reason IN
                           ('CANCELLED_BY_CUSTOMER','NO_PARTNER_AVAILABLE',
                            'NOT_BOOKED','PARTNER_FELL_THROUGH')),
    ended_at             TIMESTAMPTZ,
    is_rematching        BOOLEAN NOT NULL DEFAULT false,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX request_customer_state_idx ON request (customer_id, state, created_at DESC);
CREATE INDEX request_open_idx ON request (geography_id, service_id, schedule_date)
    WHERE state = 'REQUESTED';

CREATE TABLE request_answer (
    request_id  TEXT NOT NULL REFERENCES request(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    "values"    TEXT[] NOT NULL DEFAULT '{}',
    unit_code   TEXT,
    not_sure    BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (request_id, question_id)
);
