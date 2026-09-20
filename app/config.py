import os
from dataclasses import dataclass, field, fields

from dotenv import load_dotenv

load_dotenv()

BOOL, INT, STR, LIST, INT_LIST = "bool", "int", "str", "list", "int_list"


def parse(kind, raw):
    """Convert a raw text setting into its typed value.
    Booleans accept true/false, yes/no, on/off and 1/0; lists are comma separated.
    Raises ValueError when the text cannot be read as that kind."""
    text = "" if raw is None else str(raw).strip()
    if kind == BOOL:
        if text.lower() in ("1", "true", "yes", "on"):
            return True
        if text.lower() in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"not a boolean: {raw!r}")
    if kind == INT:
        return int(text)
    if kind == LIST:
        return [item.strip() for item in text.split(",") if item.strip()]
    if kind == INT_LIST:
        return [int(item.strip()) for item in text.split(",") if item.strip()]
    return text


def render(kind, value):
    """Turn a typed setting back into the text form used in .env.
    Lists are joined with commas and booleans become true/false.
    parse(kind, render(kind, v)) returns v."""
    if kind == BOOL:
        return "true" if value else "false"
    if kind in (LIST, INT_LIST):
        return ",".join(str(v) for v in value)
    return "" if value is None else str(value)


def setting(env, default, kind=STR, group="Application", help="", secret=False, restart=False, editable=True):
    """Declare one configuration value read from the environment.
    The metadata drives the admin Runtime config page.
    restart marks values that only take effect when the server starts."""

    def load():
        """Read this setting from the environment, or use the default.
        A blank value counts as missing.
        Called when a Config is created."""
        raw = os.getenv(env)
        return default if raw is None or raw.strip() == "" else parse(kind, raw)

    meta = {"env": env, "kind": kind, "group": group, "help": help, "secret": secret, "restart": restart, "editable": editable}
    return field(default_factory=load, metadata=meta)


