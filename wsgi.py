"""WSGI entrypoint for production servers (gunicorn / uWSGI).

Usage:
    gunicorn -c gunicorn.conf.py wsgi:app

Importing ``app`` runs configuration validation and database migrations
exactly once, so the process fails fast on misconfiguration (e.g. a missing
SECRET_KEY in production) instead of starting in a broken state.
"""

from app import app

# Expose the WSGI callable under the conventional name.
application = app

if __name__ == "__main__":  # pragma: no cover - convenience for `python wsgi.py`
    app.run()
