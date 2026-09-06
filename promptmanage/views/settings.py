"""Settings page: preferences, the site password, and backup import/export."""

import json
import logging
import os
import sqlite3
from io import BytesIO

from flask import (
    flash, g, jsonify, redirect, render_template, request, send_file, url_for,
)
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from .. import db_busy_response
from ..db import get_db, get_setting, is_locked_error, set_setting
from ..i18n import LANG_DEFAULT, SUPPORTED_LANGS
from ..routing import route
from ..security import (
    bump_auth_revision, hash_password, looks_legacy_sha256, reset_session, verify_password,
)
from ..transfer import (
    ImportError_, collect_export, parse_bundle, prune_backups, replace_prompts,
    restore_settings, write_pre_import_backup,
)

log = logging.getLogger("prompt_manage")

MAX_THRESHOLD = 100_000
_ACTIONS = ("general", "auth", "import")


@route("/settings", methods=["GET", "POST"])
def settings():
    conn = get_db()
    if request.method == "GET":
        return render_template(
            "settings.html",
            threshold=get_setting(conn, "version_cleanup_threshold", "200"),
            auth_mode=get_setting(conn, "auth_mode", "off") or "off",
            has_password=bool(get_setting(conn, "auth_password_hash", "") or ""),
            language=get_setting(conn, "language", LANG_DEFAULT) or LANG_DEFAULT,
            prompt_count=conn.execute("SELECT COUNT(*) AS c FROM prompts").fetchone()["c"],
            version_count=conn.execute("SELECT COUNT(*) AS c FROM versions").fetchone()["c"],
        )

    try:
        action = (request.form.get("settings_action") or "").strip().lower()
    except BadRequest:
        flash("提交失败：上传表单解析错误", "error")
        return redirect(url_for("settings"))
    if action not in _ACTIONS:
        flash("未知的设置操作，未做任何更改", "error")
        return redirect(url_for("settings"))

    if action == "import":
        _handle_import(conn)
        return redirect(url_for("settings"))

    try:
        conn.execute("BEGIN IMMEDIATE")
        if action == "general":
            ok, authenticate = _handle_general(conn), False
        else:
            ok, authenticate = _handle_auth(conn)
        if not ok:
            conn.rollback()
            return redirect(url_for("settings"))
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        if is_locked_error(exc):
            return db_busy_response(url_for("settings"))
        log.exception("settings update failed action=%s", action)
        flash("设置保存失败，请重试", "error")
        return redirect(url_for("settings"))

    if authenticate:
        reset_session(conn, authenticated=True)
    flash("设置已保存", "success")
    return redirect(url_for("settings"))


def _handle_general(conn):
    threshold = request.form.get("version_cleanup_threshold")
    if threshold is not None:
        threshold = threshold.strip()
        if not threshold.isdigit() or not 1 <= int(threshold) <= MAX_THRESHOLD:
            flash(f"版本保留数量需为 1 到 {MAX_THRESHOLD} 之间的整数", "error")
            return False
        set_setting(conn, "version_cleanup_threshold", str(int(threshold)))

    language = request.form.get("language")
    if language is not None:
        language = language.strip().lower()
        if language not in SUPPORTED_LANGS:
            flash("语言设置无效", "error")
            return False
        set_setting(conn, "language", language)
        g.language = language
    return True


def _handle_auth(conn):
    """Validate one isolated auth change. Returns ``(ok, should_reauthenticate)``.

    Everything is validated before the first write so a rejected form can never
    apply half of itself.
    """
    previous_mode = get_setting(conn, "auth_mode", "off") or "off"
    mode = request.form.get("auth_mode", previous_mode)
    if mode not in ("off", "global"):
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

    # Changing the mode or the password always requires proving the old one.
    if saved_hash and (mode != previous_mode or new_pw):
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
    elif looks_legacy_sha256(saved_hash) and current_pw and verify_password(current_pw, saved_hash):
        new_hash = hash_password(current_pw)
        current_verified = True

    if mode != "off" and not new_hash:
        flash("请先设置访问密码", "error")
        return False, False

    changed = mode != previous_mode or new_hash != saved_hash
    set_setting(conn, "auth_mode", mode)
    set_setting(conn, "auth_password_hash", new_hash)
    if changed:
        bump_auth_revision(conn)
    g.auth_mode = mode
    g.has_password = bool(new_hash)
    return True, bool(changed and current_verified and new_hash)


