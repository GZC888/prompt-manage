"""Small JSON endpoints used by the command palette, tag editor and copy buttons."""

from flask import abort, jsonify, request

from ..db import get_db
from ..routing import route
from ..utils import parse_int_or_none, tags_from_row

SEARCH_LIMIT = 20
MAX_QUERY_LENGTH = 256


@route("/api/tags")
def api_tags():
    conn = get_db()
    tags = set()
    for row in conn.execute("SELECT id, tags FROM prompts").fetchall():
        tags.update(tags_from_row(row))
    return jsonify(sorted(tags))


@route("/api/prompt/<int:prompt_id>/content")
def api_prompt_content(prompt_id):
    """Return current or historical content on demand, for copy actions."""
    conn = get_db()
    prompt = conn.execute(
        "SELECT current_version_id FROM prompts WHERE id=?", (prompt_id,)
    ).fetchone()
    if not prompt:
        abort(404)
    version_id = parse_int_or_none(request.args.get("version_id"))
    if version_id is None:
        version_id = prompt["current_version_id"]
    if version_id is None:
        return jsonify({"content": ""})
    version = conn.execute(
        "SELECT content FROM versions WHERE id=? AND prompt_id=?", (version_id, prompt_id)
    ).fetchone()
    if not version:
        abort(404)
    return jsonify({"content": version["content"]})


@route("/api/search", methods=["GET", "POST"])
def api_search():
    raw = request.form.get("q") if request.method == "POST" else request.args.get("q")
    needle = (raw or "").strip()[:MAX_QUERY_LENGTH].lower()
    conn = get_db()
    rows = conn.execute(
        "SELECT p.id, p.name, p.source, p.notes, p.tags, v.content AS current_content "
        "FROM prompts p LEFT JOIN versions v "
        "ON v.id = p.current_version_id AND v.prompt_id = p.id "
        "WHERE p.archived_at IS NULL "
        "ORDER BY p.pinned DESC, p.updated_at DESC"
    ).fetchall()
    results = []
    for row in rows:
        name = row["name"] or ""
        if needle:
            haystack = " ".join([
                name, row["source"] or "", row["notes"] or "",
                " ".join(tags_from_row(row)), row["current_content"] or "",
            ]).lower()
            if needle not in haystack:
                continue
        results.append({"id": row["id"], "name": name, "source": row["source"] or ""})
        if len(results) >= SEARCH_LIMIT:
            break
    return jsonify(results)
