"""High-value backend contracts for bootstrap, restore, and data integrity.

These tests intentionally exercise complete request paths.  A successful HTTP
response is not enough: destructive operations must reconstruct the original
model and preserve the security boundary around it.
"""

import csv
import importlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.helpers import CSRF, ROOT, create_prompt, login, seed_password, set_csrf, unlock


def _prompt_payload(
    appmod,
    *,
    prompt_id=1,
    version_id=11,
    name="Imported",
    content="imported body",
):
    ts = appmod.now_ts()
    return {
        "id": prompt_id,
        "name": name,
        "source": "contract-test",
        "notes": "restore fixture",
        "color": "#123456",
        "tags": ["one", "two"],
        "image_data": None,
        "pinned": False,
        "favorite": True,
        "archived_at": None,
        "last_used_at": None,
        "copy_count": 2,
        "require_password": False,
        "created_at": ts,
        "updated_at": ts,
        "current_version_id": version_id,
        "versions": [
            {
                "id": version_id,
                "prompt_id": prompt_id,
                "version": "1.0.0",
                "content": content,
                "created_at": ts,
                "parent_version_id": None,
            }
        ],
    }


def _envelope(appmod, prompts, *, settings=None, app_name="prompt-manage", schema=None):
    payload = {
        "app": app_name,
        "schema_version": appmod.SCHEMA_VERSION if schema is None else schema,
        "exported_at": appmod.now_ts(),
        "prompts": prompts,
    }
    if settings is not None:
        payload["settings"] = settings
    return payload


def _import_data(payload, *, filename="restore.json", **extra):
    data = {
        "settings_action": "import",
        "_csrf_token": CSRF,
        "import_file": (
            io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            filename,
        ),
    }
    data.update(extra)
    return data


def _names(appmod):
    conn = appmod.get_db()
    try:
        return [row["name"] for row in conn.execute("SELECT name FROM prompts ORDER BY id")]
    finally:
        conn.close()


def _setting_snapshot(appmod):
    conn = appmod.get_db()
    try:
        keys = (
            "language",
            "version_cleanup_threshold",
            "auth_mode",
            "auth_password_hash",
            "auth_revision",
        )
        return {key: appmod.get_setting(conn, key) for key in keys}
    finally:
        conn.close()


def test_failed_auth_action_cannot_apply_general_settings_or_import(client, appmod):
    create_prompt(appmod, "KeepMe", "must survive")
    login(client, appmod, "current-password", mode="global")
    set_csrf(client)
    before = _setting_snapshot(appmod)
    incoming = _envelope(appmod, [_prompt_payload(appmod, name="MustNotImport")])

    data = {
        "settings_action": "auth",
        "_csrf_token": CSRF,
        "auth_mode": "global",
        "current_password": "wrong-password",
        "new_password": "replacement-password",
        "confirm_password": "replacement-password",
        # Fields from the other two commands must be ignored.
        "language": "en",
        "version_cleanup_threshold": "1",
        "import_file": (
            io.BytesIO(json.dumps(incoming).encode("utf-8")),
            "must-not-import.json",
        ),
    }
    client.post("/settings", data=data, content_type="multipart/form-data")

    after = _setting_snapshot(appmod)
    assert _names(appmod) == ["KeepMe"]
    assert after == before
    assert appmod.verify_password("current-password", after["auth_password_hash"])
    assert not Path(appmod.db_path()).with_name("backups").exists()


def test_unknown_settings_action_is_a_noop(client, appmod):
    create_prompt(appmod, "KeepMe", "must survive")
    set_csrf(client)
    before = _setting_snapshot(appmod)
    incoming = _envelope(appmod, [_prompt_payload(appmod, name="MustNotImport")])
    data = _import_data(incoming)
    data.update(
        settings_action="general+auth+import",
        language="en",
        version_cleanup_threshold="1",
        auth_mode="global",
        new_password="replacement-password",
        confirm_password="replacement-password",
    )

    client.post("/settings", data=data, content_type="multipart/form-data")

    assert _names(appmod) == ["KeepMe"]
    assert _setting_snapshot(appmod) == before


