import io
import json
import os
import subprocess
import sys

from tests.helpers import CSRF, create_prompt, set_csrf
from tests.helpers import ROOT


def _settings_post(client, **extra):
    data = {
        "settings_action": "import",
        "version_cleanup_threshold": "200", "language": "zh", "auth_mode": "off",
        "_csrf_token": CSRF,
    }
    data.update(extra)
    return data


def test_import_invalid_json_preserves_data(client, appmod):
    create_prompt(appmod, "KeepMe", "keepcontent")
    set_csrf(client)
    data = _settings_post(client)
    data["import_file"] = (io.BytesIO(b"{ this is not valid json "), "bad.json")
    client.post("/settings", data=data, content_type="multipart/form-data")

    conn = appmod.get_db()
    names = [r["name"] for r in conn.execute("SELECT name FROM prompts").fetchall()]
    conn.close()
    assert "KeepMe" in names  # existing data untouched by failed import


def test_import_valid_overwrites_and_backs_up(client, appmod):
    create_prompt(appmod, "OldOne", "oldcontent")
    set_csrf(client)
    payload = json.dumps({
        "prompts": [
            {"name": "NewOne", "tags": ["t1"], "versions": [{"version": "1.0.0", "content": "newcontent"}]}
        ],
    })
    data = _settings_post(client)
    data["import_file"] = (io.BytesIO(payload.encode("utf-8")), "data.json")
    client.post("/settings", data=data, content_type="multipart/form-data")

    conn = appmod.get_db()
    names = [r["name"] for r in conn.execute("SELECT name FROM prompts").fetchall()]
    conn.close()
    assert "NewOne" in names and "OldOne" not in names

    backups = os.path.join(os.path.dirname(appmod.db_path()), "backups")
    assert os.path.isdir(backups)
    assert any(f.startswith("pre-import-") for f in os.listdir(backups))


def test_import_csv_roundtrip(client, appmod):
    create_prompt(appmod, "Seed", "seedcontent")
    set_csrf(client)
    # Export CSV (off mode is open), then re-import it.
    csv_body = client.get("/export?format=csv").get_data()
    data = _settings_post(client)
    data["import_file"] = (io.BytesIO(csv_body), "data.csv")
    r = client.post("/settings", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200
    conn = appmod.get_db()
    names = [r["name"] for r in conn.execute("SELECT name FROM prompts").fetchall()]
    conn.close()
    assert "Seed" in names


def test_import_csv_invalid_json_fails_with_row_and_preserves_data(client, appmod):
    create_prompt(appmod, "KeepMe", "keepcontent")
    set_csrf(client)
    bad_csv = "id,name,tags,versions\n1,Bad,{broken,[]\n"
    data = _settings_post(client)
    data["import_file"] = (io.BytesIO(bad_csv.encode("utf-8")), "bad.csv")
    r = client.post("/settings", data=data, content_type="multipart/form-data", follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "导入失败：第2行数据格式错误" in body
    conn = appmod.get_db()
    names = [row["name"] for row in conn.execute("SELECT name FROM prompts").fetchall()]
    conn.close()
    assert "KeepMe" in names
    assert "Bad" not in names


def test_import_unsupported_type_rejected(client, appmod):
    create_prompt(appmod, "KeepMe", "keepcontent")
    set_csrf(client)
    data = _settings_post(client)
    data["import_file"] = (io.BytesIO(b"hello"), "notes.txt")
    client.post("/settings", data=data, content_type="multipart/form-data")
    conn = appmod.get_db()
    names = [r["name"] for r in conn.execute("SELECT name FROM prompts").fetchall()]
    conn.close()
    assert "KeepMe" in names


def _run_import_app(tmp_path, with_secret):
    env = dict(os.environ)
    env["APP_ENV"] = "production"
    env["DB_PATH"] = str(tmp_path / "prod.sqlite3")
    if with_secret:
        env["SECRET_KEY"] = "x" * 40
    else:
        env.pop("SECRET_KEY", None)
    code = "import app; print('STARTED_OK')"
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)


def test_production_without_secret_key_fails(tmp_path):
    r = _run_import_app(tmp_path, with_secret=False)
    assert r.returncode != 0
    assert "SECRET_KEY" in (r.stderr + r.stdout)
    assert "STARTED_OK" not in r.stdout


def test_production_with_secret_key_starts(tmp_path):
    r = _run_import_app(tmp_path, with_secret=True)
    assert r.returncode == 0, r.stderr
    assert "STARTED_OK" in r.stdout
