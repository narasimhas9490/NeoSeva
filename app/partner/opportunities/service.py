from app.core import clock
from app.core.db import many, one, run, scalar, tx
from app.core.errors import ApiError, conflict, not_found, unprocessable
from app.core.ids import new_id
from app.core.paging import PAGE_SIZE, decode_cursor, page
from app.shared import presenters as P
from app.shared.pricing import allowed_units, comparable_cost, parse_pricing
from app.shared.settings import catalog_setting, geography, texts
from app.shared.templates import formatted_answers

OPEN_SQL = """
    SELECT r.*, p.name AS place_name, s.name AS service_name, n.notified_at, n.declined_at,
           EXISTS (SELECT 1 FROM offer o WHERE o.request_id = r.id AND o.partner_id = :me) AS has_offer
    FROM opportunity_notification n
    JOIN request r ON r.id = n.request_id
    JOIN place p ON p.id = r.place_id
    JOIN service s ON s.id = r.service_id
    JOIN geography g ON g.id = r.geography_id
    WHERE n.partner_id = :me
"""
STILL_OPEN = " AND r.state = 'REQUESTED' AND r.schedule_date >= (CAST(:now AS TIMESTAMPTZ) AT TIME ZONE g.timezone)::date"


def opportunity_shape(conn, row, all_texts, partner):
    """Put a request into the shape a partner sees before booking.
    Only the approximate area and two trust facts: no landmark, pin, name or number.
    canSendOffer is decided here, never by his app."""
    can_send = (
        row["state"] == "REQUESTED"
        and not row["has_offer"]
        and partner["status"] == "ACTIVE"
        and row["schedule_date"] >= clock.local_today(geography(conn, row["geography_id"])["timezone"])
    )
    return {
        "requestId": row["id"],
        "service": {"id": row["service_id"], "name": row["service_name"]},
        "workType": P.work_type(conn, row["work_type_id"]),
        "approximateArea": P.approximate_area(row["place_id"], row["place_name"], all_texts),
        "schedule": {"date": row["schedule_date"].isoformat(), "dayPart": row["day_part"]},
        "answers": formatted_answers(conn, row["id"], all_texts),
        "allowedOfferUnits": allowed_units(conn, row["service_id"]),
        "description": row["description"],
        "media": P.request_media(conn, row["id"]),
        "customerTrust": P.customer_trust(conn, row["customer_id"]),
        "canSendOffer": can_send,
        "postedAt": clock.iso_utc(row["created_at"]),
    }


def partner_row(conn, partner_id):
    """Load the partner's own profile row.
    Needed for his status in canSendOffer and eligibility.
    404 when he has no partner profile."""
    row = one(conn, "SELECT * FROM partner_profile WHERE user_id = :p", p=partner_id)
    if row is None:
        raise not_found("PARTNER_NOT_FOUND", "No partner profile.")
    return row


def list_open(conn, partner_id, cursor=None, limit=PAGE_SIZE):
    """List the work he has been told about that is still open.
    Declined jobs and anything booked, cancelled or past are left out.
    Newest first, cursor-paged; returns (items, nextCursor)."""
    partner = partner_row(conn, partner_id)
    after = decode_cursor(cursor)
    sql = OPEN_SQL + STILL_OPEN + " AND n.declined_at IS NULL"
    params = {"me": partner_id, "lim": limit + 1, "now": clock.now_utc()}
    if after:
        sql += " AND (n.notified_at, r.id) < (CAST(:at AS TIMESTAMPTZ), :aid)"
        params.update(at=after["at"], aid=after["id"])
    rows = many(conn, sql + " ORDER BY n.notified_at DESC, r.id DESC LIMIT :lim", **params)
    rows, next_cursor = page(rows, lambda r: {"at": r["notified_at"].isoformat(), "id": r["id"]}, limit)
    all_texts = texts(conn)
    items = [opportunity_shape(conn, r, all_texts, partner) for r in rows]
    return items, next_cursor


def read_one(conn, partner_id, request_id):
    """Read one piece of work he was told about.
    Once it is booked or ended he sees it only if he sent a price on it.
    Anything he was never told about is 404."""
    partner = partner_row(conn, partner_id)
    row = one(conn, OPEN_SQL + " AND r.id = :r", me=partner_id, r=request_id)
    if row is None or (row["state"] != "REQUESTED" and not row["has_offer"]):
        raise not_found()
    return opportunity_shape(conn, row, texts(conn), partner)


def send_offer(partner_id, request_id, data):
    """Send his one price on a request.
    Eligibility, openness, one offer each, then pricing and daypart rules.
    comparableCost is worked out here; sending a price is always free."""
    with tx() as conn:
        partner = partner_row(conn, partner_id)
        notified = one(
            conn, "SELECT 1 FROM opportunity_notification WHERE request_id = :r AND partner_id = :p", r=request_id, p=partner_id
        )
        request = one(conn, "SELECT * FROM request WHERE id = :r FOR UPDATE", r=request_id)
        if request is None:
            raise not_found()
        if notified is None or partner["status"] != "ACTIVE":
            raise ApiError(403, "NOT_ELIGIBLE", "You cannot send prices right now.")
        geo = geography(conn, request["geography_id"])
        if request["state"] != "REQUESTED" or request["schedule_date"] < clock.local_today(geo["timezone"]):
            raise conflict("REQUEST_NOT_OPEN", "This request is no longer open.")
        if scalar(conn, "SELECT 1 FROM offer WHERE request_id = :r AND partner_id = :p", r=request_id, p=partner_id):
            raise conflict("OFFER_ALREADY_SENT", "You already sent a price for this.")
        day_part = data.get("offeredDaypart")
        if day_part is None and request["day_part"] != "ANY_TIME":
            day_part = request["day_part"]
        if day_part not in clock.DAYPARTS:
            raise unprocessable("INVALID_OFFERED_DAYPART", "Name the part of the day you can come.")
        kind, columns = parse_pricing(conn, request["service_id"], data.get("pricing"), catalog_setting(conn))
        note = data.get("note")
        offer_id = new_id("ofr")
        run(
            conn,
            """INSERT INTO offer (id, request_id, partner_id, status, pricing_type, exact_amount_minor, rate_minor,
                   unit_code, inspection_charge_minor, comparable_cost_minor, offered_day_part, note, created_at)
               VALUES (:id, :r, :p, 'ACTIVE', :t, :exact, :rate, :unit, :insp, :cost, :dp, :note, :now)""",
            id=offer_id,
            r=request_id,
            p=partner_id,
            t=kind,
            exact=columns["exact"],
            rate=columns["rate"],
            unit=columns["unit_code"],
            insp=columns["inspection"],
            cost=comparable_cost(conn, request_id, kind, columns),
            dp=day_part,
            note=note.strip()[:500] if isinstance(note, str) and note.strip() else None,
            now=clock.now_utc(),
        )
        return offer_id, request["customer_id"]


def decline(partner_id, request_id):
    """Record that he is not interested in a piece of work.
    Free, with no consequence for his standing.
    It stops showing in his list; unknown work is 404."""
    with tx() as conn:
        if not run(
            conn,
            """UPDATE opportunity_notification SET declined_at = COALESCE(declined_at, :now)
               WHERE request_id = :r AND partner_id = :p""",
            now=clock.now_utc(),
            r=request_id,
            p=partner_id,
        ):
            raise not_found()
