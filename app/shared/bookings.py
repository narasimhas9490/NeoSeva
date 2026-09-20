import hmac
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from app.core import clock
from app.core.db import one, run, scalar, tx
from app.core.errors import ApiError, bad_request, conflict, not_found, unprocessable
from app.core.ids import new_id
from app.core.money import fee_minor, max_offer_minor, money, times
from app.shared import presenters as P
from app.shared.settings import catalog_setting, geography, platform_setting, texts

BOOKING_SQL = """
    SELECT b.*, r.service_id, r.work_type_id, r.description, r.place_id, r.landmark, r.accuracy_meters,
           ST_Y(r.pin::geometry) AS latitude, ST_X(r.pin::geometry) AS longitude,
           r.geography_id, p.name AS place_name, s.name AS service_name,
           cu.phone_e164 AS customer_phone, cp.display_name AS customer_name,
           pu.phone_e164 AS partner_phone, pp.display_name AS partner_name
    FROM booking b
    JOIN request r ON r.id = b.request_id
    JOIN place p ON p.id = r.place_id
    JOIN service s ON s.id = r.service_id
    JOIN app_user cu ON cu.id = b.customer_id
    LEFT JOIN customer_profile cp ON cp.user_id = b.customer_id
    JOIN app_user pu ON pu.id = b.partner_id
    JOIN partner_profile pp ON pp.user_id = b.partner_id
"""


def load_booking(conn, booking_id, customer_id=None, partner_id=None, lock=False):
    """Load a booking with everything both shapes need.
    Scoped to a customer or a partner, so somebody else's booking is None.
    lock takes a row lock on the booking for the write that follows."""
    where = " WHERE b.id = :id"
    if customer_id:
        where += " AND b.customer_id = :c"
    if partner_id:
        where += " AND b.partner_id = :p"
    if lock:
        locked = one(
            conn,
            "SELECT b.id FROM booking b" + where + " FOR UPDATE",
            id=booking_id,
            c=customer_id,
            p=partner_id,
        )
        if locked is None:
            return None
    return one(conn, BOOKING_SQL + where, id=booking_id, c=customer_id, p=partner_id)


def job_amount(row):
    """Return what the job comes to, or None when nobody could price it.
    Once a final amount exists it is the price, everywhere.
    Otherwise the frozen booked cost."""
    return row["final_amount_minor"] if row["final_amount_minor"] is not None else row["booked_cost_minor"]


def needs_final_amount(row):
    """Say whether this job reaches the end with no price.
    A visit charge, or a rate where she gave no quantity.
    Those are the jobs where he names the final amount."""
    return row["booked_pricing_type"] == "INSPECTION" or (
        row["booked_pricing_type"] == "UNIT_RATE" and row["booked_cost_minor"] is None
    )


def pin_blocked_by_agreement(row):
    """Say whether her PIN must stay hidden until she agrees a price.
    Only on a visit charge with a final amount she has not agreed.
    Her code is her yes, never a yes to a number she has not read."""
    return (
        row["booked_pricing_type"] == "INSPECTION"
        and row["final_amount_minor"] is not None
        and not row["final_amount_agreed"]
    )


def _pricing(conn, row):
    """Build the frozen booked pricing shape.
    Read from the booked_* snapshot, never through the offer.
    She confirmed against these numbers and keeps seeing them."""
    return P.pricing_shape(
        conn,
        row["service_id"],
        row["booked_pricing_type"],
        row["booked_exact_amount_minor"],
        row["booked_rate_minor"],
        row["booked_unit_code"],
        row["booked_inspection_charge_minor"],
    )


def pin_text(catalog, partner_name):
    """Fill the served PIN guidance with the partner's name.
    pinGuidance and the PIN instruction both use it, so they cannot disagree.
    None when the guidance has been blanked."""
    guidance = catalog["pin_guidance"]
    if not guidance:
        return None
    return guidance.replace("{partnerName}", partner_name or "")


def customer_actions(row):
    """Decide which buttons her booked job shows.
    Calling is live while the job is running; nothing once it has ended.
    The app draws only what is listed here."""
    if row["state"] in ("BOOKED", "ARRIVED"):
        return ["CALL_PARTNER", "WHATSAPP_PARTNER"]
    return []


