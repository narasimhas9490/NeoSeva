from dataclasses import asdict

from flask import Blueprint, current_app, g, make_response, request

from app.admin import auth
from app.admin.api import crud, hooks, images
from app.admin.api.registry import RESOURCES
from app.common.media.routes import store_image
from app.core import clock
from app.core.adapters.storage import storage
from app.core.db import many, one, run, scalar, tx
from app.core.envelope import body, created, ok
from app.core.errors import ApiError, bad_request, conflict, not_found, unprocessable
from app.core.idempotency import idempotent
from app.core.ids import new_id
from app.jobs import scheduler
from app.partner.identity.routes import delete_identity_images
from app.shared import matching
from app.shared.templates import formatted_answers
from app.shared.settings import texts

bp = Blueprint("admin_api", __name__, url_prefix="/admin/api")

ADJUSTMENT_TYPES = ("MANUAL_ADJUSTMENT", "PROMO", "SYSTEM_RESTORE", "REFERRAL_REWARD", "TOP_UP")


@bp.post("/login")
def login():
    """Sign an admin in and set the session cookie.
    The cookie is HttpOnly and scoped to the admin paths.
    A wrong name or password is 401 INVALID_CREDENTIALS."""
    data = body()
    token, username = auth.login(data.get("username") or "", data.get("password") or "")
    response = ok({"username": username})
    response.set_cookie(
        auth.COOKIE,
        token,
        httponly=True,
        samesite="Strict",
        secure=current_app.config["NS"].app_env == "production",
        path="/admin",
        max_age=current_app.config["NS"].admin_session_hours * 3600,
    )
    return response


@bp.post("/logout")
def logout():
    """Sign the admin out and clear the cookie.
    The session row is revoked, not deleted.
    Always succeeds."""
    auth.logout(request.cookies.get(auth.COOKIE))
    response = ok({"signedOut": True})
    response.delete_cookie(auth.COOKIE, path="/admin")
    return response


@bp.get("/me")
@auth.require_admin
def me():
    """Say who is signed in to the console.
    Used by the dashboard to decide between login and home.
    401 when nobody is."""
    return ok({"username": g.admin})


@bp.get("/meta")
@auth.require_admin
def meta():
    """Describe every resource the console can show or edit.
    The dashboard builds its menus, tables and forms from this.
    Nothing outside the registry is reachable."""
    return ok([asdict(r) for r in RESOURCES])


@bp.get("/r/<name>")
@auth.require_admin
def list_resource(name):
    """List rows of one resource with search, filters and paging.
    q searches text columns; f.<column>=value filters exactly.
    Returns up to 100 rows and the total."""
    res = crud.resource(name)
    with tx() as conn:
        rows, total = crud.list_rows(conn, res, request.args)
    return ok(rows, total=total, pageSize=crud.PAGE)


@bp.post("/r/<name>/get")
@auth.require_admin
def get_resource(name):
    """Read one row by its key.
    The key is sent in the body because some keys have two columns.
    404 when it does not exist."""
    res = crud.resource(name)
    with tx() as conn:
        return ok(crud.get_row(conn, res, body().get("key")))


@bp.post("/r/<name>")
@auth.require_admin
def create_resource(name):
    """Create a row in one resource.
    Validation hooks run first and version bumps run after.
    The database's own constraints have the final word."""
    res = crud.resource(name)
    values = body().get("values") or {}
    with tx() as conn:
        if name == "request_question":
            hooks.default_question_version(conn, values)
        if name in hooks.BEFORE:
            hooks.BEFORE[name](conn, values, None)
        key = crud.create_row(conn, res, values)
        if name in hooks.AFTER:
            hooks.AFTER[name](conn)
        return created(crud.get_row(conn, res, key))


