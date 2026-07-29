from tests.helpers import create_prompt


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["status"] == "ok"
    assert payload["build_sha"] == "dev"
    assert payload["initialized"] is True


def test_index_ok(client):
    assert client.get("/").status_code == 200


def test_index_renders_prompt(client, appmod):
    create_prompt(appmod, "Hello", "world content")
    html = client.get("/").get_data(as_text=True)
    assert "Hello" in html


def test_security_headers_present(client):
    r = client.get("/")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "Content-Security-Policy" in r.headers


def test_static_css_served(client):
    assert client.get("/static/css/style.css").status_code == 200
