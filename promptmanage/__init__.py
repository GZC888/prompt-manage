"""Prompt Manage — a lightweight personal prompt manager.

Flask + SQLite + Jinja, no frontend framework. This package is split by concern:

* :mod:`promptmanage.config`     — environment parsing and validation
* :mod:`promptmanage.db`         — connections, settings rows
* :mod:`promptmanage.migrations` — schema history
* :mod:`promptmanage.security`   — passwords, sessions, CSRF, access control
* :mod:`promptmanage.views`      — the routes, grouped by page

Production runs ``wsgi:app`` under gunicorn; ``python app.py`` is development
only and never enables the debugger unless ``FLASK_DEBUG=true``.
"""

import json
import logging
import sqlite3

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request, url_for,
)
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import load_config
from .db import close_db, get_db, is_locked_error
from .i18n import LANG_DEFAULT, SUPPORTED_LANGS, translate
from .migrations import run_migrations
from .security import (
    auth_configured, auth_mode, can_manage, client_ip, current_session_id,
    get_csrf_token, is_authenticated, valid_csrf,
)
from .utils import current_path_with_query, safe_referrer, wants_json

log = logging.getLogger("prompt_manage")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Endpoints reachable without the site password.
PUBLIC_ENDPOINTS = {"setup", "login", "static", "healthz", "logo_png", "favicon"}
# Endpoints exempt from CSRF (they change no state).
CSRF_EXEMPT_ENDPOINTS = {"healthz"}
# Endpoints whose responses may contain user content and must never be cached
# by a shared proxy.
_PRIVATE_ENDPOINTS = {
    "setup", "login", "logout", "settings", "export_all", "prompt_detail",
    "new_prompt", "versions_page", "diff_view", "api_prompt_content",
    "api_search", "api_tags", "index",
}
_STATIC_ENDPOINTS = {"static", "healthz", "logo_png", "favicon"}


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(load_config())
    app.secret_key = app.config["SECRET_KEY"]

    # Only trust forwarding headers when the deployment opts in. Host, port and
    # prefix are never trusted: the app only needs the client IP and scheme,
    # while the rest can create unsafe redirects if the container port is
    # reachable directly.
    if app.config["TRUST_PROXY_HEADERS"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    app.jinja_env.filters["loads"] = json.loads
    # Format an ISO timestamp: first n characters, "T" separator as a space.
    app.jinja_env.filters["ts"] = lambda s, n=19: (s or "")[:n].replace("T", " ")
    # Map a tag to one of 8 stable colour classes so the same tag always looks
    # the same without storing a colour per tag.
    app.jinja_env.filters["hue"] = lambda value: sum(ord(c) for c in str(value or "")) % 8

    _register_lifecycle(app)
    _register_error_handlers(app)

    from . import views  # noqa: PLC0415  (routes import the app package)
    views.register(app)

    with app.app_context():
        run_migrations()
    return app


def _register_lifecycle(app):
    @app.before_request
    def _load_request_state():
        if request.endpoint in _STATIC_ENDPOINTS or request.path.startswith("/static/"):
            return None
        if request.endpoint is None:
            return None

        conn = get_db()
        settings_map = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('auth_mode', 'language', 'auth_password_hash', 'auth_revision', "
                "'bootstrap_completed')"
            ).fetchall()
        }
        mode = settings_map.get("auth_mode") or "off"
        g.auth_mode = mode if mode in ("off", "global") else "global"
        language = (settings_map.get("language") or LANG_DEFAULT).lower()
        g.language = language if language in SUPPORTED_LANGS else LANG_DEFAULT
        g.has_password = bool(settings_map.get("auth_password_hash") or "")
        g.auth_revision = settings_map.get("auth_revision") or "1"
        g.bootstrap_completed = (
            app.config["APP_ENV"] != "production"
            or settings_map.get("bootstrap_completed") == "1"
        )

        # A genuinely new production database exposes nothing until an owner has
        # claimed it through /setup. Upgraded databases are marked complete by
        # migration 9 and never enter this branch.
        if not g.bootstrap_completed and request.endpoint != "setup":
            if request.method in ("GET", "HEAD"):
                return redirect(url_for("setup"), code=303)
            return jsonify({"status": "setup_required"}), 503

        # An anonymous visitor has no server-side session to revoke; rejecting
        # the write before it starts keeps a public CSRF token from contending
        # for SQLite's writer lock.
        if request.endpoint == "logout" and not current_session_id(create=False):
            return redirect(url_for("index"))

        if (
            g.auth_mode == "global"
            and g.has_password
            and request.endpoint not in PUBLIC_ENDPOINTS
            and not is_authenticated()
        ):
            return redirect(url_for("login", next=current_path_with_query()))

        # CSRF runs after the auth gate so an anonymous hostile upload fails at
        # the auth boundary before Werkzeug parses a large multipart body.
        if (
            request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.endpoint not in CSRF_EXEMPT_ENDPOINTS
        ):
            try:
                ok = valid_csrf()
            except BadRequest:
                abort(400)
            if not ok:
                log.warning("CSRF rejected endpoint=%s ip=%s", request.endpoint, client_ip())
                abort(403)
        return None

    app.teardown_appcontext(close_db)

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Build-SHA", app.config["BUILD_SHA"])
        if app.config["ENABLE_SECURITY_HEADERS"]:
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
        if app.config["ENABLE_HSTS"] and request.is_secure:
            value = f"max-age={max(0, int(app.config['HSTS_MAX_AGE']))}"
            if app.config["HSTS_INCLUDE_SUBDOMAINS"]:
                value += "; includeSubDomains"
            resp.headers.setdefault("Strict-Transport-Security", value)

        locked = getattr(g, "auth_mode", "off") != "off"
        if request.endpoint in _PRIVATE_ENDPOINTS or (
            locked and request.endpoint not in _STATIC_ENDPOINTS
        ):
            resp.headers["Cache-Control"] = "private, no-store"
            resp.headers["Pragma"] = "no-cache"
            # The library partial answers the same URL as the full page.
            resp.headers["Vary"] = "Cookie, X-Partial"
        return resp

    @app.context_processor
    def _template_globals():
        language = getattr(g, "language", LANG_DEFAULT)
        return {
            "t": lambda text: translate(language, text),
            "lang": language,
            "lang_html": "en" if language == "en" else "zh-CN",
            "csrf_token": get_csrf_token,
            "auth_mode": auth_mode(),
            "is_authenticated": is_authenticated(),
            "auth_configured": auth_configured(),
            "can_manage": can_manage(),
        }


def _register_error_handlers(app):
    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(_error):
        flash("上传失败：文件过大", "error")
        return redirect(safe_referrer(url_for("index"))), 303

    @app.errorhandler(sqlite3.OperationalError)
    def _database_busy(error):
        if is_locked_error(error):
            log.warning("request failed because SQLite is busy: %s", error)
            return db_busy_response(url_for("index"))
        log.exception("database operational error")
        return jsonify({"status": "error", "message": "database unavailable"}), 503

    @app.errorhandler(404)
    def _not_found(_error):
        if wants_json() or request.path.startswith("/api/"):
            return jsonify({"status": "not_found"}), 404
        return render_template("error.html", code=404), 404

    @app.errorhandler(403)
    def _forbidden(_error):
        if wants_json() or request.path.startswith("/api/"):
            return jsonify({"status": "forbidden"}), 403
        return render_template("error.html", code=403), 403


def db_busy_response(default_path):
    """A retryable response for the moments SQLite holds the writer lock."""
    if wants_json():
        response = jsonify({"status": "busy", "retry_after": 2})
    else:
        flash("数据库正忙，请稍后重试", "error")
        response = redirect(safe_referrer(default_path))
    response.headers["Retry-After"] = "2"
    return response, 503


app = create_app()
