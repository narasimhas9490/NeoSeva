import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import DBAPIError, IntegrityError

from app.admin.api.registry import BY_NAME
from app.core.db import many, one, run, scalar
from app.core.errors import ApiError, bad_request, not_found, unprocessable

PAGE = 100


def resource(name):
    """Find a registered resource by name or raise 404.
    Only tables in the registry are reachable from the admin API.
    Returns the Resource."""
    res = BY_NAME.get(name)
    if res is None:
        raise not_found("UNKNOWN_RESOURCE", f"No admin resource {name}.")
    return res


def _select_list(res):
    """Build the SELECT list for a resource.
    Points become lat and lng, geometry becomes GeoJSON text.
    Every identifier comes from the registry."""
    parts = []
    for c in res.columns:
        if c.type == "point":
            parts.append(f"ST_Y({c.name}::geometry) AS {c.name}_lat, ST_X({c.name}::geometry) AS {c.name}_lng")
        elif c.type == "geojson":
            parts.append(f"ST_AsGeoJSON({c.name}) AS {c.name}")
        else:
            parts.append(f'"{c.name}"')
    return ", ".join(parts)


def _plain(value):
    """Turn a database value into something JSON can carry.
    Decimals become numbers, dates become ISO strings.
    Lists are converted item by item."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _row_out(res, row):
    """Shape one database row for the admin UI.
    Point columns are regrouped into {lat, lng}.
    Every value is JSON-safe."""
    out = {}
    for c in res.columns:
        if c.type == "point":
            lat, lng = row.get(f"{c.name}_lat"), row.get(f"{c.name}_lng")
            out[c.name] = None if lat is None else {"lat": lat, "lng": lng}
        else:
            out[c.name] = _plain(row.get(c.name))
    return out


def _key_where(res, key):
    """Build the WHERE clause that finds one row by its key.
    key is a dict holding every key column.
    A missing key column is a 400."""
    if not isinstance(key, dict) or any(k not in key for k in res.key):
        raise bad_request("INVALID_KEY", f"key needs {res.key}.")
    return " AND ".join(f'"{k}" = :k_{k}' for k in res.key), {f"k_{k}": key[k] for k in res.key}


def list_rows(conn, res, args):
    """List rows of a resource with optional search, filters and paging.
    q searches every text column; f.<column> filters exactly.
    Returns rows plus the total count."""
    where, params = [], {}
    q = (args.get("q") or "").strip()
    if q:
        text_cols = [c.name for c in res.columns if c.type in ("text", "longtext")]
        if text_cols:
            where.append("(" + " OR ".join(f'CAST("{c}" AS TEXT) ILIKE :q' for c in text_cols) + ")")
            params["q"] = f"%{q}%"
    for arg, value in args.items():
        if arg.startswith("f.") and res.col(arg[2:]) and value != "":
            where.append(f'CAST("{arg[2:]}" AS TEXT) = :f_{arg[2:]}')
            params[f"f_{arg[2:]}"] = value
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = scalar(conn, f"SELECT count(*) FROM {res.table}{clause}", **params)
    offset = max(int(args.get("offset", 0) or 0), 0)
    rows = many(
        conn,
        f"SELECT {_select_list(res)} FROM {res.table}{clause} ORDER BY {res.order} LIMIT {PAGE} OFFSET :off",
        off=offset,
        **params,
    )
    return [_row_out(res, r) for r in rows], total


def get_row(conn, res, key):
    """Read one row of a resource by its key.
    404 when it does not exist.
    Returns the row in admin shape."""
    where, params = _key_where(res, key)
    row = one(conn, f"SELECT {_select_list(res)} FROM {res.table} WHERE {where}", **params)
    if row is None:
        raise not_found()
    return _row_out(res, row)


def _coerce(col, value):
    """Convert one submitted form value to the column's type.
    Empty strings become null; arrays accept a list or comma separated text.
    A value that cannot be converted is a 422 naming the column."""
    if value == "" or value is None:
        return None
    try:
        if col.type == "int":
            return int(value)
        if col.type == "numeric":
            return Decimal(str(value))
        if col.type == "bool":
            return value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes", "on")
        if col.type in ("int_array", "text_array"):
            items = value if isinstance(value, list) else [v.strip() for v in str(value).split(",") if v.strip()]
            return [int(v) for v in items] if col.type == "int_array" else [str(v) for v in items]
        if col.type == "point":
            if not isinstance(value, dict):
                raise ValueError
            lat, lng = value.get("lat"), value.get("lng")
            if lat in ("", None) and lng in ("", None):
                return None
            return (float(lat), float(lng))
        if col.type == "geojson":
            parsed = value if isinstance(value, dict) else json.loads(value)
            if parsed.get("type") not in ("Polygon", "MultiPolygon"):
                raise ValueError
            return json.dumps(parsed)
        return str(value)
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError):
        raise unprocessable("INVALID_VALUE", f"{col.label} has the wrong format.", {"column": col.name})


def _value_sql(col, param):
    """Return the SQL expression that stores one column value.
    Points and GeoJSON are converted to PostGIS geography.
    Plain columns bind directly."""
    if col.type == "point":
        return (
            f"CASE WHEN CAST(:{param}_lat AS DOUBLE PRECISION) IS NULL THEN NULL ELSE "
            f"ST_SetSRID(ST_MakePoint(CAST(:{param}_lng AS DOUBLE PRECISION), CAST(:{param}_lat AS DOUBLE PRECISION)), 4326)::geography END"
        )
    if col.type == "geojson":
        return f"CASE WHEN CAST(:{param} AS TEXT) IS NULL THEN NULL ELSE ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(CAST(:{param} AS TEXT)), 4326))::geography END"
    return f":{param}"


def _bind(col, param, value, params):
    """Put one coerced value into the parameter dict.
    Points bind as two parameters, lat and lng.
    Everything else binds as one."""
    if col.type == "point":
        params[f"{param}_lat"], params[f"{param}_lng"] = value if value else (None, None)
    else:
        params[param] = value


def _clean(res, values, creating):
    """Pick the writable columns out of a submitted form.
    Read-only and timestamp columns are ignored; required ones must be present.
    Returns a list of (column, coerced value)."""
    if not isinstance(values, dict):
        raise bad_request("INVALID_VALUES", "values must be an object.")
    out = []
    for c in res.columns:
        if c.readonly or c.type == "timestamp" or c.name not in values:
            continue
        if not creating and c.name in res.key:
            continue
        v = _coerce(c, values[c.name])
        if c.required and v is None:
            raise unprocessable("VALUE_REQUIRED", f"{c.label} is required.", {"column": c.name})
        out.append((c, v))
    if creating:
        given = {c.name for c, _ in out}
        missing = [c.label for c in res.columns if c.required and not c.readonly and c.name not in given]
        if missing:
            raise unprocessable("VALUE_REQUIRED", f"Required: {', '.join(missing)}.")
    return out


def _db_error(exc):
    """Translate a database refusal into a readable 422.
    Constraint names and messages come straight from Postgres.
    The schema is the last line of defence and its word is final."""
    message = str(getattr(exc, "orig", exc)).split("\n")[0]
    code = "CONSTRAINT_VIOLATION" if isinstance(exc, IntegrityError) else "DATABASE_REFUSED"
    return ApiError(422, code, message)


def create_row(conn, res, values):
    """Insert a new row into a resource.
    Only registry columns are written.
    Returns the key of the new row."""
    if not res.creatable:
        raise ApiError(405, "NOT_ALLOWED", "Rows cannot be created here.")
    cols = _clean(res, values, creating=True)
    params = {}
    for i, (c, v) in enumerate(cols):
        _bind(c, f"v{i}", v, params)
    names = ", ".join(f'"{c.name}"' for c, _ in cols)
    exprs = ", ".join(_value_sql(c, f"v{i}") for i, (c, _) in enumerate(cols))
    try:
        with conn.begin_nested():
            run(conn, f"INSERT INTO {res.table} ({names}) VALUES ({exprs})", **params)
    except DBAPIError as exc:
        raise _db_error(exc)
    return {k: values.get(k) for k in res.key}


def update_row(conn, res, key, values):
    """Update one row of a resource by its key.
    Key columns and read-only columns are never changed.
    404 when nothing matched."""
    if not res.editable:
        raise ApiError(405, "NOT_ALLOWED", "Rows cannot be edited here.")
    cols = _clean(res, values, creating=False)
    if not cols:
        return
    where, params = _key_where(res, key)
    for i, (c, v) in enumerate(cols):
        _bind(c, f"v{i}", v, params)
    sets = ", ".join(f'"{c.name}" = {_value_sql(c, f"v{i}")}' for i, (c, _) in enumerate(cols))
    try:
        with conn.begin_nested():
            count = run(conn, f"UPDATE {res.table} SET {sets} WHERE {where}", **params)
    except DBAPIError as exc:
        raise _db_error(exc)
    if not count:
        raise not_found()


def delete_row(conn, res, key):
    """Delete one row of a resource by its key.
    Rows other tables still point at are refused by the database.
    404 when nothing matched."""
    if not res.deletable:
        raise ApiError(405, "NOT_ALLOWED", "Rows cannot be deleted here.")
    where, params = _key_where(res, key)
    try:
        with conn.begin_nested():
            count = run(conn, f"DELETE FROM {res.table} WHERE {where}", **params)
    except DBAPIError as exc:
        raise _db_error(exc)
    if not count:
        raise not_found()
