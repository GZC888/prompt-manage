"""SQLite access: connections, the request-scoped handle, and settings rows."""

import logging
import sqlite3

from flask import current_app, g, has_app_context

log = logging.getLogger("prompt_manage")

# PRAGMA cannot be parameterized, so table names used with PRAGMA must come
# from this whitelist rather than from anything caller-supplied.
_KNOWN_TABLES = {
    "prompts", "versions", "settings", "login_attempts",
    "schema_migrations", "auth_sessions",
}


def db_path():
    return current_app.config["DB_PATH"]


def connect(path, autocommit=False):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    if autocommit:
        conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    if path != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
    return conn


def get_db():
    """Return the connection for this request.

    Inside an app context the connection is cached on ``g`` and closed exactly
    once by the teardown handler. Route code must therefore never close it:
    context processors still query the database while a template renders, and a
    handler-side ``close()`` used to make those queries fail silently.

    Outside an app context (CLI, migrations, tests) a standalone connection is
    returned and the caller owns it.
    """
    if not has_app_context():
        return connect(current_app.config["DB_PATH"])
    conn = getattr(g, "_db", None)
    if conn is None:
        conn = g._db = connect(db_path())
    return conn


def close_db(_exc=None):
    conn = g.pop("_db", None)
    if conn is not None:
        conn.close()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def columns(conn, table):
    if table not in _KNOWN_TABLES:
        raise ValueError(f"Invalid table name: {table}")
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def is_locked_error(exc):
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()
