from flask import Blueprint, g, request

from app.core import clock
from app.core.adapters.push import notify_user
from app.core.auth import require_user
from app.core.db import many, run, tx
from app.core.envelope import body, no_content, ok
from app.core.errors import bad_request, conflict, not_found
from app.core.idempotency import idempotent
from app.core.paging import PAGE_SIZE, decode_cursor, page
from app.shared import bookings

bp = Blueprint("partner_jobs", __name__, url_prefix="/partner")


def active_jobs(conn, partner_id):
    """List his running jobs as short rows.
    BOOKED and ARRIVED, soonest first.
    Used by the jobs list and by his home screen."""
    rows = many(
        conn,
        bookings.BOOKING_SQL + " WHERE b.partner_id = :p AND b.state IN ('BOOKED','ARRIVED') ORDER BY b.schedule_date, b.created_at",
        p=partner_id,
    )
    return [bookings.partner_list_row(conn, r) for r in rows]


def _his(conn, booking_id, lock=False):
    """Load one of his bookings or raise 404.
    Somebody else's job is not found, never forbidden.
    lock takes a row lock for the write that follows."""
    row = bookings.load_booking(conn, booking_id, partner_id=g.user_id, lock=lock)
    if row is None:
        raise not_found()
    return row


@bp.get("/jobs")
@require_user("PARTNER")
def list_jobs():
    """List his jobs in the ACTIVE or HISTORY bucket.
    Rows carry her given name and the village, never her number or spot.
    HISTORY is cursor-paged, newest first."""
    bucket = request.args.get("bucket", "ACTIVE")
    with tx() as conn:
        if bucket == "ACTIVE":
            return ok(active_jobs(conn, g.user_id), nextCursor=None)
        if bucket != "HISTORY":
            raise bad_request("INVALID_BUCKET", "bucket must be ACTIVE or HISTORY.")
        after = decode_cursor(request.args.get("cursor"))
        sql = bookings.BOOKING_SQL + " WHERE b.partner_id = :p AND b.state IN ('COMPLETED','CANCELLED')"
        params = {"p": g.user_id, "lim": PAGE_SIZE + 1}
        if after:
            sql += " AND (b.created_at, b.id) < (CAST(:at AS TIMESTAMPTZ), :aid)"
            params.update(at=after["at"], aid=after["id"])
        rows = many(conn, sql + " ORDER BY b.created_at DESC, b.id DESC LIMIT :lim", **params)
        rows, next_cursor = page(rows, lambda r: {"at": r["created_at"].isoformat(), "id": r["id"]})
        return ok([bookings.partner_list_row(conn, r) for r in rows], nextCursor=next_cursor)


@bp.get("/bookings/<booking_id>")
@require_user("PARTNER")
def get_booking(booking_id):
    """Read one of his jobs in full.
    Her name, number and exact spot, unlocked by the booking.
    introduction.amount says what the job will cost him."""
    with tx() as conn:
        return ok(bookings.partner_booking_shape(conn, _his(conn, booking_id)))


@bp.post("/bookings/<booking_id>/on-my-way")
@require_user("PARTNER")
def on_my_way(booking_id):
    """Say he has set off; the booking stays BOOKED.
    Tapping again overwrites the time; only the latest matters.
    Returns 204."""
    with tx() as conn:
        row = _his(conn, booking_id, lock=True)
        if row["state"] != "BOOKED":
            raise conflict("BOOKING_NOT_ACTIVE", "On my way is only for a booked job.")
        run(conn, "UPDATE booking SET on_my_way_at = :now WHERE id = :id", now=clock.now_utc(), id=booking_id)
    notify_user(row["customer_id"], "CUSTOMER", "ON_MY_WAY", {"bookingId": booking_id})
    return no_content()


@bp.post("/bookings/<booking_id>/arrive")
@require_user("PARTNER")
@idempotent
def arrive(booking_id):
    """Say he is there, with his position if the phone has one.
    Evidence is optional; a field with no fix must never block arrival.
    Moves the job and its request to ARRIVED."""
    evidence = body().get("evidence")
    point = accuracy = None
    if isinstance(evidence, dict) and evidence.get("latitude") is not None and evidence.get("longitude") is not None:
        try:
            lat, lng = float(evidence["latitude"]), float(evidence["longitude"])
        except (TypeError, ValueError):
            raise bad_request("INVALID_COORDINATES", "evidence latitude and longitude must be numbers.")
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            point = (lat, lng)
            accuracy = evidence.get("accuracyMeters") if isinstance(evidence.get("accuracyMeters"), (int, float)) else None
    with tx() as conn:
        row = _his(conn, booking_id, lock=True)
        if row["state"] != "BOOKED":
            raise conflict("BOOKING_NOT_ARRIVABLE", "Only a booked job can be arrived at.")
        bookings.mark_arrived(conn, row, point, accuracy)
        shape = bookings.partner_booking_shape(conn, _his(conn, booking_id))
    notify_user(row["customer_id"], "CUSTOMER", "ARRIVED", {"bookingId": booking_id})
    return ok(shape)


@bp.post("/bookings/<booking_id>/final-amount")
@require_user("PARTNER")
@idempotent
def final_amount(booking_id):
    """Say what a job nobody could price up front came to.
    A rate job sends how many; a visit sends the inclusive total.
    introduction.amount is recomputed in the returned shape."""
    data = body()
    with tx() as conn:
        row = _his(conn, booking_id, lock=True)
        bookings.set_final_amount(conn, row, data)
        shape = bookings.partner_booking_shape(conn, _his(conn, booking_id))
    notify_user(row["customer_id"], "CUSTOMER", "FINAL_AMOUNT", {"bookingId": booking_id})
    return ok(shape)


@bp.post("/bookings/<booking_id>/complete")
@require_user("PARTNER")
@idempotent
def complete(booking_id):
    """Type her four digits to complete the job.
    Wrong is 422 WRONG_PIN; too many is 429 with retryAfterSeconds.
    A job still waiting on its price cannot be completed yet."""
    pin = body().get("pin")
    with tx() as conn:
        row = _his(conn, booking_id)
        if row["state"] == "COMPLETED":
            return ok(bookings.partner_booking_shape(conn, row))
        if row["state"] not in ("BOOKED", "ARRIVED"):
            raise conflict("BOOKING_NOT_ACTIVE", "This booking is not running.")
        if bookings.needs_final_amount(row) and row["final_amount_minor"] is None:
            raise conflict("FINAL_AMOUNT_REQUIRED", "Send the final amount first.")
        if bookings.pin_blocked_by_agreement(row):
            raise conflict("FINAL_AMOUNT_NOT_AGREED", "She has not agreed the price yet.")
    bookings.check_pin(booking_id, g.user_id, pin)
    with tx() as conn:
        row = _his(conn, booking_id, lock=True)
        if row["state"] in ("BOOKED", "ARRIVED"):
            bookings.complete(conn, row, "PARTNER_PIN")
        shape = bookings.partner_booking_shape(conn, _his(conn, booking_id))
    notify_user(row["customer_id"], "CUSTOMER", "COMPLETED", {"bookingId": booking_id})
    return ok(shape)
