from decimal import Decimal

from app.core.db import one, run, scalar
from app.core.errors import unprocessable


def validate_question(conn, values, key):
    """Refuse a question the app could not draw or that points the wrong way.
    Number questions need a unit and all four bounds; showIf points only upwards.
    Runs before the row is written."""
    current = one(conn, "SELECT * FROM request_question WHERE id = :id", id=(key or {}).get("id")) if key else None
    merged = {**(current or {}), **values}
    if merged.get("type") == "NUMBER_WITH_UNIT":
        missing = [f for f in ("unit_code", "unit_label", "min_value", "max_value", "step_value", "default_value") if merged.get(f) in (None, "")]
        if missing:
            raise unprocessable("QUESTION_INCOMPLETE", f"A number question needs {', '.join(missing)}.")
        low, high, step, default = (Decimal(str(merged[f])) for f in ("min_value", "max_value", "step_value", "default_value"))
        if not (low <= default <= high) or step <= 0 or low > high:
            raise unprocessable("QUESTION_INCOMPLETE", "Bounds must satisfy min <= default <= max and step > 0.")
    parent_id = merged.get("depends_on_question_id")
    if parent_id:
        parent = one(conn, "SELECT service_id, sort_order FROM request_question WHERE id = :id", id=parent_id)
        if parent is None or parent["service_id"] != merged.get("service_id"):
            raise unprocessable("INVALID_SHOW_IF", "showIf must point at a question of the same service.")
        if int(parent["sort_order"]) >= int(merged.get("sort_order") or 0) or parent_id == merged.get("id"):
            raise unprocessable("INVALID_SHOW_IF", "showIf may only point at a question above this one.")
        if not merged.get("depends_on_values"):
            raise unprocessable("INVALID_SHOW_IF", "showIf needs the answers that reveal this question.")


def validate_help_me_choose(conn, values, key):
    """Refuse a help-me-choose option whose service runs nowhere.
    An option must never send her to wait for offers that cannot come.
    The null-service 'something else' option is always allowed."""
    service_id = values.get("service_id")
    if service_id and not scalar(conn, "SELECT 1 FROM service_geography WHERE service_id = :s", s=service_id):
        raise unprocessable("SERVICE_NOT_RUNNING", "That service does not run in any area yet.")


def bump_template_version(conn):
    """Raise the template version of every question together.
    Any change to any question or option makes a new version.
    Posts from apps holding the old questions are then refused as stale."""
    run(conn, "UPDATE request_question SET template_version = template_version + 1")


def bump_help_me_choose_version(conn):
    """Raise the help-me-choose version after its options change.
    Lets us tell later which version somebody answered.
    Stored on catalog_setting and served in the catalog."""
    run(conn, "UPDATE catalog_setting SET help_me_choose_version = help_me_choose_version + 1")


def default_question_version(conn, values):
    """Give a new question the current template version.
    A new question joins the version being served, then everything is bumped.
    The admin never types the version by hand."""
    values["template_version"] = scalar(conn, "SELECT COALESCE(MAX(template_version), 1) FROM request_question")


BEFORE = {"request_question": validate_question, "help_me_choose_option": validate_help_me_choose}
AFTER = {
    "request_question": bump_template_version,
    "request_question_option": bump_template_version,
    "help_me_choose_option": bump_help_me_choose_version,
}
