"""Backup import/export.

One format only: a JSON bundle. It carries every prompt with its full version
history, so a restore reproduces the library exactly.

An uploaded bundle is untrusted input that is about to *replace* the database,
so it is validated completely before a single row is written: ids must be sane
integers, timestamps must be real and not in the future, and the version graph
must reference only versions inside the same prompt and contain no cycles.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

from flask import current_app
from werkzeug.exceptions import RequestEntityTooLarge

from .db import db_path, get_setting, set_setting
from .utils import now_ts, parse_tags, sanitize_color, tags_from_row

log = logging.getLogger("prompt_manage")

SCHEMA_VERSION = 3

# Browsers hold JSON numbers as IEEE-754 doubles. Keep imported ids below that
# exact-integer ceiling, with room left for later AUTOINCREMENT rows, so one
# crafted backup cannot poison the sequences or show imprecise ids in the UI.
_MAX_SAFE_WEB_ID = (1 << 53) - 1
_MAX_IMPORT_ID = _MAX_SAFE_WEB_ID - 1_000_000_000

EXPORT_SETTING_KEYS = ("version_cleanup_threshold", "language")
AUTH_SETTING_KEYS = ("auth_mode", "auth_password_hash", "auth_revision")
_TEXT_FIELDS = ("source", "notes", "color", "created_at", "updated_at", "archived_at")


class ImportError_(ValueError):
    """A human-readable, already-translated import failure."""


def _fail(message):
    raise ImportError_(f"导入失败：{message}")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def _as_int(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _fail(f"{label} 类型无效")
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            _fail(f"{label} 类型无效")
        value = int(text)
    if positive and value < 1:
        _fail(f"{label} 必须为正整数")
    if value > _MAX_IMPORT_ID:
        _fail(f"{label} 过大，超过安全导入范围")
    return value


def _as_bool(value, label):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip() in ("0", "1"):
        return value.strip() == "1"
    _fail(f"{label} 类型无效")


def _as_text(value, label):
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{label} 类型无效")
    return value


def _as_timestamp(value, label):
    text = _as_text(value, label)
    if text is None or not text.strip():
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        _fail(f"{label} 时间格式无效")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    # A future timestamp would make ordering and "current version" selection
    # attacker-controlled. Allow a small margin for clock skew.
    if parsed > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5):
        _fail(f"{label} 不能晚于当前时间")
    return parsed.isoformat()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_bundle(upload):
    """Read and fully validate an uploaded JSON backup."""
    max_bytes = current_app.config["MAX_IMPORT_SIZE_MB"] * 1024 * 1024
    raw = upload.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise RequestEntityTooLarge()
    if not (upload.filename or "").lower().endswith(".json"):
        _fail("仅支持 .json 备份文件")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("JSON 格式无效")

    if not isinstance(data, dict) or not isinstance(data.get("prompts"), list):
        _fail("缺少 prompts 列表")
    has_app, has_schema = "app" in data, "schema_version" in data
    if has_app != has_schema:
        _fail("app 与 schema_version 必须同时存在")
    if has_app:
        if data["app"] != "prompt-manage":
            _fail("app 标识无效")
        if not isinstance(data["schema_version"], int) or not 1 <= data["schema_version"] <= SCHEMA_VERSION:
            _fail("schema_version 不受支持")

    prompts = [_parse_prompt(item, index) for index, item in enumerate(data["prompts"], start=1)]
    if not prompts:
        _fail("未发现任何提示词")
    seen_ids = set()
    for prompt in prompts:
        if prompt["id"] is None:
            continue
        if prompt["id"] in seen_ids:
            _fail(f"提示词 ID {prompt['id']} 重复")
        seen_ids.add(prompt["id"])

    return {"prompts": prompts, "settings": _parse_settings(data.get("settings"))}


def _parse_prompt(item, position):
    if not isinstance(item, dict):
        _fail(f"第 {position} 条提示词格式无效")
    label = f"第 {position} 条提示词"
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        _fail(f"{label} 缺少名称")

    prompt = {
        "id": _as_int(item["id"], f"{label} 的 ID", positive=True) if item.get("id") is not None else None,
        "name": name.strip()[:200],
        "source": _as_text(item.get("source"), f"{label} 的来源"),
        "notes": _as_text(item.get("notes"), f"{label} 的备注"),
        "color": sanitize_color(_as_text(item.get("color"), f"{label} 的颜色")),
        "tags": parse_tags([t for t in (item.get("tags") or []) if isinstance(t, str)])
        if isinstance(item.get("tags"), list) else [],
        # "favorite" is accepted from older bundles and folded into "pinned",
        # matching what migration 12 did to existing databases.
        "pinned": 1 if (
            _as_bool(item.get("pinned", False), f"{label} 的置顶状态")
            or _as_bool(item.get("favorite", False), f"{label} 的收藏状态")
        ) else 0,
        "archived_at": _as_timestamp(item.get("archived_at"), f"{label} 的归档时间"),
        "created_at": _as_timestamp(item.get("created_at"), f"{label} 的创建时间"),
        "updated_at": _as_timestamp(item.get("updated_at"), f"{label} 的更新时间"),
    }
    prompt["versions"] = _parse_versions(item.get("versions"), label)
    prompt["current_version_id"] = _resolve_current(item.get("current_version_id"), prompt, label)
    return prompt


def _parse_versions(raw, label):
    if not isinstance(raw, list) or not raw:
        _fail(f"{label} 缺少版本记录")
    versions, ids = [], set()
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            _fail(f"{label} 的第 {position} 个版本格式无效")
        version_label = f"{label} 的第 {position} 个版本"
        content = item.get("content")
        if not isinstance(content, str):
            _fail(f"{version_label} 缺少内容")
        number = item.get("version")
        if not isinstance(number, str) or not number.strip():
            _fail(f"{version_label} 缺少版本号")
        version_id = (
            _as_int(item["id"], f"{version_label} 的 ID", positive=True)
            if item.get("id") is not None else None
        )
        if version_id is not None:
            if version_id in ids:
                _fail(f"{version_label} 的 ID 重复")
            ids.add(version_id)
        versions.append({
            "id": version_id,
            "version": number.strip()[:40],
            "content": content,
            "created_at": _as_timestamp(item.get("created_at"), f"{version_label} 的创建时间"),
            "parent_version_id": (
                _as_int(item["parent_version_id"], f"{version_label} 的父版本", positive=True)
                if item.get("parent_version_id") is not None else None
            ),
        })

    parents = {v["id"]: v["parent_version_id"] for v in versions if v["id"] is not None}
    for version in versions:
        parent = version["parent_version_id"]
        if parent is None:
            continue
        if parent not in ids:
            _fail(f"{label} 的父版本 {parent} 不在同一提示词内")
        # Walk to the root; a loop here would survive the import and make the
        # history page recurse forever.
        seen, cursor = set(), parent
        while cursor is not None:
            if cursor in seen:
                _fail(f"{label} 的版本父子关系存在循环")
            seen.add(cursor)
            cursor = parents.get(cursor)
    return versions


def _resolve_current(raw, prompt, label):
    if raw is None:
        return None
    current = _as_int(raw, f"{label} 的当前版本", positive=True)
    if current not in {v["id"] for v in prompt["versions"] if v["id"] is not None}:
        _fail(f"{label} 的当前版本不在其版本列表中")
    return current


def _parse_settings(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail("settings 格式无效")
    out = {}
    threshold = raw.get("version_cleanup_threshold")
    if threshold is not None:
        value = _as_int(threshold, "版本保留数量", positive=True)
        out["version_cleanup_threshold"] = str(min(value, 100_000))
    language = raw.get("language")
    if language is not None:
        from .i18n import SUPPORTED_LANGS  # noqa: PLC0415  (avoids an import cycle)

        if language not in SUPPORTED_LANGS:
            _fail("语言设置无效")
        out["language"] = language
    for key in AUTH_SETTING_KEYS:
        if key in raw:
            out[key] = _as_text(raw[key], key) or ""
    if out.get("auth_mode") not in (None, "off", "global", "per"):
        _fail("认证方式无效")
    if out.get("auth_mode") == "per":
        # Per-prompt mode no longer exists; treat it as the site-wide password
        # so a restored backup never ends up less protected than it was.
        out["auth_mode"] = "global"
    return out


# ---------------------------------------------------------------------------
# Applying an import
# ---------------------------------------------------------------------------
def replace_prompts(conn, prompts):
    """Delete everything and insert the validated bundle. Caller owns the transaction."""
    conn.execute("DELETE FROM versions")
    conn.execute("DELETE FROM prompts")
    # Explicit high ids advance AUTOINCREMENT permanently unless sqlite_sequence
    # is reset; clearing it also repairs a database poisoned by an earlier import.
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('prompts', 'versions')")

    for prompt in prompts:
        fallback_ts = now_ts()
        cur = conn.execute(
            "INSERT INTO prompts(id, name, source, notes, color, tags, pinned, archived_at, "
            "created_at, updated_at, current_version_id) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                prompt["id"], prompt["name"], prompt["source"], prompt["notes"], prompt["color"],
                json.dumps(prompt["tags"], ensure_ascii=False), prompt["pinned"],
                prompt["archived_at"], prompt["created_at"] or fallback_ts,
                prompt["updated_at"] or fallback_ts,
            ),
        )
        prompt_id = prompt["id"] if prompt["id"] is not None else cur.lastrowid

        id_map, inserted = {}, []
        for version in prompt["versions"]:
            cur = conn.execute(
                "INSERT INTO versions(id, prompt_id, version, content, created_at, parent_version_id) "
                "VALUES(?,?,?,?,?,NULL)",
                (version["id"], prompt_id, version["version"], version["content"],
                 version["created_at"] or fallback_ts),
            )
            new_id = version["id"] if version["id"] is not None else cur.lastrowid
            if version["id"] is not None:
                id_map[version["id"]] = new_id
            inserted.append((new_id, version["parent_version_id"]))

        for new_id, parent in inserted:
            if parent is not None:
                conn.execute(
                    "UPDATE versions SET parent_version_id=? WHERE id=? AND prompt_id=?",
                    (id_map[parent], new_id, prompt_id),
                )

        current = id_map.get(prompt["current_version_id"]) if prompt["current_version_id"] else None
        if current is None:
            row = conn.execute(
                "SELECT id FROM versions WHERE prompt_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                (prompt_id,),
            ).fetchone()
            current = row["id"] if row else None
        conn.execute("UPDATE prompts SET current_version_id=? WHERE id=?", (current, prompt_id))


def restore_settings(conn, imported, *, restore_auth=False):
    """Apply imported settings. Returns True when authentication changed."""
    for key in EXPORT_SETTING_KEYS:
        if key in imported:
            set_setting(conn, key, imported[key])
    if not restore_auth:
        return False
    if "auth_mode" not in imported or "auth_password_hash" not in imported:
        _fail("备份不包含完整认证设置")
    if imported["auth_mode"] != "off" and not imported["auth_password_hash"]:
        _fail("认证设置缺少密码")
    try:
        local = int(get_setting(conn, "auth_revision", "1") or "1")
        incoming = int(imported.get("auth_revision", "1") or "1")
    except (TypeError, ValueError):
        local, incoming = 1, 1
    set_setting(conn, "auth_mode", imported["auth_mode"])
    set_setting(conn, "auth_password_hash", imported["auth_password_hash"])
    set_setting(conn, "auth_revision", str(max(local, incoming) + 1))
    conn.execute("DELETE FROM auth_sessions")
    return True


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def collect_export(conn, *, include_auth=False):
    keys = EXPORT_SETTING_KEYS + (AUTH_SETTING_KEYS if include_auth else ())
    settings = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        if row["key"] in keys
    }
    prompts = []
    for row in conn.execute("SELECT * FROM prompts ORDER BY id ASC").fetchall():
        versions = conn.execute(
            "SELECT id, version, content, created_at, parent_version_id "
            "FROM versions WHERE prompt_id=? ORDER BY created_at ASC, id ASC",
            (row["id"],),
        ).fetchall()
        prompts.append({
            "id": row["id"], "name": row["name"], "source": row["source"],
            "notes": row["notes"], "color": row["color"], "tags": tags_from_row(row),
            "pinned": bool(row["pinned"]), "archived_at": row["archived_at"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "current_version_id": row["current_version_id"],
            "versions": [dict(version) for version in versions],
        })
    return {
        "app": "prompt-manage",
        "schema_version": SCHEMA_VERSION,
        "exported_at": now_ts(),
        "settings": settings,
        "prompts": prompts,
    }


def write_pre_import_backup(conn):
    """Snapshot the current database before an import replaces it."""
    backups_dir = os.path.join(os.path.dirname(db_path()) or ".", "backups")
    os.makedirs(backups_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = os.path.join(backups_dir, f"pre-import-{stamp}.json")
    payload = collect_export(conn, include_auth=True)
    temp_path = None
    try:
        handle_fd, temp_path = tempfile.mkstemp(dir=backups_dir, suffix=".json.tmp")
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        if os.path.getsize(temp_path) == 0:
            raise OSError("backup file is empty")
        with open(temp_path, "r", encoding="utf-8") as handle:
            json.load(handle)  # a backup that cannot be re-read is not a backup
        os.replace(temp_path, target)
        log.info("wrote pre-import backup to %s", target)
        return target
    except Exception as exc:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                log.warning("could not remove incomplete backup %s", temp_path)
        log.error("pre-import backup failed: %s", exc)
        raise OSError(f"备份失败：{exc}") from exc


def prune_backups(backups_dir):
    """Keep the backup directory bounded so repeated imports cannot fill the volume."""
    try:
        paths = [
            os.path.join(backups_dir, name)
            for name in os.listdir(backups_dir)
            if name.startswith("pre-import-") and name.endswith(".json")
        ]
    except OSError:
        log.warning("could not inspect backup directory %s", backups_dir)
        return
    paths.sort(key=os.path.getmtime, reverse=True)
    for path in paths[current_app.config["IMPORT_BACKUP_RETENTION"]:]:
        try:
            os.remove(path)
        except OSError:
            log.warning("could not prune old backup %s", path)
