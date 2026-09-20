CREATE TABLE admin_user (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE admin_session (
    id            TEXT PRIMARY KEY,
    admin_user_id TEXT NOT NULL REFERENCES admin_user(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ
);

INSERT INTO app_user (id, phone_e164, language) VALUES ('usr_system_admin', '+0000000000', 'en')
    ON CONFLICT (id) DO NOTHING;