def _handle_import(conn):
    try:
        upload = request.files.get("import_file")
    except BadRequest:
        flash("导入失败：上传表单解析错误", "error")
        return False
    if not upload or not upload.filename:
        flash("导入失败：请选择文件", "error")
        return False

    # Parse and validate everything before touching the database.
    try:
        bundle = parse_bundle(upload)
    except RequestEntityTooLarge:
        flash("导入失败：文件过大", "error")
        return False
    except ImportError_ as exc:
        flash(str(exc), "error")
        return False
    except Exception:
        log.exception("import parse failed")
        flash("导入失败，请重试", "error")
        return False

    restore_auth = request.form.get("restore_auth") == "1"
    if restore_auth and not _auth_restore_allowed(conn, bundle):
        return False

    try:
        # The writer lock is taken before the snapshot, so no writer can open a
        # gap between what gets backed up and what gets replaced.
        conn.execute("BEGIN IMMEDIATE")
        backup_path = write_pre_import_backup(conn)
        replace_prompts(conn, bundle["prompts"])
        auth_changed = restore_settings(conn, bundle["settings"], restore_auth=restore_auth)
        conn.commit()
    except OSError as exc:
        conn.rollback()
        log.exception("pre-import backup failed")
        flash(f"导入失败：{exc}", "error")
        return False
    except (ImportError_, sqlite3.Error) as exc:
        conn.rollback()
        log.exception("import failed; rolled back")
        flash(f"导入失败：{exc}" if isinstance(exc, ImportError_) else "导入失败：数据库写入失败", "error")
        return False
    except Exception:
        conn.rollback()
        log.exception("import failed; rolled back")
        flash("导入失败，请重试", "error")
        return False

    try:
        prune_backups(os.path.dirname(backup_path))
    except Exception:
        log.exception("failed to prune import backups")
    if auth_changed:
        g.auth_mode = get_setting(conn, "auth_mode", "off") or "off"
        g.has_password = bool(get_setting(conn, "auth_password_hash", "") or "")
        g.auth_revision = get_setting(conn, "auth_revision", "1") or "1"
    flash(f"已导入 {len(bundle['prompts'])} 条提示词并覆盖原有数据", "success")
    return True


def _auth_restore_allowed(conn, bundle):
    """Restoring credentials needs both the current and the backup password."""
    imported = bundle["settings"]
    if not {"auth_mode", "auth_password_hash"}.issubset(imported):
        flash("导入失败：备份不包含完整认证设置", "error")
        return False
    saved_hash = get_setting(conn, "auth_password_hash", "") or ""
    if saved_hash and not verify_password(request.form.get("restore_current_password") or "", saved_hash):
        flash("导入失败：恢复认证设置前必须验证当前密码", "error")
        return False
    backup_hash = imported.get("auth_password_hash") or ""
    if backup_hash and not verify_password(request.form.get("restore_backup_password") or "", backup_hash):
        flash("导入失败：必须验证备份中的密码后才能恢复认证设置", "error")
        return False
    return True


@route("/export")
def export_all():
    conn = get_db()
    include_auth = request.args.get("include_auth") == "1"
    try:
        # Pin every read to one SQLite snapshot so prompts, versions and
        # settings cannot come from different points in time.
        conn.execute("BEGIN")
        payload = collect_export(conn, include_auth=include_auth)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        if is_locked_error(exc):
            return db_busy_response(url_for("settings"))
        log.exception("export failed")
        return jsonify({"status": "error"}), 503

    body = BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    body.seek(0)
    return send_file(
        body,
        mimetype="application/json; charset=utf-8",
        as_attachment=True,
        download_name="prompts_export.json",
        max_age=0,
    )
