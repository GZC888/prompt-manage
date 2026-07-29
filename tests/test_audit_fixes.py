"""Regression tests for issues found in the audit pass.

Each test below pins the behaviour of a specific fix so the bug cannot silently
return. See the corresponding finding in the audit for context.
"""
import base64
import io
import json

from tests.helpers import CSRF, create_prompt, seed_password, set_csrf, unlock


def _settings_data(**extra):
    data = {
        "settings_action": "import",
        "version_cleanup_threshold": "200", "language": "zh", "auth_mode": "off",
        "_csrf_token": CSRF,
    }
    data.update(extra)
    if any(key in extra for key in ("current_password", "new_password", "confirm_password")):
        data["settings_action"] = "auth"
    return data


# --- Password change must verify the current password -----------------------
def test_password_change_rejects_wrong_current_password(client, appmod):
    seed_password(appmod, "originalpass1", "global")
    set_csrf(client)
    client.post("/login", data={"password": "originalpass1", "_csrf_token": CSRF})
    set_csrf(client)
    client.post("/settings", data=_settings_data(
        auth_mode="global", current_password="WRONG-current",
        new_password="attacker-newpass", confirm_password="attacker-newpass",
    ))
    conn = appmod.get_db()
    stored = appmod.get_setting(conn, "auth_password_hash")
    conn.close()
    # The original password still works; the attacker's value was NOT written.
    assert appmod.verify_password("originalpass1", stored)
    assert not appmod.verify_password("attacker-newpass", stored)


def test_password_change_accepts_correct_current_password(client, appmod):
    seed_password(appmod, "originalpass1", "global")
    set_csrf(client)
    client.post("/login", data={"password": "originalpass1", "_csrf_token": CSRF})
    set_csrf(client)
    client.post("/settings", data=_settings_data(
        auth_mode="global", current_password="originalpass1",
        new_password="brandnewpass1", confirm_password="brandnewpass1",
    ))
    conn = appmod.get_db()
    stored = appmod.get_setting(conn, "auth_password_hash")
    conn.close()
    assert appmod.verify_password("brandnewpass1", stored)
    assert not appmod.verify_password("originalpass1", stored)


# --- Empty import must not wipe the library ---------------------------------
def test_empty_json_import_does_not_wipe(client, appmod):
    create_prompt(appmod, "KeepMe", "keepcontent")
    set_csrf(client)
    data = _settings_data()
    data["import_file"] = (io.BytesIO(b"[]"), "empty.json")
    r = client.post("/settings", data=data, content_type="multipart/form-data", follow_redirects=True)
    conn = appmod.get_db()
    names = [row["name"] for row in conn.execute("SELECT name FROM prompts").fetchall()]
    conn.close()
    assert "KeepMe" in names
    assert "已导入并覆盖所有数据" not in r.get_data(as_text=True)


def test_header_only_csv_import_does_not_wipe(client, appmod):
    create_prompt(appmod, "KeepMe", "keepcontent")
    set_csrf(client)
    data = _settings_data()
    data["import_file"] = (io.BytesIO(b"id,name,versions\n"), "empty.csv")
    client.post("/settings", data=data, content_type="multipart/form-data")
    conn = appmod.get_db()
    names = [row["name"] for row in conn.execute("SELECT name FROM prompts").fetchall()]
    conn.close()
    assert "KeepMe" in names


# --- Import robustness against partial / foreign payloads -------------------
def test_import_defaults_missing_name_and_version(client, appmod):
    create_prompt(appmod, "Old", "old")
    set_csrf(client)
    payload = json.dumps({"prompts": [{"tags": [], "versions": [{"content": "c"}]}]})
    data = _settings_data()
    data["import_file"] = (io.BytesIO(payload.encode("utf-8")), "d.json")
    client.post("/settings", data=data, content_type="multipart/form-data")
    conn = appmod.get_db()
    pnames = [r["name"] for r in conn.execute("SELECT name FROM prompts").fetchall()]
    vvers = [r["version"] for r in conn.execute("SELECT version FROM versions").fetchall()]
    conn.close()
    assert pnames == ["未命名提示词"]
    assert vvers == ["1.0.0"]


