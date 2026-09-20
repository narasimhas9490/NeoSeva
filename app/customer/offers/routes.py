from flask import Blueprint, g

from app.core import clock
from app.core.adapters.push import notify_user
from app.core.auth import require_user
from app.core.db import many, tx
from app.core.envelope import body, created, ok
from app.core.errors import bad_request, not_found
from app.core.idempotency import idempotent
from app.core.money import money
from app.shared import bookings
from app.shared import presenters as P
from app.shared.pricing import mark_claims
from app.shared.settings import platform_setting, texts

bp = Blueprint("customer_offers", __name__, url_prefix="/customer/requests")


def offers_for(conn, request_id, service_id):
    """Build her view of every offer on a request.
    Partner trust is three facts; the two platform claims are computed here.
    No partner phone number appears before she books."""
    platform = platform_setting(conn)
    rows = many(
        conn,
        """SELECT o.*, pp.display_name, m.url AS photo_url FROM offer o
           JOIN partner_profile pp ON pp.user_id = o.partner_id
           LEFT JOIN media m ON m.id = pp.profile_photo_media_id AND m.deleted_at IS NULL
           WHERE o.request_id = :r AND o.status <> 'WITHDRAWN' ORDER BY o.created_at""",
        r=request_id,
    )
    offers = []
    for r in rows:
        trust = P.partner_trust(conn, r["partner_id"], platform)
        offers.append(
            {
                "id": r["id"],
                "requestId": r["request_id"],
                "partner": {
                    "id": r["partner_id"],
                    "displayName": r["display_name"] or None,
                    "profilePhoto": r["photo_url"],
                    "trust": trust,
                },
                "pricing": P.pricing_shape(
                    conn,
                    service_id,
                    r["pricing_type"],
                    r["exact_amount_minor"],
                    r["rate_minor"],
                    r["unit_code"],
                    r["inspection_charge_minor"],
                ),
                "comparableCost": money(r["comparable_cost_minor"]),
                "offeredDaypart": r["offered_day_part"],
                "note": r["note"],
                "status": r["status"],
                "createdAt": clock.iso_utc(r["created_at"]),
                "_cost": r["comparable_cost_minor"],
                "_completed": trust["completedJobs"],
            }
        )
    mark_claims(offers)
    for o in offers:
        o.pop("_cost")
        o.pop("_completed")
    return offers


@bp.get("/<request_id>/offers")
@require_user("CUSTOMER")
def list_offers(request_id):
    """Return the request and its offers together.
    Both her waiting screen and her confirm screen show both.
    Somebody else's request is 404."""
    with tx() as conn:
        row = P.load_request(conn, request_id, g.user_id)
        if row is None:
            raise not_found()
        return ok({"request": P.request_shape(conn, row, texts(conn)), "offers": offers_for(conn, request_id, row["service_id"])})


@bp.post("/<request_id>/book")
@require_user("CUSTOMER")
@idempotent
def book(request_id):
    """Book one offer: one transaction or nothing.
    Already booked carries the booking id so her app can go to the job.
    Returns 201 with the full booking shape."""
    offer_id = body().get("offerId")
    if not isinstance(offer_id, str):
        raise bad_request("INVALID_REQUEST", "offerId is required.")
    booking_id, partner_id = bookings.book(g.user_id, request_id, offer_id)
    notify_user(partner_id, "PARTNER", "BOOKED", {"bookingId": booking_id})
    with tx() as conn:
        return created(bookings.customer_booking_shape(conn, bookings.load_booking(conn, booking_id, customer_id=g.user_id)))
