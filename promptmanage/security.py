"""Passwords, brute-force protection, sessions, CSRF and access control.

Access control has exactly two shapes now:

* ``auth_mode = "off"``    — the library is open (single user, or fronted by an
  identity proxy such as Cloudflare Access).
* ``auth_mode = "global"`` — one password guards the whole site.

The previous per-prompt password mode is gone; see migration 12, which promotes
any existing ``per`` installation to ``global`` so nothing becomes more visible
than it was.
"""

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import time

from flask import current_app, g, has_request_context, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, get_setting, set_setting
from .utils import now_ts

log = logging.getLogger("prompt_manage")

SESSION_ID_KEY = "sid"

_LEGACY_SHA256 = re.compile(r"[0-9a-f]{64}")
_SALT = re.compile(r"[A-Za-z0-9]{8,64}")
_PBKDF2_METHOD = re.compile(r"pbkdf2:sha256:(\d{1,8})")
_SCRYPT_METHOD = re.compile(r"scrypt:(\d+):(\d+):(\d+)")
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_HEX128 = re.compile(r"[0-9a-fA-F]{128}")


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(raw):
    return generate_password_hash(raw or "")


def looks_legacy_sha256(stored):
    return bool(stored) and bool(_LEGACY_SHA256.fullmatch(stored))


def _is_supported_hash(stored):
    """Reject anything that is not a hash this app could have written.

    An imported backup is attacker-influenced data; feeding an arbitrary string
    to Werkzeug's verifier is how you end up with a surprising parser as your
    authentication boundary.
    """
    if not stored:
        return False
    if looks_legacy_sha256(stored):
        return True
    parts = stored.split("$")
    if len(parts) != 3:
        return False
    method, salt, digest = parts
    if not _SALT.fullmatch(salt):
        return False
    if method.startswith("pbkdf2:sha256:"):
        match = _PBKDF2_METHOD.fullmatch(method)
        if not match or not 100_000 <= int(match.group(1)) <= 2_000_000:
            return False
        return bool(_HEX64.fullmatch(digest))
    if method.startswith("scrypt:"):
        match = _SCRYPT_METHOD.fullmatch(method)
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
        return bool(_HEX128.fullmatch(digest))
    return False


def verify_password(raw, stored):
    """Return True if ``raw`` matches ``stored`` (Werkzeug or legacy SHA-256)."""
    if not _is_supported_hash(stored):
        return False
    if looks_legacy_sha256(stored):
        digest = hashlib.sha256((raw or "").encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, stored)
    try:
        return check_password_hash(stored, raw or "")
    except (ValueError, TypeError):
        return False


def check_and_migrate_password(conn, raw):
    """Verify the site password, upgrading a legacy SHA-256 hash on success."""
    stored = get_setting(conn, "auth_password_hash", "") or ""
    if not verify_password(raw, stored):
        return False
    if looks_legacy_sha256(stored):
        set_setting(conn, "auth_password_hash", hash_password(raw))
        conn.commit()
        log.info("Migrated legacy SHA-256 password hash to Werkzeug hash")
    return True


# ---------------------------------------------------------------------------
# Brute-force protection (per IP + route, persisted in login_attempts)
# ---------------------------------------------------------------------------
def client_ip():
    return request.remote_addr or "unknown"


def rate_limit_status(conn, route):
    """Return ``(locked, retry_after_seconds)`` for this IP and route."""
    if has_request_context():
        g.rate_limit_global = False
    config = current_app.config
    window = config["AUTH_LOGIN_WINDOW_SECONDS"]
    max_attempts = config["AUTH_LOGIN_MAX_ATTEMPTS"]
    lock = config["AUTH_LOCK_SECONDS"]
    now = time.time()
    try:
        global_max = config["GLOBAL_LOGIN_MAX_ATTEMPTS"]
        if global_max > 0:
            failures = conn.execute(
                "SELECT COUNT(*) AS c FROM login_attempts "
                "WHERE success=0 AND CAST(created_at AS REAL) > ?",
                (now - config["GLOBAL_LOGIN_WINDOW_SECONDS"],),
            ).fetchone()["c"]
            if failures >= global_max:
                if has_request_context():
                    g.rate_limit_global = True
                return True, lock * 2

        rows = conn.execute(
            "SELECT CAST(created_at AS REAL) AS created_at FROM login_attempts "
            "WHERE ip=? AND route=? AND success=0 AND CAST(created_at AS REAL) > ?",
            (client_ip(), route, now - window),
        ).fetchall()
        recent = [float(row["created_at"]) for row in rows]
        if max_attempts > 0 and len(recent) >= max_attempts:
            remaining = lock - (now - max(recent))
            if remaining > 0:
                return True, int(remaining) + 1
        return False, 0
    except (sqlite3.Error, TypeError, ValueError) as exc:
        # Failing closed: an unreadable attempt log must not become an unlimited
        # password oracle.
        log.error("rate_limit_status failed route=%s ip=%s: %s", route, client_ip(), exc)
        return True, lock


