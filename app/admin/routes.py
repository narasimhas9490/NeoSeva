from pathlib import Path

from flask import Blueprint, current_app, send_from_directory

from app.core.errors import not_found

STATIC = Path(__file__).resolve().parent / "static"

bp = Blueprint("admin_pages", __name__, url_prefix="/admin")


@bp.get("")
@bp.get("/")
def index():
    """Serve the admin dashboard page.
    The page signs in and then drives everything through /admin/api.
    Hidden entirely when ADMIN_API_ENABLED is false."""
    if not current_app.config["NS"].admin_api_enabled:
        raise not_found()
    return send_from_directory(STATIC, "index.html")


@bp.get("/static/<path:name>")
def static_file(name):
    """Serve the dashboard's stylesheet and script.
    Only files inside the admin static folder are reachable.
    Hidden entirely when ADMIN_API_ENABLED is false."""
    if not current_app.config["NS"].admin_api_enabled:
        raise not_found()
    return send_from_directory(STATIC, name)
