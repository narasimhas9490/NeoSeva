-- Admins upload service icons through the console.
-- An admin is not an app_user, so a picture may be owned by an admin name instead.
ALTER TABLE media DROP CONSTRAINT media_purpose_check;
ALTER TABLE media ADD CONSTRAINT media_purpose_check CHECK (purpose IN (
    'REQUEST_PHOTO','PARTNER_PROFILE_PHOTO','IDENTITY_DOCUMENT',
    'IDENTITY_SELFIE','COMPLAINT_EVIDENCE','SERVICE_ICON'));

ALTER TABLE media ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE media ADD COLUMN uploaded_by_admin TEXT;
ALTER TABLE media ADD CONSTRAINT media_has_owner CHECK (user_id IS NOT NULL OR uploaded_by_admin IS NOT NULL);