@bp.put("/r/<name>")
@auth.require_admin
def update_resource(name):
    """Update a row in one resource.
    Key columns never change; read-only columns are ignored.
    Hooks validate before and bump versions after."""
    res = crud.resource(name)
    data = body()
    key, values = data.get("key"), data.get("values") or {}
    if name == "request_question":
        values.pop("template_version", None)
    with tx() as conn:
        if name in hooks.BEFORE:
            hooks.BEFORE[name](conn, values, key)
        replaced = images.old_urls(conn, res, key, values)
        crud.update_row(conn, res, key, values)
        if name in hooks.AFTER:
            hooks.AFTER[name](conn)
        row = crud.get_row(conn, res, key)
    images.retire(replaced)
    return ok(row)


@bp.delete("/r/<name>")
@auth.require_admin
def delete_resource(name):
    """Delete a row from one resource.
    Rows still referenced elsewhere are refused by the database.
    Version bumps run after question changes; an uploaded picture the row held is removed."""
    res = crud.resource(name)
    key = body().get("key")
    with tx() as conn:
        replaced = images.old_urls(conn, res, key)
        crud.delete_row(conn, res, key)
        if name in hooks.AFTER:
            hooks.AFTER[name](conn)
    images.retire(replaced)
    return ok({"deleted": True})


@bp.get("/stats")
@auth.require_admin
def stats():
    """Count what needs attention for the dashboard home.
    Open work, bookings by state, partners and identity checks waiting on us.
    Plus the villages people outside the area asked for most."""
    with tx() as conn:
        plain = lambda rows: [{k: crud._plain(v) for k, v in r.items()} for r in rows]
        return ok(
            {
                "requestsByState": many(conn, "SELECT state, count(*) AS n FROM request GROUP BY state ORDER BY state"),
                "bookingsByState": many(conn, "SELECT state, count(*) AS n FROM booking GROUP BY state ORDER BY state"),
                "partnersByStatus": many(conn, "SELECT status, count(*) AS n FROM partner_profile GROUP BY status ORDER BY status"),
                "identityPending": scalar(conn, "SELECT count(*) FROM identity_verification WHERE status = 'PENDING'"),
                "offersToday": scalar(conn, "SELECT count(*) FROM offer WHERE created_at > now() - interval '24 hours'"),
                "customers": scalar(conn, "SELECT count(*) FROM customer_profile"),
                "openComplaints": scalar(conn, "SELECT count(*) FROM complaint WHERE status <> 'RESOLVED'"),
                "topPlaceInterest": plain(
                    many(
                        conn,
                        """SELECT lower(trim(name_typed)) AS name, count(*) AS n, max(created_at) AS last
                           FROM place_interest GROUP BY lower(trim(name_typed)) ORDER BY n DESC, last DESC LIMIT 10""",
                    )
                ),
            }
        )


@bp.get("/partners")
@auth.require_admin
def partners():
    """List partners with everything needed to decide on them.
    Status, identity state, services, base village and balance in one row.
    Filter with ?status=."""
    status = request.args.get("status") or None
    with tx() as conn:
        rows = many(
            conn,
            """SELECT pp.user_id, pp.display_name, pp.status, pp.status_note, pp.next_step, pp.accepting_new_jobs,
                      pp.travel_radius_km, u.phone_e164, p.name AS base_place, iv.status AS identity_status,
                      iv.submitted_at, COALESCE(pb.available_minor, 0) AS balance_minor,
                      (SELECT string_agg(s.name, ', ') FROM partner_service ps JOIN service s ON s.id = ps.service_id
                        WHERE ps.partner_id = pp.user_id) AS services,
                      (SELECT count(*) FROM booking b WHERE b.partner_id = pp.user_id AND b.state = 'COMPLETED') AS completed
               FROM partner_profile pp JOIN app_user u ON u.id = pp.user_id
               LEFT JOIN place p ON p.id = pp.base_place_id
               LEFT JOIN identity_verification iv ON iv.partner_id = pp.user_id
               LEFT JOIN partner_balance pb ON pb.partner_id = pp.user_id
               WHERE (CAST(:st AS TEXT) IS NULL OR pp.status = :st)
               ORDER BY pp.created_at DESC""",
            st=status,
        )
    return ok([{k: crud._plain(v) for k, v in r.items()} for r in rows])


