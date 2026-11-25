"""Settings should load, and should complain clearly when they cannot."""

import os
from pathlib import Path

import pytest

from src.config import KEY_PATH_VAR, KEY_TEXT_VAR, Settings, get_settings


def _test_key_pem() -> str:
    """The throwaway key conftest generated, as text."""
    return Path(os.environ[KEY_PATH_VAR]).read_text()


def test_settings_load_from_the_environment():
    settings = get_settings()
    assert settings.app_id == "000000"
    assert settings.webhook_secret.startswith("test-secret")


def test_the_suite_runs_against_a_key_file():
    """conftest points at a .pem, so this is the path branch, not the text one."""
    settings = get_settings()
    assert settings.private_key is None
    assert settings.private_key_path is not None
    assert settings.private_key_path.is_file()


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
    monkeypatch.setenv(KEY_PATH_VAR, "/nowhere/missing.pem")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="Private key not found"):
        get_settings()

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# The deployment branch: the key arrives as text, because a container has no
# file to point at. conftest sets the path variable for the whole suite, so
# without these the branch would never run.
# ---------------------------------------------------------------------------


def test_the_key_can_arrive_as_text(monkeypatch):
    pem = _test_key_pem()
    monkeypatch.setenv(KEY_TEXT_VAR, pem)
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.private_key_path is None
    assert settings.read_private_key() == pem

    get_settings.cache_clear()


def test_the_text_wins_over_the_path(monkeypatch):
    """Values in the real environment win, so a deployment overrides .env.

    The path here does not exist. If it were consulted at all this would
    raise, so passing proves the text took precedence rather than merely
    that both happened to work.
    """
    pem = _test_key_pem()
    monkeypatch.setenv(KEY_TEXT_VAR, pem)
    monkeypatch.setenv(KEY_PATH_VAR, "/nowhere/missing.pem")
    get_settings.cache_clear()

    assert get_settings().read_private_key() == pem

    get_settings.cache_clear()


def test_a_blank_variable_counts_as_absent(monkeypatch):
    """Hosting providers will happily save an empty value."""
    monkeypatch.setenv(KEY_TEXT_VAR, "")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.private_key is None
    assert settings.private_key_path is not None

    get_settings.cache_clear()


def test_neither_key_variable_names_both(monkeypatch):
    monkeypatch.delenv(KEY_PATH_VAR)
    get_settings.cache_clear()

    with pytest.raises(RuntimeError) as error:
        get_settings()
    assert KEY_TEXT_VAR in str(error.value)
    assert KEY_PATH_VAR in str(error.value)

    get_settings.cache_clear()


def test_a_truncated_key_is_rejected_at_startup(monkeypatch):
    """The point of checking at boot: a bad paste fails now, not mid-review.

    A truncated PEM is a non-empty string, so it passes a presence check and
    then raises inside pyjwt on the first webhook.
    """
    pem = _test_key_pem()
    monkeypatch.setenv(KEY_TEXT_VAR, pem[: len(pem) // 2])
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="not a readable PEM"):
        get_settings()

    get_settings.cache_clear()


def test_a_key_file_holding_rubbish_is_rejected_too(monkeypatch, tmp_path):
    """The same check guards both branches, which it did not before."""
    bad = tmp_path / "not-a-key.pem"
    bad.write_text("hello\n")
    monkeypatch.setenv(KEY_PATH_VAR, str(bad))
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="not a readable PEM"):
        get_settings()

    get_settings.cache_clear()


def test_settings_with_no_key_at_all_explains_itself():
    """read_private_key should not return None or crash obscurely."""
    settings = Settings(
        webhook_secret="s",
        app_id="1",
        client_id="c",
        database_url="d",
    )
    with pytest.raises(RuntimeError, match=KEY_TEXT_VAR):
        settings.read_private_key()
