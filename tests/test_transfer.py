"""Backup export and import."""

import io
import json
import os

from .helpers import CSRF, create_prompt, login, set_csrf


def export_bundle(client):
    response = client.get("/export")
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    return json.loads(response.data.decode("utf-8"))


def upload(client, payload, filename="backup.json", **fields):
    data = {"_csrf_token": CSRF, "settings_action": "import"}
    data.update(fields)
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload, ensure_ascii=False)
    if isinstance(body, str):
        body = body.encode("utf-8")
    data["import_file"] = (io.BytesIO(body), filename)
    return client.post("/settings", data=data, content_type="multipart/form-data",
                       follow_redirects=True)


def test_export_shape(client, appmod):
    create_prompt(appmod, name="Alpha", content="hello", tags=["x"], versions=2)
    bundle = export_bundle(client)
    assert bundle["app"] == "prompt-manage"
    assert bundle["schema_version"] == appmod.SCHEMA_VERSION
    prompt = bundle["prompts"][0]
    assert prompt["name"] == "Alpha" and prompt["tags"] == ["x"]
    assert len(prompt["versions"]) == 2
    assert prompt["current_version_id"] in {v["id"] for v in prompt["versions"]}


def test_export_hides_credentials_by_default(client, appmod):
    login(client, appmod, "password123")
    assert "auth_password_hash" not in export_bundle(client)["settings"]
    full = json.loads(client.get("/export?include_auth=1").data.decode("utf-8"))
    assert full["settings"]["auth_password_hash"]


def test_round_trip(client, appmod):
    create_prompt(appmod, name="Alpha", content="hello", tags=["x"], pinned=1, versions=3)
    create_prompt(appmod, name="Beta", content="world", archived=True)
    bundle = export_bundle(client)
    set_csrf(client)
    response = upload(client, bundle)
    assert "已导入 2 条提示词" in response.get_data(as_text=True)
    assert export_bundle(client)["prompts"] == bundle["prompts"]


def test_import_replaces_existing_data(client, appmod):
    create_prompt(appmod, name="Existing")
    set_csrf(client)
    upload(client, {"prompts": [{
        "name": "Only", "versions": [{"version": "1.0.0", "content": "c"}],
    }]})
    names = [p["name"] for p in export_bundle(client)["prompts"]]
    assert names == ["Only"]


def test_import_writes_a_backup_first(client, appmod):
    create_prompt(appmod, name="Existing")
    set_csrf(client)
    upload(client, {"prompts": [{"name": "New", "versions": [{"version": "1.0.0", "content": "c"}]}]})
    backups_dir = os.path.join(os.path.dirname(appmod.app.config["DB_PATH"]), "backups")
    files = os.listdir(backups_dir)
    assert files and all(name.startswith("pre-import-") for name in files)
    saved = json.load(open(os.path.join(backups_dir, files[0]), encoding="utf-8"))
    assert saved["prompts"][0]["name"] == "Existing"


def test_old_bundles_with_favorite_become_pinned(client, appmod):
    set_csrf(client)
    upload(client, {"prompts": [{
        "name": "Starred", "favorite": True, "pinned": False,
        "versions": [{"version": "1.0.0", "content": "c"}],
    }]})
    assert export_bundle(client)["prompts"][0]["pinned"] is True


def test_import_rejects_non_json_files(client, appmod):
    create_prompt(appmod, name="Existing")
    set_csrf(client)
    response = upload(client, "id,name\n1,x\n", filename="backup.csv")
    assert "仅支持 .json" in response.get_data(as_text=True)
    assert export_bundle(client)["prompts"][0]["name"] == "Existing"


