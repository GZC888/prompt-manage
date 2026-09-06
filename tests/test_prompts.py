"""Creating, editing, versioning and deleting prompts."""

from .helpers import (
    CSRF, create_prompt, current_content, prompt_row, set_csrf, set_setting,
)


def _form(**overrides):
    data = {"_csrf_token": CSRF, "name": "Alpha", "content": "body", "bump_kind": "patch"}
    data.update(overrides)
    return data


def test_create_prompt_stores_first_version(client, appmod):
    set_csrf(client)
    response = client.post("/prompt/new", data=_form(tags="a, b, a"))
    assert response.status_code == 302
    prompt_id = int(response.headers["Location"].rsplit("/", 1)[1])
    row = prompt_row(appmod, prompt_id)
    assert row["name"] == "Alpha"
    assert appmod.tags_from_row(row) == ["a", "b"]  # duplicates collapse
    assert current_content(appmod, prompt_id) == "body"


def test_create_requires_content(client):
    set_csrf(client)
    response = client.post("/prompt/new", data=_form(content="   "), follow_redirects=True)
    assert "请输入提示词内容" in response.get_data(as_text=True)


def test_blank_name_falls_back(client, appmod):
    set_csrf(client)
    response = client.post("/prompt/new", data=_form(name="   "))
    prompt_id = int(response.headers["Location"].rsplit("/", 1)[1])
    assert prompt_row(appmod, prompt_id)["name"] == "未命名提示词"


def test_edit_without_new_version_overwrites_current(client, appmod):
    prompt_id = create_prompt(appmod, content="old")
    set_csrf(client)
    client.post(f"/prompt/{prompt_id}", data=_form(content="new"))
    assert current_content(appmod, prompt_id) == "new"
    conn = appmod.connect(appmod.app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) c FROM versions").fetchone()["c"] == 1
    conn.close()


def test_edit_with_new_version_appends(client, appmod):
    prompt_id = create_prompt(appmod, content="old")
    set_csrf(client)
    client.post(f"/prompt/{prompt_id}", data=_form(content="new", do_save_version="1"))
    conn = appmod.connect(appmod.app.config["DB_PATH"])
    rows = conn.execute(
        "SELECT version, content, parent_version_id FROM versions ORDER BY id"
    ).fetchall()
    conn.close()
    assert [row["version"] for row in rows] == ["1.0.0", "1.0.1"]
    assert rows[1]["parent_version_id"] == 1  # linked to the version it replaced


def test_bump_kinds(appmod):
    assert appmod.bump_version("1.2.3", "patch") == "1.2.4"
    assert appmod.bump_version("1.2.3", "minor") == "1.3.0"
    assert appmod.bump_version("1.2.3", "major") == "2.0.0"
    assert appmod.bump_version(None) == "1.0.0"
    assert appmod.bump_version("not-a-version") == "1.0.0"


def test_rollback_creates_a_new_version(client, appmod):
    prompt_id = create_prompt(appmod, content="latest", versions=3)
    conn = appmod.connect(appmod.app.config["DB_PATH"])
    oldest = conn.execute(
        "SELECT id, content FROM versions WHERE prompt_id=? ORDER BY id LIMIT 1", (prompt_id,)
    ).fetchone()
    conn.close()
    set_csrf(client)
    response = client.post(
        f"/prompt/{prompt_id}/rollback/{oldest['id']}", data={"_csrf_token": CSRF}
    )
    assert response.status_code == 302
    assert current_content(appmod, prompt_id) == oldest["content"]


def test_rollback_rejects_a_foreign_version(client, appmod):
    first = create_prompt(appmod, name="A")
    second = create_prompt(appmod, name="B")
    conn = appmod.connect(appmod.app.config["DB_PATH"])
    foreign = conn.execute(
        "SELECT id FROM versions WHERE prompt_id=?", (second,)
    ).fetchone()["id"]
    conn.close()
    set_csrf(client)
    response = client.post(
        f"/prompt/{first}/rollback/{foreign}", data={"_csrf_token": CSRF}, follow_redirects=True
    )
    assert "版本不存在" in response.get_data(as_text=True)


def test_delete_removes_prompt_and_versions(client, appmod):
    prompt_id = create_prompt(appmod, versions=3)
    set_csrf(client)
    client.post(f"/prompt/{prompt_id}/delete", data={"_csrf_token": CSRF})
    conn = appmod.connect(appmod.app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) c FROM prompts").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM versions").fetchone()["c"] == 0
    conn.close()


def test_prune_keeps_threshold_and_protects_current(client, appmod):
    set_setting(appmod, "version_cleanup_threshold", "3")
    prompt_id = create_prompt(appmod, content="v", versions=6)
    set_csrf(client)
    client.post(f"/prompt/{prompt_id}", data=_form(content="newest", do_save_version="1"))
    conn = appmod.connect(appmod.app.config["DB_PATH"])
    rows = conn.execute(
        "SELECT id, parent_version_id FROM versions WHERE prompt_id=?", (prompt_id,)
    ).fetchall()
    current = conn.execute(
        "SELECT current_version_id FROM prompts WHERE id=?", (prompt_id,)
    ).fetchone()["current_version_id"]
    conn.close()
    assert len(rows) == 3
    assert current in {row["id"] for row in rows}
    # Survivors may only point at other survivors, so the graph stays exportable.
    alive = {row["id"] for row in rows}
    assert all(row["parent_version_id"] in alive or row["parent_version_id"] is None for row in rows)


def test_diff_highlights_changes(client, appmod):
    prompt_id = create_prompt(appmod, content="hello there", versions=2)
    conn = appmod.connect(appmod.app.config["DB_PATH"])
    ids = [row["id"] for row in conn.execute(
        "SELECT id FROM versions WHERE prompt_id=? ORDER BY id", (prompt_id,)
    ).fetchall()]
    conn.close()
    response = client.get(f"/prompt/{prompt_id}/diff?left={ids[0]}&right={ids[1]}")
    assert response.status_code == 200
    assert b"diff-table" in response.data


def test_diff_escapes_content(appmod):
    html = appmod.word_diff_html("<script>alpha</script>", "<script>beta</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_color_is_normalised(client, appmod):
    set_csrf(client)
    response = client.post("/prompt/new", data=_form(color="#ABC"))
    prompt_id = int(response.headers["Location"].rsplit("/", 1)[1])
    assert prompt_row(appmod, prompt_id)["color"] == "#aabbcc"


def test_invalid_color_is_dropped(client, appmod):
    set_csrf(client)
    response = client.post("/prompt/new", data=_form(color="javascript:alert(1)"))
    prompt_id = int(response.headers["Location"].rsplit("/", 1)[1])
    assert prompt_row(appmod, prompt_id)["color"] is None
