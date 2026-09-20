from dataclasses import dataclass, field

CONTEXTS = [
    "CUSTOMER_CANCEL_REQUEST",
    "CUSTOMER_CANCEL_BOOKING",
    "CUSTOMER_REPORT_ISSUE",
    "PARTNER_CANCEL_BOOKING",
    "PARTNER_REPORT_ISSUE",
    "PARTNER_DISPUTE_NO_SHOW",
]


@dataclass
class Col:
    name: str
    type: str = "text"
    label: str = ""
    required: bool = False
    readonly: bool = False
    hidden: bool = False
    choices: list = field(default_factory=list)
    ref: str = ""
    help: str = ""
    list: bool = True
    purpose: str = ""


@dataclass
class Resource:
    name: str
    label: str
    group: str
    key: list
    columns: list
    order: str
    editable: bool = True
    creatable: bool = True
    deletable: bool = True
    singleton: bool = False
    table: str = ""
    help: str = ""

    def __post_init__(self):
        """Fill in defaults that depend on other fields.
        The table defaults to the resource name.
        Column labels default to a readable form of the column name."""
        self.table = self.table or self.name
        for col in self.columns:
            col.label = col.label or col.name.replace("_", " ").capitalize()

    def col(self, name):
        """Find one column by name.
        Returns None for names not in the registry.
        Every SQL identifier comes through here, never from the request."""
        return next((c for c in self.columns if c.name == name), None)


def C(name, type="text", **kw):
    """Shorthand for declaring a column.
    type is text, longtext, int, numeric, bool, int_array, text_array, point, geojson, json, timestamp or image.
    An image column holds the URL of a picture uploaded through the console.
    Extra keyword arguments set Col fields."""
    return Col(name, type, **kw)


RO = {"editable": False, "creatable": False, "deletable": False}
DAYPARTS = ["MORNING", "AFTERNOON", "EVENING", "ANY_TIME"]

