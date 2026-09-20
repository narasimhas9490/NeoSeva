import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.admin.auth import create_admin
from app.config import Config
from app.core import clock
from app.core.db import engine, init_engine, run, scalar, tx
from app.core.ids import new_id
from scripts.migrate import url_or_exit
from seed.catalog_data import EQUIPMENT, EXPERIENCE_RANGES, HELP_ME_CHOOSE, PLACES, PLACE_LABELS, REASONS, REVIEW_TAGS

GEO = "geo_podalakur"
TEMPLATE_VERSION = 3

SERVICES = [
    ("svc_tractor", "Tractor", "agriculture_heavy", "Agriculture & Heavy Machinery", True, 10),
    ("svc_jcb", "JCB", "agriculture_heavy", "Agriculture & Heavy Machinery", True, 20),
    ("svc_borewell", "Borewell", "agriculture_heavy", "Agriculture & Heavy Machinery", False, 30),
    ("svc_harvester", "Harvester", "agriculture_heavy", "Agriculture & Heavy Machinery", False, 40),
    ("svc_electrician", "Electrician", "home_repair", "Home Repair", True, 50),
    ("svc_plumber", "Plumber", "home_repair", "Home Repair", True, 60),
    ("svc_carpenter", "Carpenter", "home_repair", "Home Repair", False, 70),
    ("svc_painter", "Painter", "home_repair", "Home Repair", False, 80),
    ("svc_mason", "Mason", "construction", "Construction", False, 90),
    ("svc_welder", "Welder", "construction", "Construction", False, 100),
    ("svc_motor_repair", "Motor repair", "home_repair", "Home Repair", False, 110),
    ("svc_transport", "Goods transport", "mobility_logistics", "Mobility & Logistics", True, 120),
    ("svc_water_tanker", "Water tanker", "mobility_logistics", "Mobility & Logistics", False, 130),
]
UNLAUNCHED = {"svc_water_tanker"}

UNITS = {
    "svc_tractor": [("ACRE", "acres", "acre"), ("HOUR", "hours", "hour")],
    "svc_jcb": [("HOUR", "hours", "hour"), ("TRIP", "trips", "trip")],
    "svc_harvester": [("ACRE", "acres", "acre"), ("HOUR", "hours", "hour")],
    "svc_borewell": [("FOOT", "feet", "foot")],
    "svc_transport": [("TRIP", "trips", "trip")],
    "svc_water_tanker": [("TRIP", "trips", "trip")],
    "svc_electrician": [("HOUR", "hours", "hour"), ("DAY", "days", "day")],
    "svc_plumber": [("HOUR", "hours", "hour"), ("DAY", "days", "day")],
    "svc_carpenter": [("DAY", "days", "day")],
    "svc_painter": [("DAY", "days", "day")],
    "svc_mason": [("DAY", "days", "day")],
    "svc_welder": [("HOUR", "hours", "hour")],
    "svc_motor_repair": [("HOUR", "hours", "hour")],
}


def Q(qid, service, kind, label, short, sort, required=True, not_sure=False, unit=None, bounds=None, show_if=None, options=()):
    """Describe one seed question compactly.
    unit is (code, label, per_label); bounds is (min, max, step, default).
    options are (id, label, equipment_id) tuples for choice questions."""
    return {
        "id": qid, "service": service, "type": kind, "label": label, "short": short, "sort": sort,
        "required": required, "not_sure": not_sure, "unit": unit, "bounds": bounds, "show_if": show_if, "options": options,
    }


