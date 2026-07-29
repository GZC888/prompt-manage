"""Regression tests for the second audit pass (content-loss, import safety,
image handling, search, toggles, rollback, 404 routing).

Each test pins a specific fix so the bug cannot silently return.
"""
import io
import json

import pytest

from tests.helpers import CSRF, create_prompt, seed_password, set_csrf

PNG = b"\x89PNG\r\n\x1a\n" + b"fakepngbody" * 4


def _settings_data(**extra):
    data = {
        "settings_action": "import",
        "version_cleanup_threshold": "200", "language": "zh", "auth_mode": "off",
        "_csrf_token": CSRF,
    }
    data.update(extra)
    return data


def _current_content(appmod, pid):
    conn = appmod.get_db()
    row = conn.execute(
        "SELECT v.content AS c FROM prompts p LEFT JOIN versions v ON v.id=p.current_version_id WHERE p.id=?",
        (pid,),
    ).fetchone()
    conn.close()
    return row["c"] if row else None


def _version_count(appmod, pid):
    conn = appmod.get_db()
    n = conn.execute("SELECT COUNT(*) AS c FROM versions WHERE prompt_id=?", (pid,)).fetchone()["c"]
    conn.close()
    return n


# --- Editing content without "save as new version" must NOT be lost ----------
def test_edit_without_new_version_updates_content_in_place(client, appmod):
    pid = create_prompt(appmod, "P", "original")
    set_csrf(client)
    # do_save_version intentionally omitted (checkbox unchecked).
    client.post(f"/prompt/{pid}", data={
        "name": "P", "content": "edited-no-new-version", "bump_kind": "patch", "_csrf_token": CSRF,
    })
    assert _current_content(appmod, pid) == "edited-no-new-version"  # not silently dropped
    assert _version_count(appmod, pid) == 1                          # and no extra version created


def test_edit_with_new_version_appends_version(client, appmod):
    pid = create_prompt(appmod, "P", "original")
    set_csrf(client)
    client.post(f"/prompt/{pid}", data={
        "name": "P", "content": "v2body", "bump_kind": "patch",
        "do_save_version": "1", "_csrf_token": CSRF,
    })
    assert _version_count(appmod, pid) == 2
    assert _current_content(appmod, pid) == "v2body"


def test_prune_versions_preserves_current_version_pointer(client, appmod):
    pid = create_prompt(appmod, "P", "current-old")
    conn = appmod.get_db()
    current_id = conn.execute("SELECT current_version_id FROM prompts WHERE id=?", (pid,)).fetchone()["current_version_id"]
    conn.execute("UPDATE versions SET created_at=? WHERE id=?", ("2024-01-01T00:00:00", current_id))
    for idx in range(2, 6):
        conn.execute(
            "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) VALUES(?,?,?,?,?)",
            (pid, f"1.0.{idx}", f"v{idx}", f"2024-01-0{idx}T00:00:00", current_id),
        )
    conn.execute("UPDATE prompts SET current_version_id=? WHERE id=?", (current_id, pid))
    appmod.set_setting(conn, "version_cleanup_threshold", "2")
    appmod.prune_versions(conn, pid)
    conn.commit()
    kept_current = conn.execute("SELECT 1 FROM versions WHERE id=?", (current_id,)).fetchone()
    count = conn.execute("SELECT COUNT(*) AS c FROM versions WHERE prompt_id=?", (pid,)).fetchone()["c"]
    conn.close()
    assert kept_current is not None
    assert count == 3


def test_pruned_parent_links_keep_exports_importable(client, appmod):
    pid = create_prompt(appmod, "P", "v1")
    conn = appmod.get_db()
    appmod.set_setting(conn, "version_cleanup_threshold", "1")
    conn.commit()
    conn.close()

    set_csrf(client)
    client.post(
        f"/prompt/{pid}",
        data={
            "name": "P", "content": "v2", "do_save_version": "1",
            "bump_kind": "patch", "_csrf_token": CSRF,
        },
    )
    conn = appmod.get_db()
    versions = conn.execute(
        "SELECT id, parent_version_id FROM versions WHERE prompt_id=?", (pid,)
    ).fetchall()
    assert len(versions) == 1
    assert versions[0]["parent_version_id"] is None
    exported = client.get("/export?format=json").get_data()
    conn.execute("DELETE FROM prompts")
    conn.commit()
    conn.close()

    set_csrf(client)
    data = _settings_data()
    data["import_file"] = (io.BytesIO(exported), "roundtrip.json")
    response = client.post(
        "/settings", data=data, content_type="multipart/form-data"
    )
    assert response.status_code in (302, 303)
    assert _current_content(appmod, pid) == "v2"