def test_import_non_numeric_copy_count_does_not_abort(client, appmod):
    set_csrf(client)
    payload = json.dumps({"prompts": [
        {"name": "P", "copy_count": "abc", "versions": [{"version": "1.0.0", "content": "c"}]}
    ]})
    data = _settings_data()
    data["import_file"] = (io.BytesIO(payload.encode("utf-8")), "d.json")
    client.post("/settings", data=data, content_type="multipart/form-data")
    conn = appmod.get_db()
    rows = conn.execute("SELECT name, copy_count FROM prompts").fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0]["name"] == "P" and rows[0]["copy_count"] == 0


def test_import_preserves_current_version_id(client, appmod):
    set_csrf(client)
    # Newest by created_at is id=1; the exported current pointer is id=2.
    payload = json.dumps({"prompts": [{
        "id": 1, "name": "P", "current_version_id": 2,
        "versions": [
            {"id": 1, "version": "1.0.0", "content": "v1", "created_at": "2024-01-03T00:00:00"},
            {"id": 2, "version": "1.0.1", "content": "v2", "created_at": "2024-01-01T00:00:00"},
            {"id": 3, "version": "1.0.2", "content": "v3", "created_at": "2024-01-02T00:00:00"},
        ],
    }]})
    data = _settings_data()
    data["import_file"] = (io.BytesIO(payload.encode("utf-8")), "d.json")
    client.post("/settings", data=data, content_type="multipart/form-data")
    conn = appmod.get_db()
    cur = conn.execute("SELECT current_version_id FROM prompts WHERE id=1").fetchone()
    conn.close()
    assert cur["current_version_id"] == 2  # honoured, not recomputed to newest-by-time


# --- Cover image served via access-checked route -----------------------------
def _make_prompt_with_image(appmod, name, require_password=0):
    raw = b"\x89PNG\r\n\x1a\n" + b"fakepngbody" * 4
    data_uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    conn = appmod.get_db()
    cur = conn.cursor()
    ts = appmod.now_ts()
    cur.execute(
        "INSERT INTO prompts(name, image_data, require_password, created_at, updated_at) "
        "VALUES(?,?,?,?,?)", (name, data_uri, require_password, ts, ts),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid, raw


def test_prompt_image_route_serves_bytes(client, appmod):
    pid, raw = _make_prompt_with_image(appmod, "Img")
    r = client.get(f"/prompt/{pid}/image")
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    assert r.get_data() == raw


def test_prompt_image_404_when_absent(client, appmod):
    pid = create_prompt(appmod, "NoImg", "c")
    assert client.get(f"/prompt/{pid}/image").status_code == 404


def test_prompt_image_403_when_locked(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    pid, _ = _make_prompt_with_image(appmod, "Sec", require_password=1)
    assert client.get(f"/prompt/{pid}/image").status_code == 403
    unlock(client, pid)
    assert client.get(f"/prompt/{pid}/image").status_code == 200


def test_index_does_not_inline_base64_image(client, appmod):
    _make_prompt_with_image(appmod, "Img")
    html = client.get("/").get_data(as_text=True)
    assert "data:image/png;base64," not in html      # not inlined
    assert "/image" in html                           # references the route


# --- Open-redirect hardening in _safe_next ----------------------------------
def test_login_next_rejects_backslash_open_redirect(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    set_csrf(client)
    r = client.post("/login", data={
        "password": "longpassword123", "next": "/\\evil.com", "_csrf_token": CSRF,
    })
    assert r.status_code in (302, 303)
    assert "evil.com" not in r.headers["Location"]


# --- diff_view tolerates a NULL current_version_id --------------------------
def test_diff_view_with_null_current_version(client, appmod):
    conn = appmod.get_db()
    cur = conn.cursor()
    ts = appmod.now_ts()
    cur.execute("INSERT INTO prompts(name, created_at, updated_at) VALUES('P',?,?)", (ts, ts))
    pid = cur.lastrowid
    cur.execute("INSERT INTO versions(prompt_id, version, content, created_at) VALUES(?,?,?,?)", (pid, "1.0.0", "a", ts))
    cur.execute("INSERT INTO versions(prompt_id, version, content, created_at) VALUES(?,?,?,?)", (pid, "1.0.1", "b", ts))
    conn.commit()
    conn.close()
    # current_version_id left NULL — diff must still render rather than error out.
    r = client.get(f"/prompt/{pid}/diff")
    assert r.status_code == 200
