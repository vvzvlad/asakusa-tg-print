"""Tests for src.settings: the pure _parse_ids parser and env-driven loading.

The integration tests construct `Settings(_env_file=None)` so they ignore the
developer's real .env entirely; all input comes from monkeypatch.setenv. A dummy
TELEGRAM_BOT_TOKEN is already in os.environ (injected by conftest before any
src.* import), so an unset-token case explicitly deletes it.
"""

import pytest
from pydantic import ValidationError

from src.settings import Settings


# ── UNIT: Settings._parse_ids (pure staticmethod) ────────────────────────────

def test_parse_ids_basic_comma_separated():
    assert Settings._parse_ids("123,456") == {123, 456}


def test_parse_ids_empty_string_yields_empty_set():
    assert Settings._parse_ids("") == set()


def test_parse_ids_trims_surrounding_whitespace():
    assert Settings._parse_ids("  123 , 456  ") == {123, 456}


def test_parse_ids_deduplicates_repeated_values():
    assert Settings._parse_ids("123,123") == {123}


def test_parse_ids_rejects_non_numeric_token():
    with pytest.raises(ValueError):
        Settings._parse_ids("123,abc")


# ── INTEGRATION: env loading via Settings(_env_file=None) ────────────────────

def test_label_rotate_true_string_coerced_to_bool(monkeypatch):
    monkeypatch.setenv("LABEL_ROTATE", "true")
    s = Settings(_env_file=None)
    assert s.label_rotate is True


def test_label_rotate_false_string_coerced_to_bool(monkeypatch):
    monkeypatch.setenv("LABEL_ROTATE", "false")
    s = Settings(_env_file=None)
    assert s.label_rotate is False


def test_label_rotate_numeric_one_coerced_to_true(monkeypatch):
    monkeypatch.setenv("LABEL_ROTATE", "1")
    s = Settings(_env_file=None)
    assert s.label_rotate is True


def test_label_rotate_numeric_zero_coerced_to_false(monkeypatch):
    monkeypatch.setenv("LABEL_ROTATE", "0")
    s = Settings(_env_file=None)
    assert s.label_rotate is False


def test_printer_fit_to_page_true_string_coerced_to_bool(monkeypatch):
    monkeypatch.setenv("PRINTER_FIT_TO_PAGE", "true")
    s = Settings(_env_file=None)
    assert s.printer_fit_to_page is True


def test_printer_fit_to_page_zero_coerced_to_false(monkeypatch):
    monkeypatch.setenv("PRINTER_FIT_TO_PAGE", "0")
    s = Settings(_env_file=None)
    assert s.printer_fit_to_page is False


def test_missing_required_label_maker_url_raises(monkeypatch):
    # label_maker_url is an own-service address with no default — unset must fail
    # validation (a silent localhost fallback would mask misconfiguration on prod).
    monkeypatch.delenv("LABEL_MAKER_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_default_label_rotate_is_false_when_unset(monkeypatch):
    monkeypatch.delenv("LABEL_ROTATE", raising=False)
    s = Settings(_env_file=None)
    assert s.label_rotate is False


def test_default_printer_name_and_media_when_unset(monkeypatch):
    monkeypatch.delenv("PRINTER_NAME", raising=False)
    monkeypatch.delenv("PRINTER_MEDIA", raising=False)
    s = Settings(_env_file=None)
    assert s.printer_name == "XP-365B"
    assert s.printer_media == "Custom.165x114pt"


def test_allowed_user_ids_defaults_to_empty_string(monkeypatch):
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    s = Settings(_env_file=None)
    assert s.allowed_user_ids == ""


def test_label_maker_url_read_from_env(monkeypatch):
    monkeypatch.setenv("LABEL_MAKER_URL", "http://printer.local:9000")
    s = Settings(_env_file=None)
    assert s.label_maker_url == "http://printer.local:9000"


def test_missing_required_telegram_bot_token_raises(monkeypatch):
    # The required field has no default; with .env ignored and the env var
    # removed, construction must fail validation.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
