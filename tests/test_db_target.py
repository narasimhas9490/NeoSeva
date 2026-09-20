import pytest
from sqlalchemy import create_engine, text

from app.config import Config
from app.core.db import tx
from scripts.migrate import reset


def cfg_with(**kw):
    """Build a Config with both database URLs set to known values.
    Keyword arguments override any field."""
    cfg = Config()
    cfg.db_target, cfg.database_url, cfg.supabase_database_url = "local", "postgresql+psycopg://l/db", "postgresql+psycopg://u:p@x.supabase.com/postgres"
    for key, value in kw.items():
        setattr(cfg, key, value)
    return cfg


def test_db_target_picks_the_database_and_override_wins():
    """DB_TARGET chooses local or supabase, and an explicit target overrides it.
    An unset URL or an unknown target is a clear error, never a silent fallback."""
    assert cfg_with().url_for() == "postgresql+psycopg://l/db"
    assert "supabase" in cfg_with(db_target="supabase").url_for()
    assert "supabase" in cfg_with().url_for("supabase")
    assert cfg_with(db_target="supabase").url_for("local") == "postgresql+psycopg://l/db"
    with pytest.raises(ValueError, match="SUPABASE_DATABASE_URL"):
        cfg_with(supabase_database_url="").url_for("supabase")
    with pytest.raises(ValueError, match="DB_TARGET"):
        cfg_with(db_target="cloud").url_for()


def test_reset_refuses_a_supabase_host():
    """The schema wipe used by tests and --reset never runs against Supabase.
    The check is on the host, before any connection is made."""
    with pytest.raises(SystemExit):
        reset(create_engine("postgresql+psycopg://u:p@aws-0.pooler.supabase.com:5432/postgres"))


def test_every_table_has_row_level_security(app):
    """Migrating turns row level security on for all public tables.
    That keeps Supabase's public Data API from exposing them."""
    with app.app_context():
        with tx() as conn:
            open_tables = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND NOT rowsecurity")).all()
    assert open_tables == []