QUESTIONS = [
    Q("q_work_type", "svc_tractor", "SINGLE_CHOICE", "What work do you need?", "Work", 10,
      options=[("rotavator", "Rotavator", "ROTAVATOR"), ("ploughing", "Ploughing", "PLOUGH"), ("cultivation", "Cultivation", "CULTIVATOR")]),
    Q("q_acres", "svc_tractor", "NUMBER_WITH_UNIT", "How many acres?", "Land", 20, not_sure=True,
      unit=("ACRE", "acres", "acre"), bounds=(0.5, 20, 0.5, 2)),
    Q("q_trips", "svc_tractor", "NUMBER_WITH_UNIT", "How many trips?", "Trips", 30,
      unit=("TRIP", "trips", "trip"), bounds=(1, 20, 1, 1), show_if=("q_work_type", ["ploughing"])),
    Q("q_jcb_work", "svc_jcb", "SINGLE_CHOICE", "What work do you need?", "Work", 10,
      options=[("jcb_digging", "Digging", None), ("jcb_levelling", "Levelling", None), ("jcb_loading", "Loading", None)]),
    Q("q_jcb_hours", "svc_jcb", "NUMBER_WITH_UNIT", "How many hours?", "Time", 20, not_sure=True,
      unit=("HOUR", "hours", "hour"), bounds=(1, 12, 1, 2)),
    Q("q_harvest_crop", "svc_harvester", "SINGLE_CHOICE", "Which crop?", "Crop", 10,
      options=[("crop_paddy", "Paddy", None), ("crop_groundnut", "Groundnut", None)]),
    Q("q_harvest_acres", "svc_harvester", "NUMBER_WITH_UNIT", "How many acres?", "Land", 20, not_sure=True,
      unit=("ACRE", "acres", "acre"), bounds=(0.5, 30, 0.5, 2)),
    Q("q_plumb_problem", "svc_plumber", "MULTI_CHOICE", "What is the problem?", "Problem", 10,
      options=[("plumb_leak", "A leak", None), ("plumb_block", "A blockage", None), ("plumb_new", "New fitting", None), ("plumb_motor", "Motor pipe", None)]),
    Q("q_elec_problem", "svc_electrician", "MULTI_CHOICE", "What is the problem?", "Problem", 10,
      options=[("elec_no_power", "No power", None), ("elec_wiring", "Wiring", None), ("elec_fan_light", "Fan or light", None), ("elec_starter", "Motor starter", None)]),
    Q("q_transport_load", "svc_transport", "SINGLE_CHOICE", "What needs moving?", "Load", 10,
      options=[("load_grain", "Grain", None), ("load_fodder", "Fodder", None), ("load_household", "Household goods", None), ("load_building", "Building material", None)]),
    Q("q_transport_trips", "svc_transport", "NUMBER_WITH_UNIT", "How many trips?", "Trips", 20,
      unit=("TRIP", "trips", "trip"), bounds=(1, 10, 1, 1)),
    Q("q_tanker_trips", "svc_water_tanker", "NUMBER_WITH_UNIT", "How many tankers?", "Tankers", 10,
      unit=("TRIP", "tankers", "tanker"), bounds=(1, 10, 1, 1)),
]

CATALOG = {
    "job_fee_percent": 5.00,
    "cancellation_limit": 3,
    "cancellation_window_days": 30,
    "max_offer_rupees": 100000,
    "jobs_on_home_screen": 3,
    "travel_distances_km": [5, 10, 15, 25, 40],
    "help_me_choose_prompt": "What do you need done?",
    "terms_version": None,
    "terms_url": None,
    "support_officer_name": "Grievance Officer (to be appointed)",
    "support_officer_designation": "Grievance Officer",
    "support_email": "grievance@neoseva.in",
    "support_phone": "+910000000000",
    "support_response_promise": "We reply within 48 hours and settle within one month.",
    "booking_shares_contact": "When you confirm, {partnerName} gets your phone number and the exact spot. No other partner sees them.",
    "booking_other_offers_close": "The other offers for this request will close.",
    "booking_payment": "You pay {partnerName} directly for this job. NeoSeva doesn't collect any payment for the work itself.",
    "booking_inspection_charge_adjusted": "The visit charge is part of the price he gives you, not on top of it.",
    "pin_guidance": "Give this code to {partnerName} only after the work is finished and you are happy with it.",
    "partner_looking_is_free": "Seeing work and sending your price are both free.",
    "partner_when_charged": "When the work is done, 5% of what the customer paid comes off your credit.",
    "partner_when_reporting_a_problem": "Someone will read this. Telling us helps us see when the same thing keeps happening.",
    "partner_inspection_charge_adjusted": "Your visit charge is part of the price you give her, so price the visit as part of the job.",
}

