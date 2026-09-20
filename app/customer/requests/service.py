from app.core import clock
from app.core.db import many, one, run, scalar, tx
from app.core.errors import ApiError, bad_request, conflict, not_found, unprocessable
from app.core.ids import new_id
from app.core.paging import PAGE_SIZE, decode_cursor, page
from app.shared import matching
from app.shared import presenters as P
from app.shared.settings import geography, place, place_is_served, service_runs_in, texts
from app.shared.templates import validate_answers


def _coordinates(location):
    """Read the optional pin and its accuracy from a location object.
    Latitude and longitude come together or not at all.
    Returns (lat, lng, accuracy), all None when there is no pin."""
    lat, lng, acc = location.get("latitude"), location.get("longitude"), location.get("accuracyMeters")
    if lat is None and lng is None:
        return None, None, None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lng)):
        raise bad_request("INVALID_COORDINATES", "latitude and longitude come together as numbers.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise bad_request("INVALID_COORDINATES", "Coordinates are out of range.")
    if acc is not None and (not isinstance(acc, (int, float)) or isinstance(acc, bool) or acc < 0):
        raise bad_request("INVALID_COORDINATES", "accuracyMeters must be a positive number.")
    return float(lat), float(lng), None if acc is None else float(acc)


def _media_ids(conn, customer_id, raw):
    """Check that every attached photo exists, is hers and is a request photo.
    Duplicates are dropped and the order is kept.
    Anything else is 422 INVALID_MEDIA_REFERENCE."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(m, str) for m in raw):
        raise unprocessable("INVALID_MEDIA_REFERENCE", "mediaIds must be an array of ids.")
    ids = list(dict.fromkeys(raw))
    if ids:
        found = scalar(
            conn,
            """SELECT count(*) FROM media WHERE id = ANY(:ids) AND user_id = :u
               AND purpose = 'REQUEST_PHOTO' AND deleted_at IS NULL""",
            ids=ids,
            u=customer_id,
        )
        if found != len(ids):
            raise unprocessable("INVALID_MEDIA_REFERENCE", "A photo is missing or is not yours.")
    return ids


def validate(conn, customer_id, data):
    """Run every check a request must pass before it is written.
    Area, place, schedule, answers and photos, each with its own code.
    Returns the clean values ready for the database."""
    if scalar(conn, "SELECT is_restricted FROM customer_profile WHERE user_id = :u", u=customer_id):
        raise ApiError(403, "CUSTOMER_RESTRICTED", "This account cannot post work.")
    service_id = data.get("serviceId")
    schedule = data.get("schedule") if isinstance(data.get("schedule"), dict) else {}
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    on_date = clock.parse_date(schedule.get("date"))
    day_part = schedule.get("dayPart")
    if not isinstance(service_id, str):
        raise bad_request("INVALID_REQUEST", "serviceId is required.")
    if on_date is None or day_part not in clock.REQUEST_DAYPARTS:
        raise bad_request("INVALID_SCHEDULE", "schedule needs a YYYY-MM-DD date and a known dayPart.")
    template_version = data.get("templateVersion")
    if not isinstance(template_version, int) or isinstance(template_version, bool):
        raise bad_request("INVALID_REQUEST", "templateVersion is required.")
    place_row = place(conn, location.get("placeId")) if isinstance(location.get("placeId"), str) else None
    if not place_is_served(place_row):
        raise unprocessable("PLACE_NOT_SERVED", "That village is not in a served area.")
    if not service_runs_in(conn, service_id, place_row["geography_id"]):
        raise unprocessable("SERVICE_NOT_AVAILABLE", "That service does not run in this area.")
    geo = geography(conn, place_row["geography_id"])
    problem = clock.schedule_problem(geo, on_date, day_part)
    if problem:
        raise unprocessable(problem, "That day or time can no longer be booked.")
    work_type_id = data.get("workTypeId")
    if work_type_id is not None and not isinstance(work_type_id, str):
        raise bad_request("INVALID_REQUEST", "workTypeId must be a string.")
    version, answers = validate_answers(conn, service_id, template_version, data.get("answers"), work_type_id)
    media_ids = _media_ids(conn, customer_id, data.get("mediaIds"))
    lat, lng, accuracy = _coordinates(location)
    preferred = data.get("preferredPartnerId")
    if preferred is not None and not scalar(
        conn, "SELECT 1 FROM partner_profile WHERE user_id = :p AND status = 'ACTIVE' AND user_id <> :c", p=preferred, c=customer_id
    ):
        preferred = None
    origin = data.get("origin") if isinstance(data.get("origin"), dict) else {}
    source = origin.get("source")
    description = data.get("description")
    landmark = location.get("landmark")
    return {
        "geography_id": geo["id"],
        "service_id": service_id,
        "work_type_id": work_type_id,
        "template_version": version,
        "schedule_date": on_date,
        "day_part": day_part,
        "place_id": place_row["id"],
        "landmark": landmark.strip()[:200] if isinstance(landmark, str) and landmark.strip() else None,
        "lat": lat,
        "lng": lng,
        "accuracy": accuracy,
        "description": description.strip()[:1000] if isinstance(description, str) and description.strip() else None,
        "origin_source": str(source)[:64] if source is not None else None,
        "preferred_partner_id": preferred,
        "answers": answers,
        "media_ids": media_ids,
    }


PIN_SQL = """CASE WHEN CAST(:lat AS DOUBLE PRECISION) IS NULL THEN NULL
             ELSE ST_SetSRID(ST_MakePoint(CAST(:lng AS DOUBLE PRECISION), CAST(:lat AS DOUBLE PRECISION)), 4326)::geography END"""


def _write_children(conn, request_id, values):
    """Replace a request's answers and photos.
    Used on create and on every edit.
    Only visible answered questions are stored."""
    run(conn, "DELETE FROM request_answer WHERE request_id = :r", r=request_id)
    run(conn, "DELETE FROM request_media WHERE request_id = :r", r=request_id)
    for a in values["answers"]:
        run(
            conn,
            """INSERT INTO request_answer (request_id, question_id, "values", unit_code, not_sure)
               VALUES (:r, :q, :v, :u, :n)""",
            r=request_id,
            q=a["question_id"],
            v=a["values"],
            u=a["unit_code"],
            n=a["not_sure"],
        )
    for position, media_id in enumerate(values["media_ids"]):
        run(conn, "INSERT INTO request_media (request_id, media_id, sort_order) VALUES (:r, :m, :s)", r=request_id, m=media_id, s=position)


def create(customer_id, data):
    """Post a new request after every check passes.
    The first matching batch goes out after the commit, never inside it.
    Returns the full request shape her next screen draws from."""
    with tx() as conn:
        values = validate(conn, customer_id, data)
        request_id = new_id("rqt")
        run(
            conn,
            f"""INSERT INTO request (id, customer_id, geography_id, service_id, work_type_id, template_version,
                    state, schedule_date, day_part, place_id, landmark, pin, accuracy_meters, description,
                    origin_source, preferred_partner_id, created_at)
                VALUES (:id, :c, :g, :s, :w, :v, 'REQUESTED', :d, :dp, :p, :l, {PIN_SQL}, :acc, :desc,
                    :o, :pp, :now)""",
            id=request_id,
            c=customer_id,
            g=values["geography_id"],
            s=values["service_id"],
            w=values["work_type_id"],
            v=values["template_version"],
            d=values["schedule_date"],
            dp=values["day_part"],
            p=values["place_id"],
            l=values["landmark"],
            lat=values["lat"],
            lng=values["lng"],
            acc=values["accuracy"],
            desc=values["description"],
            o=values["origin_source"],
            pp=values["preferred_partner_id"],
            now=clock.now_utc(),
        )
        _write_children(conn, request_id, values)
    try:
        matching.advance(request_id)
    except Exception:
        pass
    return read(customer_id, request_id)


def update(customer_id, request_id, data):
    """Edit a request nobody has offered on yet.
    Unsent fields keep their current values; every create check runs again.
    409 REQUEST_NOT_EDITABLE once an offer exists or the request moved on."""
    with tx() as conn:
        row = P.load_request(conn, request_id, customer_id, lock=True)
        if row is None:
            raise not_found()
        if row["state"] != "REQUESTED" or row["active_offer_count"] > 0:
            raise conflict("REQUEST_NOT_EDITABLE", "This request can no longer be edited.")
        merged = P.draft_shape(conn, row)
        for key in ("serviceId", "workTypeId", "templateVersion", "answers", "description", "mediaIds"):
            if key in data:
                merged[key] = data[key]
        for key in ("schedule", "location"):
            if isinstance(data.get(key), dict):
                merged[key] = {**merged[key], **data[key]}
        if "preferredPartnerId" in data:
            merged["preferredPartnerId"] = data["preferredPartnerId"]
        else:
            merged["preferredPartnerId"] = one(conn, "SELECT preferred_partner_id FROM request WHERE id = :id", id=request_id)[
                "preferred_partner_id"
            ]
        merged["origin"] = {"source": one(conn, "SELECT origin_source FROM request WHERE id = :id", id=request_id)["origin_source"]}
        values = validate(conn, customer_id, merged)
        run(
            conn,
            f"""UPDATE request SET geography_id = :g, service_id = :s, work_type_id = :w, template_version = :v,
                    schedule_date = :d, day_part = :dp, place_id = :p, landmark = :l, pin = {PIN_SQL},
                    accuracy_meters = :acc, description = :desc, preferred_partner_id = :pp
                WHERE id = :id""",
            id=request_id,
            g=values["geography_id"],
            s=values["service_id"],
            w=values["work_type_id"],
            v=values["template_version"],
            d=values["schedule_date"],
            dp=values["day_part"],
            p=values["place_id"],
            l=values["landmark"],
            lat=values["lat"],
            lng=values["lng"],
            acc=values["accuracy"],
            desc=values["description"],
            pp=values["preferred_partner_id"],
        )
        _write_children(conn, request_id, values)
    return read(customer_id, request_id)


def cancel(customer_id, request_id, data):
    """Call off a request nobody has been booked for.
    Cancelling twice is fine; a booked request belongs to chunk 6.
    Open offers close; nothing about money happens."""
    reason = data.get("reasonCode")
    note = data.get("note")
    with tx() as conn:
        row = P.load_request(conn, request_id, customer_id, lock=True)
        if row is None:
            raise not_found()
        if row["state"] == "CANCELLED":
            return P.request_shape(conn, row, texts(conn))
        if row["state"] != "REQUESTED":
            raise conflict("REQUEST_NOT_CANCELLABLE", "A booked request is cancelled from the booking.")
        run(
            conn,
            """UPDATE request SET state = 'CANCELLED', ended_reason = 'CANCELLED_BY_CUSTOMER', ended_at = :now,
                   cancel_reason_code = :rc, cancel_note = :n WHERE id = :id""",
            now=clock.now_utc(),
            rc=reason if isinstance(reason, str) else None,
            n=note.strip()[:1000] if isinstance(note, str) and note.strip() else None,
            id=request_id,
        )
        run(conn, "UPDATE offer SET status = 'CLOSED' WHERE request_id = :r AND status = 'ACTIVE'", r=request_id)
        return P.request_shape(conn, P.load_request(conn, request_id, customer_id), texts(conn))


def read(customer_id, request_id):
    """Read one of her requests in full, with a draft while editable.
    Somebody else's request is 404, never 403.
    The draft lets the editor show exactly what she entered."""
    with tx() as conn:
        row = P.load_request(conn, request_id, customer_id)
        if row is None:
            raise not_found()
        return P.request_shape(conn, row, texts(conn), with_draft=True)


def list_requests(customer_id, bucket, cursor):
    """List her requests in the ACTIVE or PAST bucket.
    ACTIVE is unpaged; PAST is cursor-paged newest first.
    Returns (items, nextCursor)."""
    if bucket not in ("ACTIVE", "PAST"):
        raise bad_request("INVALID_BUCKET", "bucket must be ACTIVE or PAST.")
    states = ("REQUESTED", "BOOKED", "ARRIVED") if bucket == "ACTIVE" else ("COMPLETED", "CANCELLED")
    after = decode_cursor(cursor) if bucket == "PAST" else None
    with tx() as conn:
        sql = P.REQUEST_SQL + " WHERE r.customer_id = :c AND r.state = ANY(:states)"
        params = {"c": customer_id, "states": list(states)}
        if after:
            sql += " AND (r.created_at, r.id) < (CAST(:at AS TIMESTAMPTZ), :aid)"
            params.update(at=after["at"], aid=after["id"])
        sql += " ORDER BY r.created_at DESC, r.id DESC"
        if bucket == "PAST":
            sql += " LIMIT :lim"
            params["lim"] = PAGE_SIZE + 1
        rows = many(conn, sql, **params)
        rows, next_cursor = page(rows, lambda r: {"at": r["created_at"].isoformat(), "id": r["id"]}) if bucket == "PAST" else (rows, None)
        all_texts = texts(conn)
        return [P.request_shape(conn, r, all_texts) for r in rows], next_cursor


def active_request(conn, customer_id):
    """Return her one running request, or None.
    If there are somehow two, the most recent wins.
    Used by her home screen."""
    row = one(
        conn,
        P.REQUEST_SQL
        + " WHERE r.customer_id = :c AND r.state IN ('REQUESTED','BOOKED','ARRIVED') ORDER BY r.created_at DESC LIMIT 1",
        c=customer_id,
    )
    return None if row is None else P.request_shape(conn, row, texts(conn))
