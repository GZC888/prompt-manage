"""Health check and the two static brand assets."""

import logging
import os

from flask import current_app, jsonify, send_file

from ..db import get_db, get_setting
from ..routing import route

log = logging.getLogger("prompt_manage")

_LOGO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "logo.png")


@route("/healthz")
def healthz():
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        initialized = (
            current_app.config["APP_ENV"] != "production"
            or (get_setting(conn, "bootstrap_completed", "1") or "0") == "1"
        )
    except Exception:
        log.exception("healthz database check failed")
        return jsonify({"status": "error", "build_sha": current_app.config["BUILD_SHA"]}), 500
    response = jsonify({
        "status": "ok",
        "build_sha": current_app.config["BUILD_SHA"],
        "initialized": initialized,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@route("/logo.png")
def logo_png():
    response = send_file(_LOGO_PATH, mimetype="image/png", max_age=86400)
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@route("/favicon.ico")
def favicon():
    return logo_png()
