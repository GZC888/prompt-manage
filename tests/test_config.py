"""Configuration is validated at import time, so a bad container fails fast."""

import importlib
import sys

import pytest


def _reload(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cfg.sqlite3"))
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-0123456789abcdef")
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for name in [n for n in list(sys.modules) if n == "promptmanage" or n.startswith("promptmanage.")]:
        del sys.modules[name]
    return importlib.import_module("promptmanage")


@pytest.mark.parametrize("env", [
    {"APP_ENV": "staging"},
    {"SESSION_COOKIE_SAMESITE": "sometimes"},
    {"ENABLE_HSTS": "maybe"},
    {"APP_PORT": "not-a-number"},
    {"APP_PORT": "70000"},
    {"MAX_IMPORT_SIZE_MB": "0"},
    {"SESSION_COOKIE_SAMESITE": "None", "SESSION_COOKIE_SECURE": "false"},
])
def test_invalid_configuration_refuses_to_start(monkeypatch, tmp_path, env):
    with pytest.raises(RuntimeError):
        _reload(monkeypatch, tmp_path, **env)


@pytest.mark.parametrize("secret", ["", "dev-secret", "short"])
def test_production_rejects_a_weak_secret_key(monkeypatch, tmp_path, secret):
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _reload(monkeypatch, tmp_path, APP_ENV="production",
                DB_PATH="/tmp/prompt-manage-config-test.sqlite3", SECRET_KEY=secret)


def test_production_requires_an_absolute_db_path(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="DB_PATH"):
        _reload(monkeypatch, tmp_path, APP_ENV="production", DB_PATH="relative.sqlite3",
                SECRET_KEY="a" * 40)


def test_production_rejects_a_placeholder_bootstrap_token(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="BOOTSTRAP_TOKEN"):
        _reload(monkeypatch, tmp_path, APP_ENV="production",
                DB_PATH="/tmp/prompt-manage-config-test.sqlite3", SECRET_KEY="a" * 40,
                BOOTSTRAP_TOKEN="changeme")


def test_production_rejects_reusing_the_secret_key_as_token(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="BOOTSTRAP_TOKEN"):
        _reload(monkeypatch, tmp_path, APP_ENV="production",
                DB_PATH="/tmp/prompt-manage-config-test.sqlite3", SECRET_KEY="a" * 40,
                BOOTSTRAP_TOKEN="a" * 40)


def test_boolean_values_are_parsed_leniently(monkeypatch, tmp_path):
    module = _reload(monkeypatch, tmp_path, ENABLE_HSTS="YES", TRUST_PROXY_HEADERS="Off")
    assert module.app.config["ENABLE_HSTS"] is True
    assert module.app.config["TRUST_PROXY_HEADERS"] is False