PLATFORM = {
    "matching_batch_size": 5,
    "matching_batch_interval_minutes": 10,
    "otp_daily_limit": 10,
    "pin_max_attempts": 5,
    "pin_lockout_seconds": 300,
    "saved_place_upgrade_max_meters": 300,
    "well_rated_min_reviews": None,
    "well_rated_min_positive_percent": None,
}

DISPLAY_TEXT = [
    ("ENDED_CANCELLED_BY_CUSTOMER", "You cancelled this request.", "endedLabel for CANCELLED_BY_CUSTOMER"),
    ("ENDED_NO_PARTNER_AVAILABLE", "No partner was free for this day. Try another day.", "endedLabel for NO_PARTNER_AVAILABLE"),
    ("ENDED_NOT_BOOKED", "This day passed without a booking.", "endedLabel for NOT_BOOKED"),
    ("ENDED_PARTNER_FELL_THROUGH", "Your partner could not come. We are sorry.", "endedLabel for PARTNER_FELL_THROUGH"),
    ("NOT_SURE", "Not sure", "displayValue when she answered not sure"),
    ("DAYPART_MORNING", "Morning", "Used in scheduleLabel"),
    ("DAYPART_AFTERNOON", "Afternoon", "Used in scheduleLabel"),
    ("DAYPART_EVENING", "Evening", "Used in scheduleLabel"),
    ("DAYPART_ANY_TIME", "Any time", "Used in scheduleLabel"),
    ("AREA_LABEL_FORMAT", "{placeName} area", "approximateArea.areaLabel; {placeName} is the village"),
]


def upsert(conn, table, key_cols, row):
    """Insert a row or update it when the key already exists.
    Catalog seeding is re-runnable and always converges on this file.
    key_cols names the conflict target."""
    cols = list(row)
    updates = [c for c in cols if c not in key_cols]
    action = "DO UPDATE SET " + ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in updates) if updates else "DO NOTHING"
    run(
        conn,
        f'INSERT INTO {table} ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({", ".join(":" + c for c in cols)}) '
        f'ON CONFLICT ({", ".join(key_cols)}) {action}',
        **row,
    )


def seed_geography(conn):
    """Seed Podalakur, its 36 villages and their boundaries.
    Village boundaries are Voronoi cells clipped to 2 km, so a few gaps have no village.
    The area boundary is the hull around every village, buffered."""
    upsert(conn, "geography", ["id"], {
        "id": GEO, "label": "Podalakur Mandal", "timezone": "Asia/Kolkata", "same_day_cutoff_hour": 18, "book_ahead_days": 14,
        "minimum_notice_minutes": 60, "no_show_prompt_delay_minutes": 30, "preferred_partner_head_start_minutes": 30,
        "morning_ends_hour": 12, "afternoon_ends_hour": 17, "evening_ends_hour": 21, "is_active": True,
    })
    for place_id, name, lat, lng in PLACES:
        run(
            conn,
            """INSERT INTO place (id, geography_id, name, mandal, district, state, centre, is_active)
               VALUES (:id, :g, :n, 'Podalakur', 'SPSR Nellore', 'Andhra Pradesh',
                       ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography, true)
               ON CONFLICT (id) DO UPDATE SET name = :n, centre = EXCLUDED.centre""",
            id=place_id, g=GEO, n=name, lat=lat, lng=lng,
        )
    run(
        conn,
        """WITH cells AS (
               SELECT (ST_Dump(ST_VoronoiPolygons(ST_Collect(centre::geometry)))).geom AS cell
               FROM place WHERE geography_id = :g)
           UPDATE place p SET boundary = ST_Multi(ST_Intersection(c.cell, ST_Buffer(p.centre, 2000)::geometry))::geography
           FROM cells c WHERE p.geography_id = :g AND ST_Contains(c.cell, p.centre::geometry)""",
        g=GEO,
    )
    run(
        conn,
        """UPDATE geography SET boundary = (
               SELECT ST_Multi(ST_ConvexHull(ST_Collect(ST_Buffer(centre, 3000)::geometry)))::geography
               FROM place WHERE geography_id = :g) WHERE id = :g""",
        g=GEO,
    )


