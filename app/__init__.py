import json
import logging
import time

from flask import Flask, g, jsonify, make_response, request
from sqlalchemy import text
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from app.config import Config
from app.core import db
from app.core.envelope import error_body, ok, request_id
from app.core.errors import ApiError
from app.core.runtime import Runtime

log = logging.getLogger("neoseva")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        """Write one log record as a single JSON line.
        Used when LOG_JSON is true, for log shippers.
        Exceptions are included as text."""
        payload = {"level": record.levelname, "logger": record.name, "message": record.getMessage(), "time": self.formatTime(record)}
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(cfg):
    """Set up logging from LOG_LEVEL and LOG_JSON.
    Request bodies are never logged, so PINs and codes cannot leak.
    Called once when the app is created."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if cfg.log_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(cfg.log_level.upper())


def register_blueprints(app):
    """Attach every group of endpoints to the app.
    Common endpoints first, then customer, partner and admin.
    Customer and partner logic live in their own packages."""
    from app.admin.api.routes import bp as admin_api
    from app.admin.routes import bp as admin_pages
    from app.common.auth.routes import bp as auth
    from app.common.catalog.routes import bp as catalog
    from app.common.devices.routes import bp as devices
    from app.common.locations.routes import bp as locations
    from app.common.media.routes import bp as media
    from app.customer.bookings.routes import bp as customer_bookings
    from app.customer.home.routes import bp as customer_home
    from app.customer.offers.routes import bp as customer_offers
    from app.customer.places.routes import bp as customer_places
    from app.customer.profile.routes import bp as customer_profile
    from app.customer.requests.routes import bp as customer_requests
    from app.partner.home.routes import bp as partner_home
    from app.partner.identity.routes import bp as partner_identity
    from app.partner.jobs.routes import bp as partner_jobs
    from app.partner.offers.routes import bp as partner_offers
    from app.partner.opportunities.routes import bp as partner_opportunities
    from app.partner.profile.routes import bp as partner_profile

    for blueprint in (
        catalog, auth, locations, media, devices,
        customer_profile, customer_home, customer_places, customer_requests, customer_offers, customer_bookings,
        partner_profile, partner_identity, partner_home, partner_opportunities, partner_offers, partner_jobs,
        admin_api, admin_pages,
    ):
        app.register_blueprint(blueprint)


def register_errors(app):
    """Turn every failure into the error envelope.
    ApiError carries its own code; HTTP errors map to generic codes.
    Anything unexpected is a logged 500 that never leaks internals."""

    @app.errorhandler(ApiError)
    def api_error(exc):
        """Render an ApiError as its status and code.
        details carries structured extras such as bookingId.
        message is for developers, never shown to a user."""
        return make_response(jsonify(error_body(exc.code, exc.message, exc.details)), exc.status)

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(exc):
        """Render an oversized upload as MEDIA_TOO_LARGE.
        The limit is stated so a partner knows what to do.
        Raised by Werkzeug before the view runs."""
        limit = app.config["NS"].media_max_bytes
        return make_response(jsonify(error_body("MEDIA_TOO_LARGE", "The upload is too large.", {"maxBytes": limit})), 413)

    @app.errorhandler(HTTPException)
    def http_error(exc):
        """Render a routing or method error in the envelope.
        Unknown paths are NOT_FOUND, wrong methods METHOD_NOT_ALLOWED.
        The status is kept."""
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.code, "HTTP_ERROR")
        return make_response(jsonify(error_body(code, exc.description or code)), exc.code)

    @app.errorhandler(Exception)
    def unexpected(exc):
        """Render any unexpected failure as a 500.
        The exception is logged with the request id for support.
        The body never carries the exception text."""
        log.exception("unhandled error in %s %s (%s)", request.method, request.path, request_id())
        return make_response(jsonify(error_body("INTERNAL_ERROR", "Something went wrong on our side.")), 500)


def register_hooks(app):
    """Add request timing, access logging and CORS headers.
    The access log records method, path, status and time only.
    CORS origins come from CORS_ORIGINS."""
    cfg = app.config["NS"]

    @app.before_request
    def start_timer():
        """Remember when the request started.
        Used for the duration in the access log.
        Also creates the request id early."""
        g.started = time.perf_counter()
        request_id()
        app.extensions["runtime"].refresh()

    @app.after_request
    def finish(response):
        """Add CORS and request-id headers and log the request.
        Bodies are never logged, so PINs never reach the logs.
        Returns the response unchanged otherwise."""
        origin = request.headers.get("Origin")
        if origin and ("*" in cfg.cors_origins or origin in cfg.cors_origins):
            response.headers["Access-Control-Allow-Origin"] = "*" if "*" in cfg.cors_origins else origin
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Idempotency-Key, If-None-Match, X-App, X-Admin-Key"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            response.headers["Access-Control-Expose-Headers"] = "ETag, X-Request-Id"
        response.headers["X-Request-Id"] = request_id()
        elapsed = (time.perf_counter() - getattr(g, "started", time.perf_counter())) * 1000
        log.info("%s %s %s %.0fms", request.method, request.path, response.status_code, elapsed)
        return response


def create_app(config=None, database_url=None, start_scheduler=True):
    """Build the Flask application.
    Configuration comes from the environment unless a Config is passed.
    Tests pass their own database URL and turn the scheduler off."""
    cfg = config or Config()
    configure_logging(cfg)
    app = Flask(__name__, static_folder=None)
    app.config["NS"] = cfg
    app.config["SECRET_KEY"] = cfg.secret_key
    app.config["MAX_CONTENT_LENGTH"] = cfg.media_max_bytes + 1024 * 1024
    app.json.sort_keys = False
    db.init_engine(cfg, database_url)
    log.info("database: %s", db.engine().url.host)
    app.extensions["runtime"] = Runtime(cfg)
    register_blueprints(app)
    register_errors(app)
    register_hooks(app)

    @app.get("/health")
    def health():
        """Report whether the service and its database are up.
        The database check fails fast via DB_CONNECT_TIMEOUT.
        503 when the database cannot be reached."""
        try:
            with db.engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            return make_response(jsonify(error_body("DATABASE_UNAVAILABLE", "The database is unreachable.")), 503)
        return ok(
            {
                "status": "ok",
                "env": cfg.app_env,
                "authEnabled": cfg.auth_enabled,
                "smsEnabled": cfg.sms_enabled,
                "smsProvider": cfg.sms_provider if cfg.sms_enabled else "console",
                "schedulerEnabled": cfg.scheduler_enabled,
            }
        )

    if start_scheduler:
        from app.jobs.scheduler import start

        start(app)
    return app
