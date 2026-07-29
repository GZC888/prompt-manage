import base64
import io
import json
import os
import subprocess
import sys

from tests.helpers import CSRF, ROOT, create_prompt, login, seed_password, set_csrf


def _settings_data(**extra):
    data = {
        "settings_action": "import",
        "version_cleanup_threshold": "200",
        "language": "zh",
        "auth_mode": "off",
        "_csrf_token": CSRF,
    }
    data.update(extra)
    if any(key in extra for key in ("current_password", "new_password", "confirm_password")):
        data["settings_action"] = "auth"
    return data


def test_json_import_rejects_non_object_items_without_wiping(client, appmod):
    create_prompt(appmod, "KeepMe", "keep")
    set_csrf(client)
    data = _settings_data()
    data["import_file"] = (io.BytesIO(b"[null, 1, \"bad\"]"), "bad.json")
    response = client.post("/settings", data=data, content_type="multipart/form-data", follow_redirects=True)
    conn = appmod.get_db()
    names = [row["name"] for row in conn.execute("SELECT name FROM prompts").fetchall()]
    conn.close()
    assert names == ["KeepMe"]
    assert "格式无效" in response.get_data(as_text=True)


def test_json_import_rejects_invalid_tag_types_without_wiping(client, appmod):
    create_prompt(appmod, "KeepMe", "keep")
    set_csrf(client)
    payload = json.dumps({"prompts": [{
        "name": "Bad", "tags": [1, "ok"],
        "versions": [{"version": "1.0.0", "content": "body"}],
    }]})
    data = _settings_data()
    data["import_file"] = (io.BytesIO(payload.encode()), "bad.json")
    client.post("/settings", data=data, content_type="multipart/form-data")
    conn = appmod.get_db()
    names = [row["name"] for row in conn.execute("SELECT name FROM prompts").fetchall()]
    conn.close()
    assert names == ["KeepMe"]


def test_legacy_broken_tags_do_not_break_detail_or_export(client, appmod):
    prompt_id = create_prompt(appmod, "Legacy", "body")
    conn = appmod.get_db()
    conn.execute("UPDATE prompts SET tags=? WHERE id=?", ("{broken", prompt_id))
    conn.commit()
    conn.close()
    assert client.get(f"/prompt/{prompt_id}").status_code == 200
    exported = client.get("/export?format=json")
    assert exported.status_code == 200
    assert json.loads(exported.get_data(as_text=True))["prompts"][0]["tags"] == []


def test_anonymous_per_mode_cannot_write_public_prompt(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    prompt_id = create_prompt(appmod, "Public", "body")
    set_csrf(client)
    response = client.post(f"/prompt/{prompt_id}", data={
        "name": "Changed", "content": "changed", "_csrf_token": CSRF,
    })
    assert response.status_code == 403


def test_protection_flag_is_preserved_outside_per_mode(client, appmod):
    prompt_id = create_prompt(appmod, "Secret", "body", require_password=1)
    login(client, appmod, "longpassword123", mode="global")
    set_csrf(client)
    response = client.post(f"/prompt/{prompt_id}", data={
        "name": "Secret", "content": "updated", "do_save_version": "1",
        "bump_kind": "patch", "_csrf_token": CSRF,
    })
    assert response.status_code in (302, 303)
    conn = appmod.get_db()
    flag = conn.execute("SELECT require_password FROM prompts WHERE id=?", (prompt_id,)).fetchone()[0]
    conn.close()
    assert flag == 1


def test_password_change_invalidates_other_logged_in_sessions(appmod):
    first = appmod.app.test_client()
    second = appmod.app.test_client()
    login(first, appmod, "longpassword123", mode="global")
    login(second, appmod, "longpassword123", mode="global")
    set_csrf(first)
    response = first.post("/settings", data=_settings_data(
        auth_mode="global",
        current_password="longpassword123",
        new_password="brandnewpassword123",
        confirm_password="brandnewpassword123",
    ))
    assert response.status_code in (302, 303)
    stale = second.get("/", follow_redirects=False)
    assert stale.status_code in (302, 303)
    assert "/login" in stale.headers["Location"]


def test_password_confirmation_error_is_visible(client, appmod):
    login(client, appmod, "longpassword123", mode="per")
    set_csrf(client)
    response = client.post("/settings", data=_settings_data(
        auth_mode="per",
        current_password="longpassword123",
        new_password="brandnewpassword123",
        confirm_password="differentpassword123",
    ), follow_redirects=True)
    assert "两次输入的密码不一致" in response.get_data(as_text=True)
    conn = appmod.get_db()
    stored = appmod.get_setting(conn, "auth_password_hash", "")
    conn.close()
    assert appmod.verify_password("longpassword123", stored)


def test_password_can_be_changed_while_public_mode_is_off(client, appmod):
    login(client, appmod, "longpassword123", mode="off")
    set_csrf(client)
    response = client.post("/settings", data=_settings_data(
        auth_mode="off",
        current_password="longpassword123",
        new_password="brandnewpassword123",
        confirm_password="brandnewpassword123",
    ))
    assert response.status_code in (302, 303)
    conn = appmod.get_db()
    stored = appmod.get_setting(conn, "auth_password_hash", "")
    conn.close()
    assert appmod.verify_password("brandnewpassword123", stored)
    # Keeping a staged password while mode is off must not turn the public mode
    # into an accidental login gate.
    assert client.get("/settings").status_code == 200


def test_protected_image_is_not_publicly_cached(client, appmod):
    raw = b"\x89PNG\r\n\x1a\n" + b"content"
    data_uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    prompt_id = create_prompt(appmod, "Image", "body", require_password=1)
    conn = appmod.get_db()
    conn.execute("UPDATE prompts SET image_data=? WHERE id=?", (data_uri, prompt_id))
    conn.commit()
    conn.close()
    login(client, appmod, "longpassword123", mode="global")
    response = client.get(f"/prompt/{prompt_id}/image")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"


def test_forwarded_prefix_is_ignored_by_default(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    response = client.get("/settings", headers={"X-Forwarded-Prefix": "//evil.example"})
    assert response.status_code in (302, 303)
    assert "evil.example" not in response.headers["Location"]


def test_inline_runtime_config_is_json_escaped(client, appmod):
    conn = appmod.get_db()
    payload = "</script><script>window.__injected__=true</script>"
    appmod.set_setting(conn, "auth_mode", payload)
    conn.commit()
    conn.close()

    html = client.get("/").get_data(as_text=True)
    assert payload not in html
    assert r"\u003c/script\u003e" in html


def test_invalid_app_environment_fails_fast(tmp_path):
    env = dict(os.environ)
    env.update({
        "APP_ENV": "prodution",
        "DB_PATH": str(tmp_path / "bad.sqlite3"),
        "SECRET_KEY": "x" * 40,
    })
    result = subprocess.run(
        [sys.executable, "-c", "import app"], cwd=ROOT, env=env,
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "APP_ENV" in result.stderr + result.stdout


def test_invalid_samesite_fails_fast(tmp_path):
    env = dict(os.environ)
    env.update({
        "APP_ENV": "testing",
        "DB_PATH": str(tmp_path / "bad.sqlite3"),
        "SECRET_KEY": "x" * 40,
        "SESSION_COOKIE_SAMESITE": "bogus",
    })
    result = subprocess.run(
        [sys.executable, "-c", "import app"], cwd=ROOT, env=env,
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "SESSION_COOKIE_SAMESITE" in result.stderr + result.stdout
