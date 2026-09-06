"""The small JSON endpoints."""

from .helpers import CSRF, create_prompt, seed_password, set_csrf


def test_tags_are_unique_and_sorted(client, appmod):
    create_prompt(appmod, name="A", tags=["beta", "alpha"])
    create_prompt(appmod, name="B", tags=["alpha", "gamma"])
    assert client.get("/api/tags").get_json() == ["alpha", "beta", "gamma"]


def test_content_endpoint_returns_current_version(client, appmod):
    prompt_id = create_prompt(appmod, content="latest", versions=2)
    assert client.get(f"/api/prompt/{prompt_id}/content").get_json()["content"] == "latest"


def test_content_endpoint_can_target_a_version(client, appmod):
    prompt_id = create_prompt(appmod, content="latest", versions=2)
    conn = appmod.connect(appmod.app.config["DB_PATH"])
    first = conn.execute(
        "SELECT id, content FROM versions WHERE prompt_id=? ORDER BY id LIMIT 1", (prompt_id,)
    ).fetchone()
    conn.close()
    body = client.get(f"/api/prompt/{prompt_id}/content?version_id={first['id']}").get_json()
    assert body["content"] == first["content"]


def test_content_endpoint_rejects_a_foreign_version(client, appmod):
    first = create_prompt(appmod, name="A")
    second = create_prompt(appmod, name="B", content="secret")
    conn = appmod.connect(appmod.app.config["DB_PATH"])
    foreign = conn.execute("SELECT id FROM versions WHERE prompt_id=?", (second,)).fetchone()["id"]
    conn.close()
    assert client.get(f"/api/prompt/{first}/content?version_id={foreign}").status_code == 404


def test_search_endpoint(client, appmod):
    create_prompt(appmod, name="Alpha", content="needle", source="docs")
    create_prompt(appmod, name="Beta", content="other")
    results = client.get("/api/search?q=needle").get_json()
    assert [r["name"] for r in results] == ["Alpha"]
    assert results[0]["source"] == "docs"


def test_search_skips_archived(client, appmod):
    create_prompt(appmod, name="Archived", content="needle", archived=True)
    assert client.get("/api/search?q=needle").get_json() == []


def test_search_is_capped(client, appmod):
    for index in range(25):
        create_prompt(appmod, name=f"P{index}", content="needle")
    assert len(client.get("/api/search?q=needle").get_json()) == 20


def test_search_post_requires_csrf(client, appmod):
    create_prompt(appmod, name="Alpha", content="needle")
    assert client.post("/api/search", data={"q": "needle"}).status_code == 403
    set_csrf(client)
    response = client.post("/api/search", data={"q": "needle", "_csrf_token": CSRF})
    assert [r["name"] for r in response.get_json()] == ["Alpha"]


def test_apis_are_closed_when_a_password_is_set(client, appmod):
    create_prompt(appmod, name="Alpha")
    seed_password(appmod, "password123", "global")
    for path in ("/api/tags", "/api/search?q=a", "/api/prompt/1/content"):
        assert client.get(path).status_code == 302, path
