from app.core import clock
from app.core.db import many, one, scalar
from app.core.money import money
from app.shared.settings import text_for

ENDED_TEXT_KEYS = {
    "CANCELLED_BY_CUSTOMER": ("ENDED_CANCELLED_BY_CUSTOMER", "You cancelled this request."),
    "NO_PARTNER_AVAILABLE": ("ENDED_NO_PARTNER_AVAILABLE", "No partner was available for this request."),
    "NOT_BOOKED": ("ENDED_NOT_BOOKED", "This request ended without a booking."),
    "PARTNER_FELL_THROUGH": ("ENDED_PARTNER_FELL_THROUGH", "Your partner could not do this job."),
}


def approximate_area(place_id, place_name, all_texts):
    """Build the only location shape a partner sees before booking.
    It has a village and an area label and no field that could leak more.
    There is deliberately no way to add the landmark or the pin here."""
    return {
        "placeId": place_id,
        "placeName": place_name,
        "areaLabel": text_for(all_texts, "AREA_LABEL_FORMAT", "{placeName} area", placeName=place_name),
    }


def exact_location(row):
    """Build the exact spot, which appears only on a booking.
    Carries the landmark, the pin and how sure the phone was.
    row needs place_id, place_name, landmark, latitude, longitude, accuracy_meters."""
    return {
        "placeId": row["place_id"],
        "placeName": row["place_name"],
        "landmark": row["landmark"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "accuracyMeters": row["accuracy_meters"],
    }


def completed_jobs_as_customer(conn, user_id):
    """Count a customer's completed bookings.
    A count over bookings, never a stored column that can drift.
    Part of the trust summary a partner sees."""
    return scalar(conn, "SELECT count(*) FROM booking WHERE customer_id = :u AND state = 'COMPLETED'", u=user_id)


def completed_jobs_as_partner(conn, user_id):
    """Count a partner's completed bookings.
    Used for trust and for the most-jobs-completed claim.
    A count, never a column."""
    return scalar(conn, "SELECT count(*) FROM booking WHERE partner_id = :u AND state = 'COMPLETED'", u=user_id)


def customer_trust(conn, user_id):
    """Build the two facts a partner may know about a customer.
    Whether her phone is verified and how many jobs she has completed.
    No name, no number, no history, no score."""
    verified = scalar(conn, "SELECT phone_verified_at IS NOT NULL FROM app_user WHERE id = :u", u=user_id)
    return {"phoneVerified": bool(verified), "completedJobs": completed_jobs_as_customer(conn, user_id)}


def well_rated(conn, partner_id, platform):
    """Decide whether a partner has earned the well-rated mark.
    Needs enough reviews with enough VERY_GOOD; both thresholds are admin set.
    False whenever the thresholds are not decided yet."""
    min_reviews = platform["well_rated_min_reviews"]
    min_percent = platform["well_rated_min_positive_percent"]
    if not min_reviews or min_percent is None:
        return False
    row = one(
        conn,
        "SELECT count(*) AS n, count(*) FILTER (WHERE verdict = 'VERY_GOOD') AS good FROM review WHERE partner_id = :p",
        p=partner_id,
    )
    return row["n"] >= min_reviews and row["good"] * 100 >= min_percent * row["n"]


def partner_trust(conn, partner_id, platform):
    """Build the three facts a customer sees about a partner.
    Identity checked, jobs completed, and the well-rated threshold.
    There is no rating and no average anywhere."""
    identity = scalar(conn, "SELECT status FROM identity_verification WHERE partner_id = :p", p=partner_id)
    return {
        "identityVerified": identity == "VERIFIED",
        "completedJobs": completed_jobs_as_partner(conn, partner_id),
        "wellRated": well_rated(conn, partner_id, platform),
    }


def unit_shape(conn, service_id, unit_code):
    """Describe a pricing unit with its two words.
    label counts things, perLabel follows the word per.
    Falls back to the code when the service has no such unit configured."""
    row = one(
        conn, "SELECT code, label, per_label FROM service_offer_unit WHERE service_id = :s AND code = :c", s=service_id, c=unit_code
    )
    if row is None:
        return {"code": unit_code, "label": unit_code, "perLabel": None}
    return {"code": row["code"], "label": row["label"], "perLabel": row["per_label"]}


def pricing_shape(conn, service_id, pricing_type, exact, rate, unit_code, inspection):
    """Put an offer's or booking's pricing into its wire shape.
    FIXED carries exactAmount, UNIT_RATE rate and unit, INSPECTION inspectionCharge.
    Exactly the fields for the type and nothing else."""
    if pricing_type == "FIXED":
        return {"type": "FIXED", "exactAmount": money(exact)}
    if pricing_type == "UNIT_RATE":
        return {"type": "UNIT_RATE", "rate": money(rate), "unit": unit_shape(conn, service_id, unit_code)}
    return {"type": "INSPECTION", "inspectionCharge": money(inspection)}


def schedule_label(on_date, day_part, all_texts):
    """Format a schedule as '19 Sep • Morning'.
    The daypart word is served text so it can be corrected.
    Both apps print it as it is."""
    part = text_for(all_texts, f"DAYPART_{day_part}", day_part.replace("_", " ").title())
    return f"{clock.short_date(on_date)} • {part}"


def ended_label(ended_reason, all_texts):
    """Return the served sentence for how a request ended.
    Each of the four endings has its own wording and author.
    None when the request has not ended."""
    if ended_reason is None:
        return None
    key, default = ENDED_TEXT_KEYS[ended_reason]
    return text_for(all_texts, key, default)


REQUEST_SQL = """
    SELECT r.*, s.name AS service_name, p.name AS place_name,
           ST_Y(r.pin::geometry) AS latitude, ST_X(r.pin::geometry) AS longitude,
           (SELECT count(*) FROM offer o WHERE o.request_id = r.id AND o.status IN ('ACTIVE','BOOKED')) AS offer_count,
           (SELECT count(*) FROM offer o WHERE o.request_id = r.id AND o.status = 'ACTIVE') AS active_offer_count,
           (SELECT count(*) FROM opportunity_notification n WHERE n.request_id = r.id) AS notified_count,
           (SELECT b.id FROM booking b WHERE b.request_id = r.id ORDER BY b.created_at DESC LIMIT 1) AS booking_id
    FROM request r JOIN service s ON s.id = r.service_id JOIN place p ON p.id = r.place_id
"""


def load_request(conn, request_id, customer_id=None, lock=False):
    """Load one request with the counts its shape needs.
    With customer_id it returns None for somebody else's request.
    lock takes a row lock on the request for a write that follows."""
    if lock:
        base = one(
            conn,
            "SELECT id FROM request WHERE id = :id" + (" AND customer_id = :c" if customer_id else "") + " FOR UPDATE",
            id=request_id,
            c=customer_id,
        )
        if base is None:
            return None
    sql = REQUEST_SQL + " WHERE r.id = :id" + (" AND r.customer_id = :c" if customer_id else "")
    return one(conn, sql, id=request_id, c=customer_id)


def request_shape(conn, row, all_texts, with_draft=False):
    """Put a request into the shape both her screens draw from.
    canEdit and canCancel are decided here, never by the app.
    with_draft adds the answers themselves while the request is editable."""
    can_edit = row["state"] == "REQUESTED" and row["active_offer_count"] == 0
    shape = {
        "id": row["id"],
        "state": row["state"],
        "service": {"id": row["service_id"], "name": row["service_name"]},
        "summary": {
            "scheduleLabel": schedule_label(row["schedule_date"], row["day_part"], all_texts),
            "areaLabel": row["place_name"],
            "description": row["description"],
        },
        "offerCount": row["offer_count"],
        "notifiedPartnerCount": row["notified_count"],
        "isRematching": row["is_rematching"],
        "rematch": None,
        "bookingId": row["booking_id"],
        "endedReason": row["ended_reason"],
        "endedLabel": ended_label(row["ended_reason"], all_texts),
        "canEdit": can_edit,
        "canCancel": row["state"] == "REQUESTED",
        "createdAt": clock.iso_utc(row["created_at"]),
    }
    if with_draft:
        shape["draft"] = draft_shape(conn, row) if can_edit else None
    return shape


def draft_shape(conn, row):
    """Return what she entered, so the editor can be filled back in.
    The formatted summary cannot be put back into a form; this can.
    Answers, schedule, location and photo ids as she sent them."""
    answers = many(
        conn,
        'SELECT question_id, "values" AS vals, unit_code, not_sure FROM request_answer WHERE request_id = :r',
        r=row["id"],
    )
    media_ids = [
        m["media_id"]
        for m in many(conn, "SELECT media_id FROM request_media WHERE request_id = :r ORDER BY sort_order", r=row["id"])
    ]
    return {
        "serviceId": row["service_id"],
        "workTypeId": row["work_type_id"],
        "templateVersion": row["template_version"],
        "answers": [
            {
                "questionId": a["question_id"],
                "values": a["vals"],
                **({"unit": a["unit_code"]} if a["unit_code"] else {}),
                **({"notSure": True} if a["not_sure"] else {}),
            }
            for a in answers
        ],
        "description": row["description"],
        "schedule": {"date": row["schedule_date"].isoformat(), "dayPart": row["day_part"]},
        "location": {
            "placeId": row["place_id"],
            "landmark": row["landmark"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "accuracyMeters": row["accuracy_meters"],
        },
        "mediaIds": media_ids,
    }


def work_type(conn, work_type_id):
    """Name the kind of work within a service, such as Rotavator.
    The name is the label of the work-type option.
    None when the request names no work type."""
    if not work_type_id:
        return None
    label = scalar(conn, "SELECT label FROM request_question_option WHERE id = :id", id=work_type_id)
    return {"id": work_type_id, "name": label or work_type_id}


def request_media(conn, request_id):
    """List the photos attached to a request.
    Only pictures with a public URL are sent.
    Deleted media is left out."""
    rows = many(
        conn,
        """SELECT m.id, m.url, m.thumbnail_url FROM request_media rm JOIN media m ON m.id = rm.media_id
           WHERE rm.request_id = :r AND m.deleted_at IS NULL ORDER BY rm.sort_order""",
        r=request_id,
    )
    return [{"id": r["id"], "url": r["url"], "thumbnailUrl": r["thumbnail_url"]} for r in rows]


def booking_events(row):
    """Build the events list from the booking's own timestamps.
    There is no event table; each moment is a column.
    On my way is an event, not a state."""
    columns = [
        ("BOOKED", "created_at"),
        ("ON_MY_WAY", "on_my_way_at"),
        ("ARRIVED", "arrived_at"),
        ("COMPLETED", "completed_at"),
        ("CANCELLED", "cancelled_at"),
    ]
    events = [{"type": kind, "at": row[col]} for kind, col in columns if row.get(col) is not None]
    events.sort(key=lambda e: e["at"])
    return [{"type": e["type"], "at": clock.iso_utc(e["at"])} for e in events]
