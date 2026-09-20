from flask import Blueprint, g

from app.core import clock
from app.core.auth import require_user
from app.core.db import one, run, tx
from app.core.envelope import ok
from app.core.errors import conflict, not_found
from app.core.idempotency import idempotent
from app.partner.offers.service import sent_offer, sent_offers

bp = Blueprint("partner_offers", __name__, url_prefix="/partner/offers")


@bp.get("")
@require_user("PARTNER")
def list_offers():
    """List every price he has sent.
    Each row names its job through requestContext.
    NOT_SELECTED and CLOSED stay distinct."""
    with tx() as conn:
        return ok(sent_offers(conn, g.user_id))


@bp.delete("/<offer_id>")
@require_user("PARTNER")
@idempotent
def withdraw(offer_id):
    """Take back a price she has not acted on.
    If she booked him first, the booking stands and he gets OFFER_ALREADY_BOOKED.
    Withdrawing twice returns the withdrawn offer."""
    with tx() as conn:
        row = one(conn, "SELECT * FROM offer WHERE id = :id AND partner_id = :p FOR UPDATE", id=offer_id, p=g.user_id)
        if row is None:
            raise not_found()
        if row["status"] == "BOOKED":
            raise conflict("OFFER_ALREADY_BOOKED", "She booked you for this job.")
        if row["status"] == "ACTIVE":
            run(
                conn,
                "UPDATE offer SET status = 'WITHDRAWN', withdrawn_at = :now WHERE id = :id",
                now=clock.now_utc(),
                id=offer_id,
            )
        elif row["status"] != "WITHDRAWN":
            raise conflict("OFFER_NOT_ACTIVE", "This offer has already closed.")
        return ok(sent_offer(conn, g.user_id, offer_id))
