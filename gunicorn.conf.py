"""Gunicorn configuration.

All values can be overridden via environment variables so the same image
works for a tiny personal VPS or a slightly larger deployment.
"""

import os


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Network ---------------------------------------------------------------------
_port = os.environ.get("APP_PORT", "3501")
bind = os.environ.get("GUNICORN_BIND", f"0.0.0.0:{_port}")

# Workers ---------------------------------------------------------------------
# SQLite is single-file; a small number of workers + threads is plenty for a
# personal tool and avoids excessive lock contention. Override if needed.
workers = _int_env("GUNICORN_WORKERS", 2)
threads = _int_env("GUNICORN_THREADS", 4)
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
timeout = _int_env("GUNICORN_TIMEOUT", 60)
graceful_timeout = _int_env("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _int_env("GUNICORN_KEEPALIVE", 5)

# Import the app once in the master process and fork workers from it. This also
# means database migrations (run at import time) execute a single time instead
# of racing across workers.
preload_app = True

# Logging ---------------------------------------------------------------------
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")  # stdout
errorlog = os.environ.get("GUNICORN_ERROR_LOG", "-")    # stderr
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Deliberately omit the query string: full-text searches can contain prompt
# fragments and must not be copied into reverse-proxy or container logs.
access_log_format = os.environ.get(
    "GUNICORN_ACCESS_LOG_FORMAT",
    '%(h)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(D)sus',
)
