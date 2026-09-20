import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

from app.core import clock
from app.core.db import many, run, tx
from app.core.idempotency import purge_old
from app.shared import matching

log = logging.getLogger("neoseva.jobs")


def expire_past_requests():
    """End open requests whose day has passed with nobody booked.
    With offers the ending is NOT_BOOKED; without any it is NO_PARTNER_AVAILABLE.
    Their open offers become CLOSED, never NOT_SELECTED."""
    now = clock.now_utc()
    with tx() as conn:
        rows = many(
            conn,
            """SELECT r.id, EXISTS (SELECT 1 FROM offer o WHERE o.request_id = r.id AND o.status <> 'WITHDRAWN') AS had_offers
               FROM request r JOIN geography g ON g.id = r.geography_id
               WHERE r.state = 'REQUESTED' AND r.schedule_date < (CAST(:now AS TIMESTAMPTZ) AT TIME ZONE g.timezone)::date
               FOR UPDATE OF r""",
            now=now,
        )
        for r in rows:
            run(
                conn,
                "UPDATE request SET state = 'CANCELLED', ended_reason = :e, ended_at = :now WHERE id = :id",
                e="NOT_BOOKED" if r["had_offers"] else "NO_PARTNER_AVAILABLE",
                now=now,
                id=r["id"],
            )
            run(conn, "UPDATE offer SET status = 'CLOSED' WHERE request_id = :r AND status = 'ACTIVE'", r=r["id"])
    return len(rows)


def tick():
    """Run every periodic job once.
    Matching batches, request expiry and idempotency cleanup.
    Each job is isolated so one failure does not stop the rest."""
    from flask import current_app

    runtime = current_app.extensions.get("runtime")
    if runtime:
        runtime.refresh()
        if not runtime.cfg.scheduler_enabled:
            return
    for job in (expire_past_requests, matching.advance_all, purge_old):
        try:
            job()
        except Exception:
            log.exception("scheduled job %s failed", job.__name__)


def start(app):
    """Start the background scheduler for a running app.
    Always started; each tick checks SCHEDULER_ENABLED so it can be switched live.
    The tick interval comes from SCHEDULER_TICK_SECONDS."""
    cfg = app.config["NS"]
    if cfg.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return None
    scheduler = BackgroundScheduler(daemon=True)

    def run_tick():
        """Run one tick inside the application context.
        The jobs read configuration through current_app.
        Called by APScheduler on its own thread."""
        with app.app_context():
            tick()

    scheduler.add_job(run_tick, "interval", seconds=cfg.scheduler_tick_seconds, id="neoseva-tick", max_instances=1)
    scheduler.start()
    return scheduler
