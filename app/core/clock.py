from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DAYPARTS = ("MORNING", "AFTERNOON")
REQUEST_DAYPARTS = DAYPARTS + ("ANY_TIME",)

MONTH_NAMES = {
    "en": [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ],
    "te": [
        "జనవరి", "ఫిబ్రవరి", "మార్చి", "ఏప్రిల్", "మే", "జూన్",
        "జూలై", "ఆగస్టు", "సెప్టెంబర్", "అక్టోబర్", "నవంబర్", "డిసెంబర్",
    ],
}

_offset = timedelta(0)


def set_offset(delta):
    """Shift the application clock by a fixed amount.
    Tests use it to stand past a cutoff without touching the machine clock.
    Pass timedelta(0) to return to real time."""
    global _offset
    _offset = delta


def now_utc():
    """Return the current moment as an aware UTC datetime.
    Every rule that reads the time goes through this one function.
    Honours the offset set by set_offset."""
    return datetime.now(timezone.utc) + _offset


def local_now(tz_name):
    """Return the current wall clock in a geography's timezone.
    Cutoffs and dayparts are read from this, never the server or phone clock.
    tz_name is the geography.timezone column, e.g. Asia/Kolkata."""
    return now_utc().astimezone(ZoneInfo(tz_name))


def local_today(tz_name):
    """Return today's date in the geography's own calendar.
    A request for 'today' means today there, not today in UTC.
    Used by every date-in-past and horizon check."""
    return local_now(tz_name).date()


def zoneless_local(moment, tz_name):
    """Format a moment as a local wall clock without a zone suffix.
    This is how serverTime and arrivalExpectedBy go on the wire.
    Returns None when the moment is None."""
    if moment is None:
        return None
    return moment.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None).isoformat(timespec="seconds")


def iso_utc(moment):
    """Format a moment as UTC ISO-8601 with a trailing Z.
    Used for createdAt, postedAt and event timestamps.
    Returns None when the moment is None."""
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def daypart_end(geo, on_date, day_part):
    """Return the aware local moment a daypart ends on a date.
    Hours come from the geography's morning/afternoon columns.
    ANY_TIME ends when the afternoon ends; there is no evening."""
    hours = {
        "MORNING": geo["morning_ends_hour"],
        "AFTERNOON": geo["afternoon_ends_hour"],
        "ANY_TIME": geo["afternoon_ends_hour"],
    }
    return datetime(on_date.year, on_date.month, on_date.day, hours[day_part], tzinfo=ZoneInfo(geo["timezone"]))


def arrival_expected_by(geo, on_date, day_part):
    """Compute when a partner is expected by for a booked daypart.
    It is the end of the daypart plus the no-show grace, in local time.
    Stored as TIMESTAMPTZ and formatted zoneless on the way out."""
    return daypart_end(geo, on_date, day_part) + timedelta(minutes=geo["no_show_prompt_delay_minutes"])


def has_notice(geo, on_date, day_part):
    """Say whether a daypart still has the minimum notice remaining.
    ANY_TIME passes when any single daypart of that day still does.
    The geography's minimum_notice_minutes is the threshold."""
    now = local_now(geo["timezone"])
    notice = timedelta(minutes=geo["minimum_notice_minutes"])
    parts = DAYPARTS if day_part == "ANY_TIME" else (day_part,)
    return any(daypart_end(geo, on_date, part) - now >= notice for part in parts)


def schedule_problem(geo, on_date, day_part, include_request_rules=True):
    """Return the error code that refuses a schedule, or None.
    Order: date in past, same-day cutoff, horizon, then notice.
    Booking re-checks only past and notice, so it passes include_request_rules=False."""
    now = local_now(geo["timezone"])
    today = now.date()
    if on_date < today:
        return "DATE_IN_PAST"
    if include_request_rules:
        if on_date == today and now.hour >= geo["same_day_cutoff_hour"]:
            return "SAME_DAY_CLOSED"
        if on_date > today + timedelta(days=geo["book_ahead_days"]):
            return "BEYOND_BOOKING_HORIZON"
    if not has_notice(geo, on_date, day_part):
        return "INSUFFICIENT_NOTICE"
    return None


def parse_date(value):
    """Parse a YYYY-MM-DD string into a date.
    Returns None for anything that is not a real calendar date.
    Callers turn None into their own 400."""
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def short_date(on_date):
    """Format a date as '19 Sep' for schedule labels.
    Built by hand because %-d is not portable to Windows.
    The month is the English three-letter abbreviation."""
    return f"{on_date.day} {on_date.strftime('%b')}"


def full_date(on_date, language):
    """Format a date as '19 September' in the reader's language.
    Built by hand, like short_date, so it is portable to Windows.
    Falls back to English month names for a language not in MONTH_NAMES."""
    months = MONTH_NAMES.get(language, MONTH_NAMES["en"])
    return f"{on_date.day} {months[on_date.month - 1]}"
