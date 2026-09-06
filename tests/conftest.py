"""Pytest fixtures: a fresh, isolated application per test.

Configuration is read at import time, so isolation means dropping the whole
``promptmanage`` package from ``sys.modules`` and importing it again against a
throwaway database.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def appmod(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("AUTH_LOGIN_MAX_ATTEMPTS", "10")
    monkeypatch.setenv("AUTH_LOGIN_WINDOW_SECONDS", "900")
    monkeypatch.setenv("AUTH_LOCK_SECONDS", "900")
    for name in [n for n in list(sys.modules) if n == "app" or n == "promptmanage" or n.startswith("promptmanage.")]:
        del sys.modules[name]
    import app as app_module

    app_module.app.config.update(TESTING=True)
    return app_module


@pytest.fixture
def client(appmod):
    return appmod.app.test_client()
