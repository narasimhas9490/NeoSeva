from app.core import clock
from app.core.db import many, one
from app.core.errors import not_found
from app.shared import presenters as P
from app.shared.settings import texts

SENT_SQL = """
    SELECT o.*, r.service_id, r.work_type_id, r.place_id, r.schedule_date, r.day_part,
           s.name AS service_name, p.name AS place_name,
           (SELECT b.id FROM booking b WHERE b.offer_id = o.id ORDER BY b.created_at DESC LIMIT 1) AS booking_id
    FROM offer o JOIN request r ON r.id = o.request_id
    JOIN service s ON s.id = r.service_id JOIN place p ON p.id = r.place_id
"""


def sent_shape(conn, row, all_texts):
    """Put one of his sent prices into its shape.
    requestContext says which job it was, with the approximate area only.
    bookingId is set once she chose him."""
    return {
        "id": row["id"],
        "requestId": row["request_id"],
        "status": row["status"],
        "pricing": P.pricing_shape(
            conn,
            row["service_id"],
            row["pricing_type"],
            row["exact_amount_minor"],
            row["rate_minor"],
            row["unit_code"],
            row["inspection_charge_minor"],
        ),
        "offeredDaypart": row["offered_day_part"],
        "note": row["note"],
        "requestContext": {
            "service": {"id": row["service_id"], "name": row["service_name"]},
            "workType": P.work_type(conn, row["work_type_id"]),
            "approximateArea": P.approximate_area(row["place_id"], row["place_name"], all_texts),
            "schedule": {"date": row["schedule_date"].isoformat(), "dayPart": row["day_part"]},
        },
        "bookingId": row["booking_id"],
        "createdAt": clock.iso_utc(row["created_at"]),
    }


def sent_offer(conn, partner_id, offer_id):
    """Read one of his offers in the sent-offer shape.
    Somebody else's offer is 404.
    Used after sending and withdrawing."""
    row = one(conn, SENT_SQL + " WHERE o.id = :id AND o.partner_id = :p", id=offer_id, p=partner_id)
    if row is None:
        raise not_found()
    return sent_shape(conn, row, texts(conn))


def sent_offers(conn, partner_id, statuses=None):
    """List every price he has sent, newest first.
    statuses narrows the list, e.g. to ACTIVE for his home screen.
    Five statuses exist and are sent exactly as stored."""
    sql = SENT_SQL + " WHERE o.partner_id = :p"
    params = {"p": partner_id}
    if statuses:
        sql += " AND o.status = ANY(:st)"
        params["st"] = list(statuses)
    rows = many(conn, sql + " ORDER BY o.created_at DESC", **params)
    all_texts = texts(conn)
    return [sent_shape(conn, r, all_texts) for r in rows]
