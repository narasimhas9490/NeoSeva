import base64
import json

from app.core.errors import bad_request

PAGE_SIZE = 20


def encode_cursor(values):
    """Turn the sort key of the last row into an opaque cursor.
    The apps pass it back unchanged to fetch the next page.
    values is a small dict such as {createdAt, id}."""
    raw = json.dumps(values, default=str, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor):
    """Read a cursor produced by encode_cursor.
    Returns None when no cursor was given.
    A cursor that cannot be read is a 400 INVALID_CURSOR."""
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, json.JSONDecodeError):
        raise bad_request("INVALID_CURSOR", "The cursor could not be read.")


def page(rows, key, size=PAGE_SIZE):
    """Split a size + 1 fetch into one page and the next cursor.
    key builds the cursor dict from the last row kept.
    nextCursor is None at the end of the list."""
    if len(rows) > size:
        kept = rows[:size]
        return kept, encode_cursor(key(kept[-1]))
    return rows, None
