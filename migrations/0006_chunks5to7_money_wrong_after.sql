CREATE TABLE credit_ledger_transaction (
    id           TEXT PRIMARY KEY,
    partner_id   TEXT NOT NULL REFERENCES partner_profile(user_id),
    type         TEXT NOT NULL CHECK (type IN
                   ('TOP_UP','JOB_FEE','SYSTEM_RESTORE','REFERRAL_REWARD',
                    'PROMO','MANUAL_ADJUSTMENT')),
    amount_minor BIGINT NOT NULL,
    booking_id   TEXT REFERENCES booking(id),
    job_label    TEXT,
    note         TEXT,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX credit_partner_time_idx
    ON credit_ledger_transaction (partner_id, created_at DESC);

CREATE UNIQUE INDEX credit_one_job_fee_per_booking
    ON credit_ledger_transaction (booking_id) WHERE type = 'JOB_FEE';

CREATE VIEW partner_balance AS
SELECT partner_id, COALESCE(SUM(amount_minor), 0) AS available_minor
FROM credit_ledger_transaction
GROUP BY partner_id;

CREATE TABLE job_report (
    id          TEXT PRIMARY KEY,
    booking_id  TEXT NOT NULL REFERENCES booking(id),
    reported_by TEXT NOT NULL CHECK (reported_by IN ('CUSTOMER','PARTNER')),
    context     TEXT NOT NULL CHECK (context IN
                  ('CUSTOMER_REPORT_ISSUE','PARTNER_REPORT_ISSUE','PARTNER_DISPUTE_NO_SHOW')),
    reason_code TEXT,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX job_report_booking_idx ON job_report (booking_id);

CREATE INDEX booking_partner_cancelled_idx
    ON booking (partner_id, cancelled_at)
    WHERE cancelled_by = 'PARTNER';

CREATE TABLE review (
    booking_id  TEXT PRIMARY KEY REFERENCES booking(id),
    customer_id TEXT NOT NULL REFERENCES app_user(id),
    partner_id  TEXT NOT NULL REFERENCES partner_profile(user_id),
    verdict     TEXT NOT NULL CHECK (verdict IN ('NOT_GOOD','OKAY','VERY_GOOD')),
    tag_codes   TEXT[] NOT NULL DEFAULT '{}',
    comment     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX review_partner_idx ON review (partner_id);

CREATE TABLE saved_partner (
    customer_id TEXT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    partner_id  TEXT NOT NULL REFERENCES partner_profile(user_id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, partner_id)
);

CREATE TABLE referral (
    id                 TEXT PRIMARY KEY,
    referrer_user_id   TEXT NOT NULL REFERENCES app_user(id),
    referred_user_id   TEXT REFERENCES app_user(id),
    role               TEXT NOT NULL CHECK (role IN ('CUSTOMER','PARTNER')),
    code               TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('SENT','JOINED','QUALIFIED','REWARDED')),
    qualifying_booking_id TEXT REFERENCES booking(id),
    rewarded_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (referred_user_id)
);

CREATE TABLE complaint (
    id          TEXT PRIMARY KEY,
    raised_by   TEXT NOT NULL REFERENCES app_user(id),
    booking_id  TEXT REFERENCES booking(id),
    subject     TEXT,
    body        TEXT NOT NULL,
    media_ids   TEXT[] NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL CHECK (status IN ('OPEN','IN_REVIEW','RESOLVED')),
    resolution  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
