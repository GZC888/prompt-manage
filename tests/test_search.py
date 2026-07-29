from tests.helpers import create_prompt, seed_password, unlock


def test_search_does_not_match_locked_content(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    create_prompt(appmod, "Visible", "hello world")
    create_prompt(appmod, "Hidden", "SECRETMARKER inside", require_password=1)

    # Query a word that only appears inside the locked prompt's content. The
    # query echoes back in the search box, so we assert on the *secret body* and
    # the locked prompt's name instead — neither may surface.
    html = client.get("/?q=inside").get_data(as_text=True)
    assert "SECRETMARKER" not in html
    # The locked prompt must not surface when matched only by its content.
    assert "Hidden" not in html


def test_search_by_name_shows_locked_card_without_content(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    create_prompt(appmod, "HiddenName", "SECRETMARKER inside", require_password=1)

    html = client.get("/?q=HiddenName").get_data(as_text=True)
    assert "HiddenName" in html          # name is public (shown on locked card)
    assert "SECRETMARKER" not in html    # content never leaks


def test_search_matches_unlocked_content(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    pid = create_prompt(appmod, "Hidden", "FINDABLE after unlock", require_password=1)
    unlock(client, pid)
    html = client.get("/?q=FINDABLE").get_data(as_text=True)
    assert "Hidden" in html


def test_locked_card_has_no_data_content_attribute(client, appmod):
    seed_password(appmod, "longpassword123", "per")
    create_prompt(appmod, "Hidden", "DATACONTENTLEAK", require_password=1)
    html = client.get("/").get_data(as_text=True)
    assert "DATACONTENTLEAK" not in html
    assert "data-content" not in html or "DATACONTENTLEAK" not in html
