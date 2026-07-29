"""Prompt Manage — a lightweight personal prompt manager.

Flask + SQLite + Jinja, no heavyweight frontend framework. This module holds the
application, configuration (from environment variables), database access and
migrations, the permission/authentication layer and all routes.

Production is served via gunicorn (``wsgi:app``); ``python app.py`` is for local
development only and never enables the debugger unless ``FLASK_DEBUG=true``.
"""

import base64
import binascii
import csv
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from urllib.parse import urlparse

from flask import (
    Flask, abort, flash, g, has_app_context, has_request_context, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)
from markupsafe import Markup, escape
from werkzeug.exceptions import BadRequest, HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import difflib

from i18n import LANG_DEFAULT, SUPPORTED_LANGS, translate

log = logging.getLogger("prompt_manage")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def _env(name, default=None):
    return os.environ.get(name, default)


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in ("1", "true", "yes", "y", "on"):
        return True
    if normalized in ("0", "false", "no", "n", "off"):
        return False
    raise RuntimeError(
        f"{name} must be a boolean value: true/false, yes/no, on/off, or 1/0"
    )


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _require_int_range(name, value, minimum, maximum=None):
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
        raise RuntimeError(f"{name} must be in range {bound}")


def _env_samesite(name, default="Lax"):
    raw = (os.environ.get(name, default) or default).strip().lower()
    allowed = {"lax": "Lax", "strict": "Strict", "none": "None"}
    if raw not in allowed:
        raise RuntimeError(f"{name} must be one of: Lax, Strict, None")
    return allowed[raw]


APP_ENV = (_env("APP_ENV", "production") or "production").strip().lower()
if APP_ENV not in {"production", "development", "testing"}:
    raise RuntimeError("APP_ENV must be production, development, or testing")
APP_PORT = _env_int("APP_PORT", 3501)
DEFAULT_DB_PATH = _env("DB_PATH", "/app/data/data.sqlite3")

PERMANENT_SESSION_DAYS = _env_int("PERMANENT_SESSION_DAYS", 3650)
SESSION_COOKIE_SAMESITE = _env_samesite("SESSION_COOKIE_SAMESITE", "Lax")
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", APP_ENV == "production")

AUTH_LOGIN_MAX_ATTEMPTS = _env_int("AUTH_LOGIN_MAX_ATTEMPTS", 10)
AUTH_LOGIN_WINDOW_SECONDS = _env_int("AUTH_LOGIN_WINDOW_SECONDS", 900)
AUTH_LOCK_SECONDS = _env_int("AUTH_LOCK_SECONDS", 900)
GLOBAL_LOGIN_MAX_ATTEMPTS = _env_int("GLOBAL_LOGIN_MAX_ATTEMPTS", 1000)
GLOBAL_LOGIN_WINDOW_SECONDS = _env_int("GLOBAL_LOGIN_WINDOW_SECONDS", 3600)

MAX_IMPORT_SIZE_MB = _env_int("MAX_IMPORT_SIZE_MB", 10)
MAX_IMAGE_SIZE_MB = _env_int("MAX_IMAGE_SIZE_MB", 5)
IMPORT_BACKUP_RETENTION = _env_int("IMPORT_BACKUP_RETENTION", 20)
ENABLE_SECURITY_HEADERS = _env_bool("ENABLE_SECURITY_HEADERS", True)
TRUST_PROXY_HEADERS = _env_bool("TRUST_PROXY_HEADERS", False)
ENABLE_HSTS = _env_bool("ENABLE_HSTS", APP_ENV == "production")
HSTS_MAX_AGE = _env_int("HSTS_MAX_AGE", 31536000)
HSTS_INCLUDE_SUBDOMAINS = _env_bool("HSTS_INCLUDE_SUBDOMAINS", False)
BUILD_SHA = (_env("BUILD_SHA", "dev") or "dev").strip()[:128]
BOOTSTRAP_TOKEN = (_env("BOOTSTRAP_TOKEN", "") or "").strip()
_require_int_range("APP_PORT", APP_PORT, 1, 65535)
_require_int_range("PERMANENT_SESSION_DAYS", PERMANENT_SESSION_DAYS, 1)
_require_int_range("AUTH_LOGIN_MAX_ATTEMPTS", AUTH_LOGIN_MAX_ATTEMPTS, 1)
_require_int_range("AUTH_LOGIN_WINDOW_SECONDS", AUTH_LOGIN_WINDOW_SECONDS, 1)
_require_int_range("AUTH_LOCK_SECONDS", AUTH_LOCK_SECONDS, 1)
_require_int_range("GLOBAL_LOGIN_MAX_ATTEMPTS", GLOBAL_LOGIN_MAX_ATTEMPTS, 1)
_require_int_range("GLOBAL_LOGIN_WINDOW_SECONDS", GLOBAL_LOGIN_WINDOW_SECONDS, 1)
_require_int_range("MAX_IMPORT_SIZE_MB", MAX_IMPORT_SIZE_MB, 1)
_require_int_range("MAX_IMAGE_SIZE_MB", MAX_IMAGE_SIZE_MB, 1)
_require_int_range("IMPORT_BACKUP_RETENTION", IMPORT_BACKUP_RETENTION, 1)
_require_int_range("HSTS_MAX_AGE", HSTS_MAX_AGE, 0)
if APP_ENV == "production" and (
    not isinstance(DEFAULT_DB_PATH, str)
    or not DEFAULT_DB_PATH.startswith("/")
    or DEFAULT_DB_PATH == ":memory:"
):
    raise RuntimeError("DB_PATH must be an absolute persistent path in production")
if SESSION_COOKIE_SAMESITE == "None" and not SESSION_COOKIE_SECURE:
    raise RuntimeError("SESSION_COOKIE_SAMESITE=None requires SESSION_COOKIE_SECURE=true")
_WEAK_BOOTSTRAP_TOKENS = {
    "bootstrap-secret", "change-me", "changeme", "secret", "password",
    "replace-me-with-another-random-token",
}
if APP_ENV == "production" and BOOTSTRAP_TOKEN and (
    len(BOOTSTRAP_TOKEN) < 32 or BOOTSTRAP_TOKEN.lower() in _WEAK_BOOTSTRAP_TOKENS
):
    raise RuntimeError(
        "BOOTSTRAP_TOKEN is too short or uses a known placeholder. Generate an "
        "independent random token (e.g. `openssl rand -hex 32`), or leave it "
        "empty after setup is complete."
    )

# CSV cells beginning with these characters are interpreted as formulas by
# spreadsheet programs.  Prefixing them with an apostrophe keeps exports safe;
# our own CSV importer removes that marker only when the file identifies itself
# as a Prompt Manage export.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
# Browser APIs represent JSON numbers as IEEE-754 doubles. Keep imported IDs
# below that exact-integer ceiling and reserve ample room for later AUTOINCREMENT
# rows, instead of letting one crafted backup permanently poison both SQLite
# sequences or produce imprecise IDs in the UI.
_MAX_SAFE_WEB_ID = (1 << 53) - 1
_IMPORT_ID_HEADROOM = 1_000_000_000
_MAX_IMPORT_ID = _MAX_SAFE_WEB_ID - _IMPORT_ID_HEADROOM
_IMPORT_TEXT_FIELDS = {
    "name", "source", "notes", "color", "image_data", "archived_at", "last_used_at",
    "created_at", "updated_at",
}
_IMPORT_SETTING_KEYS = {
    "version_cleanup_threshold", "language", "auth_mode", "auth_password_hash",
    "auth_revision", "bootstrap_completed",
}
_EXPORT_SETTING_KEYS = {"version_cleanup_threshold", "language"}
_SENSITIVE_ENDPOINTS = {
    "setup", "login", "logout", "settings", "export_all", "unlock_prompt",
    "prompt_detail", "new_prompt", "versions_page", "diff_view",
    "api_prompt_content", "api_search", "api_tags", "index",
}

_WEAK_SECRET_KEYS = {
    "", "dev-secret", "change-me", "changeme", "secret", "password",
    "replace-me-with-a-long-random-string",
}


def _resolve_secret_key():
    """Resolve the Flask secret key, failing fast on weak production keys."""
    raw = (_env("SECRET_KEY", "") or "").strip()
    if APP_ENV == "production":
        if not raw or raw.lower() in _WEAK_SECRET_KEYS or len(raw) < 32:
            raise RuntimeError(
                "SECRET_KEY is missing or too weak. In production you must set a "
                "strong, random SECRET_KEY (e.g. `openssl rand -hex 32`). "
                "Refusing to start."
            )
        return raw
    if not raw:
        log.warning(
            "SECRET_KEY not set; using an insecure development key. "
            "Set APP_ENV=production and a strong SECRET_KEY before deploying."
        )
        return "dev-secret-not-for-production"
    return raw


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
# Only trust forwarding headers when the deployment explicitly opts in. Host,
# port and prefix are intentionally never trusted: the app only needs the
# original client IP and scheme, while the other fields can create unsafe
# redirects when the container port is reachable directly.
if TRUST_PROXY_HEADERS:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

app.secret_key = _resolve_secret_key()
if (
    APP_ENV == "production"
    and BOOTSTRAP_TOKEN
    and hmac.compare_digest(BOOTSTRAP_TOKEN, app.secret_key)
):
    raise RuntimeError("BOOTSTRAP_TOKEN must be different from SECRET_KEY")
app.config.update(
    DB_PATH=DEFAULT_DB_PATH,
    APP_ENV=APP_ENV,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=PERMANENT_SESSION_DAYS),
    AUTH_LOGIN_MAX_ATTEMPTS=AUTH_LOGIN_MAX_ATTEMPTS,
    AUTH_LOGIN_WINDOW_SECONDS=AUTH_LOGIN_WINDOW_SECONDS,
    AUTH_LOCK_SECONDS=AUTH_LOCK_SECONDS,
    GLOBAL_LOGIN_MAX_ATTEMPTS=GLOBAL_LOGIN_MAX_ATTEMPTS,
    GLOBAL_LOGIN_WINDOW_SECONDS=GLOBAL_LOGIN_WINDOW_SECONDS,
    MAX_IMPORT_SIZE_MB=MAX_IMPORT_SIZE_MB,
    MAX_IMAGE_SIZE_MB=MAX_IMAGE_SIZE_MB,
    IMPORT_BACKUP_RETENTION=IMPORT_BACKUP_RETENTION,
    ENABLE_SECURITY_HEADERS=ENABLE_SECURITY_HEADERS,
    TRUST_PROXY_HEADERS=TRUST_PROXY_HEADERS,
    ENABLE_HSTS=ENABLE_HSTS,
    HSTS_MAX_AGE=HSTS_MAX_AGE,
    HSTS_INCLUDE_SUBDOMAINS=HSTS_INCLUDE_SUBDOMAINS,
    BUILD_SHA=BUILD_SHA,
    BOOTSTRAP_TOKEN=BOOTSTRAP_TOKEN,
    # Hard cap on request bodies: import limit + image limit + headroom for form
    # fields. Specific friendly limits are still enforced per upload below.
    MAX_CONTENT_LENGTH=(MAX_IMPORT_SIZE_MB + MAX_IMAGE_SIZE_MB + 6) * 1024 * 1024,
)

app.jinja_env.filters["loads"] = json.loads
# Format an ISO timestamp for display: take the first n chars and turn the
# "T" separator into a space. NULL-safe. Replaces 9 inline copies in templates.
app.jinja_env.filters["ts"] = lambda s, n=19: (s or "")[:n].replace("T", " ")


# ---------------------------------------------------------------------------
# Time / small utilities
# ---------------------------------------------------------------------------
def now_ts():
    """Naive UTC ISO timestamp (kept consistent with historical data)."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def parse_tags(s):
    if not s:
        return []
    if isinstance(s, list):
        return [str(t).strip() for t in s if str(t).strip()]
    parts = []
    for raw in str(s).replace("，", ",").split(","):
        p = raw.strip()
        if p:
            parts.append(p)
    return parts


def parse_bool_value(val):
    return ("" if val is None else str(val)).strip().lower() in ("1", "true", "yes", "y", "on")


def parse_int_or_none(val):
    s = ("" if val is None else str(val)).strip()
    if not re.fullmatch(r"-?\d+", s):
        return None
    try:
        return int(s)
    except (ValueError, OverflowError):
        return None


def _strict_int(value, label, *, positive=False, nonnegative=False):
    """Parse an actual integer value (or a canonical CSV integer string)."""
    if isinstance(value, bool):
        raise ValueError(f"导入失败：{label} 类型无效")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"(?:0|[1-9]\d*)", value.strip()):
        try:
            number = int(value.strip())
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"导入失败：{label} 类型无效") from exc
    else:
        raise ValueError(f"导入失败：{label} 类型无效")
    if number < -9223372036854775808 or number > 9223372036854775807:
        raise ValueError(f"导入失败：{label} 超出 SQLite 整数范围")
    if positive and number < 1:
        raise ValueError(f"导入失败：{label} 必须为正整数")
    if nonnegative and number < 0:
        raise ValueError(f"导入失败：{label} 不能为负数")
    return number


def _strict_import_id(value, label):
    number = _strict_int(value, label, positive=True)
    if number > _MAX_IMPORT_ID:
        raise ValueError(f"导入失败：{label} 过大，超过安全导入范围")
    return number


def _strict_bool(value, label):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip() in {"0", "1"}:
        return value.strip() == "1"
    raise ValueError(f"导入失败：{label} 类型无效")


def _normalize_import_timestamp(value, label, *, allow_empty=True):
    """Validate an ISO timestamp and normalize aware values to UTC-naive ISO."""
    if value is None or value == "":
        if allow_empty:
            return None
        raise ValueError(f"导入失败：{label} 不能为空")
    if not isinstance(value, str):
        raise ValueError(f"导入失败：{label} 类型无效")
    raw = value.strip()
    if not raw:
        if allow_empty:
            return None
        raise ValueError(f"导入失败：{label} 不能为空")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"导入失败：{label} 时间格式无效") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    # Future timestamps make ordering/current selection attacker-controlled and
    # are not produced by this application. Allow a small clock-skew margin.
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    if parsed > now_utc_naive + timedelta(minutes=5):
        raise ValueError(f"导入失败：{label} 不能晚于当前时间")
    return parsed.isoformat()


def _csv_safe_cell(value):
    text = "" if value is None else str(value)
    stripped = text.lstrip(" \t\r\n")
    if text.startswith("'") or stripped.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


def _csv_unescape_cell(value, own_export=False):
    text = "" if value is None else str(value)
    if own_export and text.startswith("'"):
        remainder = text[1:]
        stripped = remainder.lstrip(" \t\r\n")
        if remainder.startswith("'") or stripped.startswith(_CSV_FORMULA_PREFIXES):
            return remainder
    return text


def _bootstrap_is_complete(conn):
    if APP_ENV != "production":
        return True
    return (get_setting(conn, "bootstrap_completed", "1") or "0") == "1"


def parse_json_text(val, default, strict=False):
    s = ("" if val is None else str(val)).strip()
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        if strict:
            raise ValueError(f"Invalid JSON: {e}") from e
        return default


def _safe_local_target(raw, default_path):
    """Return a same-origin path, never an absolute or protocol-relative URL."""
    if not raw:
        return default_path
    raw = str(raw).replace("\\", "/")
    try:
        parsed = urlparse(raw)
        if parsed.netloc and parsed.netloc != request.host:
            return default_path
        path = parsed.path or "/"
        if not path.startswith("/"):
            path = "/" + path
        if path.startswith("//"):
            return default_path
        return path + (("?" + parsed.query) if parsed.query else "")
    except Exception:
        return default_path


def _safe_referrer(default_path):
    return _safe_local_target(request.referrer, default_path)


def _is_db_locked(exc):
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _db_busy_response(default_path):
    """Return a retryable response when SQLite is temporarily writer-locked."""
    retry_after = "2"
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest" or (
        request.accept_mimetypes.best == "application/json"
        and request.accept_mimetypes["application/json"] > request.accept_mimetypes["text/html"]
    )
    if wants_json:
        response = jsonify({"status": "busy", "retry_after": int(retry_after)})
    else:
        flash("数据库正忙，请稍后重试", "error")
        response = redirect(_safe_referrer(default_path))
    response.headers["Retry-After"] = retry_after
    return response, 503


def sanitize_color(val):
    """Normalize color to #rrggbb, or None if empty/invalid."""
    s = (val or "").strip()
    if not s:
        return None
    if re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", s):
        if len(s) == 4:
            s = "#" + "".join(c * 2 for c in s[1:])
        return s.lower()
    return None


