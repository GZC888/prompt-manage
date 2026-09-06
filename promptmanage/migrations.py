"""Schema migrations.

Each migration is idempotent and recorded in ``schema_migrations``. They run
once at startup under a single database-wide writer lock and fail fast, so a
half-applied schema can never serve traffic.

Migrations 1-11 build the historical schema. Migration 12 trims the features
this fork no longer ships (per-prompt passwords, cover images, a separate
"favorite" flag, copy counters) while preserving every piece of user data that
still has a home.
"""

import json
import logging
import os
import sqlite3

from flask import current_app

from .db import columns, connect, db_path, get_setting, set_setting
from .utils import now_ts

log = logging.getLogger("prompt_manage")


def _m_base(conn):
    # A brand-new production database must be claimed through /setup before any
    # application data is exposed. Existing/legacy databases are treated as
    # initialized so an upgrade cannot unexpectedly lock out their owner.
    existing = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('prompts', 'versions', 'settings')"
        ).fetchall()
    }
    had_app_schema = existing == {"prompts", "versions", "settings"}
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source TEXT,
            notes TEXT,
            color TEXT,
            tags TEXT,
            image_data TEXT,
            pinned INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            current_version_id INTEGER,
            require_password INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id INTEGER NOT NULL,
            version TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT,
            parent_version_id INTEGER,
            FOREIGN KEY(prompt_id) REFERENCES prompts(id)
        )
        """
    )
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    app_env = current_app.config["APP_ENV"]
    defaults = (
        ("version_cleanup_threshold", "200"),
        ("auth_mode", "off"),
        ("auth_password_hash", ""),
        ("auth_revision", "1"),
        ("language", "zh"),
        ("bootstrap_completed", "1" if app_env != "production" or had_app_schema else "0"),
    )
    for key, value in defaults:
        if get_setting(conn, key) is None:
            set_setting(conn, key, value)


def _m_prompt_security_cols(conn):
    cols = columns(conn, "prompts")
    if "require_password" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN require_password INTEGER DEFAULT 0")
    if "color" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN color TEXT")
    if "image_data" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN image_data TEXT")


def _m_prompt_feature_cols(conn):
    cols = columns(conn, "prompts")
    if "favorite" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
    if "archived_at" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN archived_at TEXT DEFAULT NULL")
    if "last_used_at" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN last_used_at TEXT DEFAULT NULL")
    if "copy_count" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN copy_count INTEGER NOT NULL DEFAULT 0")


def _m_login_attempts(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            route TEXT NOT NULL,
            success INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_route_time "
        "ON login_attempts(ip, route, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_success_time "
        "ON login_attempts(success, created_at)"
    )


def _m_indexes(conn):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prompts_pinned_updated ON prompts(pinned, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prompts_require_password ON prompts(require_password)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_prompt_created ON versions(prompt_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_prompt_version ON versions(prompt_id, version)")


def _m_security_hardening(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_unlocks (
            session_id TEXT NOT NULL,
            prompt_id INTEGER NOT NULL,
            unlocked_at TEXT NOT NULL,
            PRIMARY KEY(session_id, prompt_id),
            FOREIGN KEY(prompt_id) REFERENCES prompts(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prompt_unlocks_prompt ON prompt_unlocks(prompt_id)"
    )


def _m_versions_on_delete_cascade(conn):
    fks = conn.execute("PRAGMA foreign_key_list(versions)").fetchall()
    if any(row["table"] == "prompts" and (row["on_delete"] or "").upper() == "CASCADE" for row in fks):
        return
    conn.execute("DROP TABLE IF EXISTS versions_new")
    conn.execute(
        """
        CREATE TABLE versions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id INTEGER NOT NULL,
            version TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT,
            parent_version_id INTEGER,
            FOREIGN KEY(prompt_id) REFERENCES prompts(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "INSERT INTO versions_new(id, prompt_id, version, content, created_at, parent_version_id) "
        "SELECT id, prompt_id, version, content, created_at, parent_version_id FROM versions"
    )
    conn.execute("DROP TABLE versions")
    conn.execute("ALTER TABLE versions_new RENAME TO versions")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_prompt_created ON versions(prompt_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_prompt_version ON versions(prompt_id, version)")


def _m_auth_revision(conn):
    if get_setting(conn, "auth_revision") is None:
        set_setting(conn, "auth_revision", "1")


def _m_bootstrap_state(conn):
    # Fresh databases created by _m_base already carry the correct value, so a
    # missing key identifies an existing installation being upgraded. Those must
    # stay accessible.
    if get_setting(conn, "bootstrap_completed") is None:
        set_setting(conn, "bootstrap_completed", "1")


def _m_auth_sessions(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_sessions (
            session_id TEXT PRIMARY KEY,
            auth_revision TEXT NOT NULL,
            authenticated_at TEXT NOT NULL
        )
        """
    )


def _m_unlock_auth_revision(conn):
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_unlocks'"
    ).fetchone():
        return
    unlock_cols = {r["name"] for r in conn.execute("PRAGMA table_info(prompt_unlocks)").fetchall()}
    if "auth_revision" not in unlock_cols:
        conn.execute("ALTER TABLE prompt_unlocks ADD COLUMN auth_revision TEXT")
    conn.execute(
        "UPDATE prompt_unlocks SET auth_revision=("
        "SELECT value FROM settings WHERE key='auth_revision') WHERE auth_revision IS NULL"
    )


# SQLite gained ALTER TABLE ... DROP COLUMN in 3.35. On anything older the
# retired columns are simply left in place: no code reads them, so they are
# inert rather than a reason to fail an upgrade.
_CAN_DROP_COLUMN = sqlite3.sqlite_version_info >= (3, 35, 0)

# Prompt columns this fork no longer has any code path for.
_RETIRED_COLUMNS = frozenset(
    {"favorite", "image_data", "require_password", "copy_count", "last_used_at"}
)


def _archive_removed_covers(conn):
    """Write any stored cover images to a JSON file before the column goes away."""
    rows = conn.execute(
        "SELECT id, name, image_data FROM prompts WHERE image_data IS NOT NULL AND image_data != ''"
    ).fetchall()
    if not rows:
        return
    target = os.path.join(
        os.path.dirname(db_path()) or ".", f"removed-covers-{now_ts()[:19].replace(':', '')}.json"
    )
    try:
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(
                [{"id": r["id"], "name": r["name"], "image_data": r["image_data"]} for r in rows],
                handle,
                ensure_ascii=False,
            )
        log.warning("Cover images are no longer supported; %s saved to %s", len(rows), target)
    except OSError:
        log.exception("could not archive cover images before dropping the column")


def _m_simplify_schema(conn):
    """Retire per-prompt passwords, cover images, favorites and copy counters."""
    # Per-prompt mode disappears. Promoting it to the global password keeps every
    # previously protected prompt behind a password instead of exposing it.
    if (get_setting(conn, "auth_mode", "off") or "off") == "per":
        set_setting(conn, "auth_mode", "global")
        log.warning("auth_mode 'per' is no longer supported; promoted to 'global'")

    cols = columns(conn, "prompts")
    if "favorite" in cols:
        # "Favorite" and "pinned" always meant the same thing to users. Keep the
        # union so nothing a user starred silently disappears.
        conn.execute("UPDATE prompts SET pinned=1 WHERE favorite=1 AND (pinned IS NULL OR pinned=0)")
    if "image_data" in cols:
        _archive_removed_covers(conn)

    conn.execute("DROP TABLE IF EXISTS prompt_unlocks")
    conn.execute("DROP INDEX IF EXISTS idx_prompt_unlocks_prompt")
    # SQLite refuses to drop a column any index still mentions, and older
    # releases of this project indexed several of them under names this code
    # never used. Discover them instead of guessing.
    for index in conn.execute("PRAGMA index_list(prompts)").fetchall():
        name = index["name"]
        if name.startswith("sqlite_autoindex"):
            continue
        indexed = {row["name"] for row in conn.execute(f"PRAGMA index_info({name!r})").fetchall()}
        if indexed & _RETIRED_COLUMNS:
            conn.execute(f"DROP INDEX IF EXISTS {name!r}")
    if _CAN_DROP_COLUMN:
        for column in sorted(_RETIRED_COLUMNS & cols):
            conn.execute(f"ALTER TABLE prompts DROP COLUMN {column}")
    else:
        log.warning(
            "SQLite %s cannot drop columns; retired prompt columns are left unused",
            sqlite3.sqlite_version,
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prompts_pinned_updated ON prompts(pinned, updated_at)")


MIGRATIONS = [
    (1, "base_schema", _m_base),
    (2, "prompt_security_columns", _m_prompt_security_cols),
    (3, "prompt_feature_columns", _m_prompt_feature_cols),
    (4, "login_attempts", _m_login_attempts),
    (5, "indexes", _m_indexes),
    (6, "security_hardening", _m_security_hardening),
    (7, "versions_on_delete_cascade", _m_versions_on_delete_cascade),
    (8, "auth_revision", _m_auth_revision),
    (9, "bootstrap_state", _m_bootstrap_state),
    (10, "auth_sessions", _m_auth_sessions),
    (11, "unlock_auth_revision", _m_unlock_auth_revision),
    (12, "simplify_schema", _m_simplify_schema),
]


def run_migrations():
    """Apply pending migrations under one database-wide writer lock."""
    os.makedirs(os.path.dirname(db_path()) or ".", exist_ok=True)
    conn = connect(db_path(), autocommit=True)
    applied_now = []
    active = None
    try:
        # Serializing the decision *and* the execution matters: without the lock
        # two fresh workers can read the same pending set and one dies halfway
        # through startup with "database is locked".
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        known = {version: name for version, name, _fn in MIGRATIONS}
        done = set()
        for row in conn.execute("SELECT version, name FROM schema_migrations").fetchall():
            if row["version"] not in known:
                # Written by a newer build. Refusing to touch it is the only safe
                # move: this code has no idea what that schema contains.
                raise RuntimeError(
                    f"Database schema version {row['version']} is newer than this application"
                )
            if known[row["version"]] == row["name"]:
                done.add(row["version"])
                continue
            # Early releases recorded other names for the same steps. Refusing to
            # start would strand a real database, and skipping the step could
            # leave a column missing — so re-run it (all migrations are
            # idempotent) and adopt the current name.
            log.warning(
                "migration %s was recorded as %r; re-running it as %r",
                row["version"], row["name"], known[row["version"]],
            )

        for version, name, fn in MIGRATIONS:
            if version in done:
                continue
            active = (version, name)
            fn(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations(version, name, applied_at) "
                "VALUES(?,?,?)",
                (version, name, now_ts()),
            )
            applied_now.append((version, name))
        conn.execute("COMMIT")
        for version, name in applied_now:
            log.info("Applied migration %s (%s)", version, name)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if active:
            log.exception("Migration %s (%s) failed; aborting startup", active[0], active[1])
        else:
            log.exception("Could not acquire migration lock; aborting startup")
        raise
    finally:
        conn.close()
