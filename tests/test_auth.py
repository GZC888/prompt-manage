from tests.helpers import CSRF, seed_password, set_csrf


def test_login_success_sets_permanent_session(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    set_csrf(client)
    r = client.post("/login", data={"password": "longpassword123", "_csrf_token": CSRF})
    assert r.status_code in (302, 303)
    with client.session_transaction() as sess:
        assert sess.get("auth_ok") is True
        assert sess.permanent is True


def test_login_wrong_password(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    set_csrf(client)
    r = client.post("/login", data={"password": "nope", "_csrf_token": CSRF})
    assert r.status_code == 200
    with client.session_transaction() as sess:
        assert not sess.get("auth_ok")


def test_logout_clears_session(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    set_csrf(client)
    client.post("/login", data={"password": "longpassword123", "_csrf_token": CSRF})
    set_csrf(client)
    client.post("/logout", data={"_csrf_token": CSRF})
    with client.session_transaction() as sess:
        assert not sess.get("auth_ok")
        assert not sess.get("unlocked_prompts")


def test_logout_revokes_a_copied_auth_cookie(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    set_csrf(client)
    client.post("/login", data={"password": "longpassword123", "_csrf_token": CSRF})
    stolen = client.get_cookie(appmod.app.config["SESSION_COOKIE_NAME"])
    assert stolen is not None

    copied = appmod.app.test_client()
    copied.set_cookie(appmod.app.config["SESSION_COOKIE_NAME"], stolen.value)
    assert copied.get("/settings").status_code == 200

    set_csrf(client)
    client.post("/logout", data={"_csrf_token": CSRF})
    revoked = copied.get("/settings", follow_redirects=False)
    assert revoked.status_code in (302, 303)
    assert "/login" in revoked.headers["Location"]


def test_anonymous_per_logout_cannot_revoke_owner_session(appmod):
    owner = appmod.app.test_client()
    visitor = appmod.app.test_client()
    seed_password(appmod, "longpassword123", "per")
    set_csrf(owner)
    owner.post("/login", data={"password": "longpassword123", "_csrf_token": CSRF})
    set_csrf(visitor)
    visitor.post("/logout", data={"_csrf_token": CSRF})
    assert owner.get("/settings").status_code == 200


def test_global_mode_redirects_unauthenticated(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    r = client.get("/")
    assert r.status_code in (302, 303)
    assert "/login" in r.headers["Location"]


def test_login_rate_limited(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    appmod.app.config["AUTH_LOGIN_MAX_ATTEMPTS"] = 3
    set_csrf(client)
    for _ in range(3):
        client.post("/login", data={"password": "wrong", "_csrf_token": CSRF})
    r = client.post("/login", data={"password": "wrong", "_csrf_token": CSRF})
    assert r.status_code == 429


def test_global_login_rate_limit_blocks_distributed_attempts(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    appmod.app.config["AUTH_LOGIN_MAX_ATTEMPTS"] = 10
    appmod.app.config["GLOBAL_LOGIN_MAX_ATTEMPTS"] = 2
    appmod.app.config["GLOBAL_LOGIN_WINDOW_SECONDS"] = 3600
    set_csrf(client)
    for ip in ("198.51.100.1", "198.51.100.2"):
        client.post(
            "/login",
            data={"password": "wrong", "_csrf_token": CSRF},
            environ_overrides={"REMOTE_ADDR": ip},
        )
    r = client.post(
        "/login",
        data={"password": "wrong", "_csrf_token": CSRF},
        environ_overrides={"REMOTE_ADDR": "198.51.100.3"},
    )
    assert r.status_code == 429
    assert "系统检测到大量登录失败尝试" in r.get_data(as_text=True)


def test_no_maximum_password_length(client, appmod):
    # Set a very long passphrase via settings (open in off mode), then log in.
    long_pw = "correct horse battery staple " * 10  # ~290 chars
    set_csrf(client)
    client.post("/settings", data={
        "auth_mode": "global", "new_password": long_pw, "confirm_password": long_pw,
        "version_cleanup_threshold": "200", "language": "zh", "_csrf_token": CSRF,
    })
    set_csrf(client)
    r = client.post("/login", data={"password": long_pw, "_csrf_token": CSRF})
    assert r.status_code in (302, 303)


def test_password_inputs_have_no_maxlength(client, appmod):
    seed_password(appmod, "longpassword123", "global")
    set_csrf(client)
    client.post("/login", data={"password": "longpassword123", "_csrf_token": CSRF})
    # auth page (unlock view also uses the same template) and settings page
    assert "maxlength" not in client.get("/login").get_data(as_text=True).lower()
    assert "maxlength" not in client.get("/settings").get_data(as_text=True).lower()