RESOURCES = [
    Resource(
        "geography", "Geographies", "Areas & places", ["id"],
        [
            C("id", required=True), C("label", required=True), C("timezone", required=True, help="IANA name, e.g. Asia/Kolkata"),
            C("same_day_cutoff_hour", "int", required=True), C("book_ahead_days", "int", required=True),
            C("minimum_notice_minutes", "int", required=True), C("no_show_prompt_delay_minutes", "int", required=True),
            C("preferred_partner_head_start_minutes", "int", required=True), C("morning_ends_hour", "int", required=True),
            C("afternoon_ends_hour", "int", required=True), C("evening_ends_hour", "int", required=True),
            C("is_active", "bool"), C("boundary", "geojson", list=False, help="GeoJSON Polygon or MultiPolygon for the service area"),
        ],
        "id", help="Everything about how a day works in an area. Changes reach apps through the catalog.",
    ),
    Resource(
        "place", "Villages", "Areas & places", ["id"],
        [
            C("id", required=True, help="Readable key, e.g. plc_podalakur"), C("geography_id", required=True, ref="geography"),
            C("name", required=True), C("mandal", required=True), C("district", required=True), C("state", required=True),
            C("centre", "point", required=True), C("boundary", "geojson", list=False, help="GeoJSON; decides which village a pin is in"),
            C("is_active", "bool"),
        ],
        "name",
    ),
    Resource(
        "service", "Services", "Services", ["id"],
        [
            C("id", required=True, help="Permanent, e.g. svc_tractor"), C("name", required=True), C("icon_url", "image", label="Icon", purpose="SERVICE_ICON"),
            C("category", required=True), C("category_label", required=True), C("common_on_home", "bool"),
            C("sort_order", "int"), C("is_active", "bool"),
        ],
        "sort_order, name",
    ),
    Resource(
        "service_geography", "Service areas", "Services", ["service_id", "geography_id"],
        [C("service_id", required=True, ref="service"), C("geography_id", required=True, ref="geography")],
        "service_id", editable=False, help="A service with no row here runs nowhere.",
    ),
    Resource(
        "service_equipment", "Equipment", "Services", ["service_id", "id"],
        [C("service_id", required=True, ref="service"), C("id", required=True, help="e.g. ROTAVATOR"), C("label", required=True), C("sort_order", "int")],
        "service_id, sort_order",
    ),
    Resource(
        "service_offer_unit", "Offer units", "Services", ["service_id", "code"],
        [
            C("service_id", required=True, ref="service"), C("code", required=True, help="e.g. ACRE"),
            C("label", required=True, help="Counting word: acres"), C("per_label", help="After 'per': acre"), C("sort_order", "int"),
        ],
        "service_id, sort_order",
        help="Units a partner may price this service in (allowedOfferUnits).",
    ),
    Resource(
        "request_question", "Questions", "Request questions", ["id"],
        [
            C("id", required=True), C("service_id", required=True, ref="service"),
            C("template_version", "int", help="Set and raised automatically on every question change"),
            C("type", required=True, choices=["SINGLE_CHOICE", "MULTI_CHOICE", "NUMBER_WITH_UNIT"]),
            C("label", required=True), C("short_label", help="Heading the partner reads, e.g. Land"),
            C("required", "bool"), C("allow_not_sure", "bool"),
            C("unit_code", list=False), C("unit_label", list=False), C("unit_per_label", list=False),
            C("min_value", "numeric", list=False), C("max_value", "numeric", list=False),
            C("step_value", "numeric", list=False), C("default_value", "numeric", list=False),
            C("depends_on_question_id", ref="request_question", list=False), C("depends_on_values", "text_array", list=False),
            C("sort_order", "int"),
        ],
        "service_id, sort_order",
        help="Three types only. Number questions need unit, min, max, step and default.",
    ),
    Resource(
        "request_question_option", "Question options", "Request questions", ["id"],
        [
            C("id", required=True, help="e.g. rotavator"), C("question_id", required=True, ref="request_question"),
            C("label", required=True), C("service_id", ref="service"), C("icon_url", list=False),
            C("equipment_id", help="Equipment a partner needs for this work type"), C("sort_order", "int"),
        ],
        "question_id, sort_order",
    ),
    Resource(
        "help_me_choose_option", "Help me choose", "Catalog lists", ["id"],
        [
            C("id", required=True), C("label", required=True), C("icon_url"), C("service_id", ref="service", help="Empty = something else"),
            C("sort_order", "int"), C("is_active", "bool"),
        ],
        "sort_order",
    ),
    Resource(
        "reason", "Reasons", "Catalog lists", ["context", "code"],
        [C("context", required=True, choices=CONTEXTS), C("code", required=True), C("label", required=True), C("sort_order", "int"), C("is_active", "bool")],
        "context, sort_order",
    ),
    Resource("place_label", "Place labels", "Catalog lists", ["code"], [C("code", required=True), C("label", required=True), C("sort_order", "int")], "sort_order"),
    Resource(
        "review_tag", "Review tags", "Catalog lists", ["code"],
        [C("code", required=True), C("label", required=True), C("positive", "bool", required=True), C("sort_order", "int")],
        "sort_order",
    ),
    Resource("experience_range", "Experience ranges", "Catalog lists", ["code"], [C("code", required=True), C("label", required=True), C("sort_order", "int")], "sort_order"),
    Resource(
        "display_text", "Display text", "Settings", ["key"],
        [C("key", required=True), C("label", "longtext", required=True), C("note", "longtext")],
        "key", help="Served sentences: endings, daypart words, area label format.",
    ),
    Resource(
        "catalog_setting", "Catalog settings", "Settings", ["id"],
        [
            C("id", "int", readonly=True), C("job_fee_percent", "numeric", required=True, help="Can only ever be cut"),
            C("cancellation_limit", "int", required=True), C("cancellation_window_days", "int", required=True),
            C("max_offer_rupees", "int", required=True, help="0 means no ceiling"), C("jobs_on_home_screen", "int", required=True),
            C("travel_distances_km", "int_array", required=True, help="Comma separated, e.g. 5,10,15,25,40"),
            C("help_me_choose_prompt", required=True), C("help_me_choose_version", "int", readonly=True),
            C("terms_version"), C("terms_url"), C("support_officer_name"), C("support_officer_designation"),
            C("support_email"), C("support_phone"), C("support_response_promise", "longtext"),
            C("booking_shares_contact", "longtext", required=True), C("booking_other_offers_close", "longtext", required=True),
            C("booking_payment", "longtext", required=True), C("booking_inspection_charge_adjusted", "longtext", required=True),
            C("pin_guidance", "longtext", required=True, help="{partnerName} is replaced with his name"),
            C("partner_looking_is_free", "longtext", required=True), C("partner_when_charged", "longtext", required=True),
            C("partner_when_reporting_a_problem", "longtext", required=True), C("partner_inspection_charge_adjusted", "longtext", required=True),
        ],
        "id", creatable=False, deletable=False, singleton=True,
    ),
    Resource(
        "platform_setting", "Platform settings", "Settings", ["id"],
        [
            C("id", "int", readonly=True), C("matching_batch_size", "int", required=True), C("matching_batch_interval_minutes", "int", required=True),
            C("otp_daily_limit", "int", required=True), C("pin_max_attempts", "int", required=True), C("pin_lockout_seconds", "int", required=True),
            C("saved_place_upgrade_max_meters", "int", required=True),
            C("well_rated_min_reviews", "int", help="Empty until the founders decide"), C("well_rated_min_positive_percent", "int"),
        ],
        "id", creatable=False, deletable=False, singleton=True,
    ),
    Resource(
        "app_user", "Users", "People", ["id"],
        [C("id"), C("phone_e164"), C("phone_verified_at", "timestamp"), C("language", choices=["te", "en"]), C("created_at", "timestamp")],
        "created_at DESC", **RO,
    ),
    Resource(
        "customer_profile", "Customers", "People", ["user_id"],
        [C("user_id", readonly=True), C("display_name"), C("default_place_id", ref="place"), C("is_restricted", "bool"), C("created_at", "timestamp", readonly=True)],
        "created_at DESC", creatable=False, deletable=False,
    ),
    Resource(
        "partner_profile", "Partners", "People", ["user_id"],
        [
            C("user_id", readonly=True), C("display_name"), C("status", readonly=True), C("status_note", readonly=True),
            C("base_place_id", ref="place"), C("experience_range_code", ref="experience_range"), C("accepting_new_jobs", "bool"),
            C("travel_radius_km", "int"), C("next_step", readonly=True), C("profile_photo_media_id", readonly=True, list=False),
            C("accepted_terms_version", list=False), C("created_at", "timestamp", readonly=True),
        ],
        "created_at DESC", creatable=False, deletable=False, help="Status changes go through the Partners page actions.",
    ),
    Resource(
        "partner_service", "Partner services", "People", ["partner_id", "service_id"],
        [C("partner_id", ref="partner_profile"), C("service_id", ref="service"), C("equipment_ids", "text_array")],
        "partner_id", **RO,
    ),
    Resource(
        "identity_verification", "Identity checks", "People", ["partner_id"],
        [
            C("partner_id"), C("status"), C("document_type"), C("document_media_id"), C("selfie_media_id"),
            C("submitted_at", "timestamp"), C("reviewed_at", "timestamp"), C("reviewed_by"), C("review_note", "longtext"),
        ],
        "submitted_at DESC", **RO,
    ),
    Resource(
        "saved_place", "Saved places", "People", ["id"],
        [C("id"), C("user_id"), C("label_code"), C("place_id"), C("landmark"), C("pin", "point"), C("accuracy_meters", "numeric"), C("created_at", "timestamp")],
        "created_at DESC", **RO,
    ),
    Resource(
        "request", "Requests", "Work", ["id"],
        [
            C("id"), C("customer_id"), C("geography_id"), C("service_id"), C("work_type_id"), C("template_version", "int", list=False),
            C("state"), C("schedule_date"), C("day_part"), C("place_id"), C("landmark", list=False), C("pin", "point", list=False),
            C("accuracy_meters", "numeric", list=False), C("description", "longtext", list=False), C("origin_source", list=False),
            C("preferred_partner_id", list=False), C("ended_reason"), C("ended_at", "timestamp", list=False),
            C("cancel_reason_code", list=False), C("cancel_note", list=False), C("is_rematching", "bool", list=False),
            C("created_at", "timestamp"),
        ],
        "created_at DESC", **RO,
    ),
    Resource(
        "request_answer", "Request answers", "Work", ["request_id", "question_id"],
        [C("request_id"), C("question_id"), C("values", "text_array"), C("unit_code"), C("not_sure", "bool")],
        "request_id", **RO,
    ),
    Resource(
        "opportunity_notification", "Notifications", "Work", ["request_id", "partner_id"],
        [C("request_id"), C("partner_id"), C("batch", "int"), C("notified_at", "timestamp"), C("declined_at", "timestamp")],
        "notified_at DESC", **RO,
    ),
    Resource(
        "offer", "Offers", "Work", ["id"],
        [
            C("id"), C("request_id"), C("partner_id"), C("status"), C("pricing_type"), C("exact_amount_minor", "int"),
            C("rate_minor", "int"), C("unit_code"), C("inspection_charge_minor", "int"), C("comparable_cost_minor", "int"),
            C("offered_day_part"), C("note", list=False), C("created_at", "timestamp"), C("withdrawn_at", "timestamp", list=False),
        ],
        "created_at DESC", **RO,
    ),
    Resource(
        "booking", "Bookings", "Work", ["id"],
        [
            C("id"), C("request_id"), C("offer_id", list=False), C("customer_id"), C("partner_id"), C("state"),
            C("booked_pricing_type"), C("booked_cost_minor", "int"), C("schedule_date"), C("day_part"),
            C("arrival_expected_by", "timestamp", list=False), C("final_amount_minor", "int", list=False),
            C("final_amount_agreed", "bool", list=False), C("on_my_way_at", "timestamp", list=False),
            C("arrived_at", "timestamp", list=False), C("completed_at", "timestamp"), C("completed_by", list=False),
            C("cancelled_at", "timestamp", list=False), C("cancelled_by", list=False), C("partner_fell_through", "bool", list=False),
            C("created_at", "timestamp"),
        ],
        "created_at DESC", help="The completion PIN is never shown here.", **RO,
    ),
    Resource(
        "credit_ledger_transaction", "Ledger", "Money", ["id"],
        [
            C("id"), C("partner_id"), C("type"), C("amount_minor", "int"), C("booking_id"), C("job_label"),
            C("note"), C("created_by"), C("created_at", "timestamp"),
        ],
        "created_at DESC", help="Append only. Corrections are new rows via Adjust credit.", **RO,
    ),
    Resource("partner_balance", "Balances", "Money", ["partner_id"], [C("partner_id"), C("available_minor", "int")], "available_minor", **RO),
    Resource(
        "job_report", "Job reports", "Support", ["id"],
        [C("id"), C("booking_id"), C("reported_by"), C("context"), C("reason_code"), C("note", "longtext"), C("created_at", "timestamp")],
        "created_at DESC", **RO,
    ),
    Resource(
        "review", "Reviews", "Support", ["booking_id"],
        [C("booking_id"), C("customer_id"), C("partner_id"), C("verdict"), C("tag_codes", "text_array"), C("comment", "longtext"), C("created_at", "timestamp")],
        "created_at DESC", **RO,
    ),
    Resource("saved_partner", "Saved partners", "Support", ["customer_id", "partner_id"], [C("customer_id"), C("partner_id"), C("created_at", "timestamp")], "created_at DESC", **RO),
    Resource(
        "referral", "Referrals", "Support", ["id"],
        [C("id"), C("referrer_user_id"), C("referred_user_id"), C("role"), C("code"), C("status"), C("qualifying_booking_id"), C("rewarded_at", "timestamp"), C("created_at", "timestamp")],
        "created_at DESC", **RO,
    ),
    Resource(
        "complaint", "Complaints", "Support", ["id"],
        [
            C("id", readonly=True), C("raised_by", readonly=True), C("booking_id", readonly=True), C("subject", readonly=True),
            C("body", "longtext", readonly=True), C("status", required=True, choices=["OPEN", "IN_REVIEW", "RESOLVED"]),
            C("resolution", "longtext"), C("created_at", "timestamp", readonly=True), C("resolved_at", "timestamp", readonly=True),
        ],
        "created_at DESC", creatable=False, deletable=False, help="48 hours to acknowledge, one month to settle.",
    ),
    Resource(
        "place_interest", "Place interest", "Demand", ["id"],
        [C("id"), C("name_typed"), C("phone_e164"), C("created_at", "timestamp")],
        "created_at DESC", **RO,
    ),
    Resource(
        "media", "Media", "System", ["id"],
        [C("id"), C("user_id"), C("purpose"), C("content_type"), C("url"), C("bytes", "int"), C("created_at", "timestamp"), C("deleted_at", "timestamp")],
        "created_at DESC", **RO,
    ),
    Resource(
        "device", "Devices", "System", ["device_id"],
        [C("device_id"), C("user_id"), C("app"), C("platform"), C("push_token", list=False), C("updated_at", "timestamp")],
        "updated_at DESC", **RO,
    ),
    Resource(
        "auth_session", "Sessions", "System", ["id"],
        [C("id"), C("user_id"), C("app"), C("issued_at", "timestamp"), C("expires_at", "timestamp"), C("revoked_at", "timestamp")],
        "issued_at DESC", **RO,
    ),
    Resource(
        "otp_request", "OTP requests", "System", ["id"],
        [C("id"), C("phone_e164"), C("purpose"), C("expires_at", "timestamp"), C("attempts", "int"), C("consumed_at", "timestamp"), C("created_at", "timestamp")],
        "created_at DESC", **RO,
    ),
    Resource(
        "idempotency_key", "Idempotency keys", "System", ["id"],
        [C("id"), C("user_id"), C("endpoint"), C("key"), C("state"), C("status_code", "int"), C("created_at", "timestamp")],
        "created_at DESC", **RO,
    ),
]

BY_NAME = {r.name: r for r in RESOURCES}
