"""Every page renders, and the request-scoped connection survives rendering."""

import pytest

from .helpers import CSRF, create_prompt, login, set_csrf


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok" and body["initialized"] is True
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/", "/prompt/new", "/settings", "/export"])
def test_core_pages_render(client, appmod, path):
    create_prompt(appmod, name="Alpha", content="hello world", tags=["work"])
    assert client.get(path).status_code == 200


def test_prompt_pages_render(client, appmod):
    prompt_id = create_prompt(appmod, name="Alpha", content="one\ntwo", versions=3)
    for path in (
        f"/prompt/{prompt_id}",
        f"/prompt/{prompt_id}?mode=edit",
        f"/prompt/{prompt_id}/versions",
        f"/prompt/{prompt_id}/diff",
    ):
        assert client.get(path).status_code == 200, path


def test_missing_prompt_renders_404_page(client):
    response = client.get("/prompt/9999")
    assert response.status_code == 404
    assert b"404" in response.data


def test_api_404_is_json(client):
    response = client.get("/api/prompt/9999/content")
    assert response.status_code == 404
    assert response.get_json()["status"] == "not_found"


def test_logo_and_favicon(client):
    for path in ("/logo.png", "/favicon.ico"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.mimetype == "image/png"


def test_security_headers(client):
    headers = client.get("/").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "private, no-store"


def test_context_processor_still_has_a_live_connection(client, appmod, caplog):
    """Regression: handlers used to close the request connection before render.

    Context processors query the database while the template renders, so the
    close made them fail silently and the header lost its authenticated state.
    """
    login(client, appmod, "password123")
    with caplog.at_level("ERROR"):
        response = client.get("/")
    assert response.status_code == 200
    assert b"/logout" in response.data  # only rendered when is_authenticated is true
    assert "Cannot operate on a closed database" not in caplog.text


def test_empty_library_while_authenticated(client, appmod, caplog):
    """The same regression with zero rows, where nothing warms the g cache."""
    login(client, appmod, "password123")
    with caplog.at_level("ERROR"):
        response = client.get("/")
    assert b"/logout" in response.data
    assert caplog.text == ""


def test_index_partial_returns_only_the_library(client, appmod):
    create_prompt(appmod, name="Alpha")
    response = client.get("/", headers={"X-Partial": "library"})
    assert response.status_code == 200
    assert b"libraryShell" in response.data
    assert b"<!DOCTYPE html>" not in response.data


def test_toggle_pin_and_archive(client, appmod):
    prompt_id = create_prompt(appmod)
    set_csrf(client)
    for path in (f"/prompt/{prompt_id}/pin", f"/prompt/{prompt_id}/archive"):
        response = client.post(path, data={"_csrf_token": CSRF})
        assert response.status_code == 302
    from .helpers import prompt_row

    row = prompt_row(appmod, prompt_id)
    assert row["pinned"] == 1 and row["archived_at"]


def test_toggle_returns_json_for_xhr(client, appmod):
    prompt_id = create_prompt(appmod)
    set_csrf(client)
    response = client.post(
        f"/prompt/{prompt_id}/pin",
        data={"_csrf_token": CSRF},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert response.get_json() == {"status": "ok", "column": "pinned", "enabled": True}
