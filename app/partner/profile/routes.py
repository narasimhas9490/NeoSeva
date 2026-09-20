from flask import Blueprint, current_app, g

from app.config import language
from app.core.auth import require_user
from app.core.db import many, run, scalar, tx
from app.core.envelope import body, ok
from app.core.errors import ApiError, bad_request, conflict, unprocessable
from app.core.idempotency import idempotent
from app.partner.profile import service as S
from app.shared.settings import catalog_setting, place, place_is_served, service_runs_in

bp = Blueprint("partner_profile", __name__, url_prefix="/partner/me")


@bp.get("")
@require_user("PARTNER")
def get_me():
    """Return his whole profile, the densest read in his app.
    Every screen reads something from it.
    nextStep is what the onboarding router follows."""
    with tx() as conn:
        return ok(S.profile_shape(conn, g.user_id))


@bp.patch("/basic")
@require_user("PARTNER")
def patch_basic():
    """Save his name, base village, experience, photo and language.
    Moves nextStep on to SERVICES, never backwards.
    Each field is checked against the catalog it comes from."""
    data = body()
    name = data.get("fullName")
    if not isinstance(name, str) or not name.strip():
        raise bad_request("INVALID_NAME", "fullName is required.")
    with tx() as conn:
        row = S.load(conn, g.user_id, lock=True)
        place_row = place(conn, data.get("basePlaceId")) if isinstance(data.get("basePlaceId"), str) else None
        if place_row is None:
            raise unprocessable("PLACE_NOT_FOUND", "No such village.")
        if not place_is_served(place_row):
            raise unprocessable("PLACE_NOT_SERVED", "That village is not in a served area.")
        experience = data.get("experienceRange")
        if experience is not None and not scalar(conn, "SELECT 1 FROM experience_range WHERE code = :c", c=experience):
            raise unprocessable("INVALID_EXPERIENCE_RANGE", "That experience range is not offered.")
        photo = data.get("profilePhotoMediaId")
        if photo is not None and not scalar(
            conn,
            """SELECT 1 FROM media WHERE id = :m AND user_id = :u
               AND purpose = 'PARTNER_PROFILE_PHOTO' AND deleted_at IS NULL""",
            m=photo,
            u=g.user_id,
        ):
            raise unprocessable("INVALID_MEDIA_REFERENCE", "That photo is missing or not yours.")
        run(
            conn,
            """UPDATE partner_profile SET display_name = :n, base_place_id = :p, experience_range_code = :e,
                   profile_photo_media_id = :m, next_step = :step WHERE user_id = :u""",
            n=name.strip()[:80],
            p=place_row["id"],
            e=experience,
            m=photo,
            step=S.later_step(row["next_step"], "SERVICES"),
            u=g.user_id,
        )
        if data.get("language"):
            run(conn, "UPDATE app_user SET language = :l WHERE id = :u", l=language(data["language"], current_app.config["NS"]), u=g.user_id)
        return ok(S.profile_shape(conn, g.user_id))