def bump_version(current, kind="patch"):
    if not current:
        return "1.0.0"
    try:
        major, minor, patch = (int(x) for x in current.split("."))
    except Exception:
        return "1.0.0"
    if kind == "major":
        major, minor, patch = major + 1, 0, 0
    elif kind == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------
def db_path():
    return app.config["DB_PATH"]


def _connect(path, autocommit=False):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    if autocommit:
        conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    if path != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
    return conn


def get_db():
    """Return a SQLite connection.

    Inside a request/app context the connection is cached on ``g`` and closed
    automatically by :func:`_close_db` (so it is released even if a handler
    raises before its own ``close()``). Outside a context — CLI, tests,
    migrations — a standalone connection is returned for the caller to close.
    """
    if not has_app_context():
        return _connect(db_path())
    conn = getattr(g, "_db", None)
    if conn is None:
        conn = g._db = _connect(db_path())
    return conn


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _columns(conn, table):
    """Return column names for a whitelisted table.

    SQLite does not parameterize PRAGMA table names, so callers must use one of
    the known schema tables.
    """
    allowed_tables = {
        "prompts",
        "versions",
        "settings",
        "login_attempts",
        "schema_migrations",
        "prompt_unlocks",
        "auth_sessions",
    }
    if table not in allowed_tables:
        raise ValueError(f"Invalid table name: {table}")
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ---------------------------------------------------------------------------
# Migrations — recorded in schema_migrations, run once at startup, fail-fast.
# Every migration is idempotent so it can also upgrade legacy databases that
# predate the schema_migrations table.
# ---------------------------------------------------------------------------
def _m_base(conn):
    # A brand-new production database must be claimed through /setup before any
    # application data is exposed. Existing/legacy databases are treated as
    # initialized so an upgrade cannot unexpectedly lock out their owner.
    existing_base_tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('prompts', 'versions', 'settings')"
        ).fetchall()
    }
    had_app_schema = existing_base_tables == {"prompts", "versions", "settings"}
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source TEXT,
            notes TEXT,
            color TEXT,
            tags TEXT,
            image_data TEXT,
            pinned INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            current_version_id INTEGER,
            require_password INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id INTEGER NOT NULL,
            version TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT,
            parent_version_id INTEGER,
            FOREIGN KEY(prompt_id) REFERENCES prompts(id)
        )
        """
    )
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    defaults = (
        ("version_cleanup_threshold", "200"),
        ("auth_mode", "off"),
        ("auth_password_hash", ""),
        ("auth_revision", "1"),
        ("language", "zh"),
        (
            "bootstrap_completed",
            "1" if APP_ENV != "production" or had_app_schema else "0",
        ),
    )
    for key, val in defaults:
        if get_setting(conn, key) is None:
            set_setting(conn, key, val)


def _m_prompt_security_cols(conn):
    cols = _columns(conn, "prompts")
    if "require_password" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN require_password INTEGER DEFAULT 0")
    if "color" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN color TEXT")
    if "image_data" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN image_data TEXT")


def _m_prompt_feature_cols(conn):
    cols = _columns(conn, "prompts")
    if "favorite" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
    if "archived_at" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN archived_at TEXT DEFAULT NULL")
    if "last_used_at" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN last_used_at TEXT DEFAULT NULL")
    if "copy_count" not in cols:
        conn.execute("ALTER TABLE prompts ADD COLUMN copy_count INTEGER NOT NULL DEFAULT 0")


def _m_login_attempts(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            route TEXT NOT NULL,
            success INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_route_time "
        "ON login_attempts(ip, route, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_success_time "
        "ON login_attempts(success, created_at)"
    )


def _m_security_hardening(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_unlocks (
            session_id TEXT NOT NULL,
            prompt_id INTEGER NOT NULL,
            unlocked_at TEXT NOT NULL,
            PRIMARY KEY(session_id, prompt_id),
            FOREIGN KEY(prompt_id) REFERENCES prompts(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prompt_unlocks_prompt "
        "ON prompt_unlocks(prompt_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_success_time "
        "ON login_attempts(success, created_at)"
    )


def _m_indexes(conn):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prompts_pinned_updated ON prompts(pinned, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prompts_require_password ON prompts(require_password)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_prompt_created ON versions(prompt_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_prompt_version ON versions(prompt_id, version)")


def _m_versions_on_delete_cascade(conn):
    fks = conn.execute("PRAGMA foreign_key_list(versions)").fetchall()
    if any(r["table"] == "prompts" and (r["on_delete"] or "").upper() == "CASCADE" for r in fks):
        return
    conn.execute("DROP TABLE IF EXISTS versions_new")
    conn.execute(
        """
        CREATE TABLE versions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id INTEGER NOT NULL,
            version TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT,
            parent_version_id INTEGER,
            FOREIGN KEY(prompt_id) REFERENCES prompts(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "INSERT INTO versions_new(id, prompt_id, version, content, created_at, parent_version_id) "
        "SELECT id, prompt_id, version, content, created_at, parent_version_id FROM versions"
    )
    conn.execute("DROP TABLE versions")
    conn.execute("ALTER TABLE versions_new RENAME TO versions")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_prompt_created ON versions(prompt_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_prompt_version ON versions(prompt_id, version)")


def _m_auth_revision(conn):
    if get_setting(conn, "auth_revision") is None:
        set_setting(conn, "auth_revision", "1")


def _m_auth_sessions(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_sessions (
            session_id TEXT PRIMARY KEY,
            auth_revision TEXT NOT NULL,
            authenticated_at TEXT NOT NULL
        )
        """
    )


def _m_unlock_auth_revision(conn):
    if "auth_revision" not in _columns(conn, "prompt_unlocks"):
        conn.execute("ALTER TABLE prompt_unlocks ADD COLUMN auth_revision TEXT")
    conn.execute(
        "UPDATE prompt_unlocks SET auth_revision=("
        "SELECT value FROM settings WHERE key='auth_revision'"
        ") WHERE auth_revision IS NULL"
    )


def _m_bootstrap_state(conn):
    # Fresh databases created by the current _m_base already carry the correct
    # value. A missing key therefore identifies an existing installation being
    # upgraded, which must remain accessible for compatibility.
    if get_setting(conn, "bootstrap_completed") is None:
        set_setting(conn, "bootstrap_completed", "1")


MIGRATIONS = [
    (1, "base_schema", _m_base),
    (2, "prompt_security_columns", _m_prompt_security_cols),
    (3, "prompt_feature_columns", _m_prompt_feature_cols),
    (4, "login_attempts", _m_login_attempts),
    (5, "indexes", _m_indexes),
    (6, "security_hardening", _m_security_hardening),
    (7, "versions_on_delete_cascade", _m_versions_on_delete_cascade),
    (8, "auth_revision", _m_auth_revision),
    (9, "bootstrap_state", _m_bootstrap_state),
    (10, "auth_sessions", _m_auth_sessions),
    (11, "unlock_auth_revision", _m_unlock_auth_revision),
]


def run_migrations():
    """Apply pending migrations under one database-wide writer lock."""
    os.makedirs(os.path.dirname(db_path()) or ".", exist_ok=True)
    conn = _connect(db_path(), autocommit=True)
    pending_logs = []
    active_migration = None
    try:
        # Serialize the complete migration decision + execution sequence. Without
        # this lock, two fresh workers can both read the same pending set and one
        # will fail halfway through startup with ``database is locked``.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        known_migrations = {version: name for version, name, _fn in MIGRATIONS}
        applied_rows = conn.execute(
            "SELECT version, name FROM schema_migrations"
        ).fetchall()
        for row in applied_rows:
            if row["version"] not in known_migrations:
                raise RuntimeError(
                    f"Database schema version {row['version']} is newer than this application"
                )
            if known_migrations[row["version"]] != row["name"]:
                raise RuntimeError(
                    f"Migration name mismatch for version {row['version']}: "
                    f"database has {row['name']!r}, code expects {known_migrations[row['version']]!r}"
                )
        applied = {r["version"] for r in applied_rows}
        for version, name, fn in MIGRATIONS:
            if version in applied:
                continue
            active_migration = (version, name)
            fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
                (version, name, now_ts()),
            )
            pending_logs.append((version, name))
        conn.execute("COMMIT")
        for version, name in pending_logs:
            log.info("Applied migration %s (%s)", version, name)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if active_migration:
            log.exception(
                "Migration %s (%s) failed; aborting startup",
                active_migration[0],
                active_migration[1],
            )
        else:
            log.exception("Could not acquire migration lock; aborting startup")
        raise
    finally:
        conn.close()


def prune_versions(conn, prompt_id):
    try:
        threshold_s = get_setting(conn, "version_cleanup_threshold", "200")
        try:
            threshold = int(threshold_s)
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
        if len(rows) > threshold:
            deleted_ids = {r["id"] for r in rows[threshold:]}
            if current_id and current_id in deleted_ids:
                log.info(
                    "Protected current_version_id=%s from pruning for prompt_id=%s",
                    current_id,
                    prompt_id,
                )
            deleted_ids.discard(current_id)
            to_delete = [(version_id,) for version_id in deleted_ids]
            if to_delete:
                parent_map = {r["id"]: r["parent_version_id"] for r in rows}
                retained_ids = set(parent_map) - deleted_ids

                def nearest_retained(parent_id):
                    seen = set()
                    while parent_id in deleted_ids:
                        if parent_id in seen:
                            return None
                        seen.add(parent_id)
                        parent_id = parent_map.get(parent_id)
                    return parent_id if parent_id in retained_ids else None

                # Children can outlive a pruned parent. Rewire them to the
                # nearest retained ancestor before deleting, so an exported
                # bundle remains importable and the graph stays acyclic.
                for deleted_id in deleted_ids:
                    replacement = nearest_retained(parent_map.get(deleted_id))
                    conn.execute(
                        "UPDATE versions SET parent_version_id=? "
                        "WHERE prompt_id=? AND parent_version_id=?",
                        (replacement, prompt_id, deleted_id),
                    )
                conn.executemany("DELETE FROM versions WHERE id=?", to_delete)
    except sqlite3.Error as e:
        log.error("prune_versions failed for prompt_id=%s: %s", prompt_id, e)
        raise


def compute_current_version(conn, prompt_id):
    try:
        conn.execute(
            """
            UPDATE prompts
            SET current_version_id = (
                SELECT id
                FROM versions
                WHERE prompt_id=?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            ),
            updated_at=?
            WHERE id=?
            """,
            (prompt_id, now_ts(), prompt_id),
        )
    except sqlite3.Error as e:
        log.error("compute_current_version failed for prompt_id=%s: %s", prompt_id, e)
        raise


# ---------------------------------------------------------------------------
# Passwords (Werkzeug hashing with transparent migration from legacy SHA-256)
# ---------------------------------------------------------------------------
def hash_password(raw):
    return generate_password_hash(raw or "")


def _looks_legacy_sha256(stored):
    return bool(stored) and bool(re.fullmatch(r"[0-9a-f]{64}", stored))


def _is_supported_password_hash(stored):
    if not stored:
        return False
    if _looks_legacy_sha256(stored):
        return True
    parts = stored.split("$")
    if len(parts) != 3:
        return False
    method, salt, digest = parts
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", salt):
        return False
    if method.startswith("pbkdf2:sha256:"):
        match = re.fullmatch(r"pbkdf2:sha256:(\d{1,8})", method)
        if not match or not 100_000 <= int(match.group(1)) <= 2_000_000:
            return False
        return bool(re.fullmatch(r"[0-9a-fA-F]{64}", digest))
    if method.startswith("scrypt:"):
        match = re.fullmatch(r"scrypt:(\d+):(\d+):(\d+)", method)
        if not match:
            return False
        n_value, r_value, p_value = (int(value) for value in match.groups())
        if not (
            16_384 <= n_value <= 65_536
            and n_value & (n_value - 1) == 0
            and 1 <= r_value <= 16
            and 1 <= p_value <= 4
        ):
            return False
        return bool(re.fullmatch(r"[0-9a-fA-F]{128}", digest))
    return False


def verify_password(raw, stored):
    """Return True if ``raw`` matches ``stored`` (Werkzeug or legacy SHA-256)."""
    if not stored or not _is_supported_password_hash(stored):
        return False
    if _looks_legacy_sha256(stored):
        digest = hashlib.sha256((raw or "").encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, stored)
    try:
        return check_password_hash(stored, raw or "")
    except Exception:
        return False


def check_and_migrate_password(conn, raw):
    """Verify the global password; upgrade a legacy SHA-256 hash on success."""
    stored = get_setting(conn, "auth_password_hash", "") or ""
    if not verify_password(raw, stored):
        return False
    if _looks_legacy_sha256(stored):
        set_setting(conn, "auth_password_hash", hash_password(raw))
        conn.commit()
        log.info("Migrated legacy SHA-256 password hash to Werkzeug hash")
    return True


# ---------------------------------------------------------------------------
# Brute-force protection (per IP + route, persisted in login_attempts)
# ---------------------------------------------------------------------------
def _client_ip():
    return request.remote_addr or "unknown"


def rate_limit_status(conn, route):
    """Return (locked: bool, retry_after_seconds: int)."""
    if has_request_context():
        g.rate_limit_global = False
    window = app.config["AUTH_LOGIN_WINDOW_SECONDS"]
    max_attempts = app.config["AUTH_LOGIN_MAX_ATTEMPTS"]
    lock = app.config["AUTH_LOCK_SECONDS"]
    global_window = app.config["GLOBAL_LOGIN_WINDOW_SECONDS"]
    global_max_attempts = app.config["GLOBAL_LOGIN_MAX_ATTEMPTS"]
    now = time.time()
    try:
        if global_max_attempts > 0:
            global_cutoff = now - global_window
            global_failures = conn.execute(
                "SELECT COUNT(*) AS c FROM login_attempts "
                "WHERE success=0 AND CAST(created_at AS REAL) > ?",
                (global_cutoff,),
            ).fetchone()["c"]
            if global_failures >= global_max_attempts:
                if has_request_context():
                    g.rate_limit_global = True
                return True, lock * 2

        rows = conn.execute(
            "SELECT CAST(created_at AS REAL) AS created_at "
            "FROM login_attempts WHERE ip=? AND route=? AND success=0 "
            "AND CAST(created_at AS REAL) > ?",
            (_client_ip(), route, now - window),
        ).fetchall()
        recent = [float(r["created_at"]) for r in rows]
        if max_attempts > 0 and len(recent) >= max_attempts:
            newest = max(recent)
            remaining = lock - (now - newest)
            if remaining > 0:
                return True, int(remaining) + 1
        return False, 0
    except (sqlite3.Error, TypeError, ValueError) as e:
        log.error("rate_limit_status failed route=%s ip=%s: %s", route, _client_ip(), e)
        return True, lock


def record_attempt(conn, route, success, *, commit=True):
    conn.execute(
        "INSERT INTO login_attempts(ip, route, success, created_at) VALUES(?,?,?,?)",
        (_client_ip(), route, 1 if success else 0, str(time.time())),
    )
    if success:
        conn.execute(
            "DELETE FROM login_attempts WHERE ip=? AND route=?", (_client_ip(), route)
        )
    # Opportunistically drop rows older than the rate-limit horizon so the table
    # stays bounded even against IPs that fail forever (created_at is stored as a
    # stringified epoch, so compare numerically).
    cutoff = time.time() - max(
        app.config["AUTH_LOGIN_WINDOW_SECONDS"],
        app.config["AUTH_LOCK_SECONDS"],
        app.config["GLOBAL_LOGIN_WINDOW_SECONDS"],
        app.config["AUTH_LOCK_SECONDS"] * 2,
    ) - 60
    conn.execute("DELETE FROM login_attempts WHERE CAST(created_at AS REAL) < ?", (cutoff,))
    if commit:
        conn.commit()
    log.info("auth attempt route=%s ip=%s success=%s", route, _client_ip(), bool(success))


# ---------------------------------------------------------------------------
# Permission / authentication service
# ---------------------------------------------------------------------------
def get_auth_mode():
    return getattr(g, "auth_mode", None) or "off"


def auth_configured():
    # ``off`` is intentionally public even when an owner keeps a password
    # staged for a later mode switch. Checking only hash presence used to send
    # public owners to /login unexpectedly.
    return bool(getattr(g, "has_password", False) and get_auth_mode() != "off")


def is_global_authenticated():
    if not session.get("auth_ok"):
        return False
    revision = getattr(g, "auth_revision", None)
    if revision is None or str(session.get("auth_revision", "")) != str(revision):
        return False
    sid = _current_session_id(create=False)
    if not sid:
        return False
    cached = getattr(g, "_auth_session_valid", None)
    if cached is not None:
        return cached
    try:
        row = get_db().execute(
            "SELECT auth_revision FROM auth_sessions WHERE session_id=?", (sid,)
        ).fetchone()
        valid = bool(row and str(row["auth_revision"]) == str(revision))
    except sqlite3.Error:
        log.exception("failed to validate authenticated session")
        valid = False
    g._auth_session_valid = valid
    return valid


def can_manage():
    """Whether this request has owner-level write access."""
    return not auth_configured() or is_global_authenticated()


SESSION_ID_KEY = "sid"


def _current_session_id(create=False):
    if not has_request_context():
        return None
    sid = session.get(SESSION_ID_KEY)
    if isinstance(sid, str) and len(sid) >= 32:
        return sid
    if not create:
        return None
    sid = secrets.token_urlsafe(32)
    session[SESSION_ID_KEY] = sid
    session.permanent = True
    return sid


def _clear_server_session_state(conn=None):
    sid = session.get(SESSION_ID_KEY) if has_request_context() else None
    if not sid:
        return
    db = conn or get_db()
    try:
        db.execute("DELETE FROM prompt_unlocks WHERE session_id=?", (sid,))
        db.execute("DELETE FROM auth_sessions WHERE session_id=?", (sid,))
        db.commit()
    except sqlite3.Error:
        db.rollback()
        log.exception("failed to clear server session state")


def _reset_session(conn=None, authenticated=False):
    flashes = session.get("_flashes")
    _clear_server_session_state(conn)
    session.clear()
    if flashes:
        session["_flashes"] = flashes
    session.permanent = True
    session[SESSION_ID_KEY] = secrets.token_urlsafe(32)
    if authenticated:
        revision = getattr(g, "auth_revision", None)
        if revision is None:
            db = conn or get_db()
            revision = get_setting(db, "auth_revision", "1")
        db = conn or get_db()
        db.execute(
            "INSERT OR REPLACE INTO auth_sessions(session_id, auth_revision, authenticated_at) "
            "VALUES(?,?,?)",
            (session[SESSION_ID_KEY], str(revision or "1"), now_ts()),
        )
        db.commit()
        session["auth_ok"] = True
        session["auth_at"] = now_ts()
        session["auth_revision"] = str(revision or "1")
        g._auth_session_valid = True


def mark_prompt_unlocked(conn, prompt_id):
    sid = _current_session_id(create=True)
    if not sid:
        raise RuntimeError("missing request session")
    conn.execute(
        "INSERT OR REPLACE INTO prompt_unlocks("
        "session_id, prompt_id, unlocked_at, auth_revision) VALUES(?,?,?,?)",
        (sid, prompt_id, now_ts(), str(get_setting(conn, "auth_revision", "1") or "1")),
    )
    if has_request_context():
        g.pop("_unlocked_prompt_ids", None)


def get_unlocked_prompt_ids():
    if not has_request_context():
        return set()
    cached = getattr(g, "_unlocked_prompt_ids", None)
    if cached is not None:
        return cached
    # Legacy client-side unlock lists are intentionally ignored and cleared. Unlock
    # state is now verified against the server-side prompt_unlocks table so a forged
    # session cookie cannot grant access to arbitrary protected prompts.
    if "unlocked_prompts" in session:
        session.pop("unlocked_prompts", None)
    sid = _current_session_id(create=False)
    if not sid:
        return set()
    try:
        rows = get_db().execute(
            "SELECT prompt_id FROM prompt_unlocks "
            "WHERE session_id=? AND auth_revision=?",
            (sid, str(getattr(g, "auth_revision", "1") or "1")),
        ).fetchall()
        result = {int(r["prompt_id"]) for r in rows}
        g._unlocked_prompt_ids = result
        return result
    except (sqlite3.Error, TypeError, ValueError):
        log.exception("failed to load unlocked prompt ids")
        g._unlocked_prompt_ids = set()
        return set()


def _prompt_requires_password(prompt):
    try:
        return bool(prompt["require_password"])
    except (KeyError, IndexError, TypeError):
        return False


def can_view_prompt(prompt):
    """Whether the current session may see this prompt's protected content."""
    mode = get_auth_mode()
    if mode == "off":
        return True
    if is_global_authenticated():  # logged-in owner sees everything
        return True
    if mode == "global":
        return False  # before_request normally redirects unauthenticated users
    # per-prompt mode, not globally authenticated:
    if not _prompt_requires_password(prompt):
        return True
    return int(prompt["id"]) in get_unlocked_prompt_ids()


def fetch_prompt(conn, prompt_id):
    return conn.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()


def require_prompt_access(conn, prompt_id, *, for_write=False):
    """Return (prompt, response). ``response`` is non-None when the caller must
    short-circuit: a redirect to the unlock page for GET reads, or a 403 abort
    for writes / API calls."""
    prompt = fetch_prompt(conn, prompt_id)
    if not prompt:
        if for_write:
            abort(404)
        flash("未找到该提示词", "error")
        return None, redirect(url_for("index"))
    if can_view_prompt(prompt):
        return prompt, None
    if for_write:
        log.warning("blocked write to locked prompt id=%s ip=%s", prompt_id, _client_ip())
        abort(403)
    # GET on a locked prompt -> send to unlock page (per mode) or login (global)
    if get_auth_mode() == "global":
        return prompt, redirect(url_for("login", next=request.full_path.rstrip("?")))
    return prompt, redirect(
        url_for("unlock_prompt", prompt_id=prompt_id, next=request.full_path.rstrip("?"))
    )


def accessible_prompt_ids(conn):
    rows = conn.execute("SELECT id, require_password FROM prompts").fetchall()
    return {r["id"] for r in rows if can_view_prompt(r)}


def exportable_prompt_ids(conn):
    """Prompts included in a *default* export.

    Distinct from :func:`accessible_prompt_ids`: even an authenticated owner does
    not get protected-but-unlocked prompts in a default export. Per spec, the
    full backup requires the explicit ``include_locked=1`` flag.
    """
    mode = get_auth_mode()
    rows = conn.execute("SELECT id, require_password FROM prompts").fetchall()
    if mode in ("off", "global"):
        return {r["id"] for r in rows}
    # A default export is a portable, non-secret view. An unlock grants read
    # access for the current session only; it must not silently promote a
    # protected prompt into a normal export. Use the explicit full-export flag
    # when an owner intentionally needs those records.
    return {r["id"] for r in rows if not r["require_password"]}


def require_admin():
    """Guard settings/export. Returns a response to short-circuit, or None.

    When no password is configured (fresh install / off mode) the area is open
    so the owner can perform first-time setup. Once a password exists, an
    authenticated session is required.
    """
    if not auth_configured():
        return None
    if is_global_authenticated():
        return None
    if request.method == "GET":
        flash("请先登录以访问该页面", "error")
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    abort(403)


# ---------------------------------------------------------------------------
# CSRF (lightweight, session-based)
# ---------------------------------------------------------------------------
def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


CSRF_EXEMPT_ENDPOINTS = {"healthz"}
PUBLIC_ENDPOINTS = {"setup", "login", "static", "healthz", "logo_png", "favicon"}
MANAGE_ENDPOINTS = {
    "new_prompt", "prompt_detail", "toggle_pin", "toggle_favorite", "toggle_archive",
    "delete_prompt", "rollback_version", "mark_copied",
}


def _valid_csrf():
    # Read the header first so API clients can be validated without forcing a
    # multipart body parse; ordinary HTML forms fall back to the form field.
    sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
    stored = session.get("csrf_token")
    return bool(sent and stored and hmac.compare_digest(sent, stored))


# ---------------------------------------------------------------------------
# Request lifecycle
# ---------------------------------------------------------------------------
@app.before_request
def _before():
    if request.endpoint in ("static", "healthz", "logo_png", "favicon") or request.path.startswith("/static/"):
        return
    if request.endpoint is None:
        return

    # Load per-request settings once (on the request-scoped connection, which is
    # reused by the route and closed by _close_db).
    conn = get_db()
    settings_map = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM settings "
            "WHERE key IN ('auth_mode', 'language', 'auth_password_hash', "
            "'auth_revision', 'bootstrap_completed')"
        ).fetchall()
    }
    g.auth_mode = (settings_map.get("auth_mode") or "off")
    lang = (settings_map.get("language") or LANG_DEFAULT).lower()
    g.language = lang if lang in SUPPORTED_LANGS else "zh"
    g.has_password = bool(settings_map.get("auth_password_hash") or "")
    g.auth_revision = settings_map.get("auth_revision") or "1"
    g.bootstrap_completed = settings_map.get("bootstrap_completed") == "1"

    # A genuinely new production database exposes no application/auth surface
    # until a single owner has claimed it through /setup. Existing databases are
    # marked complete by the migration and never enter this branch.
    if not _bootstrap_is_complete(conn) and request.endpoint != "setup":
        if request.method in ("GET", "HEAD"):
            return redirect(url_for("setup"), code=303)
        return jsonify({"status": "setup_required"}), 503

    # Anonymous per-prompt visitors do not need a database-backed logout
    # operation. Rejecting this endpoint before any write prevents a public
    # CSRF token from being used to contend for SQLite's writer lock.
    if (
        request.endpoint == "logout"
        and not is_global_authenticated()
        and not _current_session_id(create=False)
    ):
        return redirect(url_for("index"))

    # Global password mode: gate everything except public endpoints.
    if g.auth_mode == "global" and g.has_password and request.endpoint not in PUBLIC_ENDPOINTS:
        if not is_global_authenticated():
            nxt = request.full_path.rstrip("?") if request.query_string else request.path
            return redirect(url_for("login", next=nxt))

    # In per-prompt mode the site can be browsed anonymously, but that must not
    # turn the shared password into an anonymous write API. Prompt unlocks grant
    # read access only; owner actions require an explicit site login.
    if (
        g.has_password
        and request.endpoint in MANAGE_ENDPOINTS
        and (request.method != "GET" or request.endpoint == "new_prompt")
        and not can_manage()
    ):
        if request.method == "GET":
            nxt = request.full_path.rstrip("?") if request.query_string else request.path
            return redirect(url_for("login", next=nxt))
        abort(403)

    # CSRF protection for state-changing requests runs after authentication
    # gating. This lets an anonymous hostile upload fail at the auth boundary
    # before Werkzeug parses a potentially large multipart body to inspect a
    # form token.
    if (
        request.method in ("POST", "PUT", "PATCH", "DELETE")
        and request.endpoint not in CSRF_EXEMPT_ENDPOINTS
    ):
        try:
            ok = _valid_csrf()
        except BadRequest:
            abort(400)
        if not ok:
            log.warning("CSRF rejected endpoint=%s ip=%s", request.endpoint, _client_ip())
            abort(403)


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("_db", None)
    if conn is not None:
        conn.close()


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Build-SHA", app.config.get("BUILD_SHA", "dev"))
    if app.config.get("ENABLE_SECURITY_HEADERS"):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; "
            "form-action 'self'",
        )
    if app.config.get("ENABLE_HSTS") and request.is_secure:
        max_age = max(0, int(app.config.get("HSTS_MAX_AGE", 0)))
        value = f"max-age={max_age}"
        if app.config.get("HSTS_INCLUDE_SUBDOMAINS"):
            value += "; includeSubDomains"
        resp.headers.setdefault("Strict-Transport-Security", value)

    endpoint = request.endpoint
    auth_mode = getattr(g, "auth_mode", "off")
    if endpoint in _SENSITIVE_ENDPOINTS or (
        auth_mode != "off" and endpoint not in {"static", "healthz", "logo_png", "favicon"}
    ):
        resp.headers["Cache-Control"] = "private, no-store"
        resp.headers["Pragma"] = "no-cache"
        resp.headers.setdefault("Vary", "Cookie")
    return resp