@dataclass
class Config:
    app_env: str = setting("APP_ENV", "development", help="development or production; production refuses unsafe switches", restart=True, editable=False)
    debug: bool = setting("DEBUG", False, BOOL, help="Flask debug mode", restart=True)
    secret_key: str = setting("SECRET_KEY", "dev", secret=True, restart=True, editable=False)
    log_level: str = setting("LOG_LEVEL", "INFO", help="DEBUG, INFO, WARNING or ERROR")
    log_json: bool = setting("LOG_JSON", False, BOOL, help="Write logs as JSON lines", restart=True)
    cors_origins: list = setting("CORS_ORIGINS", ["*"], LIST, help="Comma separated origins, or *")

    db_target: str = setting("DB_TARGET", "local", group="Database", restart=True, editable=False, help="local or supabase: which database the app, migrate and seed use")
    database_url: str = setting("DATABASE_URL", "", group="Database", secret=True, restart=True, editable=False, help="The local database")
    supabase_database_url: str = setting("SUPABASE_DATABASE_URL", "", group="Database", secret=True, restart=True, editable=False, help="Supabase Postgres, session pooler, postgresql+psycopg://")
    test_database_url: str = setting("TEST_DATABASE_URL", "", group="Database", secret=True, restart=True, editable=False)
    sqlalchemy_echo: bool = setting("SQLALCHEMY_ECHO", False, BOOL, group="Database", restart=True)
    db_pool_size: int = setting("DB_POOL_SIZE", 5, INT, group="Database", restart=True)
    db_max_overflow: int = setting("DB_MAX_OVERFLOW", 10, INT, group="Database", restart=True)
    db_pool_recycle: int = setting("DB_POOL_RECYCLE", 1800, INT, group="Database", restart=True)
    db_connect_timeout: int = setting("DB_CONNECT_TIMEOUT", 5, INT, group="Database", restart=True)

    auth_enabled: bool = setting("AUTH_ENABLED", True, BOOL, group="Auth", help="Off: protected endpoints skip tokens and act as the dev user. Refused in production.")
    dev_fake_user_id: str = setting("DEV_FAKE_USER_ID", "usr_dev_local", group="Auth", help="Who the apps act as while auth is off")
    dev_fake_user_phone: str = setting("DEV_FAKE_USER_PHONE", "+919999999999", group="Auth")

    jwt_secret_key: str = setting("JWT_SECRET_KEY", "dev", group="JWT", help="Changing it signs everybody out", secret=True)
    jwt_algorithm: str = setting("JWT_ALGORITHM", "HS256", group="JWT")
    jwt_issuer: str = setting("JWT_ISSUER", "neoseva", group="JWT")
    jwt_audience: str = setting("JWT_AUDIENCE", "neoseva-apps", group="JWT")
    jwt_access_ttl_seconds: int = setting("JWT_ACCESS_TTL_SECONDS", 3600, INT, group="JWT")
    jwt_refresh_ttl_days: int = setting("JWT_REFRESH_TTL_DAYS", 90, INT, group="JWT")
    jwt_leeway_seconds: int = setting("JWT_LEEWAY_SECONDS", 30, INT, group="JWT")
    jwt_rotate_refresh: bool = setting("JWT_ROTATE_REFRESH", True, BOOL, group="JWT", help="Issue a new refresh token on every refresh")

    sms_enabled: bool = setting("SMS_ENABLED", False, BOOL, group="SMS", help="Off: codes go to the server log, never to a phone")
    sms_provider: str = setting("SMS_PROVIDER", "console", group="SMS", help="console or twofactor")
    sms_fixed_otp: str = setting("SMS_FIXED_OTP", "", group="SMS", help="Used for every code while SMS is off. Refused in production.", secret=True)
    sms_log_otp: bool = setting("SMS_LOG_OTP", False, BOOL, group="SMS", help="Print codes in the log while SMS is off")
    sms_fail_silently: bool = setting("SMS_FAIL_SILENTLY", False, BOOL, group="SMS", help="Issue the code even when the vendor fails")

    twofactor_api_key: str = setting("TWOFACTOR_API_KEY", "", group="2Factor.in", secret=True)
    twofactor_base_url: str = setting("TWOFACTOR_BASE_URL", "https://2factor.in/API/V1", group="2Factor.in")
    twofactor_sender_id: str = setting("TWOFACTOR_SENDER_ID", "", group="2Factor.in")
    twofactor_template_login_en: str = setting("TWOFACTOR_TEMPLATE_LOGIN_EN", "", group="2Factor.in")
    twofactor_template_login_te: str = setting("TWOFACTOR_TEMPLATE_LOGIN_TE", "", group="2Factor.in")
    twofactor_timeout_seconds: int = setting("TWOFACTOR_TIMEOUT_SECONDS", 10, INT, group="2Factor.in")

    otp_length: int = setting("OTP_LENGTH", 6, INT, group="OTP")
    otp_ttl_seconds: int = setting("OTP_TTL_SECONDS", 300, INT, group="OTP")
    otp_max_attempts: int = setting("OTP_MAX_ATTEMPTS", 5, INT, group="OTP")
    otp_resend_backoff_seconds: list = setting("OTP_RESEND_BACKOFF_SECONDS", [30, 60, 120, 300], INT_LIST, group="OTP", help="Wait before each resend, in seconds")
    otp_resend_window_hours: int = setting("OTP_RESEND_WINDOW_HOURS", 1, INT, group="OTP")

    storage_backend: str = setting("STORAGE_BACKEND", "local", group="Media", restart=True, editable=False)
    media_max_bytes: int = setting("MEDIA_MAX_BYTES", 5 * 1024 * 1024, INT, group="Media")
    media_allowed_content_types: list = setting("MEDIA_ALLOWED_CONTENT_TYPES", ["image/jpeg", "image/png"], LIST, group="Media")
    media_public_root: str = setting("MEDIA_PUBLIC_ROOT", "var/media/public", group="Media", help="Profile and request photos")
    media_private_root: str = setting("MEDIA_PRIVATE_ROOT", "var/media/private", group="Media", help="Identity images; never served")
    media_public_base_url: str = setting("MEDIA_PUBLIC_BASE_URL", "http://localhost:5000/media", group="Media", help="Prefix of public image URLs (local storage only)")
    supabase_url: str = setting("SUPABASE_URL", "", group="Media", restart=True, editable=False, help="Project URL, used when STORAGE_BACKEND=supabase")
    supabase_service_role_key: str = setting("SUPABASE_SERVICE_ROLE_KEY", "", group="Media", secret=True, restart=True, editable=False)
    supabase_public_bucket: str = setting("SUPABASE_PUBLIC_BUCKET", "neoseva-public", group="Media", restart=True, editable=False, help="Public bucket: service icons, profile and request photos")
    supabase_private_bucket: str = setting("SUPABASE_PRIVATE_BUCKET", "neoseva-private", group="Media", restart=True, editable=False, help="Private bucket: identity images")

    catalog_etag_enabled: bool = setting("CATALOG_ETAG_ENABLED", True, BOOL, group="Catalog")
    catalog_cache_seconds: int = setting("CATALOG_CACHE_SECONDS", 0, INT, group="Catalog")

    admin_api_enabled: bool = setting("ADMIN_API_ENABLED", True, BOOL, group="Admin", help="Only changeable in .env, so the console cannot lock itself out", editable=False)
    admin_api_key: str = setting("ADMIN_API_KEY", "", group="Admin", secret=True)
    admin_bootstrap_username: str = setting("ADMIN_BOOTSTRAP_USERNAME", "admin", group="Admin", restart=True, editable=False)
    admin_bootstrap_password: str = setting("ADMIN_BOOTSTRAP_PASSWORD", "", group="Admin", secret=True, restart=True, editable=False)
    admin_session_hours: int = setting("ADMIN_SESSION_HOURS", 12, INT, group="Admin")

    scheduler_enabled: bool = setting("SCHEDULER_ENABLED", True, BOOL, group="Background jobs", help="Off: matching batches and expiry stop")
    scheduler_tick_seconds: int = setting("SCHEDULER_TICK_SECONDS", 60, INT, group="Background jobs", restart=True)

    default_area_id: str = setting("DEFAULT_AREA_ID", "geo_podalakur", group="Area defaults")
    default_timezone: str = setting("DEFAULT_TIMEZONE", "Asia/Kolkata", group="Area defaults")
    default_language: str = setting("DEFAULT_LANGUAGE", "en", group="Area defaults", help="te or en")
    supported_languages: list = setting("SUPPORTED_LANGUAGES", ["en", "te"], LIST, group="Area defaults")

    def url_for(self, target=None):
        """Return the database URL for local or supabase; None means DB_TARGET.
        A target with no URL configured is a ValueError naming the setting to fill in.
        Anything other than local or supabase is refused."""
        target = target or self.db_target
        if target not in ("local", "supabase"):
            raise ValueError(f"DB_TARGET must be local or supabase, not {target!r}")
        url = self.supabase_database_url if target == "supabase" else self.database_url
        if not url:
            raise ValueError(f"{'SUPABASE_DATABASE_URL' if target == 'supabase' else 'DATABASE_URL'} is not set")
        return url


FIELDS = {f.metadata["env"]: f for f in fields(Config)}


def language(value, cfg):
    """Pick a supported language code, falling back to the default.
    Only te and en are ever stored, whatever the list says.
    Used by sign-in, devices and partner profiles."""
    allowed = [code for code in cfg.supported_languages if code in ("te", "en")] or ["en"]
    if value in allowed:
        return value
    return cfg.default_language if cfg.default_language in allowed else allowed[0]
