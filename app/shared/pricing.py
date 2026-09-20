from decimal import Decimal, InvalidOperation

from app.core.db import many
from app.core.errors import unprocessable
from app.core.money import max_offer_minor, read_money, times

PRICING_FIELDS = {
    "FIXED": {"exactAmount"},
    "UNIT_RATE": {"rate", "unit"},
    "INSPECTION": {"inspectionCharge"},
}
ALL_PRICING_FIELDS = {"exactAmount", "rate", "unit", "inspectionCharge"}


def allowed_units(conn, service_id):
    """List the units a job in this service may be priced in.
    The service decides; neither app holds this table.
    Each unit carries code, label and perLabel."""
    rows = many(
        conn,
        "SELECT code, label, per_label FROM service_offer_unit WHERE service_id = :s ORDER BY sort_order, code",
        s=service_id,
    )
    return [{"code": r["code"], "label": r["label"], "perLabel": r["per_label"]} for r in rows]


def parse_pricing(conn, service_id, pricing, catalog):
    """Validate the pricing a partner sends and return its column values.
    Exactly the fields for the type, positive INR amounts, an allowed unit.
    Refuses above maxOfferRupees; a zero ceiling means no ceiling."""
    if not isinstance(pricing, dict) or pricing.get("type") not in PRICING_FIELDS:
        raise unprocessable("INVALID_PRICING", "pricing.type must be FIXED, UNIT_RATE or INSPECTION.")
    kind = pricing["type"]
    present = {k for k in ALL_PRICING_FIELDS if pricing.get(k) is not None}
    if present != PRICING_FIELDS[kind]:
        raise unprocessable("INVALID_PRICING", f"{kind} carries exactly {sorted(PRICING_FIELDS[kind])}.")
    columns = {"exact": None, "rate": None, "unit_code": None, "inspection": None}
    if kind == "FIXED":
        columns["exact"] = read_money(pricing["exactAmount"])
        amount = columns["exact"]
    elif kind == "UNIT_RATE":
        columns["rate"] = read_money(pricing["rate"])
        amount = columns["rate"]
        unit = pricing["unit"]
        code = unit.get("code") if isinstance(unit, dict) else None
        if code not in {u["code"] for u in allowed_units(conn, service_id)}:
            raise unprocessable("INVALID_OFFER_UNIT", "That unit is not allowed for this job.", {"unit": code})
        columns["unit_code"] = code
    else:
        columns["inspection"] = read_money(pricing["inspectionCharge"])
        amount = columns["inspection"]
    if amount is None:
        raise unprocessable("INVALID_PRICING", "Amounts are positive whole paise in INR.")
    ceiling = max_offer_minor(catalog["max_offer_rupees"])
    if ceiling is not None and amount > ceiling:
        raise unprocessable(
            "OFFER_ABOVE_MAXIMUM", "The amount is above the maximum.", {"maxOfferRupees": int(catalog["max_offer_rupees"])}
        )
    return kind, columns


def request_quantity(conn, request_id, unit_code):
    """Find the one quantity she gave in the unit of his offer.
    Exactly one sure, single-valued answer in that unit, or nothing.
    More than one match means no quantity, never a guess."""
    rows = many(
        conn,
        """SELECT "values"[1] AS v FROM request_answer
           WHERE request_id = :r AND unit_code = :u AND not_sure = false AND array_length("values", 1) = 1""",
        r=request_id,
        u=unit_code,
    )
    if len(rows) != 1:
        return None
    try:
        return Decimal(rows[0]["v"])
    except (InvalidOperation, TypeError):
        return None


def comparable_cost(conn, request_id, kind, columns):
    """Compute what the job costs her, or None.
    FIXED is itself; a rate times her quantity is the product; otherwise None.
    Never a rate: this is the headline price on her screens."""
    if kind == "FIXED":
        return columns["exact"]
    if kind == "UNIT_RATE":
        quantity = request_quantity(conn, request_id, columns["unit_code"])
        return None if quantity is None else times(columns["rate"], quantity)
    return None


def mark_claims(offers):
    """Set isLowestPrice and isMostJobsCompleted on active offers.
    One unpriceable offer makes every lowest-price flag false; ties mark everyone.
    With one offer nothing is claimed, because saying nothing is always honest."""
    active = [o for o in offers if o["status"] == "ACTIVE"]
    for o in offers:
        o["isLowestPrice"] = False
        o["isMostJobsCompleted"] = False
    if len(active) < 2:
        return offers
    costs = [o["_cost"] for o in active]
    if all(c is not None for c in costs):
        lowest = min(costs)
        for o in active:
            o["isLowestPrice"] = o["_cost"] == lowest
    top = max(o["_completed"] for o in active)
    if top > 0:
        for o in active:
            o["isMostJobsCompleted"] = o["_completed"] == top
    return offers