@app.context_processor
def _inject_globals():
    lang = getattr(g, "language", LANG_DEFAULT)
    configured = auth_configured()
    authenticated = is_global_authenticated()

    return {
        "t": lambda s: translate(lang, s),
        "lang": lang,
        "lang_html": "en" if lang == "en" else "zh-CN",
        "csrf_token": get_csrf_token,
        "auth_mode": get_auth_mode(),
        "is_authenticated": authenticated,
        "auth_configured": configured,
        "can_manage": not configured or authenticated,
        "has_unlocks": False if authenticated else bool(get_unlocked_prompt_ids()),
    }


@app.errorhandler(RequestEntityTooLarge)
def _too_large(_e):
    # The same hard cap guards image uploads (new/edit prompt) and data imports;
    # use wording that matches whichever endpoint was hit.
    if request.endpoint in ("new_prompt", "prompt_detail"):
        flash("上传失败：文件过大", "error")
    else:
        flash("导入失败：文件过大", "error")
    return redirect(_safe_referrer(url_for("index"))), 303


@app.errorhandler(sqlite3.OperationalError)
def _database_operational_error(error):
    if _is_db_locked(error):
        log.warning("request failed because SQLite is busy: %s", error)
        return _db_busy_response(url_for("index"))
    log.exception("database operational error")
    return jsonify({"status": "error", "message": "database unavailable"}), 503


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        initialized = _bootstrap_is_complete(conn)
    except Exception:
        log.exception("healthz database check failed")
        return jsonify({"status": "error", "build_sha": app.config["BUILD_SHA"]}), 500
    response = jsonify({
        "status": "ok",
        "build_sha": app.config["BUILD_SHA"],
        "initialized": initialized,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """Claim a brand-new production database exactly once."""
    conn = get_db()
    if _bootstrap_is_complete(conn):
        abort(404)

    expected_token = app.config.get("BOOTSTRAP_TOKEN", "") or ""
    selected_mode = request.form.get("auth_mode", "global") if request.method == "POST" else "global"
    if selected_mode not in {"global", "per"}:
        selected_mode = "global"

    def render_setup_error(message, status=400):
        flash(message, "error")
        return render_template(
            "setup.html", require_token=True, selected_auth_mode=selected_mode,
            setup_disabled=False,
        ), status

    if not expected_token:
        log.error("BOOTSTRAP_TOKEN is required before first-time production setup")
        flash("服务端尚未配置 BOOTSTRAP_TOKEN，初始化已禁用", "error")
        if request.method == "POST":
            return jsonify({"status": "bootstrap_token_required"}), 503
        return render_template(
            "setup.html", require_token=True, selected_auth_mode="global",
            setup_disabled=True,
        ), 503

    if request.method == "POST":
        locked, retry = rate_limit_status(conn, "setup")
        if locked:
            response = jsonify({"status": "rate_limited", "retry_after": retry})
            response.headers["Retry-After"] = str(retry)
            return response, 429

        supplied_token = request.form.get("bootstrap_token") or ""
        if not hmac.compare_digest(supplied_token, expected_token):
            record_attempt(conn, "setup", False)
            return render_setup_error("初始化令牌不正确")

        mode = selected_mode
        password = request.form.get("new_password") or request.form.get("password") or ""
        confirmation = request.form.get("confirm_password") or ""
        if mode not in {"global", "per"}:
            return render_setup_error("初始化时必须启用访问认证")
        if len(password) < 8:
            return render_setup_error("密码长度至少为 8 位")
        if password != confirmation:
            return render_setup_error("两次输入的密码不一致")

        try:
            conn.execute("BEGIN IMMEDIATE")
            if _bootstrap_is_complete(conn):
                conn.rollback()
                abort(409)
            set_setting(conn, "auth_mode", mode)
            set_setting(conn, "auth_password_hash", hash_password(password))
            set_setting(conn, "auth_revision", "2")
            set_setting(conn, "bootstrap_completed", "1")
            conn.execute("DELETE FROM prompt_unlocks")
            conn.execute("DELETE FROM auth_sessions")
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            log.exception("first-time setup failed")
            return render_setup_error("初始化失败，请重试", status=503)

        record_attempt(conn, "setup", True)
        g.auth_mode = mode
        g.has_password = True
        g.auth_revision = "2"
        g.bootstrap_completed = True
        _reset_session(conn, authenticated=True)
        flash("初始化完成", "success")
        return redirect(url_for("settings"), code=303)

    return render_template(
        "setup.html", require_token=True, selected_auth_mode=selected_mode,
        setup_disabled=False,
    )


# ---------------------------------------------------------------------------
# Static-ish assets
# ---------------------------------------------------------------------------
@app.route("/logo.png")
def logo_png():
    logo_path = os.path.join(app.root_path, "logo.png")
    if not os.path.exists(logo_path):
        return ("", 404)
    return send_file(logo_path, mimetype="image/png", max_age=86400)


@app.route("/favicon.ico")
def favicon():
    return logo_png()


# ---------------------------------------------------------------------------
# Image upload parsing
# ---------------------------------------------------------------------------
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
ALLOWED_IMAGE_MIME = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


def _image_matches_mime(raw, mime):
    if mime in ("image/jpeg", "image/jpg"):
        return raw.startswith(b"\xff\xd8\xff")
    if mime == "image/png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/webp":
        return len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
    return False


def _sanitize_image_data(val):
    """Return ``val`` only if it is a data: URI with an allowed image MIME type.

    Import payloads can carry an arbitrary ``image_data`` string. Without this
    filter a crafted file could store e.g. ``data:text/html;base64,...`` which
    :func:`prompt_image` would then serve as text/html (a stored-XSS vector), and
    an empty CSV cell ('') would round-trip into the DB and later render as a
    broken image. Anything that is not a recognised image data URI is dropped to
    NULL.
    """
    if not val or not isinstance(val, str) or not val.startswith("data:"):
        return None
    try:
        header, payload = val.split(",", 1)
        mime = header[5:].split(";", 1)[0].strip().lower()
        if mime not in ALLOWED_IMAGE_MIME or ";base64" not in header.lower():
            return None
        if mime == "image/jpg":
            mime = "image/jpeg"
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        return None
    max_bytes = app.config["MAX_IMAGE_SIZE_MB"] * 1024 * 1024
    if not raw or len(raw) > max_bytes or not _image_matches_mime(raw, mime):
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def parse_image_upload(req):
    """Return (image_data_uri | None, remove_image: bool, error_text | None)."""
    remove_image = req.form.get("remove_image") == "1"
    f = req.files.get("image_file")
    if not f or not f.filename:
        return None, remove_image, None

    # 仅用扩展名做初筛；上传的文件名不会落盘（图片以 base64 内联存库），所以无需
    # secure_filename —— 它会丢弃中文等非 ASCII 文件名，使“图片.png”被误判为无扩展名
    # 而被拒。真正的校验由下面的 MIME 与大小检查完成。
    raw_name = f.filename or ""
    ext = raw_name.rsplit(".", 1)[-1].lower() if "." in raw_name else ""
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return None, remove_image, "图片上传失败：仅支持 jpg/jpeg/png/webp 格式"

    mime = (f.mimetype or "").lower()
    if mime not in ALLOWED_IMAGE_MIME:
        return None, remove_image, "图片上传失败：仅支持 jpg/jpeg/png/webp 格式"
    if mime == "image/jpg":
        mime = "image/jpeg"

    max_bytes = app.config["MAX_IMAGE_SIZE_MB"] * 1024 * 1024
    raw = f.read(max_bytes + 1)
    if not raw:
        return None, remove_image, "图片上传失败：图片不能为空"
    if len(raw) > max_bytes:
        return None, remove_image, f"图片上传失败：文件大小不能超过 {app.config['MAX_IMAGE_SIZE_MB']}MB"
    if not _image_matches_mime(raw, mime):
        return None, remove_image, "图片上传失败：文件内容与图片格式不匹配"

    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}", remove_image, None


# ---------------------------------------------------------------------------
# Card view-models (never leak protected content to templates)
# ---------------------------------------------------------------------------
PREVIEW_LEN = 180  # characters of content shown on a home-page card
MAX_SEARCH_QUERY_LENGTH = 256


def _safe_tags(row):
    try:
        arr = json.loads(row["tags"]) if row["tags"] else []
        if not isinstance(arr, list):
            return []
        # Old/imported databases may contain mixed values. Keep only stable
        # string tags so sorting and filtering can never raise TypeError.
        out = []
        seen = set()
        for value in arr:
            if not isinstance(value, str):
                continue
            value = value.strip()
            if value and value not in seen:
                out.append(value)
                seen.add(value)
        return out
    except Exception as e:
        prompt_id = row["id"] if row is not None and "id" in row.keys() else "unknown"
        log.warning("invalid tags JSON for prompt id=%s: %s", prompt_id, e)
        return []


def build_card(row, locked):
    """Return a dict for a prompt card. Locked cards expose name + status only."""
    if locked:
        return {
            "id": row["id"],
            "name": row["name"],
            "locked": True,
            "require_password": True,
        }
    content = row["current_content"] if "current_content" in row.keys() else None
    return {
        "id": row["id"],
        "name": row["name"],
        "locked": False,
        "require_password": bool(row["require_password"]),
        "source": row["source"],
        "notes": row["notes"],
        "color": row["color"],
        "tags": _safe_tags(row),
        "has_image": bool(row["has_image"]) if "has_image" in row.keys() else bool(row["image_data"]),
        "pinned": bool(row["pinned"]),
        "favorite": bool(row["favorite"]),
        "archived": bool(row["archived_at"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "current_version": row["current_version"] if "current_version" in row.keys() else None,
        "has_content": bool(content),
        "preview": (content or "")[:PREVIEW_LEN],
        "truncated": bool(content) and len(content) > PREVIEW_LEN,
    }


# ---------------------------------------------------------------------------
# Home / listing
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    conn = get_db()
    values = request.form if request.method == "POST" else request.args
    q = values.get("q", "").strip()[:MAX_SEARCH_QUERY_LENGTH]
    sort = values.get("sort", "updated")
    view = values.get("view", "all")  # all|favorites|pinned|locked|archived
    order_by_map = {
        "created": "p.pinned DESC, p.created_at DESC, p.id DESC",
        "name": "p.pinned DESC, p.name COLLATE NOCASE ASC",
        "tags": "p.pinned DESC, p.tags COLLATE NOCASE ASC",
        "updated": "p.pinned DESC, p.updated_at DESC, p.id DESC",
    }
    order_clause = order_by_map.get(sort, order_by_map["updated"])

    selected_tags = [t for t in values.getlist("tag") if t.strip()]
    if not selected_tags and values.get("tags"):
        selected_tags = [t.strip() for t in values.get("tags", "").replace("，", ",").split(",") if t.strip()]
    selected_sources = [s for s in values.getlist("source") if s.strip()]
    if not selected_sources and values.get("sources"):
        selected_sources = [s.strip() for s in values.get("sources", "").replace("，", ",").split(",") if s.strip()]

    rows = conn.execute(
        f"""
        SELECT p.id, p.name, p.source, p.notes, p.color, p.tags, p.pinned, p.favorite,
               p.archived_at, p.created_at, p.updated_at, p.require_password,
               (p.image_data IS NOT NULL) AS has_image,
               v.content AS current_content, v.version AS current_version
        FROM prompts p
        LEFT JOIN versions v ON v.id = p.current_version_id AND v.prompt_id = p.id
        ORDER BY {order_clause}
        """
    ).fetchall()

    def norm_source(s):
        return (s or "").strip() or "(empty)"

    ql = q.lower()
    cards = []
    scope_counts = {"all": 0, "favorites": 0, "pinned": 0, "locked": 0, "archived": 0}
    tag_counts, source_counts = {}, {}
    for row in rows:
        locked = not can_view_prompt(row)

        archived = bool(row["archived_at"])
        if locked:
            scope_counts["locked"] += 1
            if not archived:
                scope_counts["all"] += 1
        else:
            if archived:
                scope_counts["archived"] += 1
            else:
                scope_counts["all"] += 1
                if row["favorite"]:
                    scope_counts["favorites"] += 1
                if row["pinned"]:
                    scope_counts["pinned"] += 1

        # A locked prompt may only reveal its name and protected state. It is
        # therefore omitted from scopes that would disclose favorite, pin, or
        # archive state. The protected scope intentionally includes protected
        # prompts regardless of their archived state.
        if locked:
            if view in {"favorites", "pinned", "archived"}:
                continue
            if view == "all" and archived:
                continue
        elif view == "archived":
            if not archived:
                continue
        else:
            if archived:
                continue
            if view == "favorites" and not row["favorite"]:
                continue
            if view == "pinned" and not row["pinned"]:
                continue
            if view == "locked" and not _prompt_requires_password(row):
                continue

        # Search: accessible prompts match across fields; locked prompts only by
        # name (their protected fields must never participate in matching).
        if q:
            if locked:
                if ql not in (row["name"] or "").lower():
                    continue
            else:
                haystack = " ".join([
                    row["name"] or "",
                    row["source"] or "",
                    row["notes"] or "",
                    " ".join(_safe_tags(row)),
                    row["current_content"] or "",
                ]).lower()
                if ql not in haystack:
                    continue

        # Facet filters operate on visible (non-locked) fields only.
        if selected_tags or selected_sources:
            if locked:
                continue
            row_tags = _safe_tags(row)
            if selected_tags and not any(t in row_tags for t in selected_tags):
                continue
            if selected_sources and norm_source(row["source"]) not in selected_sources:
                continue

        # Facet counts + suggestions exclude locked prompts.
        if not locked:
            for t in _safe_tags(row):
                tag_counts[t] = tag_counts.get(t, 0) + 1
            src = norm_source(row["source"])
            source_counts[src] = source_counts.get(src, 0) + 1

        cards.append(build_card(row, locked))

    # Database ordering must not let protected tags, timestamps, or status
    # influence their visible position. Sort accessible cards by the requested
    # field, then append locked cards in public-name order.
    visible_cards = [card for card in cards if not card["locked"]]
    locked_cards = [card for card in cards if card["locked"]]
    if sort == "name":
        visible_cards.sort(key=lambda card: ((card["name"] or "").casefold(), card["id"]))
    elif sort == "tags":
        visible_cards.sort(key=lambda card: (tuple(tag.casefold() for tag in card["tags"]), (card["name"] or "").casefold(), card["id"]))
    elif sort == "created":
        visible_cards.sort(key=lambda card: (card["created_at"] or "", card["id"]), reverse=True)
    else:
        visible_cards.sort(key=lambda card: (card["updated_at"] or "", card["id"]), reverse=True)
    visible_cards.sort(key=lambda card: not card["pinned"])
    locked_cards.sort(key=lambda card: ((card["name"] or "").casefold(), card["id"]))
    cards = visible_cards + locked_cards

    tag_suggestions = sorted(tag_counts.keys())
    conn.close()

    return render_template(
        "index.html",
        cards=cards,
        q=q,
        sort=sort,
        view=view,
        tag_suggestions=tag_suggestions,
        tag_counts=tag_counts,
        source_counts=source_counts,
        scope_counts=scope_counts,
        selected_tags=selected_tags,
        selected_sources=selected_sources,
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@app.route("/prompt/new", methods=["GET", "POST"])
def new_prompt():
    if request.method == "POST":
        name = request.form.get("name", "").strip() or "未命名提示词"
        source = request.form.get("source", "").strip()
        notes = request.form.get("notes", "").strip()
        color = sanitize_color(request.form.get("color"))
        tags = parse_tags(request.form.get("tags", ""))
        content = request.form.get("content", "")
        if not content.strip():
            flash("请输入提示词内容", "error")
            return redirect(url_for("new_prompt"))
        bump_kind = request.form.get("bump_kind", "patch")
        require_password = 1 if get_auth_mode() == "per" and request.form.get("require_password") == "1" else 0
        image_data, _, image_error = parse_image_upload(request)
        if image_error:
            flash(image_error, "error")
            return redirect(url_for("new_prompt"))

        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            ts = now_ts()
            cur.execute(
                "INSERT INTO prompts(name, source, notes, color, tags, image_data, pinned, "
                "created_at, updated_at, require_password) VALUES(?,?,?,?,?,?,0,?,?,?)",
                (name, source, notes, color, json.dumps(tags, ensure_ascii=False), image_data, ts, ts, require_password),
            )
            pid = cur.lastrowid
            version = bump_version(None, bump_kind)
            cur.execute(
                "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) "
                "VALUES(?,?,?,?,NULL)",
                (pid, version, content, ts),
            )
            cur.execute("UPDATE prompts SET current_version_id=? WHERE id=?", (cur.lastrowid, pid))
            prune_versions(conn, pid)
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if _is_db_locked(exc):
                conn.close()
                return _db_busy_response(url_for("new_prompt"))
            log.exception("create prompt failed")
            conn.close()
            flash("创建失败，请重试", "error")
            return redirect(url_for("new_prompt"))
        except sqlite3.Error:
            conn.rollback()
            log.exception("create prompt failed")
            conn.close()
            flash("创建失败，请重试", "error")
            return redirect(url_for("new_prompt"))
        conn.close()
        flash("已创建提示词并保存首个版本", "success")
        return redirect(url_for("prompt_detail", prompt_id=pid))

    return render_template("prompt_detail.html", prompt=None, versions=[], current=None)


# ---------------------------------------------------------------------------
# Detail / edit
# ---------------------------------------------------------------------------
@app.route("/prompt/<int:prompt_id>", methods=["GET", "POST"])
def prompt_detail(prompt_id):
    conn = get_db()
    if request.method == "POST":
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            conn.close()
            if _is_db_locked(exc):
                return _db_busy_response(url_for("prompt_detail", prompt_id=prompt_id))
            raise
        prompt, guard = require_prompt_access(conn, prompt_id, for_write=True)
        if guard is not None:
            conn.rollback()
            return guard

        name = request.form.get("name", "").strip() or "未命名提示词"
        source = request.form.get("source", "").strip()
        notes = request.form.get("notes", "").strip()
        color = sanitize_color(request.form.get("color"))
        tags = parse_tags(request.form.get("tags", ""))
        content = request.form.get("content", "")
        if not content.strip():
            conn.rollback()
            flash("请输入提示词内容", "error")
            return redirect(url_for("prompt_detail", prompt_id=prompt_id))
        bump_kind = request.form.get("bump_kind", "patch")
        do_save_version = request.form.get("do_save_version") == "1"
        # A disabled checkbox is absent from the browser submission. Preserve an
        # existing protection flag whenever per-prompt mode is not active.
        require_password = (
            1 if request.form.get("require_password") == "1" else 0
        ) if get_auth_mode() == "per" else int(bool(prompt["require_password"]))
        ts = now_ts()

        new_image_data, remove_image, image_error = parse_image_upload(request)
        if image_error:
            conn.rollback()
            flash(image_error, "error")
            return redirect(url_for("prompt_detail", prompt_id=prompt_id))

        old_image_data = prompt["image_data"]
        if new_image_data:
            final_image_data = new_image_data
        elif remove_image:
            final_image_data = None
        else:
            final_image_data = old_image_data

        try:
            old_require_password = int(bool(prompt["require_password"]))
            conn.execute(
                "UPDATE prompts SET name=?, source=?, notes=?, color=?, tags=?, image_data=?, "
                "updated_at=?, require_password=? WHERE id=?",
                (name, source, notes, color, json.dumps(tags, ensure_ascii=False),
                 final_image_data, ts, require_password, prompt_id),
            )
            if old_require_password != require_password:
                # A protection transition invalidates every per-session unlock,
                # including a transition back to protected mode.
                conn.execute("DELETE FROM prompt_unlocks WHERE prompt_id=?", (prompt_id,))

            if do_save_version:
                row = conn.execute(
                "SELECT v.version FROM prompts p LEFT JOIN versions v "
                "ON v.id=p.current_version_id AND v.prompt_id=p.id WHERE p.id=?",
                    (prompt_id,),
                ).fetchone()
                new_ver = bump_version(row["version"] if row else None, bump_kind)
                cur = conn.execute(
                    "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) "
                    "VALUES(?,?,?,?,(SELECT current_version_id FROM prompts WHERE id=?))",
                    (prompt_id, new_ver, content, ts, prompt_id),
                )
                new_version_id = cur.lastrowid
                conn.execute(
                    "UPDATE prompts SET current_version_id=?, updated_at=? WHERE id=?",
                    (new_version_id, ts, prompt_id),
                )
                prune_versions(conn, prompt_id)
            else:
                prompt_row = conn.execute(
                    "SELECT current_version_id FROM prompts WHERE id=?", (prompt_id,)
                ).fetchone()
                current_id = prompt_row["current_version_id"] if prompt_row else None
                if current_id:
                    cur = conn.execute(
                        "UPDATE versions SET content=? WHERE id=? AND prompt_id=?",
                        (content, current_id, prompt_id),
                    )
                    if cur.rowcount == 0:
                        current_id = None
                if not current_id:
                    cur = conn.execute(
                        "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) "
                        "VALUES(?,?,?,?,NULL)",
                        (prompt_id, "1.0.0", content, ts),
                    )
                    conn.execute(
                        "UPDATE prompts SET current_version_id=?, updated_at=? WHERE id=?",
                        (cur.lastrowid, ts, prompt_id),
                    )

            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            log.exception("save prompt failed id=%s", prompt_id)
            conn.close()
            flash("保存失败，请重试", "error")
            return redirect(url_for("prompt_detail", prompt_id=prompt_id))
        conn.close()
        flash("已保存", "success")
        return redirect(url_for("prompt_detail", prompt_id=prompt_id))

    prompt, guard = require_prompt_access(conn, prompt_id)
    if guard is not None:
        conn.close()
        return guard
    # The detail template only needs the current version's content; it links to
    # the versions page rather than listing versions, so don't load them all here.
    current = (
        conn.execute(
            "SELECT * FROM versions WHERE id=? AND prompt_id=?",
            (prompt["current_version_id"], prompt_id),
        ).fetchone()
        if prompt["current_version_id"] else None
    )
    conn.close()
    return render_template(
        "prompt_detail.html", prompt=dict(prompt), prompt_tags=_safe_tags(prompt),
        versions=[], current=current,
    )


# ---------------------------------------------------------------------------
# Status toggles (pin / favorite / archive) + copy tracking
# ---------------------------------------------------------------------------
# Allowed toggle columns are a fixed whitelist (never user input), so the
# f-string in the helper is safe. ``touch_updated`` preserves the original
# behaviour: pinning bumps updated_at, favorite/archive do not.
_TOGGLE_COLUMNS = {"pinned", "favorite", "archived_at"}


def _toggle_prompt_flag(prompt_id, column, value_fn, touch_updated=False):
    """Toggle a boolean/timestamp prompt flag using a fixed column whitelist."""
    if column not in _TOGGLE_COLUMNS:
        raise ValueError(f"Invalid column name: {column}")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prompt, guard = require_prompt_access(conn, prompt_id, for_write=True)
        if guard is not None:
            conn.rollback()
            return guard
        new_val = value_fn(prompt)
        if touch_updated:
            conn.execute(
                f"UPDATE prompts SET {column}=?, updated_at=? WHERE id=?",
                (new_val, now_ts(), prompt_id),
            )
        else:
            conn.execute(f"UPDATE prompts SET {column}=? WHERE id=?", (new_val, prompt_id))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if _is_db_locked(exc):
            log.warning("toggle prompt flag busy id=%s column=%s", prompt_id, column)
            return _db_busy_response(url_for("index"))
        log.exception("toggle prompt flag failed id=%s column=%s", prompt_id, column)
        abort(500)
    except Exception:
        conn.rollback()
        log.exception("toggle prompt flag failed id=%s column=%s", prompt_id, column)
        abort(500)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"status": "ok", "column": column, "enabled": bool(new_val)})
    return redirect(_safe_referrer(url_for("index")))


@app.route("/prompt/<int:prompt_id>/pin", methods=["POST"])
def toggle_pin(prompt_id):
    return _toggle_prompt_flag(
        prompt_id, "pinned", lambda p: 0 if p["pinned"] else 1, touch_updated=True
    )


@app.route("/prompt/<int:prompt_id>/favorite", methods=["POST"])
def toggle_favorite(prompt_id):
    return _toggle_prompt_flag(prompt_id, "favorite", lambda p: 0 if p["favorite"] else 1)


@app.route("/prompt/<int:prompt_id>/archive", methods=["POST"])
def toggle_archive(prompt_id):
    return _toggle_prompt_flag(
        prompt_id, "archived_at", lambda p: None if p["archived_at"] else now_ts()
    )


@app.route("/prompt/<int:prompt_id>/copied", methods=["POST"])
def mark_copied(prompt_id):
    conn = get_db()
    prompt, guard = require_prompt_access(conn, prompt_id, for_write=True)
    if guard is not None:
        conn.close()
        return guard
    try:
        conn.execute(
            "UPDATE prompts SET copy_count=copy_count+1, last_used_at=? WHERE id=?",
            (now_ts(), prompt_id),
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        conn.close()
        if _is_db_locked(exc):
            return _db_busy_response(url_for("prompt_detail", prompt_id=prompt_id))
        raise
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/prompt/<int:prompt_id>/image")
def prompt_image(prompt_id):
    """Serve a prompt's cover image as binary.

    The home listing references this instead of inlining a multi-MB base64 data
    URI into every card. Access is gated exactly like the prompt itself, and the
    response is cacheable.
    """
    conn = get_db()
    prompt = fetch_prompt(conn, prompt_id)
    if not prompt or not prompt["image_data"]:
        abort(404)
    if not can_view_prompt(prompt):
        abort(403)
    try:
        header, b64 = prompt["image_data"].split(",", 1)
        mime = header[5:].split(";")[0] or "application/octet-stream"
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, binascii.Error):
        log.warning("malformed image_data for prompt id=%s", prompt_id)
        abort(404)
    # Never serve a non-image MIME from stored/imported data: a crafted
    # data:text/html payload would otherwise be served as HTML (stored XSS).
    if mime not in ALLOWED_IMAGE_MIME:
        log.warning("blocked non-image image_data mime=%s for prompt id=%s", mime, prompt_id)
        abort(404)
    protected = get_auth_mode() == "global" or _prompt_requires_password(prompt)
    response = send_file(BytesIO(raw), mimetype=mime, max_age=0)
    if protected:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cookie"
    else:
        # Revalidate public covers so replacing an image is visible immediately.
        response.headers["Cache-Control"] = "private, no-cache"
    return response


# ---------------------------------------------------------------------------
# Delete / rollback
# ---------------------------------------------------------------------------
@app.route("/prompt/<int:prompt_id>/delete", methods=["POST"])
def delete_prompt(prompt_id):
    conn = get_db()
    _prompt, guard = require_prompt_access(conn, prompt_id, for_write=True)
    if guard is not None:
        conn.close()
        return guard
    try:
        conn.execute("DELETE FROM versions WHERE prompt_id=?", (prompt_id,))
        conn.execute("DELETE FROM prompts WHERE id=?", (prompt_id,))
        conn.commit()
        flash("已删除提示词及其所有版本", "success")
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if _is_db_locked(exc):
            log.warning("delete prompt busy id=%s", prompt_id)
            return _db_busy_response(url_for("prompt_detail", prompt_id=prompt_id))
        log.exception("delete prompt failed id=%s", prompt_id)
        flash("删除失败，请重试", "error")
    except Exception:
        conn.rollback()
        log.exception("delete prompt failed id=%s", prompt_id)
        flash("删除失败，请重试", "error")
    finally:
        conn.close()
    return redirect(url_for("index"))


@app.route("/prompt/<int:prompt_id>/rollback/<int:version_id>", methods=["POST"])
def rollback_version(prompt_id, version_id):
    bump_kind = request.form.get("bump_kind", "patch")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _prompt, guard = require_prompt_access(conn, prompt_id, for_write=True)
        if guard is not None:
            conn.rollback()
            return guard
        ver = conn.execute(
            "SELECT * FROM versions WHERE id=? AND prompt_id=?", (version_id, prompt_id)
        ).fetchone()
        if not ver:
            conn.rollback()
            flash("版本不存在", "error")
            return redirect(url_for("prompt_detail", prompt_id=prompt_id))
        row = conn.execute(
            "SELECT v.version FROM prompts p LEFT JOIN versions v "
            "ON v.id=p.current_version_id AND v.prompt_id=p.id WHERE p.id=?",
            (prompt_id,),
        ).fetchone()
        new_ver = bump_version(row["version"] if row else None, bump_kind)
        ts = now_ts()
        cur = conn.execute(
            "INSERT INTO versions(prompt_id, version, content, created_at, parent_version_id) "
            "VALUES(?,?,?,?,(SELECT current_version_id FROM prompts WHERE id=?))",
            (prompt_id, new_ver, ver["content"], ts, prompt_id),
        )
        conn.execute(
            "UPDATE prompts SET current_version_id=?, updated_at=? WHERE id=?",
            (cur.lastrowid, ts, prompt_id),
        )
        prune_versions(conn, prompt_id)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if _is_db_locked(exc):
            log.warning(
                "rollback version busy prompt_id=%s version_id=%s", prompt_id, version_id
            )
            return _db_busy_response(url_for("prompt_detail", prompt_id=prompt_id))
        log.exception("rollback version failed prompt_id=%s version_id=%s", prompt_id, version_id)
        flash("回滚失败，请重试", "error")
        return redirect(url_for("prompt_detail", prompt_id=prompt_id))
    except Exception:
        conn.rollback()
        log.exception("rollback version failed prompt_id=%s version_id=%s", prompt_id, version_id)
        flash("回滚失败，请重试", "error")
        return redirect(url_for("prompt_detail", prompt_id=prompt_id))
    flash("已从历史版本回滚并创建新版本", "success")
    return redirect(url_for("prompt_detail", prompt_id=prompt_id))


# ---------------------------------------------------------------------------
# Versions list
# ---------------------------------------------------------------------------
@app.route("/prompt/<int:prompt_id>/versions")
def versions_page(prompt_id):
    conn = get_db()
    prompt, guard = require_prompt_access(conn, prompt_id)
    if guard is not None:
        conn.close()
        return guard
    versions = conn.execute(
        "SELECT id, prompt_id, version, created_at, parent_version_id, "
        "substr(content, 1, 161) AS preview_content "
        "FROM versions WHERE prompt_id=? ORDER BY created_at DESC", (prompt_id,)
    ).fetchall()
    versions_dict = [dict(v) for v in versions]
    current = (
        conn.execute(
            "SELECT * FROM versions WHERE id=? AND prompt_id=?",
            (prompt["current_version_id"], prompt_id),
        ).fetchone()
        if prompt["current_version_id"] else None
    )
    conn.close()
    return render_template(
        "versions.html",
        prompt=dict(prompt),
        versions=versions_dict,
        current=dict(current) if current else None,
    )


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------
def word_diff_html(a, b):
    a_lines, b_lines = a.splitlines(), b.splitlines()
    sm = difflib.SequenceMatcher(None, a_lines, b_lines)
    rows = []

    def tokens(s):
        return re.findall(r"\w+|\s+|[^\w\s]", s, flags=re.UNICODE)

    def wrap_span(cls, s):
        return Markup(f'<span class="{cls}">{escape(s)}</span>')

    def highlight_pair(al, bl):
        ta, tb = tokens(al), tokens(bl)
        sm2 = difflib.SequenceMatcher(None, ta, tb)
        ra, rb = [], []
        for tag, i1, i2, j1, j2 in sm2.get_opcodes():
            if tag == "equal":
                ra.append(escape("".join(ta[i1:i2])))
                rb.append(escape("".join(tb[j1:j2])))
            elif tag == "delete":
                ra.append(wrap_span("diff-del", "".join(ta[i1:i2])))
            elif tag == "insert":
                rb.append(wrap_span("diff-ins", "".join(tb[j1:j2])))
            else:
                ra.append(wrap_span("diff-del", "".join(ta[i1:i2])))
                rb.append(wrap_span("diff-ins", "".join(tb[j1:j2])))
        return Markup("").join(ra), Markup("").join(rb)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append((escape(a_lines[i1 + k]), escape(b_lines[j1 + k]), ""))
        elif tag == "delete":
            for line in a_lines[i1:i2]:
                rows.append((wrap_span("diff-del", line), "", "del"))
        elif tag == "insert":
            for line in b_lines[j1:j2]:
                rows.append(("", wrap_span("diff-ins", line), "ins"))
        else:
            al, bl = a_lines[i1:i2], b_lines[j1:j2]
            for k in range(max(len(al), len(bl))):
                left_line = al[k] if k < len(al) else ""
                right_line = bl[k] if k < len(bl) else ""
                hl, hr = highlight_pair(left_line, right_line)
                rows.append((hl, hr, "chg"))

    html = ['<table class="diff-table">', "<tbody>"]
    for left_html, right_html, cls in rows:
        html.append(f'<tr class="{cls}"><td class="cell-left">{left_html}</td><td class="cell-right">{right_html}</td></tr>')
    html.append("</tbody></table>")
    return Markup("\n".join(html))


def line_diff_html(a, b):
    d = difflib.HtmlDiff(wrapcolumn=120)
    html = d.make_table(a.splitlines(), b.splitlines(), context=False, numlines=0)
    return Markup(f'<div class="line-diff">{html}</div>')


@app.route("/prompt/<int:prompt_id>/diff")
def diff_view(prompt_id):
    left_id = request.args.get("left")
    right_id = request.args.get("right")
    mode = request.args.get("mode", "word")
    conn = get_db()
    prompt, guard = require_prompt_access(conn, prompt_id)
    if guard is not None:
        conn.close()
        return guard
    # The dropdowns only need id/version/created_at; the two compared bodies are
    # fetched separately below, so avoid loading every version's full content here.
    versions = conn.execute(
        "SELECT id, version, created_at FROM versions WHERE prompt_id=? ORDER BY created_at DESC", (prompt_id,)
    ).fetchall()
    if not versions:
        conn.close()
        flash("暂无版本", "info")
        return redirect(url_for("prompt_detail", prompt_id=prompt_id))

    # Default the right side to the current version, or the newest version when
    # no current pointer is set (e.g. legacy/partial data) — versions is non-empty.
    if not right_id:
        right_id = str(prompt["current_version_id"] or versions[0]["id"])
    if not left_id:
        idx = 0
        for i, v in enumerate(versions):
            if str(v["id"]) == str(right_id):
                idx = i
                break
        left_id = str(versions[idx + 1]["id"]) if idx + 1 < len(versions) else str(versions[idx]["id"])

    left = conn.execute("SELECT * FROM versions WHERE id=? AND prompt_id=?", (left_id, prompt_id)).fetchone()
    right = conn.execute("SELECT * FROM versions WHERE id=? AND prompt_id=?", (right_id, prompt_id)).fetchone()
    conn.close()
    if not left or not right:
        flash("所选版本不存在", "error")
        return redirect(url_for("prompt_detail", prompt_id=prompt_id))

    diff_html = line_diff_html(left["content"], right["content"]) if mode == "line" else word_diff_html(left["content"], right["content"])
    return render_template("diff.html", prompt=prompt, versions=versions, left=left, right=right, mode=mode, diff_html=diff_html)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
def settings():
    guard = require_admin()
    if guard is not None:
        return guard

    conn = get_db()
    if request.method == "POST":
        try:
            _ = request.form
        except BadRequest:
            flash("提交失败：上传表单解析错误", "error")
            return redirect(url_for("settings"))

        action = _settings_action()
        if action is None:
            flash("未知的设置操作，未做任何更改", "error")
            return redirect(url_for("settings"))

        if action == "import":
            _handle_import(conn)
            return redirect(url_for("settings"))

        try:
            conn.execute("BEGIN IMMEDIATE")
            if action == "general":
                ok = _handle_general_settings(conn)
                authenticate = False
            else:
                ok, authenticate = _handle_auth_settings(conn)
            if not ok:
                conn.rollback()
                return redirect(url_for("settings"))
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            log.exception("settings update failed action=%s", action)
            flash("设置保存失败，请重试", "error")
            return redirect(url_for("settings"))

        if authenticate:
            _reset_session(conn, authenticated=True)
        flash("设置已保存", "success")
        return redirect(url_for("settings"))

    threshold = get_setting(conn, "version_cleanup_threshold", "200")
    auth_mode = get_setting(conn, "auth_mode", "off") or "off"
    has_password = bool(get_setting(conn, "auth_password_hash", "") or "")
    language = get_setting(conn, "language", LANG_DEFAULT) or LANG_DEFAULT
    conn.close()
    return render_template(
        "settings.html", threshold=threshold, auth_mode=auth_mode,
        has_password=has_password, language=language,
    )


def _settings_action():
    action = (request.form.get("settings_action") or "").strip().lower()
    if action in {"general", "auth", "import"}:
        return action
    if action:
        return None

    # Compatibility for a form rendered before the settings UI was split.
    # Infer exactly one command. Authentication fields take precedence over an
    # attached file so a rejected password change can never fall through into a
    # destructive import.
    if any(
        request.form.get(key)
        for key in ("current_password", "new_password", "confirm_password")
    ):
        return "auth"
    upload = request.files.get("import_file")
    if upload and upload.filename:
        return "import"
    if any(key in request.form for key in ("version_cleanup_threshold", "language")):
        return "general"
    return None


def _handle_general_settings(conn):
    threshold = request.form.get("version_cleanup_threshold")
    if threshold is not None:
        threshold = threshold.strip()
        if not threshold.isdigit() or int(threshold) < 1:
            flash("阈值需为正整数", "error")
            return False
        set_setting(conn, "version_cleanup_threshold", threshold)

    language = request.form.get("language")
    if language is not None:
        language = language.strip().lower()
        if language not in SUPPORTED_LANGS:
            flash("语言设置无效", "error")
            return False
        set_setting(conn, "language", language)
        g.language = language
    return True


def _handle_auth_settings(conn):
    """Validate and stage one isolated auth settings update."""
    prev_mode = get_setting(conn, "auth_mode", "off") or "off"
    mode = request.form.get("auth_mode", prev_mode)
    if mode not in ("off", "per", "global"):
        flash("认证方式无效", "error")
        return False, False
    current_pw = request.form.get("current_password") or ""
    new_pw = request.form.get("new_password") or ""
    confirm_pw = request.form.get("confirm_password") or ""
    saved_hash = get_setting(conn, "auth_password_hash", "") or ""
    current_verified = not saved_hash

    if new_pw and new_pw != confirm_pw:
        flash("两次输入的密码不一致", "error")
        return False, False
    if new_pw and len(new_pw) < 8:
        flash("密码长度至少为 8 位", "error")
        return False, False

    # Any change to the mode or password requires the existing password. Keep all
    # validation before writing settings so a rejected form cannot partially apply.
    if saved_hash and ((mode != prev_mode) or bool(new_pw)):
        if not current_pw:
            flash("请先输入当前密码以修改认证设置", "error")
            return False, False
        if not verify_password(current_pw, saved_hash):
            flash("当前密码不正确，无法修改认证设置", "error")
            return False, False
        current_verified = True

    new_hash = saved_hash
    if new_pw:
        new_hash = hash_password(new_pw)
    elif _looks_legacy_sha256(saved_hash) and current_pw and verify_password(current_pw, saved_hash):
        new_hash = hash_password(current_pw)
        current_verified = True

    if mode != "off" and not new_hash:
        flash("请先设置访问密码", "error")
        return False, False

    auth_changed = mode != prev_mode or new_hash != saved_hash
    set_setting(conn, "auth_mode", mode)
    set_setting(conn, "auth_password_hash", new_hash)
    if auth_changed:
        try:
            revision = int(get_setting(conn, "auth_revision", "1") or "1") + 1
        except (TypeError, ValueError):
            revision = 2
        set_setting(conn, "auth_revision", str(revision))
        conn.execute("DELETE FROM prompt_unlocks")
        conn.execute("DELETE FROM auth_sessions")
        g.auth_revision = str(revision)
    g.auth_mode = mode
    g.has_password = bool(new_hash)
    return True, bool(auth_changed and current_verified and new_hash)


def _handle_import(conn):
    try:
        files = request.files
    except BadRequest:
        flash("导入失败：上传表单解析错误", "error")
        return False
    f = files.get("import_file")
    if not f or not f.filename:
        flash("导入失败：请选择文件", "error")
        return False

    # Parse + validate fully BEFORE touching the database.
    try:
        bundle = _parse_import_payload(f)
    except RequestEntityTooLarge:
        flash("导入失败：文件过大", "error")
        return False
    except (json.JSONDecodeError, UnicodeDecodeError):
        # 两者都是 ValueError 的子类，必须放在 `except ValueError` 之前，否则会被
        # 它抢先捕获，把原始英文解析错误（如 "Expecting value: line 1 ..."）直接抛给用户。
        flash("导入失败：JSON 格式无效", "error")
        return False
    except ValueError as e:
        flash(str(e), "error")
        return False
    except Exception:
        log.exception("import parse failed")
        flash("导入失败，请重试", "error")
        return False

    prompts = bundle["prompts"]
    if not prompts:
        flash("导入失败：未发现任何提示词", "error")
        return False

    restore_auth = request.form.get("restore_auth") == "1"
    if restore_auth and not {
        "auth_mode", "auth_password_hash"
    }.issubset(bundle.get("settings") or {}):
        flash("导入失败：备份不包含完整认证设置", "error")
        return False
    if restore_auth:
        saved_hash = get_setting(conn, "auth_password_hash", "") or ""
        current_password = request.form.get("restore_current_password") or ""
        if saved_hash and not verify_password(current_password, saved_hash):
            flash("导入失败：恢复认证设置前必须验证当前密码", "error")
            return False
        backup_hash = (bundle.get("settings") or {}).get("auth_password_hash", "") or ""
        backup_password = request.form.get("restore_backup_password") or ""
        if backup_hash and not verify_password(backup_password, backup_hash):
            flash("导入失败：必须验证备份中的密码后才能恢复认证设置", "error")
            return False
    try:
        # The write lock is acquired before the backup snapshot. Therefore no
        # writer can create a gap between what is backed up and what is replaced.
        conn.execute("BEGIN IMMEDIATE")
        backup_path = _backup_current_data(conn)
        _replace_imported_prompts(conn, prompts)
        auth_changed = _restore_import_settings(
            conn, bundle.get("settings") or {}, restore_auth=restore_auth
        )
        conn.commit()
        if backup_path:
            try:
                _prune_import_backups(os.path.dirname(backup_path))
            except Exception:
                log.exception("failed to prune import backups after successful import")
        flash("已导入并覆盖所有数据", "success")
        if auth_changed:
            g.auth_mode = get_setting(conn, "auth_mode", "off") or "off"
            g.has_password = bool(get_setting(conn, "auth_password_hash", "") or "")
            g.auth_revision = get_setting(conn, "auth_revision", "1") or "1"
        return True
    except OSError as e:
        conn.rollback()
        log.exception("pre-import backup failed")
        flash(f"导入失败：{e}", "error")
    except sqlite3.Error as e:
        conn.rollback()
        log.exception("import failed; rolled back")
        flash(f"导入失败：数据库写入失败 - {e}", "error")
    except Exception as e:
        conn.rollback()
        log.exception("import failed; rolled back")
        flash(f"导入失败：{e}", "error")
    return False


def _replace_imported_prompts(conn, prompts):
    cur = conn.cursor()
    cur.execute("DELETE FROM versions")
    cur.execute("DELETE FROM prompts")
    cur.execute("DELETE FROM prompt_unlocks")
    # Explicit high IDs advance AUTOINCREMENT permanently unless sqlite_sequence
    # is reset. Clearing it here also repairs a database poisoned by an earlier
    # import near SQLite's integer ceiling.
    cur.execute("DELETE FROM sqlite_sequence WHERE name IN ('prompts', 'versions')")

    for prompt in prompts:
        timestamp = now_ts()
        cur.execute(
            "INSERT INTO prompts(id, name, source, notes, color, tags, image_data, pinned, "
            "favorite, archived_at, last_used_at, copy_count, created_at, updated_at, "
            "current_version_id, require_password) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (
                prompt["id"], prompt["name"], prompt["source"], prompt["notes"],
                prompt["color"], json.dumps(prompt["tags"], ensure_ascii=False),
                _sanitize_image_data(prompt["image_data"]), int(prompt["pinned"]),
                int(prompt["favorite"]), prompt["archived_at"], prompt["last_used_at"],
                prompt["copy_count"],
                prompt["created_at"] if prompt["_created_at_present"] else timestamp,
                prompt["updated_at"] if prompt["_updated_at_present"] else timestamp,
                int(prompt["require_password"]),
            ),
        )
        prompt_id = prompt["id"] if prompt["id"] is not None else cur.lastrowid
        version_id_map = {}
        inserted_versions = []
        for version in prompt["versions"]:
            cur.execute(
                "INSERT INTO versions(id, prompt_id, version, content, created_at, parent_version_id) "
                "VALUES(?,?,?,?,?,NULL)",
                (
                    version["id"], prompt_id, version["version"], version["content"],
                    version["created_at"] if version["_created_at_present"] else timestamp,
                ),
            )
            inserted_id = version["id"] if version["id"] is not None else cur.lastrowid
            if version["id"] is not None:
                version_id_map[version["id"]] = inserted_id
            inserted_versions.append((inserted_id, version["parent_version_id"]))

        for inserted_id, source_parent_id in inserted_versions:
            if source_parent_id is not None:
                cur.execute(
                    "UPDATE versions SET parent_version_id=? WHERE id=? AND prompt_id=?",
                    (version_id_map[source_parent_id], inserted_id, prompt_id),
                )

        source_current = prompt["current_version_id"]
        if source_current is not None:
            current_id = version_id_map[source_current]
            cur.execute(
                "UPDATE prompts SET current_version_id=? WHERE id=?",
                (current_id, prompt_id),
            )
        elif not prompt["_current_version_present"]:
            compute_current_version(conn, prompt_id)


def _restore_import_settings(conn, imported, *, restore_auth=False):
    if "version_cleanup_threshold" in imported:
        set_setting(conn, "version_cleanup_threshold", imported["version_cleanup_threshold"])
    if "language" in imported:
        set_setting(conn, "language", imported["language"])

    if not restore_auth:
        return False
    if "auth_mode" not in imported or "auth_password_hash" not in imported:
        raise ValueError("导入失败：备份不包含完整认证设置")

    mode = imported["auth_mode"]
    password_hash = imported["auth_password_hash"]
    if mode != "off" and not password_hash:
        raise ValueError("导入失败：认证设置缺少密码")
    try:
        local_revision = int(get_setting(conn, "auth_revision", "1") or "1")
        imported_revision = int(imported.get("auth_revision", "1") or "1")
    except (TypeError, ValueError):
        local_revision, imported_revision = 1, 1
    revision = max(local_revision, imported_revision) + 1
    set_setting(conn, "auth_mode", mode)
    set_setting(conn, "auth_password_hash", password_hash)
    set_setting(conn, "auth_revision", str(revision))
    conn.execute("DELETE FROM prompt_unlocks")
    conn.execute("DELETE FROM auth_sessions")
    return True


def _parse_import_payload(upload_file):
    max_bytes = app.config["MAX_IMPORT_SIZE_MB"] * 1024 * 1024
    raw = upload_file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise RequestEntityTooLarge()
    filename = (upload_file.filename or "").lower()

    if filename.endswith(".json"):
        data = json.loads(raw.decode("utf-8-sig"))
        strict = False
        settings_data = {}
        if isinstance(data, dict) and "prompts" in data:
            has_app = "app" in data
            has_schema = "schema_version" in data
            if has_app != has_schema:
                raise ValueError("导入失败：app 与 schema_version 必须同时存在")
            strict = has_app and has_schema
            if "app" in data and (not isinstance(data["app"], str) or data["app"] != "prompt-manage"):
                raise ValueError("导入失败：app 标识无效")
            if "schema_version" in data:
                schema = data["schema_version"]
                if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA_VERSION:
                    raise ValueError("导入失败：schema_version 不受支持")
            if "exported_at" in data:
                _normalize_import_timestamp(data["exported_at"], "exported_at", allow_empty=False)
            settings_data = _normalize_import_settings(data.get("settings") or {})
            prompts = data["prompts"]
        else:
            prompts = data
        if not isinstance(prompts, list):
            raise ValueError("导入失败：JSON 格式无效")
        return {
            "prompts": _validate_import_prompts(prompts, strict=strict),
            "settings": settings_data,
        }

    if filename.endswith(".csv"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise ValueError("导入失败：CSV 文件编码无效，请使用 UTF-8") from e
        try:
            reader = csv.DictReader(StringIO(text))
            required_headers = {"versions"}
            if not reader.fieldnames or not required_headers.issubset(set(reader.fieldnames)):
                raise ValueError("导入失败：CSV 缺少 versions 列")
            metadata_headers = {"app", "schema_version"}.intersection(reader.fieldnames)
            if metadata_headers and metadata_headers != {"app", "schema_version"}:
                raise ValueError("导入失败：CSV 元数据列不完整")
            own_export = metadata_headers == {"app", "schema_version"}
            prompts = []
            imported_settings = None
            for row_num, row in enumerate(reader, start=2):
                if not row or not any((v or "").strip() for v in row.values()):
                    continue
                try:
                    if None in row:
                        raise ValueError("列数与表头不一致")
                    if own_export:
                        if row.get("app") != "prompt-manage":
                            raise ValueError("app 标识无效")
                        if row.get("schema_version") != str(SCHEMA_VERSION):
                            raise ValueError("schema_version 不受支持")
                    decoded = {
                        key: _csv_unescape_cell(value, own_export=own_export)
                        for key, value in row.items()
                    }
                    if own_export:
                        null_fields = parse_json_text(
                            decoded.get("null_fields"), [], strict=True
                        )
                        allowed_null_fields = {
                            "source", "notes", "color", "image_data", "archived_at",
                            "last_used_at", "created_at", "updated_at", "current_version_id",
                        }
                        if (
                            not isinstance(null_fields, list)
                            or any(field not in allowed_null_fields for field in null_fields)
                        ):
                            raise ValueError("null_fields 格式无效")
                        for field in null_fields:
                            decoded[field] = None
                    tags = parse_json_text(row.get("tags"), [], strict=True)
                    if not isinstance(tags, list):
                        raise ValueError("tags 字段必须是数组")
                    versions = parse_json_text(row.get("versions"), [], strict=True)
                    if not isinstance(versions, list):
                        raise ValueError("versions 字段必须是数组")
                    for i, version_data in enumerate(versions):
                        if not isinstance(version_data, dict) or "content" not in version_data:
                            raise ValueError(f"versions[{i}] 格式无效，缺少 content 字段")
                    row_settings = _normalize_import_settings(
                        parse_json_text(row.get("settings"), {}, strict=True)
                    )
                    if imported_settings is None:
                        imported_settings = row_settings
                    elif row_settings != imported_settings:
                        raise ValueError("settings 元数据不一致")
                except ValueError as e:
                    raise ValueError(f"导入失败：第{row_num}行数据格式错误 - {e}") from e
                prompts.append({
                    "id": decoded.get("id"),
                    "name": decoded.get("name"),
                    "source": decoded.get("source"),
                    "notes": decoded.get("notes"),
                    "color": decoded.get("color"),
                    "tags": tags,
                    "image_data": decoded.get("image_data"),
                    "pinned": decoded.get("pinned"),
                    "favorite": decoded.get("favorite"),
                    "archived_at": decoded.get("archived_at") or None,
                    "last_used_at": decoded.get("last_used_at") or None,
                    "copy_count": decoded.get("copy_count"),
                    "require_password": decoded.get("require_password"),
                    "created_at": decoded.get("created_at"),
                    "updated_at": decoded.get("updated_at"),
                    "current_version_id": decoded.get("current_version_id"),
                    "versions": versions,
                })
            return {
                "prompts": _validate_import_prompts(
                    prompts, strict=own_export, csv_mode=own_export
                ),
                "settings": imported_settings or {},
            }
        except (json.JSONDecodeError, csv.Error) as e:
            raise ValueError("导入失败：CSV 格式无效") from e

    raise ValueError("导入失败：仅支持 JSON 或 CSV 文件")


def _normalize_import_settings(settings_data):
    if settings_data is None:
        return {}
    if not isinstance(settings_data, dict):
        raise ValueError("导入失败：settings 必须是对象")
    unknown = set(settings_data) - _IMPORT_SETTING_KEYS
    if unknown:
        raise ValueError(f"导入失败：settings 包含未知键 {sorted(unknown)[0]}")
    normalized = {}
    for key, value in settings_data.items():
        if not isinstance(value, str):
            raise ValueError(f"导入失败：settings.{key} 类型无效")
        normalized[key] = value
    threshold = normalized.get("version_cleanup_threshold")
    if threshold is not None and (not threshold.isdigit() or int(threshold) < 1):
        raise ValueError("导入失败：settings.version_cleanup_threshold 无效")
    language = normalized.get("language")
    if language is not None and language not in SUPPORTED_LANGS:
        raise ValueError("导入失败：settings.language 无效")
    mode = normalized.get("auth_mode")
    if mode is not None and mode not in {"off", "per", "global"}:
        raise ValueError("导入失败：settings.auth_mode 无效")
    password_hash = normalized.get("auth_password_hash")
    if password_hash is not None and len(password_hash) > 4096:
        raise ValueError("导入失败：settings.auth_password_hash 无效")
    if password_hash and not _is_supported_password_hash(password_hash):
        raise ValueError("导入失败：settings.auth_password_hash 格式无效")
    if mode in {"per", "global"} and password_hash is not None and not password_hash:
        raise ValueError("导入失败：认证设置缺少密码")
    revision = normalized.get("auth_revision")
    if revision is not None:
        _strict_int(revision, "settings.auth_revision", positive=True)
    bootstrap = normalized.get("bootstrap_completed")
    if bootstrap is not None and bootstrap not in {"0", "1"}:
        raise ValueError("导入失败：settings.bootstrap_completed 无效")
    return normalized


def _validate_import_prompts(prompts, *, strict=False, csv_mode=False):
    """Validate and normalize the complete import before any database writes."""
    if not isinstance(prompts, list) or not prompts:
        return []

    def import_int(value, label, *, positive=False, nonnegative=False):
        if strict and not csv_mode and isinstance(value, str):
            raise ValueError(f"导入失败：{label} 类型无效")
        if positive:
            return _strict_import_id(value, label)
        return _strict_int(
            value, label, positive=positive, nonnegative=nonnegative
        )

    normalized = []
    prompt_ids = set()
    global_version_ids = set()
    for index, prompt in enumerate(prompts, start=1):
        if not isinstance(prompt, dict):
            raise ValueError(f"导入失败：第{index}条提示词格式无效")
        raw_prompt_id = prompt.get("id")
        prompt_id = None if raw_prompt_id in (None, "") else import_int(
            raw_prompt_id, f"第{index}条提示词 id", positive=True
        )
        if strict and prompt_id is None:
            raise ValueError(f"导入失败：第{index}条提示词缺少 id")
        if prompt_id is not None:
            if prompt_id in prompt_ids:
                raise ValueError(f"导入失败：第{index}条提示词 id 重复")
            prompt_ids.add(prompt_id)

        versions = prompt.get("versions")
        if not isinstance(versions, list) or not versions:
            raise ValueError(f"导入失败：第{index}条提示词缺少有效版本")
        tags = prompt.get("tags") or []
        if not isinstance(tags, list):
            raise ValueError(f"导入失败：第{index}条提示词的 tags 必须是数组")
        clean_tags = []
        seen_tags = set()
        for tag in tags:
            if not isinstance(tag, str):
                raise ValueError(f"导入失败：第{index}条提示词包含无效标签")
            tag = tag.strip()
            if tag and tag not in seen_tags:
                clean_tags.append(tag)
                seen_tags.add(tag)

        clean_versions = []
        local_version_ids = set()
        for version_index, version in enumerate(versions, start=1):
            if not isinstance(version, dict) or "content" not in version:
                raise ValueError(f"导入失败：第{index}条提示词的第{version_index}个版本缺少 content")
            if not isinstance(version.get("content"), str):
                raise ValueError(f"导入失败：第{index}条提示词的第{version_index}个版本 content 无效")
            raw_version_id = version.get("id")
            version_id = None if raw_version_id in (None, "") else import_int(
                raw_version_id,
                f"第{index}条提示词第{version_index}个版本 id",
                positive=True,
            )
            if strict and version_id is None:
                raise ValueError(
                    f"导入失败：第{index}条提示词第{version_index}个版本缺少 id"
                )
            if version_id is not None:
                if version_id in global_version_ids:
                    raise ValueError("导入失败：版本 id 重复")
                global_version_ids.add(version_id)
                local_version_ids.add(version_id)

            raw_owner = version.get("prompt_id")
            owner_id = None if raw_owner in (None, "") else import_int(
                raw_owner,
                f"第{index}条提示词第{version_index}个版本 prompt_id",
                positive=True,
            )
            if owner_id is not None and owner_id != prompt_id:
                raise ValueError("导入失败：版本 prompt_id 归属无效")
            if strict and owner_id is None:
                raise ValueError("导入失败：版本缺少 prompt_id")

            version_label = version.get("version")
            if version_label in (None, "") and not strict:
                version_label = "1.0.0"
            if not isinstance(version_label, str) or not version_label.strip():
                raise ValueError("导入失败：版本号类型无效")
            raw_parent = version.get("parent_version_id")
            parent_id = None if raw_parent in (None, "") else import_int(
                raw_parent, "parent_version_id", positive=True
            )
            created_at = _normalize_import_timestamp(
                version.get("created_at"),
                f"第{index}条提示词第{version_index}个版本 created_at",
            )
            if strict and "created_at" not in version:
                raise ValueError("导入失败：版本缺少 created_at")
            clean_version = {
                "id": version_id,
                "prompt_id": prompt_id,
                "version": version_label,
                "content": version["content"],
                "created_at": created_at,
                "_created_at_present": "created_at" in version,
                "parent_version_id": parent_id,
            }
            clean_versions.append(clean_version)

        for version in clean_versions:
            parent_id = version["parent_version_id"]
            if parent_id is not None and parent_id not in local_version_ids:
                raise ValueError("导入失败：parent_version_id 归属无效")
            if parent_id is not None and parent_id == version["id"]:
                raise ValueError("导入失败：版本不能以自身为父版本")
        parent_map = {
            version["id"]: version["parent_version_id"]
            for version in clean_versions
            if version["id"] is not None
        }
        visit_state = {}

        def visit(version_id):
            state = visit_state.get(version_id, 0)
            if state == 1:
                raise ValueError("导入失败：版本 parent_version_id 存在环")
            if state == 2:
                return
            visit_state[version_id] = 1
            parent_id = parent_map.get(version_id)
            if parent_id is not None:
                visit(parent_id)
            visit_state[version_id] = 2

        for version_id in parent_map:
            visit(version_id)

        name = prompt.get("name")
        if name in (None, "") and not strict:
            name = "未命名提示词"
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"导入失败：第{index}条提示词 name 无效")
        clean_prompt = {"id": prompt_id, "name": name}
        for field in _IMPORT_TEXT_FIELDS - {"name"}:
            value = prompt.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"导入失败：第{index}条提示词 {field} 类型无效")
            clean_prompt[field] = value
        clean_prompt["color"] = sanitize_color(clean_prompt["color"])
        clean_prompt["tags"] = clean_tags
        clean_prompt["versions"] = clean_versions
        for field in ("pinned", "favorite", "require_password"):
            value = prompt.get(field, False)
            if strict and not csv_mode and not isinstance(value, bool):
                raise ValueError(f"导入失败：{field} 类型无效")
            clean_prompt[field] = (
                _strict_bool(value, field) if strict else parse_bool_value(value)
            )

        raw_copy_count = prompt.get("copy_count", 0)
        if strict:
            clean_prompt["copy_count"] = import_int(
                raw_copy_count, "copy_count", nonnegative=True
            )
        else:
            copy_count = parse_int_or_none(raw_copy_count)
            if copy_count is not None and copy_count < 0:
                raise ValueError("导入失败：copy_count 不能为负数")
            clean_prompt["copy_count"] = copy_count or 0

        for field in ("created_at", "updated_at", "archived_at", "last_used_at"):
            clean_prompt[field] = _normalize_import_timestamp(
                clean_prompt[field], f"第{index}条提示词 {field}"
            )
        if strict and (
            "created_at" not in prompt or "updated_at" not in prompt
        ):
            raise ValueError(f"导入失败：第{index}条提示词缺少创建或更新时间")

        raw_current = prompt.get("current_version_id")
        current_id = None if raw_current in (None, "") else import_int(
            raw_current, "current_version_id", positive=True
        )
        if current_id is not None and current_id not in local_version_ids:
            raise ValueError("导入失败：current_version_id 归属无效")
        if strict and "current_version_id" not in prompt:
            raise ValueError("导入失败：current_version_id 缺失")
        clean_prompt["current_version_id"] = current_id
        clean_prompt["_created_at_present"] = "created_at" in prompt
        clean_prompt["_updated_at_present"] = "updated_at" in prompt
        clean_prompt["_current_version_present"] = "current_version_id" in prompt
        normalized.append(clean_prompt)
    return normalized


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 2


