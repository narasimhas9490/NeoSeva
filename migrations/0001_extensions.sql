CREATE SCHEMA IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis SCHEMA postgis;
DO $$ BEGIN
    EXECUTE 'ALTER DATABASE ' || quote_ident(current_database()) || ' SET search_path = postgis, public';
END $$;
