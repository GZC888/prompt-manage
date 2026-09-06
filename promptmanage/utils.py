"""Small pure helpers: timestamps, parsing, colors, version numbers, redirects."""

import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import request

log = logging.getLogger("prompt_manage")

_HEX_COLOR = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
_INT_TEXT = re.compile(r"-?\d+")


def now_ts():
    """Naive UTC ISO timestamp (kept consistent with historical data)."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def parse_tags(value):
    """Normalize a tag input (list or comma/、-separated text) to a clean list."""
    if not value:
        return []
    raw_items = value if isinstance(value, list) else str(value).replace("，", ",").split(",")
    out, seen = [], set()
    for item in raw_items:
        tag = str(item).strip()
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def tags_from_row(row):
    """Read a prompt row's stored tags JSON defensively (never raises)."""
    try:
        parsed = json.loads(row["tags"]) if row["tags"] else []
    except (TypeError, ValueError) as exc:
        log.warning("invalid tags JSON for prompt id=%s: %s", _row_id(row), exc)
        return []
    if not isinstance(parsed, list):
        return []
    return parse_tags([item for item in parsed if isinstance(item, str)])


def _row_id(row):
    try:
        return row["id"]
    except (KeyError, IndexError, TypeError):
        return "unknown"


def parse_int_or_none(value):
    text = ("" if value is None else str(value)).strip()
    if not _INT_TEXT.fullmatch(text):
        return None
    try:
        return int(text)
    except (ValueError, OverflowError):
        return None


def sanitize_color(value):
    """Normalize a color to #rrggbb, or None when empty/invalid."""
    text = (value or "").strip()
    if not text or not _HEX_COLOR.fullmatch(text):
        return None
    if len(text) == 4:
        text = "#" + "".join(char * 2 for char in text[1:])
    return text.lower()


def bump_version(current, kind="patch"):
    """Return the next semantic version string after ``current``."""
    if not current:
        return "1.0.0"
    try:
        major, minor, patch = (int(part) for part in current.split("."))
    except (TypeError, ValueError):
        return "1.0.0"
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def safe_local_target(raw, default_path):
    """Return a same-origin path, never an absolute or protocol-relative URL."""
    if not raw:
        return default_path
    try:
        parsed = urlparse(str(raw).replace("\\", "/"))
        if parsed.netloc and parsed.netloc != request.host:
            return default_path
        path = parsed.path or "/"
        if not path.startswith("/"):
            path = "/" + path
        if path.startswith("//"):
            return default_path
        return path + (("?" + parsed.query) if parsed.query else "")
    except ValueError:
        return default_path


def safe_referrer(default_path):
    return safe_local_target(request.referrer, default_path)


def safe_next(default_path):
    return safe_local_target(request.values.get("next"), default_path)


def current_path_with_query():
    """This request's path plus its query string, usable as a ``next`` target."""
    return request.full_path.rstrip("?") if request.query_string else request.path


def wants_json():
    """Whether this client prefers a JSON body over an HTML redirect."""
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.accept_mimetypes
    return (
        accept.best == "application/json"
        and accept["application/json"] > accept["text/html"]
    )
