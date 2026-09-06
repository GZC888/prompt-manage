"""Listing: search, scopes, facets and ordering."""

from .helpers import create_prompt


def names(response):
    """Prompt titles in render order."""
    import re

    body = response.get_data(as_text=True)
    return re.findall(r'class="prompt-card-title">\s*<a href="/prompt/\d+">([^<]+)</a>', body)


def test_search_matches_every_visible_field(client, appmod):
    create_prompt(appmod, name="Alpha", content="needle inside", source="src", notes="note")
    create_prompt(appmod, name="Beta", content="nothing", tags=["needle-tag"])
    create_prompt(appmod, name="Gamma", content="nothing")
    assert set(names(client.get("/?q=needle"))) == {"Alpha", "Beta"}
    assert names(client.get("/?q=Gamma")) == ["Gamma"]
    assert names(client.get("/?q=src")) == ["Alpha"]
    assert names(client.get("/?q=note")) == ["Alpha"]


def test_search_is_case_insensitive(client, appmod):
    create_prompt(appmod, name="Alpha", content="MixedCase")
    assert names(client.get("/?q=mixedcase")) == ["Alpha"]


def test_scopes(client, appmod):
    create_prompt(appmod, name="Normal")
    create_prompt(appmod, name="Pinned", pinned=1)
    create_prompt(appmod, name="Archived", archived=True)
    assert set(names(client.get("/"))) == {"Normal", "Pinned"}
    assert names(client.get("/?view=pinned")) == ["Pinned"]
    assert names(client.get("/?view=archived")) == ["Archived"]


def test_unknown_scope_falls_back_to_all(client, appmod):
    create_prompt(appmod, name="Normal")
    assert names(client.get("/?view=nonsense")) == ["Normal"]


def test_pinned_prompts_sort_first(client, appmod):
    create_prompt(appmod, name="Zeta")
    create_prompt(appmod, name="Alpha", pinned=1)
    assert names(client.get("/?sort=name"))[0] == "Alpha"


def test_sorting_by_name(client, appmod):
    for name in ("Charlie", "alpha", "Bravo"):
        create_prompt(appmod, name=name)
    assert names(client.get("/?sort=name")) == ["alpha", "Bravo", "Charlie"]


def test_tag_and_source_facets(client, appmod):
    create_prompt(appmod, name="Alpha", tags=["work"], source="docs")
    create_prompt(appmod, name="Beta", tags=["home"], source="docs")
    create_prompt(appmod, name="Gamma", tags=["work", "home"])
    assert set(names(client.get("/?tag=work"))) == {"Alpha", "Gamma"}
    assert set(names(client.get("/?tag=work&tag=home"))) == {"Alpha", "Beta", "Gamma"}
    assert set(names(client.get("/?source=docs"))) == {"Alpha", "Beta"}
    assert names(client.get("/?tag=work&source=docs")) == ["Alpha"]


def test_facets_combine_with_search(client, appmod):
    create_prompt(appmod, name="Alpha", tags=["work"], content="needle")
    create_prompt(appmod, name="Beta", tags=["work"], content="other")
    assert names(client.get("/?tag=work&q=needle")) == ["Alpha"]


def test_archived_prompts_stay_out_of_search(client, appmod):
    create_prompt(appmod, name="Archived", content="needle", archived=True)
    assert names(client.get("/?q=needle")) == []


def test_scope_counts_are_rendered(client, appmod):
    create_prompt(appmod, name="A", pinned=1)
    create_prompt(appmod, name="B", archived=True)
    body = client.get("/").get_data(as_text=True)
    assert 'href="/?view=pinned"' in body
    assert 'href="/?view=archived"' in body


def test_long_query_is_truncated_not_rejected(client, appmod):
    create_prompt(appmod, name="Alpha")
    assert client.get("/?q=" + "x" * 5000).status_code == 200


def test_empty_state_offers_a_reset(client, appmod):
    create_prompt(appmod, name="Alpha")
    body = client.get("/?q=nothing-matches").get_data(as_text=True)
    assert "没有符合条件的提示词" in body
