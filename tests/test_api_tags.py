from tests.helpers import create_prompt, seed_password, unlock


def test_api_tags_hides_locked_tags(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    create_prompt(appmod, "Pub", "c", tags=["publictag"])
    create_prompt(appmod, "Sec", "c", require_password=1, tags=["secrettag"])

    tags = client.get("/api/tags").get_json()
    assert "publictag" in tags
    assert "secrettag" not in tags


def test_api_tags_reveals_after_unlock(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    pid = create_prompt(appmod, "Sec", "c", require_password=1, tags=["secrettag"])
    unlock(client, pid)
    tags = client.get("/api/tags").get_json()
    assert "secrettag" in tags


def test_api_search_marks_locked(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    create_prompt(appmod, "LockedSearch", "SECRET", require_password=1)
    results = client.get("/api/search?q=LockedSearch").get_json()
    assert len(results) == 1
    assert results[0]["locked"] is True
    assert "SECRET" not in str(results)
