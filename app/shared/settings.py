from flask import current_app

from app.core.db import many, one
from app.core.errors import ApiError


def catalog_setting(conn):
    """Read the single catalog_setting row.
    Business numbers and served sentences all live here.
    A missing row is a 500, because the seed was never run."""
    row = one(conn, "SELECT * FROM catalog_setting WHERE id = 1")
    if row is None:
        raise ApiError(500, "CATALOG_NOT_CONFIGURED", "catalog_setting has no row.")
    return row


def platform_setting(conn):
    """Read the single platform_setting row.
    Operational knobs: matching batches, OTP daily cap, PIN lockout.
    A missing row is a 500, because the seed was never run."""
    row = one(conn, "SELECT * FROM platform_setting WHERE id = 1")
    if row is None:
        raise ApiError(500, "PLATFORM_NOT_CONFIGURED", "platform_setting has no row.")
    return row


def texts(conn):
    """Read every served display sentence into a dict by key.
    Ending labels, daypart names and format strings live here.
    Admin edits them without an app release."""
    return {r["key"]: r["label"] for r in many(conn, "SELECT key, label FROM display_text")}


def text_for(all_texts, key, default="", **values):
    """Pick one display sentence and fill its {placeholders}.
    Falls back to the default when the key has not been written.
    Unknown placeholders are left as they are."""
    template = all_texts.get(key) or default
    for name, value in values.items():
        template = template.replace("{" + name + "}", "" if value is None else str(value))
    return template


def geography(conn, geography_id):
    """Read one geography row by id.
    Returns None when it does not exist.
    Its timezone and daypart hours drive every schedule rule."""
    return one(conn, "SELECT * FROM geography WHERE id = :id", id=geography_id)


def default_geography(conn):
    """Read the geography configured as DEFAULT_AREA_ID.
    Used when an app sends no areaId.
    Returns None when that row is missing."""
    return geography(conn, current_app.config["NS"].default_area_id)


def place(conn, place_id):
    """Read one place with its centre as latitude and longitude.
    Also carries its geography's active flag for served checks.
    Returns None when the id is unknown."""
    return one(
        conn,
        """SELECT p.id, p.geography_id, p.name, p.mandal, p.district, p.state, p.is_active,
                  ST_Y(p.centre::geometry) AS latitude, ST_X(p.centre::geometry) AS longitude,
                  g.is_active AS geography_active
           FROM place p JOIN geography g ON g.id = p.geography_id WHERE p.id = :id""",
        id=place_id,
    )


def place_is_served(place_row):
    """Say whether a place sits in an active, served geography.
    Both the village and its geography must be active.
    A None place is never served."""
    return bool(place_row and place_row["is_active"] and place_row["geography_active"])


def service_runs_in(conn, service_id, geography_id):
    """Say whether an active service runs in a geography.
    A service with no service_geography row runs nowhere.
    That is how a service is tested before launch."""
    return bool(
        one(
            conn,
            """SELECT 1 FROM service s JOIN service_geography sg ON sg.service_id = s.id
               WHERE s.id = :s AND sg.geography_id = :g AND s.is_active""",
            s=service_id,
            g=geography_id,
        )
    )
