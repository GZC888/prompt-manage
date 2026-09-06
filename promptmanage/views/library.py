"""The prompt library: listing, reading, editing, versions and diffs."""

import difflib
import json
import logging
import re
import sqlite3

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape

from .. import db_busy_response
from ..db import get_db, get_setting, is_locked_error
from ..routing import route
from ..utils import (
    bump_version, now_ts, parse_tags, safe_referrer, sanitize_color, tags_from_row,
    wants_json,
)

log = logging.getLogger("prompt_manage")

PREVIEW_LEN = 220
MAX_SEARCH_QUERY_LENGTH = 256
MAX_NAME_LENGTH = 200
SCOPES = ("all", "pinned", "archived")
SORTS = ("updated", "created", "name", "tags")
_EMPTY_SOURCE = "(empty)"

_CARD_QUERY = """
    SELECT p.id, p.name, p.source, p.notes, p.color, p.tags, p.pinned,
           p.archived_at, p.created_at, p.updated_at,
           v.content AS current_content, v.version AS current_version
    FROM prompts p
    LEFT JOIN versions v ON v.id = p.current_version_id AND v.prompt_id = p.id
"""


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def _norm_source(value):
    return (value or "").strip() or _EMPTY_SOURCE


def _build_card(row):
    content = row["current_content"] or ""
    return {
        "id": row["id"],
        "name": row["name"],
        "source": row["source"],
        "notes": row["notes"],
        "color": row["color"],
        "tags": tags_from_row(row),
        "pinned": bool(row["pinned"]),
        "archived": bool(row["archived_at"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "current_version": row["current_version"],
        "has_content": bool(content),
        "length": len(content),
        "preview": content[:PREVIEW_LEN],
        "truncated": len(content) > PREVIEW_LEN,
    }


def _multi_value(values, singular, plural):
    """Read a repeated query parameter, tolerating a comma-separated fallback."""
    selected = [item.strip() for item in values.getlist(singular) if item.strip()]
    if selected:
        return selected
    raw = values.get(plural, "")
    return [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]


@route("/", methods=["GET"])
def index():
    conn = get_db()
    values = request.args
    query = values.get("q", "").strip()[:MAX_SEARCH_QUERY_LENGTH]
    sort = values.get("sort", "updated")
    if sort not in SORTS:
        sort = "updated"
    view = values.get("view", "all")
    if view not in SCOPES:
        view = "all"
    selected_tags = _multi_value(values, "tag", "tags")
    selected_sources = _multi_value(values, "source", "sources")

    rows = conn.execute(_CARD_QUERY).fetchall()

    needle = query.lower()
    scope_counts = {"all": 0, "pinned": 0, "archived": 0}
    tag_counts, source_counts = {}, {}
    cards = []
    for row in rows:
        archived = bool(row["archived_at"])
        row_tags = tags_from_row(row)
        if archived:
            scope_counts["archived"] += 1
        else:
            scope_counts["all"] += 1
            if row["pinned"]:
                scope_counts["pinned"] += 1

        if view == "archived":
            if not archived:
                continue
        else:
            if archived:
                continue
            if view == "pinned" and not row["pinned"]:
                continue

        if needle:
            haystack = " ".join([
                row["name"] or "", row["source"] or "", row["notes"] or "",
                " ".join(row_tags), row["current_content"] or "",
            ]).lower()
            if needle not in haystack:
                continue

        # Facet counts describe the current scope+search, before facet filters,
        # so unchecking a box always brings its results back.
        for tag in row_tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        source = _norm_source(row["source"])
        source_counts[source] = source_counts.get(source, 0) + 1

        if selected_tags and not any(tag in row_tags for tag in selected_tags):
            continue
        if selected_sources and source not in selected_sources:
            continue
        cards.append(_build_card(row))

    if sort == "name":
        cards.sort(key=lambda card: ((card["name"] or "").casefold(), card["id"]))
    elif sort == "tags":
        cards.sort(key=lambda card: (
            tuple(tag.casefold() for tag in card["tags"]), (card["name"] or "").casefold(), card["id"]
        ))
    elif sort == "created":
        cards.sort(key=lambda card: (card["created_at"] or "", card["id"]), reverse=True)
    else:
        cards.sort(key=lambda card: (card["updated_at"] or "", card["id"]), reverse=True)
    cards.sort(key=lambda card: not card["pinned"])

    template = "_library.html" if request.headers.get("X-Partial") == "library" else "index.html"
    return render_template(
        template,
        cards=cards, q=query, sort=sort, view=view,
        tag_counts=tag_counts, source_counts=source_counts, scope_counts=scope_counts,
        selected_tags=selected_tags, selected_sources=selected_sources,
    )


# ---------------------------------------------------------------------------
# Create / edit
# ---------------------------------------------------------------------------
def _read_prompt_form():
    """Collect and normalize the editor form. Returns (fields, error)."""
    content = request.form.get("content", "")
    if not content.strip():
        return None, "请输入提示词内容"
    name = request.form.get("name", "").strip()[:MAX_NAME_LENGTH] or "未命名提示词"
    return {
        "name": name,
        "source": request.form.get("source", "").strip()[:MAX_NAME_LENGTH],
        "notes": request.form.get("notes", "").strip(),
        "color": sanitize_color(request.form.get("color")),
        "tags": json.dumps(parse_tags(request.form.get("tags", "")), ensure_ascii=False),
        "content": content,
        "bump_kind": request.form.get("bump_kind", "patch"),
    }, None


def _fetch_prompt(conn, prompt_id):
    prompt = conn.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
    if not prompt:
        abort(404)
    return prompt


@route("/prompt/new", methods=["GET", "POST"])
def new_prompt():
    if request.method == "GET":
        return render_template("prompt_detail.html", prompt=None, prompt_tags=[], current=None)

    fields, error = _read_prompt_form()
    if error:
        flash(error, "error")
        return redirect(url_for("new_prompt"))

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ts = now_ts()
        cur = conn.execute(
            "INSERT INTO prompts(name, source, notes, color, tags, pinned, created_at, updated_at) "
            "VALUES(?,?,?,?,?,0,?,?)",
            (fields["name"], fields["source"], fields["notes"], fields["color"], fields["tags"], ts, ts),
        )
        prompt_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) "
            "VALUES(?,?,?,?,NULL)",
            (prompt_id, bump_version(None, fields["bump_kind"]), fields["content"], ts),
        )
        conn.execute(
            "UPDATE prompts SET current_version_id=? WHERE id=?", (cur.lastrowid, prompt_id)
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        if is_locked_error(exc):
            return db_busy_response(url_for("new_prompt"))
        log.exception("create prompt failed")
        flash("创建失败，请重试", "error")
        return redirect(url_for("new_prompt"))
    flash("已创建提示词并保存首个版本", "success")
    return redirect(url_for("prompt_detail", prompt_id=prompt_id))


@route("/prompt/<int:prompt_id>", methods=["GET", "POST"])
def prompt_detail(prompt_id):
    conn = get_db()
    if request.method == "GET":
        prompt = _fetch_prompt(conn, prompt_id)
        current = (
            conn.execute(
                "SELECT * FROM versions WHERE id=? AND prompt_id=?",
                (prompt["current_version_id"], prompt_id),
            ).fetchone()
            if prompt["current_version_id"] else None
        )
        version_count = conn.execute(
            "SELECT COUNT(*) AS c FROM versions WHERE prompt_id=?", (prompt_id,)
        ).fetchone()["c"]
        return render_template(
            "prompt_detail.html", prompt=dict(prompt), prompt_tags=tags_from_row(prompt),
            current=current, version_count=version_count,
        )

    fields, error = _read_prompt_form()
    if error:
        flash(error, "error")
        return redirect(url_for("prompt_detail", prompt_id=prompt_id, mode="edit"))
    save_new_version = request.form.get("do_save_version") == "1"

    try:
        conn.execute("BEGIN IMMEDIATE")
        prompt = conn.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        if not prompt:
            conn.rollback()
            abort(404)
        ts = now_ts()
        conn.execute(
            "UPDATE prompts SET name=?, source=?, notes=?, color=?, tags=?, updated_at=? WHERE id=?",
            (fields["name"], fields["source"], fields["notes"], fields["color"],
             fields["tags"], ts, prompt_id),
        )
        if save_new_version:
            row = conn.execute(
                "SELECT version FROM versions WHERE id=?", (prompt["current_version_id"],)
            ).fetchone()
            cur = conn.execute(
                "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) "
                "VALUES(?,?,?,?,?)",
                (prompt_id, bump_version(row["version"] if row else None, fields["bump_kind"]),
                 fields["content"], ts, prompt["current_version_id"]),
            )
            conn.execute(
                "UPDATE prompts SET current_version_id=? WHERE id=?", (cur.lastrowid, prompt_id)
            )
            prune_versions(conn, prompt_id)
        else:
            _overwrite_current_version(conn, prompt, fields["content"], ts)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        if is_locked_error(exc):
            return db_busy_response(url_for("prompt_detail", prompt_id=prompt_id))
        log.exception("save prompt failed id=%s", prompt_id)
        flash("保存失败，请重试", "error")
        return redirect(url_for("prompt_detail", prompt_id=prompt_id, mode="edit"))
    flash("已保存", "success")
    return redirect(url_for("prompt_detail", prompt_id=prompt_id))


def _overwrite_current_version(conn, prompt, content, ts):
    """Update the current version in place, recreating it if it went missing."""
    current_id = prompt["current_version_id"]
    if current_id:
        cur = conn.execute(
            "UPDATE versions SET content=? WHERE id=? AND prompt_id=?",
            (content, current_id, prompt["id"]),
        )
        if cur.rowcount:
            return
    cur = conn.execute(
        "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) "
        "VALUES(?,?,?,?,NULL)",
        (prompt["id"], "1.0.0", content, ts),
    )
    conn.execute(
        "UPDATE prompts SET current_version_id=? WHERE id=?", (cur.lastrowid, prompt["id"])
    )


# ---------------------------------------------------------------------------
# Version retention
# ---------------------------------------------------------------------------
def prune_versions(conn, prompt_id):
    """Trim a prompt's history to the configured retention count.

    Children can outlive a pruned parent, so survivors are rewired to their
    nearest retained ancestor before the deletes; that keeps the version graph
    acyclic and an exported bundle importable.
    """
    try:
        threshold = int(get_setting(conn, "version_cleanup_threshold", "200") or 200)
    except (TypeError, ValueError):
        threshold = 200
    if threshold < 1:
        threshold = 200

    current = conn.execute(
        "SELECT current_version_id FROM prompts WHERE id=?", (prompt_id,)
    ).fetchone()
    current_id = current["current_version_id"] if current else None
    rows = conn.execute(
        "SELECT id, parent_version_id FROM versions WHERE prompt_id=? "
        "ORDER BY created_at DESC, id DESC",
        (prompt_id,),
    ).fetchall()
    if len(rows) <= threshold:
        return

    doomed = {row["id"] for row in rows[threshold:]}
    if current_id in doomed:
        log.info("protected current version %s from pruning (prompt %s)", current_id, prompt_id)
        doomed.discard(current_id)
    if not doomed:
        return

    parents = {row["id"]: row["parent_version_id"] for row in rows}
    retained = set(parents) - doomed

    def nearest_retained(parent_id):
        seen = set()
        while parent_id in doomed:
            if parent_id in seen:
                return None
            seen.add(parent_id)
            parent_id = parents.get(parent_id)
        return parent_id if parent_id in retained else None

    for version_id in doomed:
        conn.execute(
            "UPDATE versions SET parent_version_id=? WHERE prompt_id=? AND parent_version_id=?",
            (nearest_retained(parents.get(version_id)), prompt_id, version_id),
        )
    conn.executemany("DELETE FROM versions WHERE id=?", [(vid,) for vid in doomed])


# ---------------------------------------------------------------------------
# Status toggles
# ---------------------------------------------------------------------------
_TOGGLE_COLUMNS = {"pinned", "archived_at"}


def _toggle(prompt_id, column, next_value, *, touch_updated=False):
    if column not in _TOGGLE_COLUMNS:  # never user input, but keep the invariant explicit
        raise ValueError(f"Invalid column name: {column}")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prompt = conn.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        if not prompt:
            conn.rollback()
            abort(404)
        value = next_value(prompt)
        if touch_updated:
            conn.execute(
                f"UPDATE prompts SET {column}=?, updated_at=? WHERE id=?",
                (value, now_ts(), prompt_id),
            )
        else:
            conn.execute(f"UPDATE prompts SET {column}=? WHERE id=?", (value, prompt_id))
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        if is_locked_error(exc):
            return db_busy_response(url_for("index"))
        log.exception("toggle failed id=%s column=%s", prompt_id, column)
        abort(500)
    if wants_json():
        return jsonify({"status": "ok", "column": column, "enabled": bool(value)})
    return redirect(safe_referrer(url_for("index")))


@route("/prompt/<int:prompt_id>/pin", methods=["POST"])
def toggle_pin(prompt_id):
    return _toggle(prompt_id, "pinned", lambda p: 0 if p["pinned"] else 1, touch_updated=True)


@route("/prompt/<int:prompt_id>/archive", methods=["POST"])
def toggle_archive(prompt_id):
    return _toggle(prompt_id, "archived_at", lambda p: None if p["archived_at"] else now_ts())


# ---------------------------------------------------------------------------
# Delete / rollback
# ---------------------------------------------------------------------------
@route("/prompt/<int:prompt_id>/delete", methods=["POST"])
def delete_prompt(prompt_id):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM prompts WHERE id=?", (prompt_id,)).fetchone():
            conn.rollback()
            abort(404)
        conn.execute("DELETE FROM versions WHERE prompt_id=?", (prompt_id,))
        conn.execute("DELETE FROM prompts WHERE id=?", (prompt_id,))
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        if is_locked_error(exc):
            return db_busy_response(url_for("prompt_detail", prompt_id=prompt_id))
        log.exception("delete prompt failed id=%s", prompt_id)
        flash("删除失败，请重试", "error")
        return redirect(url_for("prompt_detail", prompt_id=prompt_id))
    flash("已删除提示词及其所有版本", "success")
    return redirect(url_for("index"))


@route("/prompt/<int:prompt_id>/rollback/<int:version_id>", methods=["POST"])
def rollback_version(prompt_id, version_id):
    bump_kind = request.form.get("bump_kind", "patch")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prompt = conn.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        if not prompt:
            conn.rollback()
            abort(404)
        source = conn.execute(
            "SELECT content FROM versions WHERE id=? AND prompt_id=?", (version_id, prompt_id)
        ).fetchone()
        if not source:
            conn.rollback()
            flash("版本不存在", "error")
            return redirect(url_for("versions_page", prompt_id=prompt_id))
        current = conn.execute(
            "SELECT version FROM versions WHERE id=?", (prompt["current_version_id"],)
        ).fetchone()
        ts = now_ts()
        cur = conn.execute(
            "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) "
            "VALUES(?,?,?,?,?)",
            (prompt_id, bump_version(current["version"] if current else None, bump_kind),
             source["content"], ts, prompt["current_version_id"]),
        )
        conn.execute(
            "UPDATE prompts SET current_version_id=?, updated_at=? WHERE id=?",
            (cur.lastrowid, ts, prompt_id),
        )
        prune_versions(conn, prompt_id)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        if is_locked_error(exc):
            return db_busy_response(url_for("versions_page", prompt_id=prompt_id))
        log.exception("rollback failed prompt=%s version=%s", prompt_id, version_id)
        flash("回滚失败，请重试", "error")
        return redirect(url_for("versions_page", prompt_id=prompt_id))
    flash("已从历史版本回滚并创建新版本", "success")
    return redirect(url_for("prompt_detail", prompt_id=prompt_id))


# ---------------------------------------------------------------------------
# Versions and diff
# ---------------------------------------------------------------------------
@route("/prompt/<int:prompt_id>/versions")
def versions_page(prompt_id):
    conn = get_db()
    prompt = _fetch_prompt(conn, prompt_id)
    versions = conn.execute(
        "SELECT id, version, created_at, length(content) AS length, "
        "substr(content, 1, 240) AS preview_content "
        "FROM versions WHERE prompt_id=? ORDER BY created_at DESC, id DESC",
        (prompt_id,),
    ).fetchall()
    return render_template(
        "versions.html",
        prompt=dict(prompt),
        versions=[dict(version) for version in versions],
        current_id=prompt["current_version_id"],
    )


_TOKEN = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)