@bp.post("/partners/<partner_id>/status")
@auth.require_admin
def set_partner_status(partner_id):
    """Move a partner between ACTIVATED, ACTIVE and SUSPENDED.
    ACTIVE needs finished setup and a verified identity.
    statusNote is shown to him, so it is written for him."""
    data = body()
    status, note = data.get("status"), data.get("statusNote")
    if status not in ("ACTIVE", "SUSPENDED", "ACTIVATED"):
        raise bad_request("INVALID_STATUS", "status must be ACTIVE, SUSPENDED or ACTIVATED.")
    with tx() as conn:
        row = one(conn, "SELECT * FROM partner_profile WHERE user_id = :p FOR UPDATE", p=partner_id)
        if row is None:
            raise not_found()
        if status == "ACTIVE":
            if row["next_step"] != "COMPLETE":
                raise conflict("SETUP_INCOMPLETE", "He has not finished setting up.", {"nextStep": row["next_step"]})
            if scalar(conn, "SELECT status FROM identity_verification WHERE partner_id = :p", p=partner_id) != "VERIFIED":
                raise conflict("IDENTITY_NOT_VERIFIED", "Verify his identity first.")
        if status == "ACTIVATED" and row["next_step"] != "COMPLETE":
            raise conflict("SETUP_INCOMPLETE", "He has not finished setting up.")
        run(
            conn,
            "UPDATE partner_profile SET status = :s, status_note = :n WHERE user_id = :p",
            s=status,
            n=note.strip() if isinstance(note, str) and note.strip() else None,
            p=partner_id,
        )
        if status == "SUSPENDED":
            run(conn, "UPDATE auth_session SET revoked_at = now() WHERE user_id = :p AND revoked_at IS NULL", p=partner_id)
            run(conn, "UPDATE device SET push_token = NULL WHERE user_id = :p", p=partner_id)
    return ok({"partnerId": partner_id, "status": status})


@bp.post("/identity/<partner_id>/decision")
@auth.require_admin
def identity_decision(partner_id):
    """Decide a pending identity check.
    VERIFIED and REJECTED are final and delete both images; NEEDS_ACTION asks again.
    With activate true, a verified ACTIVATED partner becomes ACTIVE at once."""
    data = body()
    status, note = data.get("status"), data.get("reviewNote")
    if status not in ("VERIFIED", "REJECTED", "NEEDS_ACTION"):
        raise bad_request("INVALID_STATUS", "status must be VERIFIED, REJECTED or NEEDS_ACTION.")
    if status != "VERIFIED" and not (isinstance(note, str) and note.strip()):
        raise unprocessable("REVIEW_NOTE_REQUIRED", "Tell him what was wrong, in words he can read.")
    with tx() as conn:
        row = one(conn, "SELECT * FROM identity_verification WHERE partner_id = :p FOR UPDATE", p=partner_id)
        if row is None:
            raise not_found()
        if row["status"] != "PENDING":
            raise conflict("IDENTITY_NOT_PENDING", "Only a pending check can be decided.")
        run(
            conn,
            """UPDATE identity_verification SET status = :s, review_note = :n, reviewed_at = :now, reviewed_by = :by
               WHERE partner_id = :p""",
            s=status,
            n=note.strip() if isinstance(note, str) and note.strip() else None,
            now=clock.now_utc(),
            by=g.admin,
            p=partner_id,
        )
        if status in ("VERIFIED", "REJECTED"):
            delete_identity_images(conn, [row["document_media_id"], row["selfie_media_id"]])
        if status == "VERIFIED" and data.get("activate", True):
            run(
                conn,
                "UPDATE partner_profile SET status = 'ACTIVE' WHERE user_id = :p AND status = 'ACTIVATED' AND next_step = 'COMPLETE'",
                p=partner_id,
            )
    return ok({"partnerId": partner_id, "status": status})


