from flask import Blueprint, g

from app.core import clock
from app.core.adapters.push import notify_user
from app.core.auth import require_user
from app.core.db import run, tx
from app.core.envelope import ok
from app.core.errors import conflict, not_found, unprocessable
from app.core.idempotency import idempotent
from app.shared import bookings
from app.shared.settings import catalog_setting

bp = Blueprint("customer_bookings", __name__, url_prefix="/customer/bookings")


def _mine(conn, booking_id, lock=False):
    """Load one of her bookings or raise 404.
    Somebody else's booking is not found, never forbidden.
    lock takes a row lock for the write that follows."""
    row = bookings.load_booking(conn, booking_id, customer_id=g.user_id, lock=lock)
    if row is None:
        raise not_found()
    return row


def _shape(conn, booking_id):
    """Reload a booking and put it into her shape.
    Used after every write so the response is current.
    Always scoped to her."""
    return bookings.customer_booking_shape(conn, _mine(conn, booking_id))


@bp.get("/<booking_id>")
@require_user("CUSTOMER")
def get_booking(booking_id):
    """Read her booked job.
    Frozen price and schedule beside live trust, actions and review.
    Carries the exact location and his phone number."""
    with tx() as conn:
        return ok(_shape(conn, booking_id))


@bp.get("/<booking_id>/completion-pin")
@require_user("CUSTOMER")
def completion_pin(booking_id):
    """Reveal her four digits, the same every time she opens it.
    Refused while a visit price waits for her agreement, and after completion.
    The PIN is never logged anywhere."""
    with tx() as conn:
        row = _mine(conn, booking_id)
        if row["state"] == "COMPLETED":
            raise conflict("BOOKING_ALREADY_COMPLETED", "This job is already complete.")
        if row["state"] == "CANCELLED":
            raise conflict("BOOKING_NOT_ACTIVE", "This booking was cancelled.")
        if bookings.pin_blocked_by_agreement(row):
            raise conflict("FINAL_AMOUNT_NOT_AGREED", "Agree the price before the code is shown.")
        name = row["booked_partner_display_name"] or row["partner_name"]
        return ok({"pin": row["completion_pin"], "instruction": bookings.pin_text(catalog_setting(conn), name)})


@bp.post("/<booking_id>/agree-amount")
@require_user("CUSTOMER")
@idempotent
def agree_amount(booking_id):
    """Say yes to the price he named after looking.
    Only on a visit charge, and only once he has named it.
    Agreeing twice is fine; her PIN appears afterwards."""
    with tx() as conn:
        row = _mine(conn, booking_id, lock=True)
        if row["booked_pricing_type"] != "INSPECTION":
            raise unprocessable("AGREEMENT_NOT_REQUIRED", "This job was priced when you booked.")
        if row["final_amount_minor"] is None:
            raise conflict("FINAL_AMOUNT_NOT_SET", "He has not named the price yet.")
        if not row["final_amount_agreed"]:
            run(
                conn,
                "UPDATE booking SET final_amount_agreed = true, final_amount_agreed_at = :now WHERE id = :id",
                now=clock.now_utc(),
                id=booking_id,
            )
        shape = _shape(conn, booking_id)
    notify_user(row["partner_id"], "PARTNER", "AMOUNT_AGREED", {"bookingId": booking_id})
    return ok(shape)


@bp.post("/<booking_id>/confirm-arrival")
@require_user("CUSTOMER")
def confirm_arrival(booking_id):
    """Say he is here when his own tap has not arrived.
    Moves the job to ARRIVED exactly as his tap would.
    Already arrived is fine; anything else is BOOKING_NOT_ARRIVABLE."""
    with tx() as conn:
        row = _mine(conn, booking_id, lock=True)
        if row["state"] == "BOOKED":
            bookings.mark_arrived(conn, row)
        elif row["state"] != "ARRIVED":
            raise conflict("BOOKING_NOT_ARRIVABLE", "This job cannot be marked arrived.")
        return ok(_shape(conn, booking_id))


@bp.post("/<booking_id>/confirm-complete")
@require_user("CUSTOMER")
def confirm_complete(booking_id):
    """Close a job that finished after he drove away.
    Recorded as completed by the customer; both routes are real completions.
    Her app asks her to confirm first because it cannot be undone."""
    with tx() as conn:
        row = _mine(conn, booking_id, lock=True)
        if row["state"] == "COMPLETED":
            return ok(_shape(conn, booking_id))
        if row["state"] not in ("BOOKED", "ARRIVED"):
            raise conflict("BOOKING_NOT_ACTIVE", "This booking is not running.")
        bookings.complete(conn, row, "CUSTOMER")
        shape = _shape(conn, booking_id)
    notify_user(row["partner_id"], "PARTNER", "COMPLETED", {"bookingId": booking_id})
    return ok(shape)