def test_columns_rejects_unknown_table(appmod):
    conn = appmod.get_db()
    try:
        assert "name" in appmod._columns(conn, "prompts")
        with pytest.raises(ValueError):
            appmod._columns(conn, "prompts); DROP TABLE prompts; --")
    finally:
        conn.close()


def test_toggle_prompt_flag_rejects_invalid_column(appmod):
    with pytest.raises(ValueError):
        appmod._toggle_prompt_flag(1, "pinned=1 WHERE 1=1 --", lambda _p: 1)


# --- A failed pre-import backup must abort the destructive import ------------
def test_import_backup_failure_aborts_and_keeps_data(client, appmod, monkeypatch):
    create_prompt(appmod, "KeepMe", "keepcontent")

    def boom(conn):
        raise OSError("disk full")

    monkeypatch.setattr(appmod, "_backup_current_data", boom)
    set_csrf(client)
    payload = json.dumps({"prompts": [{"name": "NewOnly", "versions": [{"version": "1.0.0", "content": "x"}]}]})
    data = _settings_data()
    data["import_file"] = (io.BytesIO(payload.encode("utf-8")), "d.json")
    r = client.post("/settings", data=data, content_type="multipart/form-data", follow_redirects=True)
    conn = appmod.get_db()
    names = [row["name"] for row in conn.execute("SELECT name FROM prompts").fetchall()]
    conn.close()
    assert "KeepMe" in names          # library untouched
    assert "NewOnly" not in names     # destructive import did not run
    assert "已导入并覆盖所有数据" not in r.get_data(as_text=True)


# --- Imported image_data is sanitised (CSV blank -> NULL, non-image -> NULL) --
def test_import_blank_image_data_becomes_null(client, appmod):
    set_csrf(client)
    payload = json.dumps({"prompts": [{"name": "P", "image_data": "", "versions": [{"version": "1.0.0", "content": "c"}]}]})
    data = _settings_data()
    data["import_file"] = (io.BytesIO(payload.encode("utf-8")), "d.json")
    client.post("/settings", data=data, content_type="multipart/form-data")
    conn = appmod.get_db()
    row = conn.execute("SELECT image_data FROM prompts WHERE name='P'").fetchone()
    conn.close()
    assert row["image_data"] is None  # '' would later render as a broken <img>


def test_import_strips_non_image_data_uri(client, appmod):
    set_csrf(client)
    payload = json.dumps({"prompts": [
        {"name": "Evil", "image_data": "data:text/html;base64,PHNjcmlwdD4=", "versions": [{"version": "1.0.0", "content": "c"}]}
    ]})
    data = _settings_data()
    data["import_file"] = (io.BytesIO(payload.encode("utf-8")), "d.json")
    client.post("/settings", data=data, content_type="multipart/form-data")
    conn = appmod.get_db()
    row = conn.execute("SELECT image_data FROM prompts WHERE name='Evil'").fetchone()
    conn.close()
    assert row["image_data"] is None  # text/html payload dropped, not stored


