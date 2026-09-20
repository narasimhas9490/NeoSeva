from app.core.db import many, one
from app.shared.settings import catalog_setting, default_geography, geography
from app.shared.templates import load_questions, question_shape, servable

REASON_CONTEXTS = (
    "CUSTOMER_CANCEL_REQUEST",
    "CUSTOMER_CANCEL_BOOKING",
    "CUSTOMER_REPORT_ISSUE",
    "PARTNER_CANCEL_BOOKING",
    "PARTNER_REPORT_ISSUE",
    "PARTNER_DISPUTE_NO_SHOW",
)


def resolve_area(conn, area_id):
    """Find the geography a catalog call is about.
    No areaId means the default geography; an unknown one means None.
    Returns (geography_row_or_None, known_flag)."""
    if not area_id:
        return default_geography(conn), True
    geo = geography(conn, area_id)
    return (geo, True) if geo and geo["is_active"] else (None, False)


def services(conn, geo):
    """List the services running in a geography, each with its equipment.
    Only services joined through service_geography are sent.
    An unknown area gets an empty list."""
    if geo is None:
        return []
    rows = many(
        conn,
        """SELECT s.* FROM service s JOIN service_geography sg ON sg.service_id = s.id
           WHERE sg.geography_id = :g AND s.is_active ORDER BY s.sort_order, s.name""",
        g=geo["id"],
    )
    equipment = many(
        conn,
        "SELECT * FROM service_equipment WHERE service_id = ANY(:ids) ORDER BY sort_order, id",
        ids=[r["id"] for r in rows] or [""],
    )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "iconUrl": r["icon_url"],
            "category": r["category"],
            "categoryLabel": r["category_label"],
            "commonOnHome": r["common_on_home"],
            "equipment": [{"id": e["id"], "label": e["label"]} for e in equipment if e["service_id"] == r["id"]],
        }
        for r in rows
    ]


def help_me_choose(conn, geo, catalog, running_ids):
    """Build the guided picker for customers who do not know the trade.
    Options whose service does not run here are never sent.
    The null-service 'something else' option always may be."""
    rows = many(
        conn,
        """SELECT h.*, s.name AS service_name FROM help_me_choose_option h
           LEFT JOIN service s ON s.id = h.service_id
           WHERE h.is_active ORDER BY h.sort_order, h.id""",
    )
    options = []
    if geo is not None:
        for r in rows:
            if r["service_id"] is not None and r["service_id"] not in running_ids:
                continue
            options.append(
                {
                    "id": r["id"],
                    "label": r["label"],
                    "iconUrl": r["icon_url"],
                    "serviceId": r["service_id"],
                    "serviceName": r["service_name"] if r["service_id"] else None,
                }
            )
    return {
        "templateVersion": catalog["help_me_choose_version"],
        "prompt": catalog["help_me_choose_prompt"],
        "options": options,
    }


def reasons(conn):
    """List every reason, keyed by its six contexts.
    All six keys are always present, empty or not.
    An empty list must never block anybody."""
    grouped = {context: [] for context in REASON_CONTEXTS}
    for r in many(conn, "SELECT * FROM reason WHERE is_active ORDER BY context, sort_order, code"):
        grouped[r["context"]].append({"code": r["code"], "label": r["label"]})
    return grouped


def scheduling(geo):
    """Put a geography's day into the flat scheduling block.
    The three daypart hours sit directly under scheduling, not nested.
    These numbers are the promise made to a customer."""
    return {
        "sameDayCutoffHour": geo["same_day_cutoff_hour"],
        "bookAheadDays": geo["book_ahead_days"],
        "minimumNoticeMinutes": geo["minimum_notice_minutes"],
        "noShowPromptDelayMinutes": geo["no_show_prompt_delay_minutes"],
        "preferredPartnerHeadStartMinutes": geo["preferred_partner_head_start_minutes"],
        "morningEndsHour": geo["morning_ends_hour"],
        "afternoonEndsHour": geo["afternoon_ends_hour"],
        "eveningEndsHour": geo["evening_ends_hour"],
    }


def build_catalog(conn, area_id):
    """Assemble everything both apps display, in one body.
    Eighteen keys, always all present; unknown areas get empty service lists.
    Returns (data, timezone) so meta reports the area's own clock."""
    catalog = catalog_setting(conn)
    geo, _ = resolve_area(conn, area_id)
    service_list = services(conn, geo)
    fallback = geo or default_geography(conn)
    data = {
        "services": service_list,
        "helpMeChoose": help_me_choose(conn, geo, catalog, {s["id"] for s in service_list}),
        "reasons": reasons(conn),
        "placeLabels": [
            {"code": r["code"], "label": r["label"]}
            for r in many(conn, "SELECT * FROM place_label ORDER BY sort_order, code")
        ],
        "reviewTags": [
            {"code": r["code"], "label": r["label"], "positive": r["positive"]}
            for r in many(conn, "SELECT * FROM review_tag ORDER BY sort_order, code")
        ],
        "experienceRanges": [
            {"code": r["code"], "label": r["label"]}
            for r in many(conn, "SELECT * FROM experience_range ORDER BY sort_order, code")
        ],
        "travelDistancesKm": list(catalog["travel_distances_km"] or []),
        "jobsOnHomeScreen": catalog["jobs_on_home_screen"],
        "maxOfferRupees": int(catalog["max_offer_rupees"]),
        "jobFeePercent": float(catalog["job_fee_percent"]),
        "cancellationLimit": catalog["cancellation_limit"],
        "cancellationWindowDays": catalog["cancellation_window_days"],
        "termsVersion": catalog["terms_version"],
        "termsUrl": catalog["terms_url"],
        "supportContact": {
            "officerName": catalog["support_officer_name"],
            "officerDesignation": catalog["support_officer_designation"],
            "email": catalog["support_email"],
            "phoneNumber": catalog["support_phone"],
            "responsePromise": catalog["support_response_promise"],
        },
        "bookingTerms": {
            "sharesContact": catalog["booking_shares_contact"],
            "otherOffersClose": catalog["booking_other_offers_close"],
            "payment": catalog["booking_payment"],
            "inspectionChargeAdjusted": catalog["booking_inspection_charge_adjusted"],
        },
        "partnerTerms": {
            "lookingIsFree": catalog["partner_looking_is_free"],
            "whenCharged": catalog["partner_when_charged"],
            "inspectionChargeAdjusted": catalog["partner_inspection_charge_adjusted"],
            "whenReportingAProblem": catalog["partner_when_reporting_a_problem"],
        },
        "scheduling": scheduling(fallback) if fallback else None,
    }
    return data, (fallback["timezone"] if fallback else None)


def request_template(conn, service_id):
    """Build the questions for one service.
    Questions the app cannot draw are left out rather than trapping her.
    Returns None when the service does not exist at all."""
    if one(conn, "SELECT 1 FROM service WHERE id = :id", id=service_id) is None:
        return None
    version, questions = load_questions(conn, service_id)
    return {
        "serviceId": service_id,
        "templateVersion": version,
        "questions": [question_shape(q) for q in questions if servable(q)],
    }
