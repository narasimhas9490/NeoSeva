-- Corrections to chunks 1-4, handover 2026-09-20.

-- Item 11 / chunk 8 groundwork: display_text becomes bilingual, one row per
-- (key, language) instead of one row per key. Every existing key gets a
-- Telugu row seeded as a copy of its English text; the founder edits the
-- real Telugu wording in the console afterwards (already editable there).
ALTER TABLE display_text ADD COLUMN language TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('te','en'));
ALTER TABLE display_text DROP CONSTRAINT display_text_pkey;
INSERT INTO display_text (key, label, note, language)
    SELECT key, label, note, 'te' FROM display_text;
ALTER TABLE display_text ADD PRIMARY KEY (key, language);

-- Item 6: push language is per installation, since one person may read the
-- two apps in different languages. Nullable: a device registered before this
-- migration falls back to the person's own language until it registers again.
ALTER TABLE device ADD COLUMN language TEXT CHECK (language IN ('te','en'));

-- Item 10: a number question may carry presets, common amounts she taps
-- straight to. Empty/null means none are offered.
ALTER TABLE request_question ADD COLUMN presets INTEGER[];

-- Item 4: wrong completion codes grow the wait and are counted forever.
-- failed_count still drives the current lockout cycle and keeps resetting to
-- zero at each lockout; total_wrong_count and lockout_count never do.
ALTER TABLE booking_pin_attempt ADD COLUMN total_wrong_count INT NOT NULL DEFAULT 0;
ALTER TABLE booking_pin_attempt ADD COLUMN lockout_count INT NOT NULL DEFAULT 0;
ALTER TABLE platform_setting ADD COLUMN pin_lockout_backoff_seconds INTEGER[] NOT NULL DEFAULT '{60,120,300,600}';

-- Item 1: the OTP daily cap is removed (the growing resend backoff is the
-- protection). The column stays, unread, for whoever looks at old rows;
-- it just stops being required so a fresh seed can leave it out.
ALTER TABLE platform_setting ALTER COLUMN otp_daily_limit DROP NOT NULL;

-- Item 5: no evening. Today closes at four, not six. Any pre-launch demo/test
-- data already sitting on EVENING moves to AFTERNOON before the column and
-- the allowed value are dropped; there is no such migration for a live
-- production database and none has been applied there yet.
UPDATE request SET day_part = 'AFTERNOON' WHERE day_part = 'EVENING';
ALTER TABLE request DROP CONSTRAINT IF EXISTS request_day_part_check;
ALTER TABLE request ADD CONSTRAINT request_day_part_check CHECK (day_part IN ('MORNING','AFTERNOON','ANY_TIME'));
ALTER TABLE geography DROP COLUMN evening_ends_hour;
DELETE FROM display_text WHERE key = 'DAYPART_EVENING';
