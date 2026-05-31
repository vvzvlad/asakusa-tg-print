"""Unit tests for the module-level helpers and static auth in src.bot.

Covers:
- _short        : truncation/off-by-one and custom limit.
- _reply_text   : text / caption precedence and stripping.
- _confirm_keyboard / _reprint_keyboard : callback_data prefixes used for routing.
- PrintBot._authorized : user/chat allowlist logic over the live settings.
"""

from src.bot import (
    PrintBot,
    _confirm_keyboard,
    _reply_text,
    _reprint_keyboard,
    _short,
)
from tests.conftest import make_message


# ── _short ───────────────────────────────────────────────────────────────────

def test_short_shorter_than_limit_unchanged():
    assert _short("abc", 5) == "abc"


def test_short_equal_to_limit_unchanged():
    # len == limit: no truncation.
    assert _short("abcde", 5) == "abcde"


def test_short_one_over_limit_truncates_to_exactly_limit():
    # len == limit + 1: must be truncated to EXACTLY `limit` chars ending with "…".
    result = _short("abcdef", 5)
    assert result == "abcd…"
    assert len(result) == 5


def test_short_off_by_one_example_from_spec():
    # _short("abcdef", 3) -> "ab…" (impl is text[:limit-1] + "…"), length == limit.
    result = _short("abcdef", 3)
    assert result == "ab…"
    assert len(result) == 3


def test_short_empty_string_returns_empty():
    assert _short("", 3) == ""


def test_short_uses_custom_limit():
    long_text = "x" * 50
    result = _short(long_text, 10)
    assert len(result) == 10
    assert result.endswith("…")
    assert result == "x" * 9 + "…"


def test_short_default_limit_is_900():
    # Default limit param: 900 unchanged, 901 truncated to 900.
    assert _short("a" * 900) == "a" * 900
    truncated = _short("a" * 901)
    assert len(truncated) == 900
    assert truncated == "a" * 899 + "…"


# ── _reply_text ──────────────────────────────────────────────────────────────

def test_reply_text_no_reply_returns_empty():
    msg = make_message("/print")
    assert _reply_text(msg) == ""


def test_reply_text_uses_reply_text():
    msg = make_message("/print", reply_text="hello world")
    assert _reply_text(msg) == "hello world"


def test_reply_text_strips_surrounding_whitespace():
    msg = make_message("/print", reply_text="  spaced  ")
    assert _reply_text(msg) == "spaced"


def test_reply_text_falls_back_to_caption_when_no_text():
    # reply has only a caption (text is None) -> caption is used.
    msg = make_message("/print", reply_caption="caption text")
    assert _reply_text(msg) == "caption text"


def test_reply_text_prefers_text_over_caption():
    # When both present, .text wins.
    msg = make_message("/print", reply_text="the text", reply_caption="the caption")
    assert _reply_text(msg) == "the text"


# ── keyboards ────────────────────────────────────────────────────────────────

def test_confirm_keyboard_callback_data():
    kb = _confirm_keyboard(7)
    print_btn = kb.inline_keyboard[0][0]
    cancel_btn = kb.inline_keyboard[0][1]
    assert print_btn.callback_data == "print:7"
    assert cancel_btn.callback_data == "cancel:7"


def test_confirm_keyboard_prefixes_match_routing():
    # The router registers F.data.startswith("print:") / "cancel:".
    kb = _confirm_keyboard(123)
    assert kb.inline_keyboard[0][0].callback_data.startswith("print:")
    assert kb.inline_keyboard[0][1].callback_data.startswith("cancel:")


def test_reprint_keyboard_callback_data():
    kb = _reprint_keyboard(42)
    assert kb.inline_keyboard[0][0].callback_data == "reprint:42"


def test_reprint_keyboard_prefix_matches_routing():
    # The router registers F.data.startswith("reprint:").
    kb = _reprint_keyboard(999)
    assert kb.inline_keyboard[0][0].callback_data.startswith("reprint:")


# ── PrintBot._authorized ─────────────────────────────────────────────────────

def test_authorized_user_in_user_allowlist(monkeypatch):
    monkeypatch.setattr("src.settings.settings.allowed_user_ids", "111")
    monkeypatch.setattr("src.settings.settings.allowed_chat_ids", "222")
    assert PrintBot._authorized(111, 0) is True


def test_authorized_chat_in_chat_allowlist(monkeypatch):
    monkeypatch.setattr("src.settings.settings.allowed_user_ids", "111")
    monkeypatch.setattr("src.settings.settings.allowed_chat_ids", "222")
    assert PrintBot._authorized(0, 222) is True


def test_authorized_neither_in_allowlist(monkeypatch):
    monkeypatch.setattr("src.settings.settings.allowed_user_ids", "111")
    monkeypatch.setattr("src.settings.settings.allowed_chat_ids", "222")
    assert PrintBot._authorized(0, 0) is False


def test_authorized_empty_allowlists_deny_all(monkeypatch):
    # Empty lists = nobody can print.
    monkeypatch.setattr("src.settings.settings.allowed_user_ids", "")
    monkeypatch.setattr("src.settings.settings.allowed_chat_ids", "")
    assert PrintBot._authorized(111, 222) is False
    assert PrintBot._authorized(0, 0) is False
