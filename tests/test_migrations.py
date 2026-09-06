"""Migration 12 upgrades an old database without exposing anything new."""

import json
import sqlite3

import pytest

from .helpers import get_setting

LEGACY_SCHEMA = """
CREATE TABLE prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, source TEXT, notes TEXT,
    color TEXT, tags TEXT, image_data TEXT, pinned INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT, current_version_id INTEGER,
    require_password INTEGER DEFAULT 0
);
CREATE TABLE versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, prompt_id INTEGER NOT NULL, version TEXT NOT NULL,
    content TEXT NOT NULL, created_at TEXT, parent_version_id INTEGER,
    FOREIGN KEY(prompt_id) REFERENCES prompts(id)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


@pytest.fixture
def legacy_db(appmod):
    """Rewind the database to the pre-12 schema, with legacy rows in it."""
    path = appmod.app.config["DB_PATH"]
    conn = sqlite3.connect(path)
    conn.executescript(
        "DROP TABLE IF EXISTS prompts; DROP TABLE IF EXISTS versions; DELETE FROM settings;"
    )
    conn.executescript(LEGACY_SCHEMA)
    conn.executescript(
        "ALTER TABLE prompts ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0;"
        "ALTER TABLE prompts ADD COLUMN archived_at TEXT;"
        "ALTER TABLE prompts ADD COLUMN last_used_at TEXT;"
        "ALTER TABLE prompts ADD COLUMN copy_count INTEGER NOT NULL DEFAULT 0;"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS prompt_unlocks (session_id TEXT, prompt_id INTEGER, "
        "unlocked_at TEXT, auth_revision TEXT)"
    )
    conn.execute("CREATE INDEX idx_prompts_require_password ON prompts(require_password)")
    conn.executemany(
        "INSERT INTO prompts(id, name, tags, image_data, pinned, favorite, require_password, "
        "copy_count, created_at, updated_at) VALUES(?,?,'[]',?,?,?,?,?,'2020-01-01','2020-01-01')",
        [
            (1, "Starred", None, 0, 1, 0, 7),
            (2, "Pinned", None, 1, 0, 0, 0),
            (3, "Protected", "data:image/png;base64,AAA", 0, 0, 1, 0),
        ],
    )
    conn.execute("INSERT INTO prompt_unlocks VALUES('sid', 3, '2020-01-01', '1')")
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES('auth_mode','per'),"
        "('auth_password_hash','x'),('auth_revision','1'),('language','zh'),"
        "('bootstrap_completed','1'),('version_cleanup_threshold','200')"
    )
    conn.execute("DELETE FROM schema_migrations WHERE version=12")
    conn.commit()
    conn.close()
    with appmod.app.app_context():
        appmod.run_migrations()
    return path


def test_favorites_become_pinned(appmod, legacy_db):
    conn = sqlite3.connect(legacy_db)
    conn.row_factory = sqlite3.Row
    rows = {r["name"]: r["pinned"] for r in conn.execute("SELECT name, pinned FROM prompts")}
    conn.close()
    assert rows == {"Starred": 1, "Pinned": 1, "Protected": 0}


def test_retired_columns_and_tables_are_gone(appmod, legacy_db):
    conn = sqlite3.connect(legacy_db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(prompts)")}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert not columns & {"favorite", "image_data", "require_password", "copy_count", "last_used_at"}
    assert "prompt_unlocks" not in tables


def test_per_prompt_mode_becomes_a_site_password(appmod, legacy_db):
    assert get_setting(appmod, "auth_mode") == "global"


def test_cover_images_are_archived_before_removal(appmod, legacy_db, tmp_path):
    saved = list(tmp_path.glob("removed-covers-*.json"))
    assert len(saved) == 1
    payload = json.loads(saved[0].read_text(encoding="utf-8"))
    assert payload == [{"id": 3, "name": "Protected", "image_data": "data:image/png;base64,AAA"}]


def test_the_app_still_serves_the_migrated_database(client, appmod, legacy_db):
    assert client.get("/").status_code == 302  # now behind the site password


def test_migration_is_idempotent(appmod, legacy_db):
    with appmod.app.app_context():
        appmod.run_migrations()
    conn = sqlite3.connect(legacy_db)
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    conn.close()
    assert applied == {version for version, _name, _fn in appmod.MIGRATIONS}


def test_a_newer_schema_refuses_to_downgrade(appmod):
    conn = sqlite3.connect(appmod.app.config["DB_PATH"])
    conn.execute(
        "INSERT INTO schema_migrations(version, name, applied_at) VALUES(999,'future','x')"
    )
    conn.commit()
    conn.close()
    with appmod.app.app_context(), pytest.raises(RuntimeError, match="newer than this application"):
        appmod.run_migrations()


def test_databases_recorded_under_historical_names_still_upgrade(appmod):
    """Regression: an early release used different migration names.

    Refusing to start on a name mismatch stranded a real production database —
    every step is idempotent, so re-run it and adopt the current name instead.
    """
    conn = sqlite3.connect(appmod.app.config["DB_PATH"])
    conn.executescript(
        "UPDATE schema_migrations SET name='base_tables' WHERE version=1;"
        "UPDATE schema_migrations SET name='compat_columns' WHERE version=2;"
        "DELETE FROM schema_migrations WHERE version > 5;"
    )
    conn.commit()
    conn.close()

    with appmod.app.app_context():
        appmod.run_migrations()

    conn = sqlite3.connect(appmod.app.config["DB_PATH"])
    recorded = dict(conn.execute("SELECT version, name FROM schema_migrations"))
    conn.close()
    assert recorded == {version: name for version, name, _fn in appmod.MIGRATIONS}


def test_indexes_on_retired_columns_do_not_block_the_upgrade(appmod):
    """Regression: SQLite refuses to drop a column an index still mentions."""
    conn = sqlite3.connect(appmod.app.config["DB_PATH"])
    conn.executescript(
        "ALTER TABLE prompts ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0;"
        "CREATE INDEX idx_prompts_favorite ON prompts(favorite);"
        "DELETE FROM schema_migrations WHERE version=12;"
    )
    conn.commit()
    conn.close()

    with appmod.app.app_context():
        appmod.run_migrations()

    conn = sqlite3.connect(appmod.app.config["DB_PATH"])
    columns = {row[1] for row in conn.execute("PRAGMA table_info(prompts)")}
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(prompts)")}
    conn.close()
    assert "favorite" not in columns
    assert "idx_prompts_favorite" not in indexes