def collect_export_payload(conn, allowed_ids=None, *, include_auth=False):
    prompts = conn.execute("SELECT * FROM prompts ORDER BY id ASC").fetchall()
    result = []
    for p in prompts:
        if allowed_ids is not None and p["id"] not in allowed_ids:
            continue
        keys = p.keys()
        versions = conn.execute(
            "SELECT * FROM versions WHERE prompt_id=? ORDER BY created_at ASC", (p["id"],)
        ).fetchall()
        result.append({
            "id": p["id"],
            "name": p["name"],
            "source": p["source"],
            "notes": p["notes"],
            "color": p["color"],
            "tags": _safe_tags(p),
            "image_data": p["image_data"] if "image_data" in keys else None,
            "pinned": bool(p["pinned"]),
            "favorite": bool(p["favorite"]) if "favorite" in keys else False,
            "archived_at": p["archived_at"] if "archived_at" in keys else None,
            "last_used_at": p["last_used_at"] if "last_used_at" in keys else None,
            "copy_count": p["copy_count"] if "copy_count" in keys else 0,
            "require_password": bool(p["require_password"]) if "require_password" in keys else False,
            "created_at": p["created_at"],
            "updated_at": p["updated_at"],
            "current_version_id": p["current_version_id"],
            "versions": [
                {
                    "id": v["id"], "prompt_id": v["prompt_id"], "version": v["version"],
                    "content": v["content"], "created_at": v["created_at"],
                    "parent_version_id": v["parent_version_id"],
                } for v in versions
            ],
        })
    setting_keys = _IMPORT_SETTING_KEYS if include_auth else _EXPORT_SETTING_KEYS
    settings = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        if row["key"] in setting_keys
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "app": "prompt-manage",
        "exported_at": now_ts(),
        "settings": settings,
        "prompts": result,
    }


