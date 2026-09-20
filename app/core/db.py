import json
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text

_engine = None


def init_engine(config, url=None):
    """Create the process-wide SQLAlchemy engine from configuration.
    Pool size, overflow, recycle and connect timeout all come from the env.
    Returns the engine so scripts and tests can reuse it."""
    global _engine
    url = url or config.url_for()
    connect_args = {"connect_timeout": config.db_connect_timeout}
    if "supabase" in url:
        # The pooler may run in transaction mode, which cannot hold prepared statements.
        connect_args["prepare_threshold"] = None
    _engine = create_engine(
        url,
        echo=config.sqlalchemy_echo,
        pool_size=config.db_pool_size,
        max_overflow=config.db_max_overflow,
        pool_recycle=config.db_pool_recycle,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    @event.listens_for(_engine, "connect")
    def set_search_path(dbapi_conn, _record):
        """Put PostGIS on the search path of every new connection.
        Sent as a statement, not a startup option, because poolers refuse startup options.
        Committed so the pool's rollback on checkout does not undo it."""
        with dbapi_conn.cursor() as cur:
            cur.execute("SET search_path = postgis, public")
        dbapi_conn.commit()

    return _engine


def engine():
    """Return the engine created by init_engine.
    Raises when the application was not initialised.
    Used by code that needs a raw connection outside a request."""
    if _engine is None:
        raise RuntimeError("database engine is not initialised")
    return _engine


@contextmanager
def tx():
    """Open a connection inside one database transaction.
    Commits when the block finishes and rolls back when it raises.
    Every write in the application goes through this."""
    with engine().begin() as conn:
        yield conn


def one(conn, sql, **params):
    """Run a query and return the first row as a dict.
    Returns None when the query produces no rows.
    Parameters are bound by name with :name placeholders."""
    row = conn.execute(text(sql), params).mappings().first()
    return dict(row) if row is not None else None


def many(conn, sql, **params):
    """Run a query and return every row as a list of dicts.
    An empty result is an empty list, never None.
    Parameters are bound by name with :name placeholders."""
    return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


def scalar(conn, sql, **params):
    """Run a query and return the first column of the first row.
    Returns None when there is no row.
    Used for counts, existence checks and RETURNING id."""
    return conn.execute(text(sql), params).scalar()


def run(conn, sql, **params):
    """Execute a statement and return the number of affected rows.
    Used for INSERT, UPDATE and DELETE without RETURNING.
    Parameters are bound by name with :name placeholders."""
    return conn.execute(text(sql), params).rowcount


def as_json(value):
    """Serialise a Python value for a JSONB parameter.
    Pair with CAST(:param AS jsonb) in the SQL.
    Returns None for None so nullable columns stay null."""
    return None if value is None else json.dumps(value, default=str)
