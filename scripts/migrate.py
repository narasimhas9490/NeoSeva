import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text

from app.config import Config

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def reset(engine):
    """Drop everything in the public schema and recreate it empty.
    Used to start clean in development and before every test run.
    Needs a role allowed to recreate the PostGIS extension.
    Refuses outright on a Supabase database, which holds more than our tables."""
    if "supabase" in str(engine.url.host or ""):
        raise SystemExit("Refusing to reset a Supabase database.")
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS postgis CASCADE"))
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


def migrate(engine, verbose=True):
    """Apply every migration file not yet recorded, in name order.
    Each file runs in its own transaction and is recorded on success.
    Returns the list of files applied this time."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS public.schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        done = {r[0] for r in conn.execute(text("SELECT name FROM public.schema_migrations"))}
    applied = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in done:
            continue
        with engine.begin() as conn:
            conn.exec_driver_sql("SET LOCAL search_path = public, postgis")
            conn.exec_driver_sql(path.read_text(encoding="utf-8"))
            conn.execute(text("INSERT INTO public.schema_migrations (name) VALUES (:n)"), {"n": path.name})
        applied.append(path.name)
        if verbose:
            print(f"applied {path.name}")
    lock_down_tables(engine)
    return applied


def lock_down_tables(engine):
    """Turn on row level security for every table in public.
    Supabase serves public tables over its Data API to anyone holding the public key;
    with security on and no policy that door stays shut. The app connects as a table owner,
    which is not subject to it, so nothing changes for the app. Safe to repeat."""
    with engine.begin() as conn:
        names = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND NOT rowsecurity")).all()
        for (name,) in names:
            conn.execute(text(f'ALTER TABLE public."{name}" ENABLE ROW LEVEL SECURITY'))


def url_or_exit(cfg, target):
    """Return the database URL for a target, or stop with a one-line message.
    Used by the command line scripts so a missing setting is not a traceback."""
    try:
        return cfg.url_for(target)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}")


def main():
    """Command line entry point for migrations.
    --reset wipes the schema first; --test targets TEST_DATABASE_URL; --target picks local or supabase.
    Prints each file as it is applied."""
    parser = argparse.ArgumentParser(description="Apply NeoSeva database migrations.")
    parser.add_argument("--reset", action="store_true", help="drop and recreate the public schema first")
    parser.add_argument("--test", action="store_true", help="use TEST_DATABASE_URL")
    parser.add_argument("--target", choices=("local", "supabase"), help="database to use; default is DB_TARGET")
    args = parser.parse_args()
    cfg = Config()
    engine = create_engine(cfg.test_database_url if args.test else url_or_exit(cfg, args.target))
    print(f"database: {engine.url.host}")
    if args.reset:
        reset(engine)
        print("schema reset")
    applied = migrate(engine)
    print(f"{len(applied)} migration(s) applied")


if __name__ == "__main__":
    main()