def seed_catalog(conn):
    """Seed services, equipment, units, questions and every served list.
    Everything here is editable from the admin dashboard afterwards.
    One service is left without an area to show the unlaunched state."""
    for sid, name, cat, cat_label, home, sort in SERVICES:
        upsert(conn, "service", ["id"], {
            "id": sid, "name": name, "icon_url": None, "category": cat, "category_label": cat_label,
            "common_on_home": home, "sort_order": sort, "is_active": True,
        })
        if sid not in UNLAUNCHED:
            upsert(conn, "service_geography", ["service_id", "geography_id"], {"service_id": sid, "geography_id": GEO})
    for service_id, code, label, sort in EQUIPMENT:
        upsert(conn, "service_equipment", ["service_id", "id"], {"service_id": service_id, "id": code, "label": label, "sort_order": sort})
    for service_id, units in UNITS.items():
        for i, (code, label, per) in enumerate(units):
            upsert(conn, "service_offer_unit", ["service_id", "code"], {"service_id": service_id, "code": code, "label": label, "per_label": per, "sort_order": i * 10})
    for q in QUESTIONS:
        unit = q["unit"] or (None, None, None)
        bounds = q["bounds"] or (None, None, None, None)
        upsert(conn, "request_question", ["id"], {
            "id": q["id"], "service_id": q["service"], "template_version": TEMPLATE_VERSION, "type": q["type"],
            "label": q["label"], "short_label": q["short"], "required": q["required"], "allow_not_sure": q["not_sure"],
            "unit_code": unit[0], "unit_label": unit[1], "unit_per_label": unit[2],
            "min_value": bounds[0], "max_value": bounds[1], "step_value": bounds[2], "default_value": bounds[3],
            "depends_on_question_id": q["show_if"][0] if q["show_if"] else None,
            "depends_on_values": q["show_if"][1] if q["show_if"] else None, "sort_order": q["sort"],
        })
        for i, (oid, label, equipment) in enumerate(q["options"]):
            upsert(conn, "request_question_option", ["id"], {
                "id": oid, "question_id": q["id"], "label": label, "service_id": None, "icon_url": None,
                "equipment_id": equipment, "sort_order": i * 10,
            })
    for oid, label, service_id, sort in HELP_ME_CHOOSE:
        upsert(conn, "help_me_choose_option", ["id"], {"id": oid, "label": label, "icon_url": None, "service_id": service_id, "sort_order": sort, "is_active": True})
    for context, code, label, sort in REASONS:
        upsert(conn, "reason", ["context", "code"], {"context": context, "code": code, "label": label, "sort_order": sort, "is_active": True})
    for code, label, sort in PLACE_LABELS:
        upsert(conn, "place_label", ["code"], {"code": code, "label": label, "sort_order": sort})
    for code, label, positive, sort in REVIEW_TAGS:
        upsert(conn, "review_tag", ["code"], {"code": code, "label": label, "positive": positive, "sort_order": sort})
    for code, label, sort in EXPERIENCE_RANGES:
        upsert(conn, "experience_range", ["code"], {"code": code, "label": label, "sort_order": sort})
    upsert(conn, "catalog_setting", ["id"], {"id": 1, **CATALOG})
    upsert(conn, "platform_setting", ["id"], {"id": 1, **PLATFORM})
    for key, label, note in DISPLAY_TEXT:
        upsert(conn, "display_text", ["key"], {"key": key, "label": label, "note": note})


