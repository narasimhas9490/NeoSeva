import copy
import logging
import threading
import time

from app.config import FIELDS, parse, render
from app.core.db import many, one, run, tx
from app.core.errors import ApiError, not_found, unprocessable

log = logging.getLogger("neoseva.runtime")
CHECK_EVERY_SECONDS = 2
UNSAFE_IN_PRODUCTION = {"AUTH_ENABLED": False, "SMS_FIXED_OTP": None}
_lock = threading.Lock()


class Runtime:
    def __init__(self, cfg):
        """Keep the .env configuration and the live one side by side.
        base never changes; cfg is what the app reads and gets overrides applied.
        Removing an override puts the .env value back."""
        self.base = copy.deepcopy(cfg)
        self.cfg = cfg
        self.stamp = None
        self.checked = 0.0

    def refresh(self, force=False):
        """Apply database overrides to the live configuration when they change.
        Checks at most every two seconds, and only reloads when something changed.
        A broken override is logged and skipped, never allowed to stop requests."""
        now = time.monotonic()
        if not force and now - self.checked < CHECK_EVERY_SECONDS:
            return
        self.checked = now
        try:
            with tx() as conn:
                stamp = one(conn, "SELECT count(*) AS n, max(updated_at) AS at FROM runtime_setting")
                if not force and stamp == self.stamp:
                    return
                rows = many(conn, "SELECT env_key, value FROM runtime_setting")
        except Exception:
            log.exception("could not read runtime settings")
            return
        with _lock:
            for name, f in FIELDS.items():
                if f.metadata["editable"] and not f.metadata["restart"]:
                    setattr(self.cfg, f.name, copy.deepcopy(getattr(self.base, f.name)))
            for row in rows:
                f = FIELDS.get(row["env_key"])
                if f is None or not f.metadata["editable"] or f.metadata["restart"]:
                    continue
                try:
                    setattr(self.cfg, f.name, parse(f.metadata["kind"], row["value"]))
                except ValueError:
                    log.error("ignoring bad runtime setting %s=%r", row["env_key"], row["value"])
            logging.getLogger().setLevel(str(self.cfg.log_level).upper())
            self.stamp = stamp

    def describe(self, conn):
        """List every setting with its .env value, override and effective value.
        Secrets are masked; only whether they are set is shown.
        Grouped in declaration order for the admin page."""
        overrides = {r["env_key"]: r for r in many(conn, "SELECT * FROM runtime_setting")}
        items = []
        for env, f in FIELDS.items():
            meta = f.metadata
            env_value = render(meta["kind"], getattr(self.base, f.name))
            override = overrides.get(env)
            effective = render(meta["kind"], getattr(self.cfg, f.name))

            def show(value):
                """Mask a secret value for display.
                Secrets show only whether they are set.
                Everything else is shown as it is."""
                if value is None:
                    return None
                return ("•••••• (set)" if value else "(empty)") if meta["secret"] else value

            items.append(
                {
                    "key": env,
                    "group": meta["group"],
                    "kind": meta["kind"],
                    "help": meta["help"],
                    "secret": meta["secret"],
                    "restartRequired": meta["restart"],
                    "editable": meta["editable"] and not meta["restart"],
                    "envValue": show(env_value),
                    "override": show(override["value"]) if override else None,
                    "overridden": override is not None,
                    "effective": show(effective),
                    "updatedAt": override["updated_at"].isoformat() if override else None,
                    "updatedBy": override["updated_by"] if override else None,
                }
            )
        return items

    def set(self, env_key, raw, admin):
        """Save an override for one setting and apply it at once.
        The value must parse as the setting's type; unsafe production switches are refused.
        Settings needing a restart can only be changed in .env."""
        f = FIELDS.get(env_key)
        if f is None:
            raise not_found("UNKNOWN_SETTING", f"No setting {env_key}.")
        if not f.metadata["editable"] or f.metadata["restart"]:
            raise ApiError(409, "SETTING_NOT_EDITABLE", f"{env_key} can only be changed in .env and needs a restart.")
        try:
            value = parse(f.metadata["kind"], raw)
        except ValueError:
            raise unprocessable("INVALID_SETTING_VALUE", f"{env_key} expects a {f.metadata['kind']} value.")
        if self.base.app_env == "production" and env_key in UNSAFE_IN_PRODUCTION:
            unsafe = UNSAFE_IN_PRODUCTION[env_key]
            if (unsafe is None and value) or (unsafe is not None and value == unsafe):
                raise ApiError(409, "UNSAFE_IN_PRODUCTION", f"{env_key} cannot be set that way in production.")
        with tx() as conn:
            run(
                conn,
                """INSERT INTO runtime_setting (env_key, value, updated_at, updated_by) VALUES (:k, :v, now(), :by)
                   ON CONFLICT (env_key) DO UPDATE SET value = :v, updated_at = now(), updated_by = :by""",
                k=env_key,
                v=render(f.metadata["kind"], value),
                by=admin,
            )
        self.refresh(force=True)

    def reset(self, env_key):
        """Remove the override for one setting so .env applies again.
        Takes effect immediately in this process, within seconds in others.
        An unknown key is 404."""
        if env_key not in FIELDS:
            raise not_found("UNKNOWN_SETTING", f"No setting {env_key}.")
        with tx() as conn:
            run(conn, "DELETE FROM runtime_setting WHERE env_key = :k", k=env_key)
        self.refresh(force=True)