def word_diff_html(old, new):
    """Render a two-column diff with word-level highlighting inside changes."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)

    def wrap(css_class, text):
        return Markup(f'<span class="{css_class}">{escape(text)}</span>')

    def highlight(left, right):
        left_tokens, right_tokens = _TOKEN.findall(left), _TOKEN.findall(right)
        inner = difflib.SequenceMatcher(None, left_tokens, right_tokens)
        left_out, right_out = [], []
        for tag, i1, i2, j1, j2 in inner.get_opcodes():
            if tag in ("equal", "delete", "replace"):
                chunk = "".join(left_tokens[i1:i2])
                left_out.append(escape(chunk) if tag == "equal" else wrap("diff-del", chunk))
            if tag in ("equal", "insert", "replace"):
                chunk = "".join(right_tokens[j1:j2])
                right_out.append(escape(chunk) if tag == "equal" else wrap("diff-ins", chunk))
        return Markup("").join(left_out), Markup("").join(right_out)

    rows = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append((escape(old_lines[i1 + offset]), escape(new_lines[j1 + offset]), ""))
        elif tag == "delete":
            rows.extend((wrap("diff-del", line), Markup(""), "del") for line in old_lines[i1:i2])
        elif tag == "insert":
            rows.extend((Markup(""), wrap("diff-ins", line), "ins") for line in new_lines[j1:j2])
        else:
            left_block, right_block = old_lines[i1:i2], new_lines[j1:j2]
            for offset in range(max(len(left_block), len(right_block))):
                left = left_block[offset] if offset < len(left_block) else ""
                right = right_block[offset] if offset < len(right_block) else ""
                rows.append((*highlight(left, right), "chg"))

    parts = ['<table class="diff-table"><tbody>']
    for left_html, right_html, css_class in rows:
        parts.append(
            f'<tr class="{css_class}"><td class="cell-left">{left_html}</td>'
            f'<td class="cell-right">{right_html}</td></tr>'
        )
    parts.append("</tbody></table>")
    return Markup("\n".join(parts))


@route("/prompt/<int:prompt_id>/diff")
def diff_view(prompt_id):
    conn = get_db()
    prompt = _fetch_prompt(conn, prompt_id)
    versions = conn.execute(
        "SELECT id, version, created_at FROM versions WHERE prompt_id=? "
        "ORDER BY created_at DESC, id DESC",
        (prompt_id,),
    ).fetchall()
    if not versions:
        flash("暂无版本", "info")
        return redirect(url_for("prompt_detail", prompt_id=prompt_id))

    right_id = request.args.get("right") or str(prompt["current_version_id"] or versions[0]["id"])
    left_id = request.args.get("left")
    if not left_id:
        index_of_right = next(
            (i for i, v in enumerate(versions) if str(v["id"]) == str(right_id)), 0
        )
        neighbour = min(index_of_right + 1, len(versions) - 1)
        left_id = str(versions[neighbour]["id"])

    left = conn.execute(
        "SELECT * FROM versions WHERE id=? AND prompt_id=?", (left_id, prompt_id)
    ).fetchone()
    right = conn.execute(
        "SELECT * FROM versions WHERE id=? AND prompt_id=?", (right_id, prompt_id)
    ).fetchone()
    if not left or not right:
        flash("所选版本不存在", "error")
        return redirect(url_for("versions_page", prompt_id=prompt_id))

    return render_template(
        "diff.html", prompt=dict(prompt), versions=[dict(v) for v in versions],
        left=dict(left), right=dict(right),
        diff_html=word_diff_html(left["content"], right["content"]),
    )