def user(conn, uid, phone, language="te"):
    """Create a demo person with a verified phone.
    Fixed readable ids make the demo easy to find in the admin.
    Skipped when the person already exists."""
    run(
        conn,
        """INSERT INTO app_user (id, phone_e164, phone_verified_at, language) VALUES (:id, :p, now(), :l)
           ON CONFLICT DO NOTHING""",
        id=uid, p=phone, l=language,
    )


def partner(conn, uid, phone, name, status, base, radius, services, identity=None, note=None, step="COMPLETE"):
    """Create a demo partner with services and an identity state.
    Covers every status so the admin pages have something to show.
    Skipped parts that already exist are left alone."""
    user(conn, uid, phone)
    run(
        conn,
        """INSERT INTO partner_profile (user_id, display_name, base_place_id, experience_range_code, status, status_note,
               travel_radius_km, next_step) VALUES (:u, :n, :b, '5_TO_10_YEARS', :s, :note, :r, :step)
           ON CONFLICT (user_id) DO NOTHING""",
        u=uid, n=name, b=base, s=status, note=note, r=radius, step=step,
    )
    for service_id, equipment in services:
        run(
            conn,
            "INSERT INTO partner_service (partner_id, service_id, equipment_ids) VALUES (:p, :s, :e) ON CONFLICT DO NOTHING",
            p=uid, s=service_id, e=equipment,
        )
    if identity:
        run(
            conn,
            """INSERT INTO identity_verification (partner_id, status, document_type, submitted_at, reviewed_at, reviewed_by)
               VALUES (:p, :s, 'MASKED_AADHAAR', now() - interval '3 days',
                       CASE WHEN :s = 'PENDING' THEN NULL ELSE now() - interval '2 days' END,
                       CASE WHEN :s = 'PENDING' THEN NULL ELSE 'seed' END)
               ON CONFLICT DO NOTHING""",
            p=uid, s=identity,
        )


def request_row(conn, rid, customer, service, work_type, state, on_date, day_part, place, created, answers, ended=None, landmark=None, pin=None):
    """Create a demo request with its answers.
    answers are (question_id, values, unit_code, not_sure) tuples.
    Skipped when the request already exists."""
    run(
        conn,
        """INSERT INTO request (id, customer_id, geography_id, service_id, work_type_id, template_version, state,
               schedule_date, day_part, place_id, landmark, pin, accuracy_meters, description, origin_source,
               ended_reason, ended_at, created_at)
           VALUES (:id, :c, :g, :s, :w, :v, :st, :d, :dp, :p, :l,
                   CASE WHEN CAST(:lat AS DOUBLE PRECISION) IS NULL THEN NULL
                        ELSE ST_SetSRID(ST_MakePoint(CAST(:lng AS DOUBLE PRECISION), CAST(:lat AS DOUBLE PRECISION)), 4326)::geography END,
                   :acc, NULL, 'HOME', :e, CASE WHEN CAST(:e AS TEXT) IS NULL THEN NULL ELSE :created END, :created)
           ON CONFLICT (id) DO NOTHING""",
        id=rid, c=customer, g=GEO, s=service, w=work_type, v=TEMPLATE_VERSION, st=state, d=on_date, dp=day_part, p=place,
        l=landmark, lat=pin[0] if pin else None, lng=pin[1] if pin else None, acc=18.0 if pin else None, e=ended, created=created,
    )
    for qid, values, unit, not_sure in answers:
        run(
            conn,
            """INSERT INTO request_answer (request_id, question_id, "values", unit_code, not_sure)
               VALUES (:r, :q, :v, :u, :n) ON CONFLICT DO NOTHING""",
            r=rid, q=qid, v=values, u=unit, n=not_sure,
        )