def _backup_current_data(conn):
    backups_dir = os.path.join(os.path.dirname(db_path()) or ".", "backups")
    os.makedirs(backups_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = os.path.join(backups_dir, f"pre-import-{stamp}.json")
    payload = collect_export_payload(conn, include_auth=True)  # local full backup
    temp_path = None
    try:
        temp_fd, temp_path = tempfile.mkstemp(dir=backups_dir, suffix=".json.tmp")
        with os.fdopen(temp_fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        if os.path.getsize(temp_path) == 0:
            raise OSError("Backup file is empty")
        with open(temp_path, "r", encoding="utf-8") as fh:
            json.load(fh)
        os.replace(temp_path, path)
        log.info("Wrote pre-import backup to %s", path)
        return path
    except Exception as e:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                log.warning("failed to remove incomplete backup temp file %s", temp_path)
        log.error("Backup failed: %s", e)
        raise OSError(f"备份失败：{e}") from e


def _prune_import_backups(backups_dir):
    """Keep recent import backups bounded so repeated imports cannot fill the volume."""
    try:
        paths = [
            os.path.join(backups_dir, name)
            for name in os.listdir(backups_dir)
            if name.startswith("pre-import-") and name.endswith(".json")
        ]
        paths.sort(key=lambda path: os.path.getmtime(path), reverse=True)
        for old_path in paths[app.config["IMPORT_BACKUP_RETENTION"]:]:
            try:
                os.remove(old_path)
            except OSError:
                log.warning("failed to prune old import backup %s", old_path)
    except OSError:
        log.warning("failed to inspect import backup directory %s", backups_dir)


@app.route("/export")
def export_all():
    guard = require_admin()
    if guard is not None:
        return guard

    include_locked = request.args.get("include_locked") == "1"
    conn = get_db()
    if include_locked:
        if not is_global_authenticated():
            abort(403)
    try:
        # collect_export_payload performs several reads; pin them to one SQLite
        # snapshot so prompts, versions and settings cannot come from different
        # points in time while an export is being assembled.
        conn.execute("BEGIN")
        allowed = None if include_locked else exportable_prompt_ids(conn)
        include_auth = include_locked and request.args.get("include_auth") == "1"
        data = collect_export_payload(conn, allowed, include_auth=include_auth)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    export_format = (request.args.get("format") or "json").lower()
    if export_format == "csv":
        fieldnames = [
            "app", "schema_version", "settings", "null_fields", "id", "name", "source", "notes", "color", "tags", "image_data", "pinned",
            "favorite", "archived_at", "last_used_at", "copy_count", "require_password",
            "created_at", "updated_at", "current_version_id", "versions",
        ]
        sio = StringIO(newline="")
        writer = csv.DictWriter(sio, fieldnames=fieldnames)
        writer.writeheader()
        for p in data.get("prompts", []):
            writer.writerow({
                "app": "prompt-manage", "schema_version": str(SCHEMA_VERSION),
                "settings": json.dumps(data.get("settings") or {}, ensure_ascii=False),
                "null_fields": json.dumps([
                    field for field in (
                        "source", "notes", "color", "image_data", "archived_at",
                        "last_used_at", "created_at", "updated_at", "current_version_id",
                    ) if p.get(field) is None
                ]),
                "id": _csv_safe_cell(p.get("id")), "name": _csv_safe_cell(p.get("name")),
                "source": _csv_safe_cell(p.get("source")), "notes": _csv_safe_cell(p.get("notes")),
                "color": _csv_safe_cell(p.get("color")),
                "tags": _csv_safe_cell(json.dumps(p.get("tags") or [], ensure_ascii=False)),
                "image_data": _csv_safe_cell(p.get("image_data")),
                "pinned": _csv_safe_cell("1" if p.get("pinned") else "0"),
                "favorite": _csv_safe_cell("1" if p.get("favorite") else "0"),
                "archived_at": _csv_safe_cell(p.get("archived_at")),
                "last_used_at": _csv_safe_cell(p.get("last_used_at")),
                "copy_count": _csv_safe_cell(p.get("copy_count") or 0),
                "require_password": _csv_safe_cell("1" if p.get("require_password") else "0"),
                "created_at": _csv_safe_cell(p.get("created_at")),
                "updated_at": _csv_safe_cell(p.get("updated_at")),
                "current_version_id": _csv_safe_cell(p.get("current_version_id")),
                "versions": _csv_safe_cell(json.dumps(p.get("versions") or [], ensure_ascii=False)),
            })
        bio = BytesIO(sio.getvalue().encode("utf-8"))
        bio.seek(0)
        return send_file(bio, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name="prompts_export.csv")

    bio = BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    bio.seek(0)
    return send_file(bio, mimetype="application/json; charset=utf-8", as_attachment=True, download_name="prompts_export.json")


# ---------------------------------------------------------------------------
# APIs (tags / search) — never expose protected prompts
# ---------------------------------------------------------------------------
@app.route("/api/tags")
def api_tags():
    conn = get_db()
    rows = conn.execute("SELECT id, tags, require_password FROM prompts").fetchall()
    tags = set()
    for r in rows:
        if not can_view_prompt(r):
            continue
        tags.update(_safe_tags(r))
    conn.close()
    return jsonify(sorted(tags))


@app.route("/api/prompt/<int:prompt_id>/content")
def api_prompt_content(prompt_id):
    """Return current or historical content on demand for copy actions."""
    conn = get_db()
    prompt = fetch_prompt(conn, prompt_id)
    if not prompt:
        abort(404)
    if not can_view_prompt(prompt):
        abort(403)
    version_id = parse_int_or_none(request.args.get("version_id"))
    if version_id is None:
        version_id = prompt["current_version_id"]
    if version_id is None:
        conn.close()
        return jsonify({"content": ""})
    version = conn.execute(
        "SELECT content FROM versions WHERE id=? AND prompt_id=?",
        (version_id, prompt_id),
    ).fetchone()
    conn.close()
    if not version:
        abort(404)
    return jsonify({"content": version["content"]})


@app.route("/api/search", methods=["GET", "POST"])
def api_search():
    raw_query = request.form.get("q") if request.method == "POST" else request.args.get("q")
    q = (raw_query or "").strip()[:MAX_SEARCH_QUERY_LENGTH].lower()
    conn = get_db()
    rows = conn.execute(
        "SELECT p.id, p.name, p.source, p.notes, p.tags, p.require_password, "
        "v.content AS current_content "
        "FROM prompts p LEFT JOIN versions v "
        "ON v.id = p.current_version_id AND v.prompt_id = p.id "
        "WHERE p.archived_at IS NULL"
    ).fetchall()
    out = []
    for r in rows:
        name = r["name"] or ""
        locked = not can_view_prompt(r)
        if q:
            if locked:
                # 受保护提示词只按名称匹配，绝不让其受保护字段参与搜索匹配。
                if q not in name.lower():
                    continue
            else:
                haystack = " ".join([
                    name, r["source"] or "", r["notes"] or "",
                    " ".join(_safe_tags(r)), r["current_content"] or "",
                ]).lower()
                if q not in haystack:
                    continue
        out.append({"id": r["id"], "name": name, "locked": locked})
        if len(out) >= 20:
            break
    conn.close()
    return jsonify(out)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
def _safe_next(default_path):
    return _safe_local_target(request.values.get("next"), default_path)


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = get_db()
    mode = get_setting(conn, "auth_mode", "off") or "off"
    saved_hash = get_setting(conn, "auth_password_hash", "") or ""
    nxt = _safe_next(url_for("index"))
    if request.method == "POST":
        locked, retry = rate_limit_status(conn, "login")
        if locked:
            if getattr(g, "rate_limit_global", False):
                log.warning("Global rate limit triggered ip=%s", _client_ip())
                message = "系统检测到大量登录失败尝试，已临时锁定。请1小时后重试。"
            else:
                message = "尝试过于频繁，请稍后再试"
            conn.close()
            flash(message, "error")
            return render_template("auth.html", mode=mode, action="login", next=nxt), 429
        password = request.form.get("password") or ""
        if saved_hash and check_and_migrate_password(conn, password):
            record_attempt(conn, "login", True)
            _reset_session(conn, authenticated=True)
            conn.close()
            flash("已通过认证", "success")
            return redirect(nxt)
        record_attempt(conn, "login", False)
        conn.close()
        flash("密码不正确", "error")
        return render_template("auth.html", mode=mode, action="login", next=nxt)
    # HTTP 访问但开启了“仅 HTTPS 发送会话 Cookie”时，浏览器会丢弃会话 Cookie，
    # 表现为登录/提交始终失败却无报错。主动提示，避免被误认为程序故障。
    if app.config.get("SESSION_COOKIE_SECURE") and not request.is_secure:
        flash("注意：当前为 HTTP 访问且开启了仅 HTTPS Cookie，可能导致无法登录。"
              "若未使用 HTTPS，请将环境变量 SESSION_COOKIE_SECURE 设为 false 后重启。", "error")
    conn.close()
    return render_template("auth.html", mode=mode, action="login", next=nxt)


@app.route("/logout", methods=["POST"])
def logout():
    conn = get_db()
    sid = _current_session_id(create=False)
    if not sid:
        conn.close()
        session.clear()
        return redirect(url_for("index"))
    try:
        conn.execute("BEGIN IMMEDIATE")
        if sid:
            # Revoke only this browser's server-side session. A copied signed
            # cookie carries the same sid and therefore stops authenticating,
            # while unrelated owner devices remain logged in.
            conn.execute("DELETE FROM auth_sessions WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM prompt_unlocks WHERE session_id=?", (sid,))
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if _is_db_locked(exc):
            return _db_busy_response(url_for("index"))
        log.exception("failed to revoke authenticated session on logout")
        return jsonify({"status": "error"}), 503
    except sqlite3.Error:
        conn.rollback()
        log.exception("failed to revoke authenticated session on logout")
        return jsonify({"status": "error"}), 503
    finally:
        conn.close()
    session.clear()
    flash("已退出登录", "success")
    return redirect(url_for("index"))


@app.route("/prompt/<int:prompt_id>/unlock", methods=["GET", "POST"])
def unlock_prompt(prompt_id):
    conn = get_db()
    mode = get_setting(conn, "auth_mode", "off") or "off"
    prompt = conn.execute(
        "SELECT id, name, require_password FROM prompts WHERE id=?", (prompt_id,)
    ).fetchone()
    if not prompt:
        conn.close()
        flash("未找到该提示词", "error")
        return redirect(url_for("index"))
    if mode != "per" or not _prompt_requires_password(prompt):
        conn.close()
        return redirect(url_for("prompt_detail", prompt_id=prompt_id))
    nxt = _safe_next(url_for("prompt_detail", prompt_id=prompt_id))
    if request.method == "POST":
        locked, retry = rate_limit_status(conn, "unlock")
        if locked:
            if getattr(g, "rate_limit_global", False):
                log.warning("Global rate limit triggered ip=%s", _client_ip())
                message = "系统检测到大量登录失败尝试，已临时锁定。请1小时后重试。"
            else:
                message = "尝试过于频繁，请稍后再试"
            conn.close()
            flash(message, "error")
            return render_template("auth.html", mode=mode, action="unlock", prompt=prompt, next=nxt), 429
        password = request.form.get("password") or ""
        try:
            conn.execute("BEGIN IMMEDIATE")
            current_mode = get_setting(conn, "auth_mode", "off") or "off"
            saved_hash = get_setting(conn, "auth_password_hash", "") or ""
            current_prompt = conn.execute(
                "SELECT id, require_password FROM prompts WHERE id=?", (prompt_id,)
            ).fetchone()
            if (
                current_mode == "per"
                and current_prompt
                and _prompt_requires_password(current_prompt)
                and saved_hash
                and verify_password(password, saved_hash)
            ):
                if _looks_legacy_sha256(saved_hash):
                    set_setting(conn, "auth_password_hash", hash_password(password))
                mark_prompt_unlocked(conn, prompt_id)
                record_attempt(conn, "unlock", True, commit=False)
                conn.commit()
                conn.close()
                flash("已解锁该提示词", "success")
                return redirect(nxt)
            conn.rollback()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if _is_db_locked(exc):
                return _db_busy_response(nxt)
            raise
        record_attempt(conn, "unlock", False)
        conn.close()
        flash("密码不正确", "error")
        return render_template("auth.html", mode=mode, action="unlock", prompt=prompt, next=nxt)
    conn.close()
    return render_template("auth.html", mode=mode, action="unlock", prompt=prompt, next=nxt)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def init_app():
    run_migrations()


# Run migrations at import so gunicorn (wsgi:app) and tests share one code path.
init_app()


def run():
    debug = _env_bool("FLASK_DEBUG", False)
    app.run(host="0.0.0.0", port=APP_PORT, debug=debug)


if __name__ == "__main__":
    run()
