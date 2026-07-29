from tests.helpers import CSRF, create_prompt, seed_password, set_csrf, unlock


def test_export_requires_admin_when_password_set(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    create_prompt(appmod, "Pub", "pubcontent")
    r = client.get("/export")
    assert r.status_code in (302, 303)
    assert "/login" in r.headers["Location"]


def test_export_excludes_locked_content(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    create_prompt(appmod, "Pub", "PUBLICBODY")
    create_prompt(appmod, "Sec", "SECRETEXPORTBODY", require_password=1, tags=["secrettag"])
    # Authenticate as owner so we can reach export.
    set_csrf(client)
    client.post("/login", data={"password": "longpassword123", "_csrf_token": CSRF})

    body = client.get("/export?format=json").get_data(as_text=True)
    assert "PUBLICBODY" in body
    assert "SECRETEXPORTBODY" not in body
    assert "secrettag" not in body


def test_default_export_stays_secret_even_after_prompt_unlock(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    protected_id = create_prompt(appmod, "Sec", "SECRETEXPORTBODY", require_password=1)
    set_csrf(client)
    client.post("/login", data={"password": "longpassword123", "_csrf_token": CSRF})
    unlock(client, protected_id, appmod)

    body = client.get("/export?format=json").get_data(as_text=True)
    assert "SECRETEXPORTBODY" not in body


def test_export_include_locked_requires_auth(client, appmod):
    create_prompt(appmod, "P", "c")  # off mode: settings/export open
    r = client.get("/export?include_locked=1")
    assert r.status_code == 403


def test_export_include_locked_when_authenticated(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    create_prompt(appmod, "Sec", "SECRETEXPORTBODY", require_password=1)
    set_csrf(client)
    client.post("/login", data={"password": "longpassword123", "_csrf_token": CSRF})
    body = client.get("/export?format=json&include_locked=1").get_data(as_text=True)
    assert "SECRETEXPORTBODY" in body


def test_normal_export_does_not_include_auth_hash(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    create_prompt(appmod, "P", "body")
    conn = appmod.get_db()
    password_hash = appmod.get_setting(conn, "auth_password_hash")
    conn.close()
    set_csrf(client)
    client.post("/login", data={"password": "longpassword123", "_csrf_token": CSRF})

    normal = client.get("/export?format=json").get_data(as_text=True)
    assert "auth_password_hash" not in normal
    assert password_hash not in normal

    full = client.get(
        "/export?format=json&include_locked=1&include_auth=1"
    ).get_data(as_text=True)
    assert "auth_password_hash" in full
    assert password_hash in full


def test_export_csv_excludes_locked(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    create_prompt(appmod, "Pub", "PUBLICBODY")
    create_prompt(appmod, "Sec", "SECRETCSVBODY", require_password=1)
    set_csrf(client)
    client.post("/login", data={"password": "longpassword123", "_csrf_token": CSRF})
    body = client.get("/export?format=csv").get_data(as_text=True)
    assert "PUBLICBODY" in body
    assert "SECRETCSVBODY" not in body


def test_export_off_mode_includes_schema(client, appmod):
    create_prompt(appmod, "P", "c")
    import json
    data = json.loads(client.get("/export?format=json").get_data(as_text=True))
    assert data["schema_version"] >= 1
    assert data["app"] == "prompt-manage"
    assert "exported_at" in data