def offer(conn, oid, rid, pid, status, kind, amount, unit=None, cost=None, day_part="MORNING", created=None):
    """Create a demo offer in one of the three pricing shapes.
    amount is the exact amount, the rate or the visit charge by kind.
    Skipped when the offer already exists."""
    run(
        conn,
        """INSERT INTO offer (id, request_id, partner_id, status, pricing_type, exact_amount_minor, rate_minor, unit_code,
               inspection_charge_minor, comparable_cost_minor, offered_day_part, created_at)
           VALUES (:id, :r, :p, :s, :k, :exact, :rate, :unit, :insp, :cost, :dp, :created)
           ON CONFLICT (id) DO NOTHING""",
        id=oid, r=rid, p=pid, s=status, k=kind,
        exact=amount if kind == "FIXED" else None, rate=amount if kind == "UNIT_RATE" else None,
        unit=unit, insp=amount if kind == "INSPECTION" else None, cost=cost, dp=day_part, created=created,
    )


def seed_demo(conn):
    """Seed demo customers, partners, requests, offers, bookings and money.
    Every state the apps draw has at least one example.
    Dates are relative to today so the demo never goes stale."""
    now = clock.now_utc()
    today = clock.local_today("Asia/Kolkata")
    tomorrow, day_after, last_week = today + timedelta(days=1), today + timedelta(days=2), today - timedelta(days=5)

    user(conn, "usr_seed_lakshmi", "+919876543210")
    user(conn, "usr_seed_sita", "+919876543211")
    user(conn, "usr_seed_ramana", "+919876543212", "en")
    for uid, name, place in (("usr_seed_lakshmi", "Lakshmi", "plc_podalakur"), ("usr_seed_sita", "Sita", "plc_biradavolu"), ("usr_seed_ramana", None, "plc_marupuru")):
        run(conn, "INSERT INTO customer_profile (user_id, display_name, default_place_id) VALUES (:u, :n, :p) ON CONFLICT DO NOTHING", u=uid, n=name, p=place)
    run(
        conn,
        """INSERT INTO saved_place (id, user_id, label_code, place_id, landmark, pin, accuracy_meters)
           VALUES ('spl_seed_lakshmi_field', 'usr_seed_lakshmi', 'FIELD', 'plc_podalakur', 'Near the canal bridge',
                   ST_SetSRID(ST_MakePoint(79.733456, 14.417123), 4326)::geography, 18.0),
                  ('spl_seed_lakshmi_home', 'usr_seed_lakshmi', 'HOME', 'plc_podalakur', NULL, NULL, NULL)
           ON CONFLICT DO NOTHING""",
    )

    partner(conn, "usr_seed_ravi", "+919812345678", "Ravi Kumar", "ACTIVE", "plc_podalakur", 15,
            [("svc_tractor", ["ROTAVATOR", "CULTIVATOR", "PLOUGH"])], identity="VERIFIED")
    partner(conn, "usr_seed_suresh", "+919812345679", "Suresh", "ACTIVE", "plc_biradavolu", 25,
            [("svc_tractor", ["ROTAVATOR"]), ("svc_jcb", ["BUCKET_STANDARD"])], identity="VERIFIED")
    partner(conn, "usr_seed_kiran", "+919812345680", "Kiran", "ACTIVE", "plc_podalakur", 10,
            [("svc_plumber", []), ("svc_electrician", [])], identity="VERIFIED")
    partner(conn, "usr_seed_venkat", "+919812345681", "Venkat", "ACTIVATED", "plc_duggunta", 15,
            [("svc_harvester", [])], identity="PENDING")
    partner(conn, "usr_seed_prasad", "+919812345682", "Prasad", "REGISTERED", "plc_marupuru", None, [], step="SERVICES")
    partner(conn, "usr_seed_mahesh", "+919812345683", "Mahesh", "SUSPENDED", "plc_kanuparthi", 15,
            [("svc_tractor", ["ROTAVATOR"])], identity="VERIFIED", note="Paused while we look into two missed jobs.")

    request_row(conn, "rqt_seed_open_tractor", "usr_seed_lakshmi", "svc_tractor", "rotavator", "REQUESTED", tomorrow, "MORNING",
                "plc_podalakur", now - timedelta(minutes=40),
                [("q_work_type", ["rotavator"], None, False), ("q_acres", ["3"], "ACRE", False)],
                landmark="Near the canal bridge", pin=(14.417123, 79.733456))
    for pid in ("usr_seed_ravi", "usr_seed_suresh"):
        run(conn, "INSERT INTO opportunity_notification (request_id, partner_id, batch, notified_at) VALUES ('rqt_seed_open_tractor', :p, 1, :t) ON CONFLICT DO NOTHING",
            p=pid, t=now - timedelta(minutes=39))
    offer(conn, "ofr_seed_ravi_fixed", "rqt_seed_open_tractor", "usr_seed_ravi", "ACTIVE", "FIXED", 240000, cost=240000, created=now - timedelta(minutes=30))
    offer(conn, "ofr_seed_suresh_rate", "rqt_seed_open_tractor", "usr_seed_suresh", "ACTIVE", "UNIT_RATE", 75000, unit="ACRE", cost=225000,
          day_part="AFTERNOON", created=now - timedelta(minutes=20))

    request_row(conn, "rqt_seed_booked_plumber", "usr_seed_sita", "svc_plumber", None, "BOOKED", day_after, "AFTERNOON",
                "plc_podalakur", now - timedelta(hours=5), [("q_plumb_problem", ["plumb_leak", "plumb_motor"], None, False)],
                landmark="Behind the temple", pin=(14.4169, 79.7331))
    run(conn, "INSERT INTO opportunity_notification (request_id, partner_id, batch, notified_at) VALUES ('rqt_seed_booked_plumber', 'usr_seed_kiran', 1, :t) ON CONFLICT DO NOTHING",
        t=now - timedelta(hours=5))
    offer(conn, "ofr_seed_kiran_visit", "rqt_seed_booked_plumber", "usr_seed_kiran", "BOOKED", "INSPECTION", 30000, day_part="AFTERNOON",
          created=now - timedelta(hours=4))
    run(
        conn,
        """INSERT INTO booking (id, request_id, offer_id, customer_id, partner_id, state, booked_pricing_type,
               booked_inspection_charge_minor, schedule_date, day_part, arrival_expected_by, completion_pin,
               booked_partner_display_name, created_at)
           VALUES ('bkg_seed_plumber', 'rqt_seed_booked_plumber', 'ofr_seed_kiran_visit', 'usr_seed_sita', 'usr_seed_kiran',
                   'BOOKED', 'INSPECTION', 30000, :d, 'AFTERNOON', (CAST(:d AS DATE) + time '17:30') AT TIME ZONE 'Asia/Kolkata',
                   '4827', 'Kiran', :t)
           ON CONFLICT DO NOTHING""",
        d=day_after, t=now - timedelta(hours=3),
    )

    request_row(conn, "rqt_seed_done_tractor", "usr_seed_ramana", "svc_tractor", "ploughing", "COMPLETED", last_week, "MORNING",
                "plc_marupuru", now - timedelta(days=6),
                [("q_work_type", ["ploughing"], None, False), ("q_acres", [], "ACRE", True), ("q_trips", ["2"], "TRIP", False)])
    run(conn, "INSERT INTO opportunity_notification (request_id, partner_id, batch, notified_at) VALUES ('rqt_seed_done_tractor', 'usr_seed_ravi', 1, :t) ON CONFLICT DO NOTHING",
        t=now - timedelta(days=6))
    offer(conn, "ofr_seed_ravi_done", "rqt_seed_done_tractor", "usr_seed_ravi", "BOOKED", "FIXED", 180000, cost=180000, created=now - timedelta(days=6))
    run(
        conn,
        """INSERT INTO booking (id, request_id, offer_id, customer_id, partner_id, state, booked_pricing_type,
               booked_exact_amount_minor, booked_cost_minor, schedule_date, day_part, arrival_expected_by, completion_pin,
               booked_partner_display_name, on_my_way_at, arrived_at, completed_at, completed_by, created_at)
           VALUES ('bkg_seed_done', 'rqt_seed_done_tractor', 'ofr_seed_ravi_done', 'usr_seed_ramana', 'usr_seed_ravi',
                   'COMPLETED', 'FIXED', 180000, 180000, :d, 'MORNING', (CAST(:d AS DATE) + time '12:30') AT TIME ZONE 'Asia/Kolkata',
                   '1590', 'Ravi Kumar', :t1, :t2, :t3, 'PARTNER_PIN', :t0)
           ON CONFLICT DO NOTHING""",
        d=last_week, t0=now - timedelta(days=6), t1=now - timedelta(days=5, hours=6), t2=now - timedelta(days=5, hours=5), t3=now - timedelta(days=5, hours=2),
    )
    run(
        conn,
        """INSERT INTO credit_ledger_transaction (id, partner_id, type, amount_minor, booking_id, job_label, note, created_by, created_at)
           VALUES ('ctx_seed_topup', 'usr_seed_ravi', 'TOP_UP', 50000, NULL, NULL, 'Seed top-up', 'seed', :t0),
                  ('ctx_seed_fee', 'usr_seed_ravi', 'JOB_FEE', -9000, 'bkg_seed_done', 'Tractor, Ramana', NULL, 'system', :t1)
           ON CONFLICT DO NOTHING""",
        t0=now - timedelta(days=7), t1=now - timedelta(days=5, hours=2),
    )
    run(
        conn,
        """INSERT INTO review (booking_id, customer_id, partner_id, verdict, tag_codes, comment)
           VALUES ('bkg_seed_done', 'usr_seed_ramana', 'usr_seed_ravi', 'VERY_GOOD', ARRAY['ON_TIME','GOOD_WORK'], 'Good work.')
           ON CONFLICT DO NOTHING""",
    )

    request_row(conn, "rqt_seed_cancelled", "usr_seed_lakshmi", "svc_electrician", None, "CANCELLED", tomorrow, "EVENING",
                "plc_podalakur", now - timedelta(days=1), [("q_elec_problem", ["elec_no_power"], None, False)], ended="CANCELLED_BY_CUSTOMER")
    run(conn, "UPDATE request SET cancel_reason_code = 'PLANS_CHANGED' WHERE id = 'rqt_seed_cancelled'")

    if not scalar(conn, "SELECT 1 FROM place_interest LIMIT 1"):
        for name, phone in (("Kovur", None), ("Kovur", "+919900000001"), ("kovur", None), ("Buchireddipalem", None), ("Buchireddipalem", None), ("Sangam", None)):
            run(conn, "INSERT INTO place_interest (id, name_typed, phone_e164) VALUES (:id, :n, :p)", id=new_id("pli"), n=name, p=phone)
    run(
        conn,
        """INSERT INTO complaint (id, raised_by, booking_id, subject, body, status)
           VALUES ('cmp_seed_1', 'usr_seed_ramana', 'bkg_seed_done', 'Late start', 'He came an hour later than he said.', 'OPEN')
           ON CONFLICT DO NOTHING""",
    )


def main():
    """Seed the database named by DB_TARGET, --target or --test.
    The catalog is always upserted; --no-demo skips demo people and jobs.
    Creates the bootstrap admin from ADMIN_BOOTSTRAP_USERNAME/PASSWORD."""
    parser = argparse.ArgumentParser(description="Seed NeoSeva with catalog and demo data.")
    parser.add_argument("--test", action="store_true", help="use TEST_DATABASE_URL")
    parser.add_argument("--target", choices=("local", "supabase"), help="database to use; default is DB_TARGET")
    parser.add_argument("--no-demo", action="store_true", help="catalog and settings only")
    args = parser.parse_args()
    cfg = Config()
    init_engine(cfg, cfg.test_database_url if args.test else url_or_exit(cfg, args.target))
    print(f"database: {engine().url.host}")
    with tx() as conn:
        seed_geography(conn)
        seed_catalog(conn)
        if not args.no_demo:
            seed_demo(conn)
        if cfg.admin_bootstrap_password:
            create_admin(conn, cfg.admin_bootstrap_username, cfg.admin_bootstrap_password)
    print("seeded" + ("" if args.no_demo else " with demo data"))


if __name__ == "__main__":
    main()
