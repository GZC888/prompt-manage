from tests.helpers import CSRF, create_prompt, set_csrf


def test_post_without_csrf_is_rejected(client, appmod):
    r = client.post("/prompt/new", data={"name": "a", "content": "b"})
    assert r.status_code == 403


def test_post_with_bad_csrf_is_rejected(client, appmod):
    set_csrf(client, "realtoken")
    r = client.post("/prompt/new", data={"name": "a", "content": "b", "_csrf_token": "wrongtoken"})
    assert r.status_code == 403


def test_post_with_csrf_succeeds(client, appmod):
    set_csrf(client)
    r = client.post("/prompt/new", data={"name": "a", "content": "b", "_csrf_token": CSRF})
    assert r.status_code in (302, 303)


def test_csrf_via_header(client, appmod):
    pid = create_prompt(appmod, "P", "c")
    set_csrf(client)
    r = client.post(f"/prompt/{pid}/copied", headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 200


def test_healthz_exempt_from_csrf(client):
    # healthz is GET-only, but ensure it never requires a token.
    assert client.get("/healthz").status_code == 200
