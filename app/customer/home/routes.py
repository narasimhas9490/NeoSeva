from flask import Blueprint, g

from app.core.auth import require_user
from app.core.db import tx
from app.core.envelope import ok
from app.customer.requests.service import active_request

bp = Blueprint("customer_home", __name__, url_prefix="/customer")


@bp.get("/home")
@require_user("CUSTOMER")
def home():
    """Return everything her home screen draws, in one call.
    Her one running request in full, or null.
    Saved partners stay empty until chunk 7 is built."""
    with tx() as conn:
        return ok({"activeRequest": active_request(conn, g.user_id), "savedPartners": [], "savedPartnersTotal": 0})