def record_attempt(conn, route, success, *, commit=True):
    ip = client_ip()
    conn.execute(
        "INSERT INTO login_attempts(ip, route, success, created_at) VALUES(?,?,?,?)",
        (ip, route, 1 if success else 0, str(time.time())),
    )
    if success:
        conn.execute("DELETE FROM login_attempts WHERE ip=? AND route=?", (ip, route))
    # Opportunistically drop rows older than the longest rate-limit horizon so
    # the table stays bounded even against an IP that fails forever.
    config = current_app.config
    horizon = max(
        config["AUTH_LOGIN_WINDOW_SECONDS"],
        config["GLOBAL_LOGIN_WINDOW_SECONDS"],
        config["AUTH_LOCK_SECONDS"] * 2,
    )
    conn.execute(
        "DELETE FROM login_attempts WHERE CAST(created_at AS REAL) < ?",
        (time.time() - horizon - 60,),
    )
    if commit:
        conn.commit()
    log.info("auth attempt route=%s ip=%s success=%s", route, ip, bool(success))


# ---------------------------------------------------------------------------
# Sessions and access control
# ---------------------------------------------------------------------------
def auth_mode():
    return getattr(g, "auth_mode", None) or "off"


def auth_configured():
    """Whether a password actually guards this site right now."""
    return bool(getattr(g, "has_password", False)) and auth_mode() != "off"


def current_session_id(create=False):
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


def is_authenticated():
    """Whether this browser holds a still-valid server-side session."""
    if not session.get("auth_ok"):
        return False
    revision = getattr(g, "auth_revision", None)
    if revision is None or str(session.get("auth_revision", "")) != str(revision):
        return False
    sid = current_session_id(create=False)
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
    """Whether this request has owner-level access."""
    return not auth_configured() or is_authenticated()


def clear_server_session(conn=None):
    sid = session.get(SESSION_ID_KEY) if has_request_context() else None
    if not sid:
        return
    db = conn or get_db()
    try:
        db.execute("DELETE FROM auth_sessions WHERE session_id=?", (sid,))
        db.commit()
    except sqlite3.Error:
        db.rollback()
        log.exception("failed to clear server session state")


def reset_session(conn=None, authenticated=False):
    """Rotate the session id, optionally registering it as authenticated."""
    flashes = session.get("_flashes")
    clear_server_session(conn)
    session.clear()
    if flashes:
        session["_flashes"] = flashes
    session.permanent = True
    session[SESSION_ID_KEY] = secrets.token_urlsafe(32)
    if not authenticated:
        return
    db = conn or get_db()
    revision = getattr(g, "auth_revision", None) or get_setting(db, "auth_revision", "1") or "1"
    db.execute(
        "INSERT OR REPLACE INTO auth_sessions(session_id, auth_revision, authenticated_at) "
        "VALUES(?,?,?)",
        (session[SESSION_ID_KEY], str(revision), now_ts()),
    )
    db.commit()
    session["auth_ok"] = True
    session["auth_at"] = now_ts()
    session["auth_revision"] = str(revision)
    g._auth_session_valid = True


def bump_auth_revision(conn):
    """Invalidate every existing session after a password or mode change."""
    try:
        revision = int(get_setting(conn, "auth_revision", "1") or "1") + 1
    except (TypeError, ValueError):
        revision = 2
    set_setting(conn, "auth_revision", str(revision))
    conn.execute("DELETE FROM auth_sessions")
    g.auth_revision = str(revision)
    return revision


# ---------------------------------------------------------------------------
# CSRF (session-bound, double-submit token)
# ---------------------------------------------------------------------------
def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


def valid_csrf():
    # Read the header first so API clients validate without forcing a multipart
    # body parse; ordinary HTML forms fall back to the hidden field.
    sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
    stored = session.get("csrf_token")
    return bool(sent and stored and hmac.compare_digest(sent, stored))
