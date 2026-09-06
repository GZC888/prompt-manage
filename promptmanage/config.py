"""Environment configuration.

Every knob is read from an environment variable exactly once, validated here,
and handed to Flask as a plain dict. Invalid values raise at import time so a
misconfigured container fails fast instead of misbehaving at runtime.
"""

import logging
import os
from datetime import timedelta

log = logging.getLogger("prompt_manage")

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}

_WEAK_SECRETS = {
    "", "dev-secret", "change-me", "changeme", "secret", "password",
    "replace-me-with-a-long-random-string",
}
_WEAK_BOOTSTRAP_TOKENS = {
    "bootstrap-secret", "change-me", "changeme", "secret", "password",
    "replace-me-with-another-random-token",
}


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise RuntimeError(f"{name} must be a boolean: true/false, yes/no, on/off, or 1/0")


def env_int(name, default, *, minimum=None, maximum=None):
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw.strip())
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be <= {maximum}")
    return value


def env_samesite(name, default="Lax"):
    raw = (os.environ.get(name, default) or default).strip().lower()
    allowed = {"lax": "Lax", "strict": "Strict", "none": "None"}
    if raw not in allowed:
        raise RuntimeError(f"{name} must be one of: Lax, Strict, None")
    return allowed[raw]


def _resolve_secret_key(app_env):
    raw = (env("SECRET_KEY", "") or "").strip()
    if app_env == "production":
        if not raw or raw.lower() in _WEAK_SECRETS or len(raw) < 32:
            raise RuntimeError(
                "SECRET_KEY is missing or too weak. In production you must set a "
                "strong, random SECRET_KEY (e.g. `openssl rand -hex 32`). Refusing to start."
            )
        return raw
    if not raw:
        log.warning(
            "SECRET_KEY not set; using an insecure development key. "
            "Set APP_ENV=production and a strong SECRET_KEY before deploying."
        )
        return "dev-secret-not-for-production"
    return raw


def load_config():
    """Read and validate the whole environment, returning Flask config."""
    app_env = (env("APP_ENV", "production") or "production").strip().lower()
    if app_env not in {"production", "development", "testing"}:
        raise RuntimeError("APP_ENV must be production, development, or testing")

    db_path = env("DB_PATH", "/app/data/data.sqlite3")
    if app_env == "production" and (
        not isinstance(db_path, str) or not db_path.startswith("/") or db_path == ":memory:"
    ):
        raise RuntimeError("DB_PATH must be an absolute persistent path in production")

    samesite = env_samesite("SESSION_COOKIE_SAMESITE", "Lax")
    cookie_secure = env_bool("SESSION_COOKIE_SECURE", app_env == "production")
    if samesite == "None" and not cookie_secure:
        raise RuntimeError("SESSION_COOKIE_SAMESITE=None requires SESSION_COOKIE_SECURE=true")

    secret_key = _resolve_secret_key(app_env)
    bootstrap_token = (env("BOOTSTRAP_TOKEN", "") or "").strip()
    if app_env == "production" and bootstrap_token:
        if len(bootstrap_token) < 32 or bootstrap_token.lower() in _WEAK_BOOTSTRAP_TOKENS:
            raise RuntimeError(
                "BOOTSTRAP_TOKEN is too short or uses a known placeholder. Generate an "
                "independent random token (e.g. `openssl rand -hex 32`), or leave it "
                "empty after setup is complete."
            )
        if bootstrap_token == secret_key:
            raise RuntimeError("BOOTSTRAP_TOKEN must be different from SECRET_KEY")

    max_import_mb = env_int("MAX_IMPORT_SIZE_MB", 10, minimum=1)

    return {
        "APP_ENV": app_env,
        "APP_PORT": env_int("APP_PORT", 3501, minimum=1, maximum=65535),
        "DB_PATH": db_path,
        "SECRET_KEY": secret_key,
        "BOOTSTRAP_TOKEN": bootstrap_token,
        "BUILD_SHA": (env("BUILD_SHA", "dev") or "dev").strip()[:128],
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": samesite,
        "SESSION_COOKIE_SECURE": cookie_secure,
        "PERMANENT_SESSION_LIFETIME": timedelta(
            days=env_int("PERMANENT_SESSION_DAYS", 3650, minimum=1)
        ),
        "AUTH_LOGIN_MAX_ATTEMPTS": env_int("AUTH_LOGIN_MAX_ATTEMPTS", 10, minimum=1),
        "AUTH_LOGIN_WINDOW_SECONDS": env_int("AUTH_LOGIN_WINDOW_SECONDS", 900, minimum=1),
        "AUTH_LOCK_SECONDS": env_int("AUTH_LOCK_SECONDS", 900, minimum=1),
        "GLOBAL_LOGIN_MAX_ATTEMPTS": env_int("GLOBAL_LOGIN_MAX_ATTEMPTS", 1000, minimum=1),
        "GLOBAL_LOGIN_WINDOW_SECONDS": env_int("GLOBAL_LOGIN_WINDOW_SECONDS", 3600, minimum=1),
        "MAX_IMPORT_SIZE_MB": max_import_mb,
        "IMPORT_BACKUP_RETENTION": env_int("IMPORT_BACKUP_RETENTION", 20, minimum=1),
        "ENABLE_SECURITY_HEADERS": env_bool("ENABLE_SECURITY_HEADERS", True),
        "TRUST_PROXY_HEADERS": env_bool("TRUST_PROXY_HEADERS", False),
        "ENABLE_HSTS": env_bool("ENABLE_HSTS", app_env == "production"),
        "HSTS_MAX_AGE": env_int("HSTS_MAX_AGE", 31536000, minimum=0),
        "HSTS_INCLUDE_SUBDOMAINS": env_bool("HSTS_INCLUDE_SUBDOMAINS", False),
        # Hard cap on request bodies: the import limit plus headroom for form fields.
        "MAX_CONTENT_LENGTH": (max_import_mb + 4) * 1024 * 1024,
    }
