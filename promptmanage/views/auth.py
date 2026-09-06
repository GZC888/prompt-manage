"""First-time setup, login and logout."""

import hmac
import logging
import sqlite3

from flask import (
    abort, current_app, flash, g, jsonify, redirect, render_template, request, session,
    url_for,
)

from .. import db_busy_response
from ..db import get_db, get_setting, is_locked_error, set_setting
from ..i18n import LANG_DEFAULT
from ..routing import route
from ..security import (
    check_and_migrate_password, client_ip, current_session_id, hash_password,
    is_authenticated, rate_limit_status, record_attempt, reset_session,
)
from ..utils import now_ts, safe_next

log = logging.getLogger("prompt_manage")

_RATE_LIMIT_MESSAGE = "尝试过于频繁，请稍后再试"
_GLOBAL_LIMIT_MESSAGE = "系统检测到大量登录失败尝试，已临时锁定。请 1 小时后重试。"


def _bootstrap_complete(conn):
    if current_app.config["APP_ENV"] != "production":
        return True
    return (get_setting(conn, "bootstrap_completed", "1") or "0") == "1"


@route("/setup", methods=["GET", "POST"])
def setup():
    """Claim a brand-new production database exactly once."""
    conn = get_db()
    if _bootstrap_complete(conn):
        abort(404)

    expected_token = current_app.config["BOOTSTRAP_TOKEN"]
    if not expected_token:
        log.error("BOOTSTRAP_TOKEN is required before first-time production setup")
        flash("服务端尚未配置 BOOTSTRAP_TOKEN，初始化已禁用", "error")
        if request.method == "POST":
            return jsonify({"status": "bootstrap_token_required"}), 503
        return render_template("setup.html", setup_disabled=True), 503

    if request.method == "GET":
        return render_template("setup.html", setup_disabled=False)

    def reject(message, status=400):
        flash(message, "error")
        return render_template("setup.html", setup_disabled=False), status

    locked, retry = rate_limit_status(conn, "setup")
    if locked:
        response = jsonify({"status": "rate_limited", "retry_after": retry})
        response.headers["Retry-After"] = str(retry)
        return response, 429

    if not hmac.compare_digest(request.form.get("bootstrap_token") or "", expected_token):
        record_attempt(conn, "setup", False)
        return reject("初始化令牌不正确")

    password = request.form.get("new_password") or ""
    if len(password) < 8:
        return reject("密码长度至少为 8 位")
    if password != (request.form.get("confirm_password") or ""):
        return reject("两次输入的密码不一致")

    try:
        conn.execute("BEGIN IMMEDIATE")
        if _bootstrap_complete(conn):  # another worker won the race
            conn.rollback()
            return redirect(url_for("index"))
        set_setting(conn, "auth_password_hash", hash_password(password))
        set_setting(conn, "auth_mode", "global")
        set_setting(conn, "language", request.form.get("language", LANG_DEFAULT))
        set_setting(conn, "bootstrap_completed", "1")
        set_setting(conn, "bootstrap_completed_at", now_ts())
        record_attempt(conn, "setup", True, commit=False)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        if is_locked_error(exc):
            return db_busy_response(url_for("setup"))
        log.exception("setup failed")
        return reject("初始化失败，请重试", 500)

    reset_session(conn, authenticated=True)
    flash("初始化完成，请妥善保管访问密码", "success")
    return redirect(url_for("index"))


@route("/login", methods=["GET", "POST"])
def login():
    conn = get_db()
    target = safe_next(url_for("index"))
    if is_authenticated():
        return redirect(target)

    if request.method == "GET":
        # Over plain HTTP with secure cookies enabled the browser silently drops
        # the session cookie, which looks exactly like a broken login form.
        if current_app.config["SESSION_COOKIE_SECURE"] and not request.is_secure:
            flash(
                "注意：当前为 HTTP 访问且开启了仅 HTTPS Cookie，可能导致无法登录。"
                "若未使用 HTTPS，请将环境变量 SESSION_COOKIE_SECURE 设为 false 后重启。",
                "error",
            )
        return render_template("auth.html", next=target)

    locked, _retry = rate_limit_status(conn, "login")
    if locked:
        globally_locked = getattr(g, "rate_limit_global", False)
        if globally_locked:
            log.warning("global rate limit triggered ip=%s", client_ip())
        flash(_GLOBAL_LIMIT_MESSAGE if globally_locked else _RATE_LIMIT_MESSAGE, "error")
        return render_template("auth.html", next=target), 429

    saved_hash = get_setting(conn, "auth_password_hash", "") or ""
    if saved_hash and check_and_migrate_password(conn, request.form.get("password") or ""):
        record_attempt(conn, "login", True)
        reset_session(conn, authenticated=True)
        flash("已通过认证", "success")
        return redirect(target)

    record_attempt(conn, "login", False)
    flash("密码不正确", "error")
    return render_template("auth.html", next=target), 401


@route("/logout", methods=["POST"])
def logout():
    conn = get_db()
    sid = current_session_id(create=False)
    if sid:
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Revoke only this browser's server-side session: a copied cookie
            # carries the same sid and stops authenticating, while the owner's
            # other devices stay logged in.
            conn.execute("DELETE FROM auth_sessions WHERE session_id=?", (sid,))
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            if is_locked_error(exc):
                return db_busy_response(url_for("index"))
            log.exception("failed to revoke authenticated session on logout")
            return jsonify({"status": "error"}), 503
    session.clear()
    flash("已退出登录", "success")
    return redirect(url_for("index"))