def test_import_validation_failures(client, appmod):
    create_prompt(appmod, name="Existing")
    set_csrf(client)
    cases = [
        (b"not json at all", "JSON 格式无效"),
        ({"nope": []}, "缺少 prompts 列表"),
        ({"prompts": []}, "未发现任何提示词"),
        ({"app": "prompt-manage", "prompts": []}, "必须同时存在"),
        ({"app": "evil", "schema_version": 1, "prompts": []}, "app 标识无效"),
        ({"prompts": [{"name": "", "versions": []}]}, "缺少名称"),
        ({"prompts": [{"name": "A", "versions": []}]}, "缺少版本记录"),
        ({"prompts": [{"name": "A", "versions": [{"version": "1.0.0"}]}]}, "缺少内容"),
        ({"prompts": [{"name": "A", "id": -1, "versions": [{"version": "1.0.0", "content": "c"}]}]},
         "必须为正整数"),
        ({"prompts": [{"name": "A", "id": 2 ** 60,
                       "versions": [{"version": "1.0.0", "content": "c"}]}]}, "安全导入范围"),
        ({"prompts": [{"name": "A", "created_at": "9999-01-01T00:00:00",
                       "versions": [{"version": "1.0.0", "content": "c"}]}]}, "不能晚于当前时间"),
        ({"prompts": [{"name": "A", "created_at": "not-a-date",
                       "versions": [{"version": "1.0.0", "content": "c"}]}]}, "时间格式无效"),
        ({"prompts": [{"name": "A", "versions": [
            {"id": 1, "version": "1.0.0", "content": "c", "parent_version_id": 99}]}]},
         "不在同一提示词内"),
        ({"prompts": [{"name": "A", "current_version_id": 99,
                       "versions": [{"id": 1, "version": "1.0.0", "content": "c"}]}]},
         "不在其版本列表中"),
        ({"prompts": [
            {"id": 5, "name": "A", "versions": [{"version": "1.0.0", "content": "c"}]},
            {"id": 5, "name": "B", "versions": [{"version": "1.0.0", "content": "c"}]}]},
         "重复"),
    ]
    for payload, expected in cases:
        response = upload(client, payload)
        assert expected in response.get_data(as_text=True), payload
    assert export_bundle(client)["prompts"][0]["name"] == "Existing"


def test_import_rejects_a_version_cycle(client, appmod):
    set_csrf(client)
    response = upload(client, {"prompts": [{"name": "A", "versions": [
        {"id": 1, "version": "1.0.0", "content": "a", "parent_version_id": 2},
        {"id": 2, "version": "1.0.1", "content": "b", "parent_version_id": 1},
    ]}]})
    assert "循环" in response.get_data(as_text=True)


def test_import_without_a_file(client):
    set_csrf(client)
    response = client.post("/settings", data={"_csrf_token": CSRF, "settings_action": "import"},
                           follow_redirects=True)
    assert "请选择文件" in response.get_data(as_text=True)


def test_oversized_import_is_rejected(client, appmod):
    appmod.app.config["MAX_IMPORT_SIZE_MB"] = 1
    set_csrf(client)
    payload = json.dumps({"prompts": [{
        "name": "A", "versions": [{"version": "1.0.0", "content": "x" * (2 * 1024 * 1024)}],
    }]})
    response = upload(client, payload)
    assert "文件过大" in response.get_data(as_text=True)


def test_restoring_credentials_needs_both_passwords(client, appmod):
    create_prompt(appmod, name="Alpha")
    login(client, appmod, "password123")
    set_csrf(client)
    bundle = json.loads(client.get("/export?include_auth=1").data.decode("utf-8"))

    response = upload(client, bundle, restore_auth="1")
    assert "必须验证当前密码" in response.get_data(as_text=True)

    response = upload(client, bundle, restore_auth="1", restore_current_password="password123")
    assert "必须验证备份中的密码" in response.get_data(as_text=True)

    response = upload(client, bundle, restore_auth="1", restore_current_password="password123",
                      restore_backup_password="password123")
    assert "已导入" in response.get_data(as_text=True)


def test_import_resets_the_autoincrement_sequence(client, appmod):
    set_csrf(client)
    upload(client, {"prompts": [{"id": 900, "name": "A",
                                 "versions": [{"id": 900, "version": "1.0.0", "content": "c"}]}]})
    upload(client, {"prompts": [{"name": "B", "versions": [{"version": "1.0.0", "content": "c"}]}]})
    assert export_bundle(client)["prompts"][0]["id"] == 1