def partner_actions(row):
    """Decide which buttons his job shows.
    On my way and arrive only while booked; calling while the job runs.
    The app draws only what is listed here."""
    if row["state"] == "BOOKED":
        return ["CALL_CUSTOMER", "WHATSAPP_CUSTOMER", "ON_MY_WAY", "ARRIVE"]
    if row["state"] == "ARRIVED":
        return ["CALL_CUSTOMER", "WHATSAPP_CUSTOMER"]
    return []


def customer_booking_shape(conn, row):
    """Put a booking into the shape her booked-job screen draws.
    Frozen pricing, schedule and name beside live trust, actions and review.
    Carries the exact location because she gave it."""
    catalog = catalog_setting(conn)
    platform = platform_setting(conn)
    geo = geography(conn, row["geography_id"])
    name = row["booked_partner_display_name"] or row["partner_name"]
    review = one(conn, "SELECT verdict, tag_codes, comment, created_at FROM review WHERE booking_id = :b", b=row["id"])
    return {
        "id": row["id"],
        "requestId": row["request_id"],
        "state": row["state"],
        "partner": {
            "id": row["partner_id"],
            "displayName": name,
            "phoneNumber": row["partner_phone"],
            "trust": P.partner_trust(conn, row["partner_id"], platform),
        },
        "service": {"id": row["service_id"], "name": row["service_name"]},
        "bookedPricing": _pricing(conn, row),
        "bookedCost": money(row["booked_cost_minor"]),
        "finalAmount": money(row["final_amount_minor"]),
        "finalAmountAgreed": row["final_amount_agreed"],
        "schedule": {"date": row["schedule_date"].isoformat(), "dayPart": row["day_part"]},
        "arrivalExpectedBy": clock.zoneless_local(row["arrival_expected_by"], geo["timezone"]),
        "exactLocation": P.exact_location(row),
        "events": P.booking_events(row),
        "availableActions": customer_actions(row),
        "pinGuidance": pin_text(catalog, name),
        "partnerSaved": bool(
            scalar(
                conn,
                "SELECT 1 FROM saved_partner WHERE customer_id = :c AND partner_id = :p",
                c=row["customer_id"],
                p=row["partner_id"],
            )
        ),
        "partnerFellThrough": row["partner_fell_through"],
        "review": None
        if review is None
        else {
            "verdict": review["verdict"],
            "tagCodes": review["tag_codes"],
            "comment": review["comment"],
            "createdAt": clock.iso_utc(review["created_at"]),
        },
        "createdAt": clock.iso_utc(row["created_at"]),
    }


def introduction(conn, row, catalog):
    """Work out what this job will cost him and whether the pair is new.
    amount is the fee on the final or booked amount, rounded down; zero if unpriced.
    Zero is what tells his app to ask for the final number."""
    earlier = scalar(
        conn,
        """SELECT count(*) FROM booking WHERE customer_id = :c AND partner_id = :p
           AND state = 'COMPLETED' AND id <> :id""",
        c=row["customer_id"],
        p=row["partner_id"],
        id=row["id"],
    )
    return {
        "isNewCustomerPair": earlier == 0,
        "amount": money(fee_minor(job_amount(row), catalog["job_fee_percent"])),
    }


def partner_booking_shape(conn, row):
    """Put a booking into the shape his job detail draws.
    Her name, number and exact spot are unlocked because she booked him.
    introduction.amount is computed here and charged only in chunk 5."""
    catalog = catalog_setting(conn)
    geo = geography(conn, row["geography_id"])
    all_texts = texts(conn)
    from app.shared.templates import formatted_answers

    return {
        "id": row["id"],
        "requestId": row["request_id"],
        "state": row["state"],
        "customer": {"id": row["customer_id"], "displayName": row["customer_name"], "phoneNumber": row["customer_phone"]},
        "service": {"id": row["service_id"], "name": row["service_name"]},
        "workType": P.work_type(conn, row["work_type_id"]),
        "schedule": {"date": row["schedule_date"].isoformat(), "dayPart": row["day_part"]},
        "exactLocation": P.exact_location(row),
        "answers": formatted_answers(conn, row["request_id"], all_texts),
        "description": row["description"],
        "bookedPricing": _pricing(conn, row),
        "bookedCost": money(row["booked_cost_minor"]),
        "introduction": introduction(conn, row, catalog),
        "finalAmount": money(row["final_amount_minor"]),
        "finalAmountAgreed": row["final_amount_agreed"],
        "arrivalExpectedBy": clock.zoneless_local(row["arrival_expected_by"], geo["timezone"]),
        "events": P.booking_events(row),
        "availableActions": partner_actions(row),
        "createdAt": clock.iso_utc(row["created_at"]),
    }


