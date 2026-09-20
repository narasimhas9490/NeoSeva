from app.core.db import many, one, scalar
from app.core.errors import not_found

STEPS = ("BASIC_PROFILE", "SERVICES", "TRAVEL_RADIUS", "NOTIFICATIONS", "COMPLETE")


def later_step(current, candidate):
    """Return whichever onboarding step is further along.
    nextStep never moves backwards.
    Both arguments are step names from STEPS."""
    return candidate if STEPS.index(candidate) > STEPS.index(current) else current


def load(conn, user_id, lock=False):
    """Load a partner profile row or raise 404.
    lock takes a row lock for the write that follows.
    A person without a partner profile never signed in to the partner app."""
    row = one(conn, "SELECT * FROM partner_profile WHERE user_id = :u" + (" FOR UPDATE" if lock else ""), u=user_id)
    if row is None:
        raise not_found("PARTNER_NOT_FOUND", "No partner profile.")
    return row


def missing_step(row, conn):
    """Find the first setup step whose data is missing.
    Name and base village, then services, then radius.
    Returns None when setup can be finished."""
    if not row["display_name"] or not row["base_place_id"]:
        return "BASIC_PROFILE"
    if STEPS.index(row["next_step"]) < STEPS.index("TRAVEL_RADIUS"):
        return "SERVICES"
    if row["travel_radius_km"] is None:
        return "TRAVEL_RADIUS"
    return None


def geography_of(conn, row):
    """Return the geography a partner works in.
    It is his base village's geography, or the default before he picks one.
    Used to check which services he may choose."""
    if row["base_place_id"]:
        return scalar(conn, "SELECT geography_id FROM place WHERE id = :p", p=row["base_place_id"])
    from flask import current_app

    return current_app.config["NS"].default_area_id


def profile_shape(conn, user_id):
    """Describe a partner for every screen of his app.
    status is ours, acceptingNewJobs is his, and they are different things.
    workBlock stays null until the chunk 5 gate is built."""
    row = one(
        conn,
        """SELECT pp.*, m.url AS photo_url, p.name AS base_place_name, iv.status AS identity_status
           FROM partner_profile pp
           LEFT JOIN media m ON m.id = pp.profile_photo_media_id AND m.deleted_at IS NULL
           LEFT JOIN place p ON p.id = pp.base_place_id
           LEFT JOIN identity_verification iv ON iv.partner_id = pp.user_id
           WHERE pp.user_id = :u""",
        u=user_id,
    )
    if row is None:
        raise not_found("PARTNER_NOT_FOUND", "No partner profile.")
    services = many(
        conn,
        """SELECT s.id, s.name FROM partner_service ps JOIN service s ON s.id = ps.service_id
           WHERE ps.partner_id = :u ORDER BY s.sort_order, s.name""",
        u=user_id,
    )
    return {
        "id": row["user_id"],
        "displayName": row["display_name"] or None,
        "profilePhotoUrl": row["photo_url"],
        "profilePhotoMediaId": row["profile_photo_media_id"],
        "basePlaceId": row["base_place_id"],
        "basePlaceName": row["base_place_name"],
        "experienceRange": row["experience_range_code"],
        "status": row["status"],
        "statusNote": row["status_note"],
        "identityStatus": row["identity_status"],
        "acceptingNewJobs": row["accepting_new_jobs"],
        "travelRadiusKm": row["travel_radius_km"],
        "services": [{"id": s["id"], "name": s["name"]} for s in services],
        "nextStep": row["next_step"],
        "workBlock": None,
        "acceptedTermsVersion": row["accepted_terms_version"],
        "acceptedTermsAt": row["accepted_terms_at"].isoformat() if row["accepted_terms_at"] else None,
    }