@bp.post("/media")
@auth.require_admin
def upload_media():
    """Take one picture from the console and return its id and public URL.
    The form then saves that URL on the row in a second request.
    purpose must be one an admin may upload; the same size and type rules as the apps apply."""
    purpose = request.form.get("purpose")
    upload_file = request.files.get("file")
    if purpose not in images.ADMIN_PURPOSES:
        raise bad_request("INVALID_MEDIA_PURPOSE", f"purpose must be one of {', '.join(images.ADMIN_PURPOSES)}.")
    if upload_file is None:
        raise bad_request("MEDIA_FILE_REQUIRED", "Send the image in a part named file.")
    media_id, content_type, url = store_image(current_app.config["NS"], purpose, upload_file, admin=g.admin)
    return created({"mediaId": media_id, "url": url, "contentType": content_type})


@bp.delete("/media/<media_id>")
@auth.require_admin
def discard_media(media_id):
    """Throw away a picture uploaded in the console but never saved, or replaced before saving.
    A picture a row still uses is refused with 409 MEDIA_IN_USE; an unknown one is 404.
    The file is removed once the record is."""
    with tx() as conn:
        try:
            gone = images.discard(conn, media_id)
        except LookupError:
            raise conflict("MEDIA_IN_USE", "A row still uses this picture.")
    if gone is None:
        raise not_found()
    images.remove_file(*gone)
    return ok({"deleted": True})


@bp.get("/media/<media_id>/file")
@auth.require_admin
def media_file(media_id):
    """Show an uploaded image to an admin, including identity documents.
    This is the only way an identity image is ever read back.
    Deleted images are gone for good and return 404."""
    with tx() as conn:
        row = one(conn, "SELECT * FROM media WHERE id = :id AND deleted_at IS NULL", id=media_id)
    if row is None:
        raise not_found()
    data = storage(current_app.config["NS"]).read(row["purpose"], row["storage_key"])
    if data is None:
        raise not_found()
    response = make_response(data)
    response.headers["Content-Type"] = row["content_type"]
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/ledger/adjust")
@auth.require_admin
@idempotent
def adjust_credit():
    """Add one ledger row by hand, such as a top-up or a correction.
    The ledger is append only; a correction is another row, never an edit.
    JOB_FEE is never written here; it belongs to completion."""
    data = body()
    partner_id, kind, amount, note = data.get("partnerId"), data.get("type"), data.get("amountMinor"), data.get("note")
    if kind not in ADJUSTMENT_TYPES:
        raise bad_request("INVALID_TYPE", f"type must be one of {', '.join(ADJUSTMENT_TYPES)}.")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
        raise bad_request("INVALID_AMOUNT", "amountMinor must be a non-zero whole number of paise.")
    if not (isinstance(note, str) and note.strip()):
        raise unprocessable("NOTE_REQUIRED", "Say why, for whoever reads the ledger later.")
    with tx() as conn:
        if not scalar(conn, "SELECT 1 FROM partner_profile WHERE user_id = :p", p=partner_id):
            raise not_found()
        tx_id = new_id("ctx")
        run(
            conn,
            """INSERT INTO credit_ledger_transaction (id, partner_id, type, amount_minor, note, created_by)
               VALUES (:id, :p, :t, :a, :n, :by)""",
            id=tx_id,
            p=partner_id,
            t=kind,
            a=amount,
            n=note.strip(),
            by=g.admin,
        )
        balance = scalar(conn, "SELECT available_minor FROM partner_balance WHERE partner_id = :p", p=partner_id)
    return created({"id": tx_id, "partnerId": partner_id, "balanceMinor": int(balance or 0)})


