import logging
from datetime import timedelta

from app.core import clock
from app.core.adapters.push import notify_user
from app.core.db import many, one, run, scalar, tx
from app.shared.settings import geography, platform_setting

log = logging.getLogger("neoseva.matching")

ELIGIBLE_SQL = """
    SELECT pp.user_id
    FROM partner_profile pp
    JOIN partner_service ps ON ps.partner_id = pp.user_id AND ps.service_id = :service
    JOIN place bp ON bp.id = pp.base_place_id
    JOIN place rp ON rp.id = :place
    WHERE pp.status = 'ACTIVE' AND pp.accepting_new_jobs
      AND pp.user_id <> :customer
      AND (CAST(:equipment AS TEXT) IS NULL
           OR NOT EXISTS (SELECT 1 FROM service_equipment se WHERE se.service_id = :service)
           OR CAST(:equipment AS TEXT) = ANY(ps.equipment_ids))
      AND NOT EXISTS (SELECT 1 FROM opportunity_notification n
                      WHERE n.request_id = :request AND n.partner_id = pp.user_id)
"""


def required_equipment(conn, work_type_id):
    """Find which equipment a work type needs, if any.
    Configured on the work-type option by the admin.
    None means any partner doing the service will do."""
    if not work_type_id:
        return None
    return scalar(conn, "SELECT equipment_id FROM request_question_option WHERE id = :id", id=work_type_id)


def eligible_partners(conn, request, limit):
    """Find the next partners who may see a request, nearest first.
    Active, accepting, doing the service, owning the equipment, within their radius.
    Partners already told about it are skipped."""
    rows = many(
        conn,
        ELIGIBLE_SQL
        + """ AND pp.travel_radius_km IS NOT NULL
              AND ST_DWithin(bp.centre, rp.centre, pp.travel_radius_km * 1000)
            ORDER BY ST_Distance(bp.centre, rp.centre), pp.user_id LIMIT :limit""",
        service=request["service_id"],
        place=request["place_id"],
        customer=request["customer_id"],
        equipment=required_equipment(conn, request["work_type_id"]),
        request=request["id"],
        limit=limit,
    )
    return [r["user_id"] for r in rows]


def preferred_is_eligible(conn, request):
    """Say whether her preferred partner can still be told first.
    He must be active, accepting and doing the service; distance is not checked.
    She chose him, so his radius does not stand between them."""
    row = one(
        conn,
        ELIGIBLE_SQL + " AND pp.user_id = :preferred",
        service=request["service_id"],
        place=request["place_id"],
        customer=request["customer_id"],
        equipment=required_equipment(conn, request["work_type_id"]),
        request=request["id"],
        preferred=request["preferred_partner_id"],
    )
    return row is not None


def _record(conn, request_id, partner_ids, batch):
    """Write one opportunity_notification row per partner told.
    The row is the record that he was eligible and told, push or not.
    Returns the ids actually written."""
    for partner_id in partner_ids:
        run(
            conn,
            """INSERT INTO opportunity_notification (request_id, partner_id, batch, notified_at)
               VALUES (:r, :p, :b, :now) ON CONFLICT DO NOTHING""",
            r=request_id,
            p=partner_id,
            b=batch,
            now=clock.now_utc(),
        )
    return partner_ids


def advance(request_id):
    """Send the next batch for a request if one is due.
    Batch 0 is her preferred partner; later batches wait and stop once anybody offers.
    Locks the request so a booking and a batch can never interleave."""
    with tx() as conn:
        request = one(conn, "SELECT * FROM request WHERE id = :id FOR UPDATE", id=request_id)
        if request is None or request["state"] != "REQUESTED":
            return []
        geo = geography(conn, request["geography_id"])
        if request["schedule_date"] < clock.local_today(geo["timezone"]):
            return []
        settings = platform_setting(conn)
        last = one(
            conn,
            "SELECT max(batch) AS batch, max(notified_at) AS at FROM opportunity_notification WHERE request_id = :r",
            r=request_id,
        )
        now = clock.now_utc()
        if last["batch"] is None:
            if request["preferred_partner_id"] and preferred_is_eligible(conn, request):
                told = _record(conn, request_id, [request["preferred_partner_id"]], 0)
            else:
                told = _record(conn, request_id, eligible_partners(conn, request, settings["matching_batch_size"]), 1)
        elif last["batch"] == 0:
            head_start = timedelta(minutes=geo["preferred_partner_head_start_minutes"])
            preferred_declined = scalar(
                conn,
                "SELECT 1 FROM opportunity_notification WHERE request_id = :r AND batch = 0 AND declined_at IS NOT NULL",
                r=request_id,
            )
            if now - request["created_at"] < head_start and not preferred_declined:
                return []
            told = _record(conn, request_id, eligible_partners(conn, request, settings["matching_batch_size"]), 1)
        else:
            all_declined = not scalar(
                conn, "SELECT 1 FROM opportunity_notification WHERE request_id = :r AND declined_at IS NULL", r=request_id
            )
            elapsed = now - last["at"] >= timedelta(minutes=settings["matching_batch_interval_minutes"])
            if not elapsed and not all_declined:
                return []
            offers = scalar(conn, "SELECT count(*) FROM offer WHERE request_id = :r AND status = 'ACTIVE'", r=request_id)
            if offers:
                return []
            told = _record(
                conn, request_id, eligible_partners(conn, request, settings["matching_batch_size"]), last["batch"] + 1
            )
    for partner_id in told:
        notify_user(partner_id, "PARTNER", "NEW_OPPORTUNITY", {"requestId": request_id})
    return told


def advance_all():
    """Run advance for every open request.
    Called by the scheduler tick; each request is its own transaction.
    One failing request never stops the others."""
    with tx() as conn:
        ids = [r["id"] for r in many(conn, "SELECT id FROM request WHERE state = 'REQUESTED' ORDER BY created_at")]
    for request_id in ids:
        try:
            advance(request_id)
        except Exception:
            log.exception("matching failed for %s", request_id)
