from flask import Blueprint, g

from app.core.auth import require_user
from app.core.db import one, run, tx
from app.core.envelope import body, ok
from app.core.errors import bad_request, unprocessable
from app.shared.presenters import completed_jobs_as_customer
from app.shared.settings import place

bp = Blueprint("customer_profile", __name__, url_prefix="/customer")


def profile_shape(conn, user_id):
    """Describe her profile for the customer app.
    completedJobs is a count over bookings, never a column.
    displayName may be null; she is never forced to give one."""
    row = one(
        conn,
        """SELECT u.id, u.phone_e164, c.display_name, c.default_place_id
           FROM app_user u JOIN customer_profile c ON c.user_id = u.id WHERE u.id = :u""",
        u=user_id,
    )
    return {
        "id": row["id"],
        "displayName": row["display_name"],
        "phoneNumber": row["phone_e164"],
        "defaultPlaceId": row["default_place_id"],
        "completedJobs": completed_jobs_as_customer(conn, user_id),
    }


@bp.get("/me")
@require_user("CUSTOMER")
def get_me():
    """Return her name, number, default village and completed jobs.
    The profile exists from the moment she verified in this app.
    Somebody else's profile is never reachable."""
    with tx() as conn:
        return ok(profile_shape(conn, g.user_id))


@bp.patch("/me")
@require_user("CUSTOMER")
def patch_me():
    """Change her display name or default village.
    A blank name becomes null; an unknown village is PLACE_NOT_FOUND.
    Returns the updated profile."""
    data = body()
    with tx() as conn:
        if "displayName" in data:
            name = data["displayName"]
            if name is not None and not isinstance(name, str):
                raise bad_request("INVALID_DISPLAY_NAME", "displayName must be a string or null.")
            run(
                conn,
                "UPDATE customer_profile SET display_name = :n WHERE user_id = :u",
                n=name.strip()[:80] if name and name.strip() else None,
                u=g.user_id,
            )
        if "defaultPlaceId" in data:
            place_id = data["defaultPlaceId"]
            if place_id is not None and place(conn, place_id) is None:
                raise unprocessable("PLACE_NOT_FOUND", "No such village.")
            run(conn, "UPDATE customer_profile SET default_place_id = :p WHERE user_id = :u", p=place_id, u=g.user_id)
        return ok(profile_shape(conn, g.user_id))