@bp.get("/requests/<request_id>/detail")
@auth.require_admin
def request_detail(request_id):
    """Show one request with everything that happened to it.
    Answers, who was told in which batch, every offer and every booking.
    The first place to look when matching misbehaves; PINs are never included."""
    with tx() as conn:
        request_row = one(
            conn,
            """SELECT r.*, s.name AS service_name, p.name AS place_name, cp.display_name AS customer_name, u.phone_e164
               FROM request r JOIN service s ON s.id = r.service_id JOIN place p ON p.id = r.place_id
               JOIN app_user u ON u.id = r.customer_id LEFT JOIN customer_profile cp ON cp.user_id = r.customer_id
               WHERE r.id = :id""",
            id=request_id,
        )
        if request_row is None:
            raise not_found()
        request_row.pop("pin", None)
        return ok(
            {
                "request": {k: crud._plain(v) for k, v in request_row.items()},
                "answers": formatted_answers(conn, request_id, texts(conn)),
                "notifications": [
                    {k: crud._plain(v) for k, v in r.items()}
                    for r in many(
                        conn,
                        """SELECT n.partner_id, pp.display_name, n.batch, n.notified_at, n.declined_at
                           FROM opportunity_notification n JOIN partner_profile pp ON pp.user_id = n.partner_id
                           WHERE n.request_id = :r ORDER BY n.batch, n.notified_at""",
                        r=request_id,
                    )
                ],
                "offers": [
                    {k: crud._plain(v) for k, v in r.items()}
                    for r in many(
                        conn,
                        """SELECT o.id, o.partner_id, pp.display_name, o.status, o.pricing_type, o.exact_amount_minor,
                                  o.rate_minor, o.unit_code, o.inspection_charge_minor, o.comparable_cost_minor,
                                  o.offered_day_part, o.created_at
                           FROM offer o JOIN partner_profile pp ON pp.user_id = o.partner_id
                           WHERE o.request_id = :r ORDER BY o.created_at""",
                        r=request_id,
                    )
                ],
                "bookings": [
                    {k: crud._plain(v) for k, v in r.items()}
                    for r in many(
                        conn,
                        """SELECT id, partner_id, state, booked_pricing_type, booked_cost_minor, final_amount_minor,
                                  day_part, arrival_expected_by, arrived_at, completed_at, completed_by, cancelled_at
                           FROM booking WHERE request_id = :r ORDER BY created_at""",
                        r=request_id,
                    )
                ],
            }
        )


@bp.post("/requests/<request_id>/run-matching")
@auth.require_admin
def run_matching(request_id):
    """Send the next matching batch for one request now, if one is due.
    Useful when testing matching settings by hand.
    Returns the partners told in this batch."""
    return ok({"notified": matching.advance(request_id)})


@bp.post("/jobs/tick")
@auth.require_admin
def run_tick():
    """Run every periodic job once, immediately.
    Expiry, matching batches and idempotency cleanup.
    The same work the scheduler does on its own."""
    scheduler.tick()
    return ok({"ran": True})


@bp.get("/demand")
@auth.require_admin
def demand():
    """Group place-interest rows by village name.
    This is the list that decides where to open next.
    Names are compared case-insensitively."""
    with tx() as conn:
        rows = many(
            conn,
            """SELECT lower(trim(name_typed)) AS name, count(*) AS n, count(phone_e164) AS with_phone,
                      min(created_at) AS first, max(created_at) AS last
               FROM place_interest GROUP BY lower(trim(name_typed)) ORDER BY n DESC, last DESC""",
        )
    return ok([{k: crud._plain(v) for k, v in r.items()} for r in rows])


@bp.get("/config")
@auth.require_admin
def get_config():
    """List every runtime setting with its .env, override and live values.
    Secrets show only whether they are set.
    Settings that need a restart are listed but not editable here."""
    with tx() as conn:
        return ok(current_app.extensions["runtime"].describe(conn))


@bp.put("/config/<env_key>")
@auth.require_admin
def set_config(env_key):
    """Override one setting from the console; it applies within seconds.
    The value is sent as text, exactly as it would be written in .env.
    Unsafe production switches are refused."""
    value = body().get("value")
    if value is None:
        raise bad_request("VALUE_REQUIRED", "Send the new value as text in value.")
    current_app.extensions["runtime"].set(env_key, value, g.admin)
    return get_config()


@bp.delete("/config/<env_key>")
@auth.require_admin
def reset_config(env_key):
    """Remove one override so the .env value applies again.
    Takes effect at once.
    Returns the full list afterwards."""
    current_app.extensions["runtime"].reset(env_key)
    return get_config()