def booking_ended_reason(row):
    """Name how a finished booking ended, for his history rows.
    A partner who fell through and a customer who cancelled read differently.
    None while running or when it completed."""
    if row["state"] != "CANCELLED":
        return None
    if row["partner_fell_through"]:
        return "PARTNER_FELL_THROUGH"
    if row["cancelled_by"] == "CUSTOMER":
        return "CANCELLED_BY_CUSTOMER"
    return None


def partner_list_row(conn, row):
    """Put a booking into the short row of his jobs list.
    Her given name and the village, never her number or exact spot.
    isOnMyWay agrees with the detail's events."""
    return {
        "bookingId": row["id"],
        "state": row["state"],
        "service": {"id": row["service_id"], "name": row["service_name"]},
        "customerDisplayName": row["customer_name"],
        "areaLabel": row["place_name"],
        "schedule": {"date": row["schedule_date"].isoformat(), "dayPart": row["day_part"]},
        "bookedPricing": _pricing(conn, row),
        "isOnMyWay": row["on_my_way_at"] is not None and row["state"] == "BOOKED",
        "endedReason": booking_ended_reason(row),
        "canDisputeNoShow": False,
    }


def book(customer_id, request_id, offer_id):
    """Book one offer on a request, all in one transaction or not at all.
    Checks request, offer, partner and schedule; then snapshots, closes others, stops matching.
    Returns the new booking id; every refusal has its own code."""
    try:
        with tx() as conn:
            request = one(conn, "SELECT * FROM request WHERE id = :id AND customer_id = :c FOR UPDATE", id=request_id, c=customer_id)
            if request is None:
                raise not_found()
            live = scalar(
                conn, "SELECT id FROM booking WHERE request_id = :r AND state IN ('BOOKED','ARRIVED')", r=request_id
            )
            if live:
                raise conflict("REQUEST_ALREADY_BOOKED", "This request is already booked.", {"bookingId": live})
            if request["state"] != "REQUESTED":
                raise unprocessable("REQUEST_NOT_BOOKABLE", "This request has ended.")
            offer = one(
                conn, "SELECT * FROM offer WHERE id = :o AND request_id = :r FOR UPDATE", o=offer_id, r=request_id
            )
            if offer is None or offer["status"] != "ACTIVE":
                raise conflict("OFFER_NOT_AVAILABLE", "This offer is no longer available.")
            partner = one(conn, "SELECT * FROM partner_profile WHERE user_id = :p", p=offer["partner_id"])
            if partner is None or partner["status"] != "ACTIVE":
                raise unprocessable("PARTNER_NOT_BOOKABLE", "This partner can no longer be booked.")
            geo = geography(conn, request["geography_id"])
            day_part = offer["offered_day_part"] or request["day_part"]
            problem = clock.schedule_problem(geo, request["schedule_date"], day_part, include_request_rules=False)
            if problem:
                raise unprocessable("REQUEST_NOT_BOOKABLE", "The schedule can no longer be arranged.", {"reason": problem})
            booking_id = new_id("bkg")
            run(
                conn,
                """INSERT INTO booking (id, request_id, offer_id, customer_id, partner_id, state,
                       booked_pricing_type, booked_exact_amount_minor, booked_rate_minor, booked_unit_code,
                       booked_inspection_charge_minor, booked_cost_minor, schedule_date, day_part,
                       arrival_expected_by, completion_pin, booked_partner_display_name, created_at)
                   VALUES (:id, :r, :o, :c, :p, 'BOOKED', :t, :exact, :rate, :unit, :insp, :cost,
                       :date, :dp, :arrive, :pin, :pname, :now)""",
                id=booking_id,
                r=request_id,
                o=offer_id,
                c=customer_id,
                p=offer["partner_id"],
                t=offer["pricing_type"],
                exact=offer["exact_amount_minor"],
                rate=offer["rate_minor"],
                unit=offer["unit_code"],
                insp=offer["inspection_charge_minor"],
                cost=offer["comparable_cost_minor"],
                date=request["schedule_date"],
                dp=day_part,
                arrive=clock.arrival_expected_by(geo, request["schedule_date"], day_part),
                pin=f"{secrets.randbelow(10000):04d}",
                pname=partner["display_name"],
                now=clock.now_utc(),
            )
            run(conn, "UPDATE offer SET status = 'BOOKED' WHERE id = :o", o=offer_id)
            run(
                conn,
                "UPDATE offer SET status = 'NOT_SELECTED' WHERE request_id = :r AND id <> :o AND status = 'ACTIVE'",
                r=request_id,
                o=offer_id,
            )
            run(conn, "UPDATE request SET state = 'BOOKED', is_rematching = false WHERE id = :r", r=request_id)
            return booking_id, offer["partner_id"]
    except IntegrityError:
        with tx() as conn:
            live = scalar(conn, "SELECT id FROM booking WHERE request_id = :r AND state IN ('BOOKED','ARRIVED')", r=request_id)
        raise conflict("REQUEST_ALREADY_BOOKED", "This request is already booked.", {"bookingId": live})