def test_prompt_image_rejects_non_image_mime(client, appmod):
    # Simulate a legacy row that slipped a non-image data URI past import.
    conn = appmod.get_db()
    cur = conn.cursor()
    ts = appmod.now_ts()
    cur.execute(
        "INSERT INTO prompts(name, image_data, created_at, updated_at) VALUES(?,?,?,?)",
        ("Evil", "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==", ts, ts),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    assert client.get(f"/prompt/{pid}/image").status_code == 404  # never served as text/html


# --- Image upload accepts a fully non-ASCII (Chinese) filename ---------------
def test_image_upload_accepts_chinese_filename(client, appmod):
    set_csrf(client)
    data = {
        "name": "WithImg", "content": "body", "bump_kind": "patch", "_csrf_token": CSRF,
        "image_file": (io.BytesIO(PNG), "图片.png", "image/png"),
    }
    client.post("/prompt/new", data=data, content_type="multipart/form-data", follow_redirects=True)
    conn = appmod.get_db()
    row = conn.execute("SELECT image_data FROM prompts WHERE name='WithImg'").fetchone()
    conn.close()
    assert row is not None
    assert row["image_data"] and row["image_data"].startswith("data:image/png;base64,")


# --- /api/search now matches content/source/notes/tags (not just name) -------
def test_api_search_matches_content(client, appmod):
    create_prompt(appmod, "Alpha", "a unique_body_xyz phrase")
    out = client.get("/api/search?q=unique_body_xyz").get_json()
    assert any(item["name"] == "Alpha" for item in out)


def test_api_search_locked_prompt_only_matched_by_name(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    create_prompt(appmod, "SecretName", "hidden_zzz_body", require_password=1)
    # Body of a locked prompt must never participate in matching.
    assert client.get("/api/search?q=hidden_zzz_body").get_json() == []
    by_name = client.get("/api/search?q=SecretName").get_json()
    assert by_name and by_name[0]["locked"] is True


# --- POST to an unknown URL returns 404, not a misleading 403 ----------------
def test_post_unknown_url_returns_404(client, appmod):
    set_csrf(client)
    r = client.post("/no/such/route", data={"_csrf_token": CSRF})
    assert r.status_code == 404


def test_unknown_url_stays_404_in_global_auth_mode(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    r = client.post("/no/such/route")
    assert r.status_code == 404


# --- Status-toggle success paths (previously only 403 path was tested) -------
def test_toggle_pin_favorite_archive_round_trip(client, appmod):
    pid = create_prompt(appmod, "P", "c")
    set_csrf(client)
    for route, col in (("pin", "pinned"), ("favorite", "favorite"), ("archive", "archived_at")):
        client.post(f"/prompt/{pid}/{route}", data={"_csrf_token": CSRF})
        conn = appmod.get_db()
        val = conn.execute(f"SELECT {col} FROM prompts WHERE id=?", (pid,)).fetchone()[col]
        conn.close()
        assert val  # turned on (1 for flags, a timestamp for archived_at)


def test_toggle_ajax_returns_json_and_updates_flag(client, appmod):
    pid = create_prompt(appmod, "P", "c")
    set_csrf(client)
    r = client.post(
        f"/prompt/{pid}/favorite",
        data={"_csrf_token": CSRF},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.get_json()["enabled"] is True
    conn = appmod.get_db()
    favorite = conn.execute("SELECT favorite FROM prompts WHERE id=?", (pid,)).fetchone()["favorite"]
    conn.close()
    assert favorite == 1


def test_versions_foreign_key_cascades_on_prompt_delete(appmod):
    conn = appmod.get_db()
    fks = conn.execute("PRAGMA foreign_key_list(versions)").fetchall()
    conn.close()
    assert any(r["table"] == "prompts" and r["on_delete"].upper() == "CASCADE" for r in fks)


def test_delete_prompt_removes_prompt_and_versions(client, appmod):
    pid = create_prompt(appmod, "Doomed", "c")
    set_csrf(client)
    client.post(f"/prompt/{pid}/delete", data={"_csrf_token": CSRF})
    conn = appmod.get_db()
    p = conn.execute("SELECT 1 FROM prompts WHERE id=?", (pid,)).fetchone()
    v = conn.execute("SELECT COUNT(*) AS c FROM versions WHERE prompt_id=?", (pid,)).fetchone()["c"]
    conn.close()
    assert p is None and v == 0


def test_rollback_creates_new_version_with_old_content(client, appmod):
    pid = create_prompt(appmod, "P", "original")
    set_csrf(client)
    # Save a second version so there is something to roll back from.
    client.post(f"/prompt/{pid}", data={
        "name": "P", "content": "edited", "bump_kind": "patch", "do_save_version": "1", "_csrf_token": CSRF,
    })
    conn = appmod.get_db()
    v1 = conn.execute(
        "SELECT id FROM versions WHERE prompt_id=? ORDER BY created_at ASC LIMIT 1", (pid,)
    ).fetchone()["id"]
    conn.close()
    set_csrf(client)
    client.post(f"/prompt/{pid}/rollback/{v1}", data={"bump_kind": "patch", "_csrf_token": CSRF})
    assert _version_count(appmod, pid) == 3
    assert _current_content(appmod, pid) == "original"  # rolled back content is now current


# --- Malformed JSON import shows the friendly localized message --------------
def test_import_malformed_json_friendly_message(client, appmod):
    set_csrf(client)
    data = _settings_data()
    data["import_file"] = (io.BytesIO(b"{ this is not valid json"), "bad.json")
    r = client.post("/settings", data=data, content_type="multipart/form-data", follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "导入失败：JSON 格式无效" in body
    assert "Expecting value" not in body  # raw Python parser text must not leak