def test_legacy_settings_form_prioritizes_auth_over_import(client, appmod):
    """A stale combined form must never turn a bad password into an import."""
    create_prompt(appmod, "KeepMe", "must survive")
    login(client, appmod, "current-password", mode="global")
    set_csrf(client)
    incoming = _envelope(appmod, [_prompt_payload(appmod, name="MustNotImport")])
    response = client.post(
        "/settings",
        data={
            "_csrf_token": CSRF,
            # No settings_action: this is the pre-split form shape.
            "auth_mode": "global",
            "current_password": "wrong-password",
            "new_password": "replacement-password",
            "confirm_password": "replacement-password",
            "language": "en",
            "version_cleanup_threshold": "1",
            "import_file": (
                io.BytesIO(json.dumps(incoming).encode("utf-8")),
                "must-not-import.json",
            ),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert _names(appmod) == ["KeepMe"]
    assert "MustNotImport" not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "bad_case",
    [
        "foreign_app",
        "future_schema",
        "negative_prompt_id",
        "negative_version_id",
        "huge_prompt_id",
        "exhausting_prompt_id",
        "future_timestamp",
        "duplicate_version_id",
        "cross_prompt_current",
        "string_prompt_id",
        "string_boolean",
        "invalid_auth_hash",
    ],
)
def test_invalid_import_is_rejected_before_backup_or_write(client, appmod, bad_case):
    create_prompt(appmod, "KeepMe", "must survive")
    first = _prompt_payload(appmod, prompt_id=1, version_id=11, name="One")
    payload = _envelope(appmod, [first])

    if bad_case == "foreign_app":
        payload["app"] = "another-product"
    elif bad_case == "future_schema":
        payload["schema_version"] = appmod.SCHEMA_VERSION + 1
    elif bad_case == "negative_prompt_id":
        first["id"] = -1
        first["versions"][0]["prompt_id"] = -1
    elif bad_case == "negative_version_id":
        first["current_version_id"] = -11
        first["versions"][0]["id"] = -11
    elif bad_case == "huge_prompt_id":
        first["id"] = 9223372036854775808
        first["versions"][0]["prompt_id"] = 9223372036854775808
    elif bad_case == "exhausting_prompt_id":
        first["id"] = appmod._MAX_IMPORT_ID + 1
        first["versions"][0]["prompt_id"] = appmod._MAX_IMPORT_ID + 1
    elif bad_case == "future_timestamp":
        first["versions"][0]["created_at"] = "2099-01-01T00:00:00"
    elif bad_case == "duplicate_version_id":
        second = _prompt_payload(appmod, prompt_id=2, version_id=11, name="Two")
        payload["prompts"].append(second)
    elif bad_case == "cross_prompt_current":
        second = _prompt_payload(appmod, prompt_id=2, version_id=22, name="Two")
        first["current_version_id"] = 22
        payload["prompts"].append(second)
    elif bad_case == "string_prompt_id":
        first["id"] = "1"
    elif bad_case == "string_boolean":
        first["pinned"] = "false"
    elif bad_case == "invalid_auth_hash":
        payload["settings"] = {
            "auth_mode": "global",
            "auth_password_hash": "not-a-real-password-hash",
        }

    set_csrf(client)
    response = client.post(
        "/settings",
        data=_import_data(payload),
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert _names(appmod) == ["KeepMe"]
    assert "MustNotImport" not in response.get_data(as_text=True)
    assert not Path(appmod.db_path()).with_name("backups").exists()


def test_import_id_headroom_keeps_autoincrement_usable(client, appmod):
    prompt = _prompt_payload(
        appmod,
        prompt_id=appmod._MAX_IMPORT_ID,
        version_id=appmod._MAX_IMPORT_ID - 1,
        name="HighButSafe",
    )
    set_csrf(client)
    response = client.post(
        "/settings",
        data=_import_data(_envelope(appmod, [prompt])),
        content_type="multipart/form-data",
    )
    assert response.status_code in (302, 303)
    created_id = create_prompt(appmod, "AfterHighImport", "body")
    assert created_id == appmod._MAX_IMPORT_ID + 1


def test_preimport_backup_contains_recoverable_settings(client, appmod):
    create_prompt(appmod, "BeforeImport", "original body")
    seed_password(appmod, "backup-password", mode="global")
    conn = appmod.get_db()
    appmod.set_setting(conn, "language", "en")
    appmod.set_setting(conn, "version_cleanup_threshold", "321")
    conn.commit()
    conn.close()

    # Authenticate without reseeding the settings changed above.
    set_csrf(client)
    client.post(
        "/login",
        data={"password": "backup-password", "_csrf_token": CSRF},
    )
    set_csrf(client)
    incoming = _envelope(
        appmod,
        [_prompt_payload(appmod, name="AfterImport")],
        settings={"language": "en", "version_cleanup_threshold": "321"},
    )
    client.post(
        "/settings",
        data=_import_data(incoming),
        content_type="multipart/form-data",
    )

    backups = sorted(Path(appmod.db_path()).with_name("backups").glob("pre-import-*.json"))
    assert len(backups) == 1
    backup = json.loads(backups[0].read_text(encoding="utf-8"))
    saved = backup["settings"]
    assert saved["language"] == "en"
    assert saved["version_cleanup_threshold"] == "321"
    assert saved["auth_mode"] == "global"
    assert appmod.verify_password("backup-password", saved["auth_password_hash"])
    assert saved["auth_revision"]
    assert saved["bootstrap_completed"] == "1"


def test_restore_auth_requires_explicit_opt_in(client, appmod):
    login(client, appmod, "current-password", mode="global")
    set_csrf(client)
    before = _setting_snapshot(appmod)
    restored_hash = appmod.hash_password("restored-password")
    restored_settings = {
        "language": "en",
        "version_cleanup_threshold": "444",
        "auth_mode": "global",
        "auth_password_hash": restored_hash,
        "auth_revision": before["auth_revision"],
        "bootstrap_completed": "1",
    }
    payload = _envelope(
        appmod,
        [_prompt_payload(appmod, name="DefaultRestore")],
        settings=restored_settings,
    )

    client.post(
        "/settings",
        data=_import_data(payload),
        content_type="multipart/form-data",
    )
    default_restore = _setting_snapshot(appmod)
    assert _names(appmod) == ["DefaultRestore"]
    assert default_restore["language"] == "en"
    assert default_restore["version_cleanup_threshold"] == "444"
    assert default_restore["auth_mode"] == before["auth_mode"]
    assert default_restore["auth_password_hash"] == before["auth_password_hash"]
    assert default_restore["auth_revision"] == before["auth_revision"]

    set_csrf(client)
    payload["prompts"][0]["name"] = "ExplicitRestore"
    client.post(
        "/settings",
        data=_import_data(
            payload,
            restore_auth="1",
            restore_current_password="current-password",
        ),
        content_type="multipart/form-data",
    )
    rejected_restore = _setting_snapshot(appmod)
    assert _names(appmod) == ["DefaultRestore"]
    assert rejected_restore["auth_password_hash"] == before["auth_password_hash"]

    set_csrf(client)
    client.post(
        "/settings",
        data=_import_data(
            payload,
            restore_auth="1",
            restore_current_password="current-password",
            restore_backup_password="restored-password",
        ),
        content_type="multipart/form-data",
    )
    explicit_restore = _setting_snapshot(appmod)
    assert _names(appmod) == ["ExplicitRestore"]
    assert explicit_restore["auth_mode"] == "global"
    assert appmod.verify_password("restored-password", explicit_restore["auth_password_hash"])
    assert not appmod.verify_password("current-password", explicit_restore["auth_password_hash"])
    assert int(explicit_restore["auth_revision"]) > int(before["auth_revision"])

    # Restoring credentials invalidates the session authenticated with the old
    # revision; the new password can establish a fresh owner session.
    stale = client.get("/settings", follow_redirects=False)
    assert stale.status_code in (302, 303)
    assert "/login" in stale.headers["Location"]
    set_csrf(client)
    client.post(
        "/login",
        data={"password": "restored-password", "_csrf_token": CSRF},
    )
    assert client.get("/settings").status_code == 200


def test_import_locks_database_before_creating_backup(client, appmod, monkeypatch):
    create_prompt(appmod, "Before", "original body")
    set_csrf(client)
    entered_backup = threading.Event()
    release_backup = threading.Event()
    original_backup = appmod._backup_current_data

    def blocked_backup(conn):
        entered_backup.set()
        assert release_backup.wait(timeout=5)
        return original_backup(conn)

    monkeypatch.setattr(appmod, "_backup_current_data", blocked_backup)
    incoming = _envelope(appmod, [_prompt_payload(appmod, name="Imported")])

    def do_import():
        return client.post(
            "/settings",
            data=_import_data(incoming),
            content_type="multipart/form-data",
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(do_import)
        assert entered_backup.wait(timeout=5)
        competing = sqlite3.connect(appmod.db_path(), timeout=0.1)
        competing.execute("PRAGMA busy_timeout=100")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute(
                    "INSERT INTO prompts(name, created_at, updated_at) VALUES(?,?,?)",
                    ("Between", appmod.now_ts(), appmod.now_ts()),
                )
                competing.commit()
        finally:
            competing.rollback()
            competing.close()
            release_backup.set()
        response = future.result(timeout=10)

    assert response.status_code in (302, 303)
    assert _names(appmod) == ["Imported"]


@pytest.mark.parametrize("export_format", ["json", "csv"])
def test_export_import_is_a_real_roundtrip_after_source_deletion(
    client, appmod, export_format
):
    dangerous = [
        "=HYPERLINK(\"https://invalid.example\")",
        "+SUM(1,1)",
        "-10+20",
        "@SUM(1,1)",
        "''literal leading apostrophes",
    ]
    prompt_ids = []
    for index, name in enumerate(dangerous):
        prompt_ids.append(
            create_prompt(
                appmod,
                name,
                f"body {index}\nwith a second line",
                tags=[f"tag-{index}"],
                source=dangerous[(index + 1) % len(dangerous)],
                notes=dangerous[(index + 2) % len(dangerous)],
                favorite=index % 2,
                archived=index == 3,
            )
        )

    # Make the first prompt exercise multiple versions, parent linkage, and a
    # current pointer that is not selected by timestamp ordering.
    conn = appmod.get_db()
    first_current = conn.execute(
        "SELECT current_version_id FROM prompts WHERE id=?", (prompt_ids[0],)
    ).fetchone()["current_version_id"]
    cur = conn.execute(
        "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) "
        "VALUES(?,?,?,?,?)",
        (prompt_ids[0], "1.0.1", "selected older version", "2024-01-01T00:00:00", first_current),
    )
    conn.execute(
        "UPDATE prompts SET current_version_id=?, copy_count=7, last_used_at=? WHERE id=?",
        (cur.lastrowid, "2025-01-01T00:00:00", prompt_ids[0]),
    )
    conn.commit()
    before = appmod.collect_export_payload(conn)["prompts"]
    conn.close()

    exported = client.get(f"/export?format={export_format}")
    assert exported.status_code == 200
    raw = exported.get_data()
    if export_format == "csv":
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        assert len(rows) == len(dangerous)
        for row in rows:
            for field in ("name", "source", "notes"):
                value = (row[field] or "").lstrip(" \t\r\n")
                assert not value.startswith(("=", "+", "-", "@"))

    conn = appmod.get_db()
    conn.execute("DELETE FROM prompts")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
    conn.close()

    set_csrf(client)
    response = client.post(
        "/settings",
        data={
            "settings_action": "import",
            "_csrf_token": CSRF,
            "import_file": (io.BytesIO(raw), f"roundtrip.{export_format}"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code in (302, 303)

    conn = appmod.get_db()
    after = appmod.collect_export_payload(conn)["prompts"]
    conn.close()
    assert after == before


def test_new_version_and_rollback_point_to_the_inserted_row(client, appmod):
    prompt_id = create_prompt(appmod, "FutureDirtyRow", "future current")
    conn = appmod.get_db()
    old_id = conn.execute(
        "SELECT current_version_id FROM prompts WHERE id=?", (prompt_id,)
    ).fetchone()["current_version_id"]
    conn.execute(
        "UPDATE versions SET created_at='2099-01-01T00:00:00' WHERE id=?",
        (old_id,),
    )
    conn.commit()
    conn.close()

    set_csrf(client)
    client.post(
        f"/prompt/{prompt_id}",
        data={
            "_csrf_token": CSRF,
            "name": "FutureDirtyRow",
            "content": "new content",
            "do_save_version": "1",
            "bump_kind": "patch",
        },
    )
    conn = appmod.get_db()
    saved = conn.execute(
        "SELECT p.current_version_id, v.content "
        "FROM prompts p JOIN versions v "
        "ON v.id=p.current_version_id AND v.prompt_id=p.id WHERE p.id=?",
        (prompt_id,),
    ).fetchone()
    newest_id = conn.execute(
        "SELECT MAX(id) FROM versions WHERE prompt_id=?", (prompt_id,)
    ).fetchone()[0]
    conn.close()
    assert saved["current_version_id"] == newest_id
    assert saved["content"] == "new content"

    set_csrf(client)
    client.post(
        f"/prompt/{prompt_id}/rollback/{old_id}",
        data={"_csrf_token": CSRF, "bump_kind": "patch"},
    )
    conn = appmod.get_db()
    rolled_back = conn.execute(
        "SELECT p.current_version_id, v.content "
        "FROM prompts p JOIN versions v "
        "ON v.id=p.current_version_id AND v.prompt_id=p.id WHERE p.id=?",
        (prompt_id,),
    ).fetchone()
    newest_id = conn.execute(
        "SELECT MAX(id) FROM versions WHERE prompt_id=?", (prompt_id,)
    ).fetchone()[0]
    conn.close()
    assert rolled_back["current_version_id"] == newest_id
    assert rolled_back["content"] == "future current"


def test_relocking_prompt_revokes_every_old_unlock(appmod):
    owner = appmod.app.test_client()
    visitor = appmod.app.test_client()
    prompt_id = create_prompt(
        appmod, "Protected", "private body", require_password=1
    )
    unlock(visitor, prompt_id, appmod)
    assert visitor.get(f"/prompt/{prompt_id}").status_code == 200

    login(owner, appmod, "owner-password", mode="per")
    for protected in (False, True):
        set_csrf(owner)
        data = {
            "_csrf_token": CSRF,
            "name": "Protected",
            "content": "private body",
            "bump_kind": "patch",
        }
        if protected:
            data["require_password"] = "1"
        owner.post(f"/prompt/{prompt_id}", data=data)

    blocked = visitor.get(f"/prompt/{prompt_id}", follow_redirects=False)
    assert blocked.status_code in (302, 303)
    assert "/unlock" in blocked.headers["Location"]


def test_password_change_serializes_with_prompt_unlock(appmod, monkeypatch):
    owner = appmod.app.test_client()
    visitor = appmod.app.test_client()
    prompt_id = create_prompt(
        appmod, "Protected", "private body", require_password=1
    )
    login(owner, appmod, "owner-password", mode="per")
    set_csrf(owner)
    set_csrf(visitor)

    entered_verification = threading.Event()
    release_verification = threading.Event()
    original_verify = appmod.verify_password
    pause_lock = threading.Lock()
    paused = False

    def paused_verify(raw, stored):
        nonlocal paused
        should_pause = False
        with pause_lock:
            if raw == "owner-password" and not paused:
                paused = True
                should_pause = True
        if should_pause:
            entered_verification.set()
            assert release_verification.wait(timeout=5)
        return original_verify(raw, stored)

    monkeypatch.setattr(appmod, "verify_password", paused_verify)

    with ThreadPoolExecutor(max_workers=2) as pool:
        unlock_future = pool.submit(
            visitor.post,
            f"/prompt/{prompt_id}/unlock",
            data={"password": "owner-password", "_csrf_token": CSRF},
        )
        assert entered_verification.wait(timeout=5)
        settings_future = pool.submit(
            owner.post,
            "/settings",
            data={
                "settings_action": "auth",
                "auth_mode": "per",
                "current_password": "owner-password",
                "new_password": "replacement-password",
                "confirm_password": "replacement-password",
                "_csrf_token": CSRF,
            },
        )
        release_verification.set()
        unlock_response = unlock_future.result(timeout=10)
        settings_response = settings_future.result(timeout=10)

    assert unlock_response.status_code in (302, 303)
    assert settings_response.status_code in (302, 303)
    blocked = visitor.get(f"/prompt/{prompt_id}", follow_redirects=False)
    assert blocked.status_code in (302, 303)
    assert "/unlock" in blocked.headers["Location"]


def test_cross_prompt_current_pointer_never_leaks_content(client, appmod):
    public_id = create_prompt(appmod, "Public", "public body")
    secret_id = create_prompt(
        appmod, "Secret", "CROSS_PROMPT_SECRET", require_password=1
    )
    seed_password(appmod, "owner-password", mode="per")
    conn = appmod.get_db()
    secret_version = conn.execute(
        "SELECT current_version_id FROM prompts WHERE id=?", (secret_id,)
    ).fetchone()["current_version_id"]
    conn.execute(
        "UPDATE prompts SET current_version_id=? WHERE id=?",
        (secret_version, public_id),
    )
    conn.commit()
    conn.close()

    for path in ("/", f"/prompt/{public_id}", f"/prompt/{public_id}/versions"):
        response = client.get(path)
        assert response.status_code == 200
        assert "CROSS_PROMPT_SECRET" not in response.get_data(as_text=True)
    assert client.get("/api/search?q=CROSS_PROMPT_SECRET").get_json() == []


def test_sensitive_responses_are_no_store_and_health_exposes_build(client, appmod):
    appmod.app.config.update(
        BUILD_SHA="contract-build-sha",
        ENABLE_HSTS=True,
        HSTS_MAX_AGE=12345,
    )
    prompt_id = create_prompt(appmod, "Sensitive", "private body")
    login(client, appmod, "owner-password", mode="per")

    health = client.get("/healthz", base_url="https://localhost")
    health_json = health.get_json()
    assert health_json["status"] == "ok"
    assert health_json["build_sha"] == "contract-build-sha"
    assert "max-age=12345" in health.headers["Strict-Transport-Security"]

    paths = (
        "/login",
        "/settings",
        f"/prompt/{prompt_id}",
        f"/prompt/{prompt_id}/versions",
        f"/prompt/{prompt_id}/diff",
        f"/api/prompt/{prompt_id}/content",
        "/export?format=json",
    )
    for path in paths:
        response = client.get(path, base_url="https://localhost")
        assert response.status_code == 200, path
        cache_control = response.headers.get("Cache-Control", "").lower()
        assert "private" in cache_control, path
        assert "no-store" in cache_control, path
        assert "max-age=12345" in response.headers["Strict-Transport-Security"], path


def test_concurrent_favorite_toggles_are_atomic(appmod):
    prompt_id = create_prompt(appmod, "Toggle", "body")
    workers = 8
    barrier = threading.Barrier(workers)
    clients = [appmod.app.test_client() for _ in range(workers)]
    for thread_client in clients:
        set_csrf(thread_client)

    def toggle(thread_client):
        barrier.wait(timeout=5)
        return thread_client.post(
            f"/prompt/{prompt_id}/favorite",
            data={"_csrf_token": CSRF},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        responses = list(pool.map(toggle, clients))

    assert all(response.status_code == 200 for response in responses)
    enabled = [response.get_json()["enabled"] for response in responses]
    assert enabled.count(True) == workers // 2
    assert enabled.count(False) == workers // 2
    conn = appmod.get_db()
    favorite = conn.execute(
        "SELECT favorite FROM prompts WHERE id=?", (prompt_id,)
    ).fetchone()["favorite"]
    conn.close()
    assert favorite == 0


def test_concurrent_rollbacks_allocate_unique_versions(appmod):
    prompt_id = create_prompt(appmod, "Rollback", "target body")
    conn = appmod.get_db()
    target_id = conn.execute(
        "SELECT current_version_id FROM prompts WHERE id=?", (prompt_id,)
    ).fetchone()["current_version_id"]
    cur = conn.execute(
        "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) "
        "VALUES(?,?,?,?,?)",
        (prompt_id, "1.0.1", "current body", appmod.now_ts(), target_id),
    )
    conn.execute(
        "UPDATE prompts SET current_version_id=? WHERE id=?",
        (cur.lastrowid, prompt_id),
    )
    conn.commit()
    conn.close()

    workers = 6
    barrier = threading.Barrier(workers)
    clients = [appmod.app.test_client() for _ in range(workers)]
    for thread_client in clients:
        set_csrf(thread_client)

    def rollback(thread_client):
        barrier.wait(timeout=5)
        return thread_client.post(
            f"/prompt/{prompt_id}/rollback/{target_id}",
            data={"_csrf_token": CSRF, "bump_kind": "patch"},
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        responses = list(pool.map(rollback, clients))
    assert all(response.status_code in (302, 303) for response in responses)

    conn = appmod.get_db()
    versions = conn.execute(
        "SELECT id, version, content FROM versions WHERE prompt_id=? ORDER BY id",
        (prompt_id,),
    ).fetchall()
    current_id = conn.execute(
        "SELECT current_version_id FROM prompts WHERE id=?", (prompt_id,)
    ).fetchone()["current_version_id"]
    conn.close()
    assert len(versions) == workers + 2
    assert len({row["version"] for row in versions}) == len(versions)
    assert current_id == versions[-1]["id"]
    assert versions[-1]["content"] == "target body"


def test_fresh_production_database_requires_bootstrap_token(tmp_path, monkeypatch):
    db = tmp_path / "fresh-production.sqlite3"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    bootstrap_token = "bootstrap-token-0123456789abcdef0123456789abcdef"
    monkeypatch.setenv("BOOTSTRAP_TOKEN", bootstrap_token)

    import app as app_module

    app_module = importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    production_client = app_module.app.test_client()

    for path in ("/", "/settings", "/export", "/api/search?q=secret"):
        response = production_client.get(path, follow_redirects=False)
        assert response.status_code in (302, 303), path
        assert "/setup" in response.headers["Location"], path
    assert production_client.get("/healthz").status_code == 200
    assert production_client.get("/logo.png").status_code == 200
    assert production_client.get("/favicon.ico").status_code == 200
    assert production_client.get("/setup").status_code == 200

    set_csrf(production_client)
    production_client.post(
        "/setup",
        data={
            "_csrf_token": CSRF,
            "bootstrap_token": "wrong-token",
            "auth_mode": "global",
            "new_password": "initial-owner-password",
            "confirm_password": "initial-owner-password",
        },
    )
    conn = app_module.get_db()
    assert app_module.get_setting(conn, "bootstrap_completed") == "0"
    assert not app_module.get_setting(conn, "auth_password_hash")
    conn.close()

    set_csrf(production_client)
    response = production_client.post(
        "/setup",
        data={
            "_csrf_token": CSRF,
            "bootstrap_token": bootstrap_token,
            "auth_mode": "global",
            "new_password": "initial-owner-password",
            "confirm_password": "initial-owner-password",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    conn = app_module.get_db()
    assert app_module.get_setting(conn, "bootstrap_completed") == "1"
    assert app_module.get_setting(conn, "auth_mode") == "global"
    stored = app_module.get_setting(conn, "auth_password_hash")
    conn.close()
    assert app_module.verify_password("initial-owner-password", stored)
    monkeypatch.setattr(
        app_module,
        "get_unlocked_prompt_ids",
        lambda: pytest.fail("authenticated templates must not query prompt unlocks"),
    )
    assert production_client.get("/settings").status_code == 200
    assert production_client.get("/setup", follow_redirects=False).status_code in (302, 303, 404)


def test_fresh_production_without_bootstrap_token_stays_closed(tmp_path, monkeypatch):
    db = tmp_path / "fresh-without-token.sqlite3"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("SECRET_KEY", "y" * 40)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.delenv("BOOTSTRAP_TOKEN", raising=False)

    import app as app_module

    app_module = importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    production_client = app_module.app.test_client()
    page = production_client.get("/setup")
    assert page.status_code == 503
    assert "<form" not in page.get_data(as_text=True).lower()

    set_csrf(production_client)
    response = production_client.post(
        "/setup",
        data={
            "_csrf_token": CSRF,
            "auth_mode": "global",
            "new_password": "would-be-owner-password",
            "confirm_password": "would-be-owner-password",
        },
    )
    assert response.status_code == 503
    conn = app_module.get_db()
    assert app_module.get_setting(conn, "bootstrap_completed") == "0"
    assert not app_module.get_setting(conn, "auth_password_hash")
    conn.close()


def test_partial_production_schema_stays_uninitialized(tmp_path, monkeypatch):
    db = tmp_path / "partial-production.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('language', 'zh')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("SECRET_KEY", "z" * 40)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv(
        "BOOTSTRAP_TOKEN", "bootstrap-token-0123456789abcdef0123456789abcdef"
    )

    import app as app_module

    app_module = importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    conn = app_module.get_db()
    try:
        assert app_module.get_setting(conn, "bootstrap_completed") == "0"
    finally:
        conn.close()
    response = app_module.app.test_client().get("/", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert "/setup" in response.headers["Location"]


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("SESSION_COOKIE_SECURE", "tru", "must be a boolean value"),
        (
            "BOOTSTRAP_TOKEN",
            "replace-me-with-another-random-token",
            "too short or uses a known placeholder",
        ),
    ],
)
def test_production_rejects_unsafe_environment_values(tmp_path, name, value, expected):
    env = os.environ.copy()
    env.update({
        "APP_ENV": "production",
        "DB_PATH": str(tmp_path / f"bad-{name}.sqlite3"),
        "SECRET_KEY": "production-secret-0123456789abcdef0123456789abcdef",
        "SESSION_COOKIE_SECURE": "false",
        "BOOTSTRAP_TOKEN": "",
    })
    env[name] = value
    result = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert expected in result.stderr


def test_concurrent_fresh_startups_serialize_migrations(tmp_path):
    db = tmp_path / "concurrent-startup.sqlite3"
    env = os.environ.copy()
    env.update({
        "APP_ENV": "production",
        "DB_PATH": str(db),
        "SECRET_KEY": "production-secret-0123456789abcdef0123456789abcdef",
        "SESSION_COOKIE_SECURE": "false",
        "BOOTSTRAP_TOKEN": "",
    })
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", "import app"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    results = [process.communicate(timeout=20) for process in processes]
    failures = [
        (process.returncode, stderr)
        for process, (_stdout, stderr) in zip(processes, results)
        if process.returncode != 0
    ]
    assert failures == []

    conn = sqlite3.connect(db)
    try:
        applied = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()
    assert applied == 11
