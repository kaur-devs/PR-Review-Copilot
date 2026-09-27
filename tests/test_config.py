"""Settings should load, and should complain clearly when they cannot."""

import os

import pytest

from src.config import Settings, get_settings


def test_settings_load_from_the_environment():
    settings = get_settings()
    assert settings.app_id == "000000"
    assert settings.webhook_secret.startswith("test-secret")


def test_the_private_key_path_points_at_a_real_file():
    assert get_settings().private_key_path.is_file()


def test_the_key_can_be_read():
    assert "PRIVATE KEY" in get_settings().read_private_key()


def test_settings_cannot_be_changed_after_loading():
    """frozen=True stops any part of the code rewriting a secret."""
    settings = get_settings()
    with pytest.raises(Exception):
        settings.webhook_secret = "something else"


def test_a_missing_variable_is_named_in_the_error(monkeypatch):
    """The error should say which variable is missing, not just that one is."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="GITHUB_WEBHOOK_SECRET"):
        get_settings()

    get_settings.cache_clear()


def test_a_missing_key_file_is_reported(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", "/nowhere/missing.pem")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="Private key not found"):
        get_settings()

    get_settings.cache_clear()
