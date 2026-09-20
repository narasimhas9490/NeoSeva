from flask import Blueprint, g

from app.core.auth import require_user
from app.core.db import many, one, run, scalar, tx
from app.core.envelope import body, created, no_content, ok
from app.core.errors import bad_request, not_found, unprocessable
from app.core.ids import new_id
from app.customer.requests.service import PIN_SQL, _coordinates
from app.shared.settings import place, place_is_served

bp = Blueprint("customer_places", __name__, url_prefix="/customer")

PLACES_SQL = """
    SELECT sp.id, sp.label_code, pl.label, sp.place_id, p.name AS place_name, sp.landmark,
           ST_Y(sp.pin::geometry) AS latitude, ST_X(sp.pin::geometry) AS longitude, sp.accuracy_meters
    FROM saved_place sp JOIN place_label pl ON pl.code = sp.label_code JOIN place p ON p.id = sp.place_id
"""


def place_shape(row):
    """Put a saved place into its wire shape.
    label is resolved from labelCode and sent beside it.
    The pin fields are null when she saved no pin."""
    return {
        "id": row["id"],
        "labelCode": row["label_code"],
        "label": row["label"],
        "placeId": row["place_id"],
        "placeName": row["place_name"],
        "landmark": row["landmark"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "accuracyMeters": row["accuracy_meters"],
    }


@bp.get("/places")
@require_user("CUSTOMER")
def list_places():
    """List her saved places, newest first.
    Each carries the label word so the app never looks it up.
    Unpaged; she has a handful."""
    with tx() as conn:
        rows = many(conn, PLACES_SQL + " WHERE sp.user_id = :u ORDER BY sp.created_at DESC", u=g.user_id)
    return ok([place_shape(r) for r in rows])


@bp.post("/places")
@require_user("CUSTOMER")
def save_place():
    """Save a place under a name she taps.
    The same name, village and landmark updates the existing row and returns 200.
    A new one is 201."""
    data = body()
    label_code, place_id = data.get("labelCode"), data.get("placeId")
    landmark = data.get("landmark")
    landmark = landmark.strip()[:200] if isinstance(landmark, str) and landmark.strip() else None
    lat, lng, accuracy = _coordinates(data)
    with tx() as conn:
        if not isinstance(label_code, str) or not scalar(conn, "SELECT 1 FROM place_label WHERE code = :c", c=label_code):
            raise unprocessable("INVALID_PLACE_LABEL", "That place name is not offered.")
        place_row = place(conn, place_id) if isinstance(place_id, str) else None
        if place_row is None:
            raise unprocessable("PLACE_NOT_FOUND", "No such village.")
        if not place_is_served(place_row):
            raise unprocessable("PLACE_NOT_SERVED", "That village is not in a served area.")
        existing = scalar(
            conn,
            """SELECT id FROM saved_place WHERE user_id = :u AND label_code = :c AND place_id = :p
               AND landmark IS NOT DISTINCT FROM :l FOR UPDATE""",
            u=g.user_id,
            c=label_code,
            p=place_id,
            l=landmark,
        )
        if existing:
            if lat is not None:
                run(
                    conn,
                    f"UPDATE saved_place SET pin = {PIN_SQL}, accuracy_meters = :acc WHERE id = :id",
                    lat=lat,
                    lng=lng,
                    acc=accuracy,
                    id=existing,
                )
            return ok(place_shape(one(conn, PLACES_SQL + " WHERE sp.id = :id", id=existing)))
        saved_id = new_id("spl")
        run(
            conn,
            f"""INSERT INTO saved_place (id, user_id, label_code, place_id, landmark, pin, accuracy_meters)
                VALUES (:id, :u, :c, :p, :l, {PIN_SQL}, :acc)""",
            id=saved_id,
            u=g.user_id,
            c=label_code,
            p=place_id,
            l=landmark,
            lat=lat,
            lng=lng,
            acc=accuracy,
        )
        return created(place_shape(one(conn, PLACES_SQL + " WHERE sp.id = :id", id=saved_id)))


@bp.delete("/places/<saved_place_id>")
@require_user("CUSTOMER")
def delete_place(saved_place_id):
    """Remove one of her saved places outright.
    The id is the saved-place id (spl_), never the village id.
    Somebody else's is 404; removing is never refused otherwise."""
    if not saved_place_id.startswith("spl_"):
        raise bad_request("INVALID_SAVED_PLACE_ID", "Use the saved place id, not the village id.")
    with tx() as conn:
        if not run(conn, "DELETE FROM saved_place WHERE id = :id AND user_id = :u", id=saved_place_id, u=g.user_id):
            raise not_found()
    return no_content()
