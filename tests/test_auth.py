"""Access control: the two auth modes, sessions, CSRF and rate limiting."""

import hashlib

from .helpers import (
    CSRF, clone_session, create_prompt, get_setting, login, seed_legacy_password,
    seed_password, set_csrf, set_setting,
)

PROTECTED = ["/", "/settings", "/export", "/prompt/new", "/api/tags", "/api/search"]


def test_open_mode_needs_no_login(client, appmod):
    create_prompt(appmod)
    for path in PROTECTED:
        assert client.get(path).status_code == 200, path


def test_global_mode_redirects_anonymous_visitors(client, appmod):
    seed_password(appmod, "password123", "global")
    for path in PROTECTED:
        response = client.get(path)
        assert response.status_code == 302, path
        assert response.headers["Location"].startswith("/login"), path


def test_login_grants_access_and_preserves_next(client, appmod):
    seed_password(appmod, "password123", "global")
    response = client.get("/settings")
    assert response.headers["Location"] == "/login?next=/settings"
    set_csrf(client)
    response = client.post(
        "/login", data={"password": "password123", "_csrf_token": CSRF, "next": "/settings"}
    )
    assert response.headers["Location"] == "/settings"
    assert client.get("/settings").status_code == 200


def test_wrong_password_is_rejected(client, appmod):
    seed_password(appmod, "password123", "global")
    set_csrf(client)
    response = client.post("/login", data={"password": "nope", "_csrf_token": CSRF})
    assert response.status_code == 401
    assert "密码不正确" in response.get_data(as_text=True)


def test_login_next_cannot_leave_the_site(client, appmod):
    seed_password(appmod, "password123", "global")
    set_csrf(client)
    response = client.post(
        "/login",
        data={"password": "password123", "_csrf_token": CSRF, "next": "https://evil.example/x"},
    )
    assert response.headers["Location"] == "/"


def test_logout_revokes_the_server_side_session(client, appmod):
    login(client, appmod, "password123")
    assert client.get("/settings").status_code == 200
    set_csrf(client)
    client.post("/logout", data={"_csrf_token": CSRF})
    assert client.get("/settings").status_code == 302


def test_changing_the_password_invalidates_other_sessions(client, appmod):
    login(client, appmod, "password123")
    other = clone_session(appmod, client)
    assert other.get("/settings").status_code == 200  # the copy starts out valid
    set_csrf(client)
    client.post("/settings", data={
        "_csrf_token": CSRF, "settings_action": "auth", "auth_mode": "global",
        "current_password": "password123", "new_password": "newpassword1",
        "confirm_password": "newpassword1",
    })
    # The rotating session keeps working; a copy of the old cookie does not.
    assert client.get("/settings").status_code == 200
    assert other.get("/settings").status_code == 302


def test_forged_session_cookie_is_not_enough(client, appmod):
    seed_password(appmod, "password123", "global")
    with client.session_transaction() as session:
        session["auth_ok"] = True
        session["auth_revision"] = get_setting(appmod, "auth_revision", "1")
        session["sid"] = "f" * 40  # never registered in auth_sessions
    assert client.get("/settings").status_code == 302


def test_legacy_sha256_password_is_upgraded_on_login(client, appmod):
    seed_legacy_password(appmod, "password123", "global")
    set_csrf(client)
    response = client.post("/login", data={"password": "password123", "_csrf_token": CSRF})
    assert response.status_code == 302
    stored = get_setting(appmod, "auth_password_hash")
    assert stored != hashlib.sha256(b"password123").hexdigest()
    assert appmod.verify_password("password123", stored)


def test_unsupported_hash_never_verifies(appmod):
    for stored in ("", "plain", "$$", "pbkdf2:sha256:1$salt$" + "0" * 64, "x" * 64):
        assert appmod.verify_password("password123", stored) is False


def test_csrf_is_required_for_writes(client, appmod):
    prompt_id = create_prompt(appmod)
    set_csrf(client)
    assert client.post(f"/prompt/{prompt_id}/pin", data={}).status_code == 403
    assert client.post(f"/prompt/{prompt_id}/pin", data={"_csrf_token": "wrong"}).status_code == 403
    assert client.post(f"/prompt/{prompt_id}/pin", data={"_csrf_token": CSRF}).status_code == 302


def test_csrf_accepts_the_header(client, appmod):
    prompt_id = create_prompt(appmod)
    set_csrf(client)
    response = client.post(f"/prompt/{prompt_id}/pin", headers={"X-CSRF-Token": CSRF})
    assert response.status_code == 302


def test_login_rate_limit(client, appmod, monkeypatch):
    monkeypatch.setitem(appmod.app.config, "AUTH_LOGIN_MAX_ATTEMPTS", 3)
    seed_password(appmod, "password123", "global")
    set_csrf(client)
    for _ in range(3):
        client.post("/login", data={"password": "wrong", "_csrf_token": CSRF})
    response = client.post("/login", data={"password": "password123", "_csrf_token": CSRF})
    assert response.status_code == 429


def test_successful_login_clears_the_attempt_counter(client, appmod, monkeypatch):
    monkeypatch.setitem(appmod.app.config, "AUTH_LOGIN_MAX_ATTEMPTS", 3)
    seed_password(appmod, "password123", "global")
    set_csrf(client)
    client.post("/login", data={"password": "wrong", "_csrf_token": CSRF})
    client.post("/login", data={"password": "password123", "_csrf_token": CSRF})
    conn = appmod.connect(appmod.app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) c FROM login_attempts").fetchone()["c"] == 0
    conn.close()


def test_anonymous_logout_short_circuits(client, appmod):
    set_csrf(client)
    response = client.post("/logout", data={"_csrf_token": CSRF})
    assert response.status_code == 302 and response.headers["Location"] == "/"


def test_logout_link_only_shows_when_authenticated(client, appmod):
    create_prompt(appmod)
    assert b"/logout" not in client.get("/").data
    login(client, appmod, "password123")
    assert b"/logout" in client.get("/").data


def test_login_page_is_reachable_without_a_password(client):
    assert client.get("/login").status_code == 200


def test_already_authenticated_login_redirects_home(client, appmod):
    login(client, appmod, "password123")
    response = client.get("/login")
    assert response.status_code == 302 and response.headers["Location"] == "/"


def test_session_id_is_created_and_stable(client, appmod):
    login(client, appmod, "password123")
    with client.session_transaction() as session:
        first = session["sid"]
    client.get("/")
    with client.session_transaction() as session:
        assert session["sid"] == first
        assert len(first) >= 32


def test_setup_is_hidden_once_bootstrapped(client):
    assert client.get("/setup").status_code == 404


def test_per_prompt_mode_is_promoted_to_global(client, appmod):
    """Migration 12 keeps a legacy 'per' install protected rather than open."""
    set_setting(appmod, "auth_mode", "per")
    seed_password(appmod, "password123", "per")
    conn = appmod.connect(appmod.app.config["DB_PATH"])
    conn.execute("DELETE FROM schema_migrations WHERE version=12")
    conn.commit()
    conn.close()
    with appmod.app.app_context():
        appmod.run_migrations()
    assert get_setting(appmod, "auth_mode") == "global"
    assert client.get("/").status_code == 302


def test_unknown_auth_mode_is_treated_as_protected(client, appmod):
    seed_password(appmod, "password123", "global")
    set_setting(appmod, "auth_mode", "weird-value")
    assert client.get("/").status_code == 302