def mark_arrived(conn, row, point=None, accuracy=None):
    """Move a booked job to ARRIVED and record where he was.
    The request follows the booking to ARRIVED.
    point is (latitude, longitude) or None when his phone had no fix."""
    params = {"id": row["id"], "now": clock.now_utc(), "lat": None, "lng": None, "acc": accuracy}
    if point:
        params["lat"], params["lng"] = point
    run(
        conn,
        """UPDATE booking SET state = 'ARRIVED', arrived_at = :now, arrival_accuracy_meters = :acc,
               arrival_point = CASE WHEN CAST(:lat AS DOUBLE PRECISION) IS NULL THEN NULL
                   ELSE ST_SetSRID(ST_MakePoint(CAST(:lng AS DOUBLE PRECISION), CAST(:lat AS DOUBLE PRECISION)), 4326)::geography END
           WHERE id = :id""",
        **params,
    )
    run(conn, "UPDATE request SET state = 'ARRIVED' WHERE id = :r", r=row["request_id"])


def set_final_amount(conn, row, data):
    """Record what a job nobody could price up front came to.
    A rate job sends the quantity and we multiply; a visit sends the inclusive total.
    Resets her agreement, since she has not read the new number."""
    has_quantity, has_amount = data.get("quantity") is not None, data.get("amountMinor") is not None
    if has_quantity == has_amount:
        raise bad_request("INVALID_FINAL_AMOUNT", "Send exactly one of quantity or amountMinor.")
    if row["state"] != "ARRIVED":
        raise conflict("BOOKING_NOT_ARRIVED", "The final amount is sent after arriving.")
    if not needs_final_amount(row):
        raise unprocessable("FINAL_AMOUNT_NOT_REQUIRED", "This job was priced up front.")
    if row["booked_pricing_type"] == "INSPECTION" and row["final_amount_agreed"]:
        raise conflict("FINAL_AMOUNT_ALREADY_AGREED", "She has already agreed this amount.")
    if row["booked_pricing_type"] == "UNIT_RATE":
        if not has_quantity:
            raise bad_request("INVALID_FINAL_AMOUNT", "A rate job sends the quantity, never a total.")
        try:
            quantity = Decimal(str(data["quantity"]))
        except InvalidOperation:
            raise bad_request("INVALID_FINAL_AMOUNT", "quantity must be a number.")
        if isinstance(data["quantity"], bool) or not quantity.is_finite() or quantity <= 0:
            raise bad_request("INVALID_FINAL_AMOUNT", "quantity must be positive.")
        amount = times(row["booked_rate_minor"], quantity)
    else:
        if not has_amount:
            raise bad_request("INVALID_FINAL_AMOUNT", "A visit sends the total amount, never a quantity.")
        amount = data["amountMinor"]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise bad_request("INVALID_FINAL_AMOUNT", "amountMinor must be positive whole paise.")
    ceiling = max_offer_minor(catalog_setting(conn)["max_offer_rupees"])
    if ceiling is not None and amount > ceiling:
        raise unprocessable("AMOUNT_ABOVE_MAXIMUM", "The amount is above the maximum.")
    run(
        conn,
        """UPDATE booking SET final_amount_minor = :a, final_amount_at = :now,
               final_amount_agreed = false, final_amount_agreed_at = NULL WHERE id = :id""",
        a=amount,
        now=clock.now_utc(),
        id=row["id"],
    )