@bp.put("/services")
@require_user("PARTNER")
def put_services():
    """Replace the whole list of work he does.
    Each service must run in his area and each equipment must belong to it.
    An empty list is allowed; he is entitled to do no work."""
    entries = body().get("services")
    if not isinstance(entries, list):
        raise bad_request("INVALID_REQUEST", "services must be an array.")
    with tx() as conn:
        row = S.load(conn, g.user_id, lock=True)
        geography_id = S.geography_of(conn, row)
        clean = {}
        for entry in entries:
            service_id = entry.get("serviceId") if isinstance(entry, dict) else None
            if not isinstance(service_id, str) or not scalar(conn, "SELECT 1 FROM service WHERE id = :s", s=service_id):
                raise unprocessable("SERVICE_NOT_FOUND", "No such service.", {"serviceId": service_id})
            equipment = entry.get("equipmentIds") or []
            if not service_runs_in(conn, service_id, geography_id):
                raise unprocessable("SERVICE_NOT_AVAILABLE", "That service does not run in your area.", {"serviceId": service_id})
            known = {e["id"] for e in many(conn, "SELECT id FROM service_equipment WHERE service_id = :s", s=service_id)}
            if not isinstance(equipment, list) or not set(equipment) <= known:
                raise unprocessable("INVALID_EQUIPMENT", "That equipment does not belong to the service.", {"serviceId": service_id})
            clean[service_id] = list(dict.fromkeys(equipment))
        run(conn, "DELETE FROM partner_service WHERE partner_id = :u", u=g.user_id)
        for service_id, equipment in clean.items():
            run(
                conn,
                "INSERT INTO partner_service (partner_id, service_id, equipment_ids) VALUES (:u, :s, :e)",
                u=g.user_id,
                s=service_id,
                e=equipment,
            )
        if row["next_step"] != "BASIC_PROFILE":
            run(
                conn,
                "UPDATE partner_profile SET next_step = :s WHERE user_id = :u",
                s=S.later_step(row["next_step"], "TRAVEL_RADIUS"),
                u=g.user_id,
            )
        return ok(S.profile_shape(conn, g.user_id))


@bp.patch("")
@require_user("PARTNER")
def patch_me():
    """Change his travel radius or whether he is accepting work.
    The radius must be one of the served chips; a suspended man cannot turn himself on.
    status is ours, acceptingNewJobs is his."""
    data = body()
    with tx() as conn:
        row = S.load(conn, g.user_id, lock=True)
        if "travelRadiusKm" in data:
            radius = data["travelRadiusKm"]
            if radius not in list(catalog_setting(conn)["travel_distances_km"] or []) or isinstance(radius, bool):
                raise unprocessable("INVALID_TRAVEL_RADIUS", "Pick one of the offered distances.")
            if row["next_step"] in ("BASIC_PROFILE", "SERVICES"):
                raise unprocessable("SETUP_INCOMPLETE", "Finish the earlier steps first.", {"nextStep": row["next_step"]})
            run(
                conn,
                "UPDATE partner_profile SET travel_radius_km = :r, next_step = :s WHERE user_id = :u",
                r=radius,
                s=S.later_step(row["next_step"], "NOTIFICATIONS"),
                u=g.user_id,
            )
        if "acceptingNewJobs" in data:
            accepting = data["acceptingNewJobs"]
            if not isinstance(accepting, bool):
                raise bad_request("INVALID_REQUEST", "acceptingNewJobs must be true or false.")
            if row["status"] == "SUSPENDED":
                raise conflict("PARTNER_SUSPENDED", "This account is stopped.")
            if accepting and row["next_step"] != "COMPLETE":
                raise conflict("SETUP_INCOMPLETE", "Finish setting up first.", {"nextStep": row["next_step"]})
            run(conn, "UPDATE partner_profile SET accepting_new_jobs = :a WHERE user_id = :u", a=accepting, u=g.user_id)
        return ok(S.profile_shape(conn, g.user_id))


@bp.post("/activate")
@require_user("PARTNER")
@idempotent
def activate():
    """He says he has finished setting up.
    Sets ACTIVATED and COMPLETE; ACTIVE is an admin decision after the ID check.
    SETUP_INCOMPLETE names the step to send him back to."""
    with tx() as conn:
        row = S.load(conn, g.user_id, lock=True)
        if row["status"] == "SUSPENDED":
            raise conflict("PARTNER_SUSPENDED", "This account is stopped.")
        if row["status"] in ("ACTIVATED", "ACTIVE"):
            return ok(S.profile_shape(conn, g.user_id))
        step = S.missing_step(row, conn)
        if step:
            raise ApiError(422, "SETUP_INCOMPLETE", "A setup step is unfinished.", {"nextStep": step})
        run(
            conn,
            "UPDATE partner_profile SET status = 'ACTIVATED', next_step = 'COMPLETE' WHERE user_id = :u",
            u=g.user_id,
        )
        return ok(S.profile_shape(conn, g.user_id))
