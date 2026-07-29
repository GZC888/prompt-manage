import pytest

from tests.helpers import CSRF, create_prompt, login, seed_password, set_csrf, unlock


@pytest.fixture
def locked_pid(appmod):
    seed_password(appmod, "longpassword123", "per")
    return create_prompt(appmod, "Secret", "TOPSECRETBODY", require_password=1)


def test_locked_detail_redirects_to_unlock(client, locked_pid):
    r = client.get(f"/prompt/{locked_pid}")
    assert r.status_code in (302, 303)
    assert "unlock" in r.headers["Location"]


def test_locked_detail_does_not_leak_content(client, locked_pid):
    r = client.get(f"/prompt/{locked_pid}", follow_redirects=True)
    assert "TOPSECRETBODY" not in r.get_data(as_text=True)


def test_locked_edit_forbidden(client, locked_pid):
    set_csrf(client)
    r = client.post(f"/prompt/{locked_pid}", data={"name": "x", "content": "y", "_csrf_token": CSRF})
    assert r.status_code == 403


def test_locked_delete_forbidden(client, locked_pid):
    set_csrf(client)
    r = client.post(f"/prompt/{locked_pid}/delete", data={"_csrf_token": CSRF})
    assert r.status_code == 403


def test_locked_pin_forbidden(client, locked_pid):
    set_csrf(client)
    r = client.post(f"/prompt/{locked_pid}/pin", data={"_csrf_token": CSRF})
    assert r.status_code == 403


def test_locked_favorite_forbidden(client, locked_pid):
    set_csrf(client)
    r = client.post(f"/prompt/{locked_pid}/favorite", data={"_csrf_token": CSRF})
    assert r.status_code == 403


def test_locked_archive_forbidden(client, locked_pid):
    set_csrf(client)
    r = client.post(f"/prompt/{locked_pid}/archive", data={"_csrf_token": CSRF})
    assert r.status_code == 403


def test_locked_rollback_forbidden(client, locked_pid):
    set_csrf(client)
    r = client.post(f"/prompt/{locked_pid}/rollback/1", data={"bump_kind": "patch", "_csrf_token": CSRF})
    assert r.status_code == 403


def test_locked_versions_redirects(client, locked_pid):
    r = client.get(f"/prompt/{locked_pid}/versions")
    assert r.status_code in (302, 303)
    assert "unlock" in r.headers["Location"]


def test_locked_diff_redirects(client, locked_pid):
    r = client.get(f"/prompt/{locked_pid}/diff")
    assert r.status_code in (302, 303)
    assert "unlock" in r.headers["Location"]


def test_unlocked_prompt_is_accessible(client, locked_pid):
    unlock(client, locked_pid)
    r = client.get(f"/prompt/{locked_pid}")
    assert r.status_code == 200
    assert "TOPSECRETBODY" in r.get_data(as_text=True)


def test_forged_unlocked_prompt_session_is_ignored(client, locked_pid):
    with client.session_transaction() as sess:
        sess["unlocked_prompts"] = [locked_pid]
    r = client.get(f"/prompt/{locked_pid}")
    assert r.status_code in (302, 303)
    assert "unlock" in r.headers["Location"]


def test_unlock_route_stores_server_side_unlock(client, locked_pid):
    set_csrf(client)
    r = client.post(
        f"/prompt/{locked_pid}/unlock",
        data={"password": "longpassword123", "_csrf_token": CSRF},
    )
    assert r.status_code in (302, 303)
    assert client.get(f"/prompt/{locked_pid}").status_code == 200


def test_unlocked_prompt_still_requires_owner_login_to_edit(client, locked_pid):
    unlock(client, locked_pid)
    set_csrf(client)
    r = client.post(f"/prompt/{locked_pid}", data={
        "name": "Secret", "content": "updated body", "do_save_version": "1",
        "bump_kind": "patch", "require_password": "1", "_csrf_token": CSRF,
    })
    assert r.status_code == 403


def test_logged_in_owner_can_edit_locked_prompt(client, appmod, locked_pid):
    login(client, appmod, "longpassword123", mode="per")
    set_csrf(client)
    r = client.post(f"/prompt/{locked_pid}", data={
        "name": "Secret", "content": "updated body", "do_save_version": "1",
        "bump_kind": "patch", "require_password": "1", "_csrf_token": CSRF,
    })
    assert r.status_code in (302, 303)


def test_settings_requires_auth_when_password_set(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    r = client.get("/settings")
    assert r.status_code in (302, 303)
    assert "/login" in r.headers["Location"]


def test_settings_open_when_no_password(client, appmod):
    assert client.get("/settings").status_code == 200


def test_protected_view_lists_locked_items_without_content(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    visible = create_prompt(appmod, "UnlockedSecret", "A", require_password=1)
    create_prompt(appmod, "StillLockedSecret", "B", require_password=1)
    unlock(client, visible)
    html = client.get("/?view=locked").get_data(as_text=True)
    assert "UnlockedSecret" in html
    assert "StillLockedSecret" in html
    assert ">B<" not in html
