from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

CURRENCY = "INR"


def money(amount_minor):
    """Put an amount in paise into its wire shape.
    Always {amountMinor, currency}, never a bare number or a string.
    Returns None when the amount is None."""
    if amount_minor is None:
        return None
    return {"amountMinor": int(amount_minor), "currency": CURRENCY}


def read_money(value):
    """Read a {amountMinor, currency} object sent by an app.
    Returns the amount in paise, or None when the shape is wrong.
    Only positive whole paise in INR are accepted."""
    if not isinstance(value, dict):
        return None
    amount = value.get("amountMinor")
    if value.get("currency", CURRENCY) != CURRENCY:
        return None
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return None
    return amount


def fee_minor(amount_minor, percent):
    """Compute the job fee on an amount, rounded down to the rupee.
    Two divisions: percent of paise, then floor to whole rupees.
    A ₹2,449 job at 5% is 12200 paise, never 12245."""
    if not amount_minor:
        return 0
    paise = Decimal(int(amount_minor)) * Decimal(str(percent)) / Decimal(100)
    rupees = (paise / Decimal(100)).to_integral_value(rounding=ROUND_FLOOR)
    return int(rupees) * 100


def times(rate_minor, quantity):
    """Multiply a rate in paise by a quantity, rounded to the paise.
    Used for rate × acres and for the partner's final quantity.
    Quantity may be fractional, such as half an acre."""
    total = Decimal(int(rate_minor)) * Decimal(str(quantity))
    return int(total.to_integral_value(rounding=ROUND_HALF_UP))


def max_offer_minor(max_offer_rupees):
    """Convert the served ceiling in rupees to paise.
    Missing or zero means no ceiling, never a ceiling of zero.
    Returns None when there is no ceiling."""
    if not max_offer_rupees:
        return None
    return int(max_offer_rupees) * 100
