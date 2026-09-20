from flask import Blueprint, g, request

from app.core.adapters.push import notify_user
from app.core.auth import require_user
from app.core.db import tx
from app.core.envelope import body, created, no_content, ok
from app.core.idempotency import idempotent
from app.partner.offers.service import sent_offer
from app.partner.opportunities import service

bp = Blueprint("partner_opportunities", __name__, url_prefix="/partner/opportunities")


@bp.get("")
@require_user("PARTNER")
def list_opportunities():
    """List the work near him that is still open.
    Approximate area only; nothing that locates her.
    Cursor-paged, newest first."""
    with tx() as conn:
        items, next_cursor = service.list_open(conn, g.user_id, request.args.get("cursor"))
    return ok(items, nextCursor=next_cursor)


@bp.get("/<request_id>")
@require_user("PARTNER")
def get_opportunity(request_id):
    """Read one piece of work.
    The same privacy-safe shape as the list.
    404 for work he was never told about."""
    with tx() as conn:
        return ok(service.read_one(conn, g.user_id, request_id))


@bp.post("/<request_id>/offers")
@require_user("PARTNER")
@idempotent
def send_offer(request_id):
    """Send a price for a piece of work.
    One offer per partner per request, in one of three pricing types.
    Returns 201 with the sent-offer shape."""
    offer_id, customer_id = service.send_offer(g.user_id, request_id, body())
    notify_user(customer_id, "CUSTOMER", "NEW_OFFER", {"requestId": request_id, "offerId": offer_id})
    with tx() as conn:
        return created(sent_offer(conn, g.user_id, offer_id))


@bp.post("/<request_id>/decline")
@require_user("PARTNER")
def decline(request_id):
    """Say he is not interested in a piece of work.
    Free and without consequence.
    Returns 204."""
    service.decline(g.user_id, request_id)
    return no_content()