def complete(conn, row, completed_by):
    """Complete a job and its request in the caller's transaction.
    Completed-job counts are COUNT(*), so nothing is incremented.
    Improves her saved place from his arrival point when it is close."""
    now = clock.now_utc()
    run(
        conn,
        "UPDATE booking SET state = 'COMPLETED', completed_at = :now, completed_by = :by WHERE id = :id",
        now=now,
        by=completed_by,
        id=row["id"],
    )
    run(conn, "UPDATE request SET state = 'COMPLETED' WHERE id = :r", r=row["request_id"])
    improve_saved_place(conn, row)


def improve_saved_place(conn, row):
    """Upgrade her saved place's pin with his arrival point.
    Only from a completed job, only near what she gave, only if more accurate.
    A wild fix never overwrites a good pin."""
    limit = platform_setting(conn)["saved_place_upgrade_max_meters"]
    run(
        conn,
        """UPDATE saved_place sp SET pin = b.arrival_point, accuracy_meters = b.arrival_accuracy_meters
           FROM booking b JOIN request r ON r.id = b.request_id
           WHERE b.id = :id AND b.arrival_point IS NOT NULL AND r.pin IS NOT NULL
             AND sp.user_id = b.customer_id AND sp.place_id = r.place_id
             AND sp.landmark IS NOT DISTINCT FROM r.landmark
             AND sp.pin IS NOT NULL AND ST_DWithin(sp.pin, r.pin, 1)
             AND ST_DWithin(b.arrival_point, r.pin, :limit)
             AND (sp.accuracy_meters IS NULL OR
                  (b.arrival_accuracy_meters IS NOT NULL AND b.arrival_accuracy_meters < sp.accuracy_meters))""",
        id=row["id"],
        limit=limit,
    )


def check_pin(booking_id, partner_id, pin):
    """Check the four digits he typed, with a lockout against guessing.
    Wrong attempts are written in their own transaction so they always count.
    Raises WRONG_PIN or PIN_TOO_MANY_ATTEMPTS; returns quietly when right."""
    with tx() as conn:
        row = load_booking(conn, booking_id, partner_id=partner_id, lock=True)
        if row is None:
            raise not_found()
        settings = platform_setting(conn)
        attempt = one(conn, "SELECT * FROM booking_pin_attempt WHERE booking_id = :b FOR UPDATE", b=booking_id)
        now = clock.now_utc()
        if attempt and attempt["locked_until"] and attempt["locked_until"] > now:
            wait = int((attempt["locked_until"] - now).total_seconds()) + 1
            raise ApiError(429, "PIN_TOO_MANY_ATTEMPTS", "Too many wrong codes.", {"retryAfterSeconds": wait})
        if isinstance(pin, str) and hmac.compare_digest(pin.encode(), row["completion_pin"].encode()):
            run(conn, "DELETE FROM booking_pin_attempt WHERE booking_id = :b", b=booking_id)
            return
        failed = (attempt["failed_count"] if attempt else 0) + 1
        locked_until = None
        if failed >= settings["pin_max_attempts"]:
            locked_until, failed = now + timedelta(seconds=settings["pin_lockout_seconds"]), 0
        run(
            conn,
            """INSERT INTO booking_pin_attempt (booking_id, failed_count, locked_until) VALUES (:b, :f, :l)
               ON CONFLICT (booking_id) DO UPDATE SET failed_count = :f, locked_until = :l""",
            b=booking_id,
            f=failed,
            l=locked_until,
        )
    if locked_until:
        raise ApiError(
            429, "PIN_TOO_MANY_ATTEMPTS", "Too many wrong codes.", {"retryAfterSeconds": settings["pin_lockout_seconds"]}
        )
    raise unprocessable("WRONG_PIN", "That code is not right.")
