from flask import Blueprint, request

from app.common.auth.service import valid_phone
from app.common.catalog.service import resolve_area
from app.core.db import many, one, run, tx
from app.core.envelope import accepted_empty, body, ok, use_timezone
from app.core.errors import bad_request
from app.core.ids import new_id

bp = Blueprint("locations", __name__)


@bp.get("/locations/places")
def list_places():
    """Send every village in an area, unpaged.
    The app holds the list and filters it as she types.
    Unknown areas get an empty list, never a 404."""
    with tx() as conn:
        geo, _ = resolve_area(conn, request.args.get("areaId"))
        if geo is None:
            return ok([])
        use_timezone(geo["timezone"])
        rows = many(
            conn,
            """SELECT id, geography_id, name, mandal, district, state,
                      ST_Y(centre::geometry) AS lat, ST_X(centre::geometry) AS lng
               FROM place WHERE geography_id = :g AND is_active ORDER BY name""",
            g=geo["id"],
        )
    return ok(
        [
            {
                "id": r["id"],
                "areaId": r["geography_id"],
                "name": r["name"],
                "mandal": r["mandal"],
                "district": r["district"],
                "state": r["state"],
                "latitude": round(r["lat"], 6),
                "longitude": round(r["lng"], 6),
            }
            for r in rows
        ]
    )


def _coordinates():
    """Read latitude and longitude from the query string.
    Accepts latitude/longitude or the short lat/lng.
    Anything missing or out of range is 400 INVALID_COORDINATES."""
    raw_lat = request.args.get("latitude", request.args.get("lat"))
    raw_lng = request.args.get("longitude", request.args.get("lng"))
    try:
        lat, lng = float(raw_lat), float(raw_lng)
    except (TypeError, ValueError):
        raise bad_request("INVALID_COORDINATES", "latitude and longitude are required numbers.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise bad_request("INVALID_COORDINATES", "Coordinates are out of range.")
    return lat, lng


@bp.get("/geographies/resolve")
def resolve():
    """Say which served area and village a pin falls in.
    Villages are found by their real boundaries, never the nearest centre.
    Outside everything is 200 with nulls; inside the area but no village is a null place."""
    lat, lng = _coordinates()
    point = "ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)"
    with tx() as conn:
        place = one(
            conn,
            f"""SELECT p.id, p.name, g.id AS geo_id, g.label AS geo_label, g.timezone
                FROM place p JOIN geography g ON g.id = p.geography_id
                WHERE p.is_active AND g.is_active AND p.boundary IS NOT NULL
                  AND ST_Contains(p.boundary::geometry, {point}) LIMIT 1""",
            lat=lat,
            lng=lng,
        )
        if place:
            use_timezone(place["timezone"])
            return ok(
                {
                    "geography": {"id": place["geo_id"], "label": place["geo_label"]},
                    "place": {"id": place["id"], "name": place["name"]},
                }
            )
        geo = one(
            conn,
            f"""SELECT id, label, timezone FROM geography
                WHERE is_active AND boundary IS NOT NULL AND ST_Contains(boundary::geometry, {point}) LIMIT 1""",
            lat=lat,
            lng=lng,
        )
    if geo:
        use_timezone(geo["timezone"])
        return ok({"geography": {"id": geo["id"], "label": geo["label"]}, "place": None})
    return ok({"geography": None, "place": None})


@bp.post("/locations/place-interest")
def place_interest():
    """Record the name of a village we do not serve yet.
    No coordinates, and no deduplication: ten people is ten rows.
    Returns 202 with an empty body; the app ignores failures."""
    data = body()
    name = data.get("placeName")
    if not isinstance(name, str) or not name.strip():
        raise bad_request("INVALID_PLACE_NAME", "placeName must not be blank.")
    phone = data.get("phoneNumber")
    with tx() as conn:
        run(
            conn,
            "INSERT INTO place_interest (id, name_typed, phone_e164) VALUES (:id, :n, :p)",
            id=new_id("pli"),
            n=name.strip()[:200],
            p=phone if valid_phone(phone) else None,
        )
    return accepted_empty()
