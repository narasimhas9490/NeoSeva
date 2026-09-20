import pytest

from app.config import Config
from app.core.adapters.storage import SupabaseStorage, storage


@pytest.fixture
def sb():
    """Build a Supabase backend whose HTTP calls are recorded, not sent.
    Every call answers 200 with an empty body unless a test changes the reply."""
    cfg = Config()
    cfg.storage_backend = "supabase"
    cfg.supabase_url = "https://proj.supabase.co/"
    cfg.supabase_service_role_key = "sb_secret_x"
    store = storage(cfg)
    store.calls, store.reply = [], (200, b"")
    SupabaseStorage._ensured.clear()

    def fake(method, path, data=None, content_type="application/json"):
        store.calls.append((method, path, content_type))
        return store.reply

    store._call = fake
    return store


def test_public_purpose_goes_to_the_public_bucket_and_returns_its_url(sb):
    """A service icon is uploaded to the public bucket with its image type.
    The returned URL is the bucket's public object URL.
    The bucket is created once, before the first upload."""
    url = sb.save("SERVICE_ICON", "service_icon/med_1.png", b"x")
    assert url == "https://proj.supabase.co/storage/v1/object/public/neoseva-public/service_icon/med_1.png"
    assert sb.calls[0][:2] == ("POST", "bucket")
    assert sb.calls[1] == ("POST", "object/neoseva-public/service_icon/med_1.png", "image/png")
    sb.save("SERVICE_ICON", "service_icon/med_2.png", b"x")
    assert [c[1] for c in sb.calls].count("bucket") == 1


def test_identity_images_are_private_and_have_no_url(sb):
    """Identity images go to the private bucket and never get a public URL.
    Reading them back uses the authenticated endpoint."""
    assert sb.save("IDENTITY_SELFIE", "identity_selfie/med_1.jpg", b"x") is None
    assert sb.calls[-1][1] == "object/neoseva-private/identity_selfie/med_1.jpg"
    sb.reply = (200, b"bytes")
    assert sb.read("IDENTITY_SELFIE", "identity_selfie/med_1.jpg") == b"bytes"
    assert sb.calls[-1][1] == "object/authenticated/neoseva-private/identity_selfie/med_1.jpg"


def test_refusals_are_oserrors_and_missing_files_are_quiet(sb):
    """A rejected upload raises OSError, which the upload route turns into MEDIA_UPLOAD_FAILED.
    Deleting or reading a file that is gone is not an error."""
    sb.save("SERVICE_ICON", "service_icon/a.png", b"x")
    sb.reply = (403, b"denied")
    with pytest.raises(OSError):
        sb.save("SERVICE_ICON", "service_icon/b.png", b"x")
    sb.reply = (404, b"")
    sb.delete("SERVICE_ICON", "service_icon/a.png")
    assert sb.read("SERVICE_ICON", "service_icon/a.png") is None


def test_backend_refuses_to_start_without_credentials():
    """Choosing supabase without the URL and key is a clear startup error.
    It never falls back to disk silently."""
    cfg = Config()
    cfg.storage_backend, cfg.supabase_url, cfg.supabase_service_role_key = "supabase", "", ""
    with pytest.raises(RuntimeError):
        storage(cfg)
