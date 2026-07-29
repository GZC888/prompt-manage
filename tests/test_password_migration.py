import re

from tests.helpers import CSRF, seed_legacy_password, set_csrf


def _is_legacy_hex(s):
    return bool(re.fullmatch(r"[0-9a-f]{64}", s or ""))


def test_legacy_sha256_login_succeeds_and_migrates(client, appmod):
    raw = "1234"  # an old short password must still work
    seed_legacy_password(appmod, raw, "global")

    conn = appmod.get_db()
    assert _is_legacy_hex(appmod.get_setting(conn, "auth_password_hash"))
    conn.close()

    set_csrf(client)
    r = client.post("/login", data={"password": raw, "_csrf_token": CSRF})
    assert r.status_code in (302, 303)

    conn = appmod.get_db()
    new_hash = appmod.get_setting(conn, "auth_password_hash")
    conn.close()
    assert not _is_legacy_hex(new_hash)  # upgraded away from raw SHA-256
    assert ":" in new_hash  # werkzeug format, e.g. pbkdf2:sha256:...


def test_legacy_wrong_password_rejected(client, appmod):
    seed_legacy_password(appmod, "1234", "global")
    set_csrf(client)
    r = client.post("/login", data={"password": "9999", "_csrf_token": CSRF})
    assert r.status_code == 200
    with client.session_transaction() as sess:
        assert not sess.get("auth_ok")


def test_verify_password_helpers(appmod):
    h = appmod.hash_password("a-strong-passphrase")
    assert appmod.verify_password("a-strong-passphrase", h)
    assert not appmod.verify_password("wrong", h)
    import hashlib
    legacy = hashlib.sha256(b"abcd").hexdigest()
    assert appmod.verify_password("abcd", legacy)
    assert not appmod.verify_password("abce", legacy)


def test_verify_password_rejects_unsupported_cost_parameters(appmod):
    # Do not invoke Werkzeug's KDF parser for hashes outside the application
    # allowlist; a hand-edited legacy DB must not become a login-time CPU sink.
    expensive = "pbkdf2:sha256:99999999$abcdefgh$" + ("0" * 64)
    assert not appmod.verify_password("anything", expensive)
