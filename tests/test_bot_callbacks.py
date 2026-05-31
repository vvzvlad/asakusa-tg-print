"""Tests for the inline-callback handlers in src/bot.py.

Covers `_cb_print`, `_cb_cancel`, `_cb_reprint` and the `_cb_authorized`
helper. The `bot` fixture (see tests/conftest.py) wires mocked collaborators
and authorizes user 111 / chat 222; callbacks are built with `make_callback`.
"""

from unittest.mock import AsyncMock

# Error types raised by collaborators; imported per the test brief so failure
# branches can be driven via `.side_effect`.
from src.grist import GristError  # noqa: F401
from src.label_maker import LabelMakerError  # noqa: F401
from src.printer import PrinterError
from tests.conftest import make_callback, make_message


# ── _cb_print ────────────────────────────────────────────────────────────────

async def test_cb_print_denied_user_alerts_and_prints_nothing(bot):
    """An unauthorized user gets a show_alert answer and nothing is printed."""
    bot._pending[7] = {"pdf": b"x", "text": "hello", "kind": "text"}
    # Use a message in a non-authorized chat so neither the user nor the chat
    # allowlist grants access (chat 222 is authorized, so avoid it here).
    msg = make_message(user_id=999, chat_id=333)
    cb = make_callback("print:7", user_id=999, message=msg)

    await bot._cb_print(cb)

    cb.answer.assert_awaited_once_with("Печать запрещена", show_alert=True)
    bot.printer.print_pdf.assert_not_awaited()
    # The job must remain untouched in _pending (was never popped).
    assert bot._pending[7]["text"] == "hello"


async def test_cb_print_unknown_token_reports_stale_and_clears_markup(bot):
    """A token absent from _pending answers 'устарело' and clears the markup."""
    cb = make_callback("print:999")

    await bot._cb_print(cb)

    answer_text = cb.answer.await_args.args[0]
    assert "устарело" in answer_text
    cb.message.edit_reply_markup.assert_awaited_once()
    bot.printer.print_pdf.assert_not_awaited()


async def test_cb_print_printer_error_sets_error_caption_and_pops_job(bot):
    """A PrinterError edits the caption to ❌ and the job stays popped."""
    bot._pending[7] = {"pdf": b"x", "text": "hello", "kind": "text"}
    bot.printer.print_pdf.side_effect = PrinterError("printer offline")
    cb = make_callback("print:7")

    await bot._cb_print(cb)

    cb.message.edit_caption.assert_awaited_once()
    caption = cb.message.edit_caption.await_args.kwargs["caption"]
    assert "❌" in caption
    assert "Ошибка печати" in caption
    # The job was popped before printing, so a retry on the same token is stale.
    assert 7 not in bot._pending


async def test_cb_print_happy_prints_stores_and_shows_reprint_button(bot):
    """The happy path prints, stores a label, and shows a reprint keyboard."""
    bot._pending[7] = {"pdf": b"x", "text": "hello", "kind": "text"}
    cb = make_callback("print:7")

    await bot._cb_print(cb)

    bot.printer.print_pdf.assert_awaited_once_with(b"x")

    # A label was persisted with the job's text / authorized user.
    label = await bot.storage.get_label(1)
    assert label is not None
    assert label["text"] == "hello"
    assert label["user_id"] == 111
    assert label["kind"] == "text"

    # The caption was edited with a reprint keyboard pointing at that label.
    cb.message.edit_caption.assert_awaited_once()
    markup = cb.message.edit_caption.await_args.kwargs["reply_markup"]
    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == "reprint:1"
    assert "✅" in cb.message.edit_caption.await_args.kwargs["caption"]


async def test_cb_print_with_no_message_uses_chat_id_zero(bot):
    """callback.message=None must not crash; add_label gets chat_id 0."""
    bot._pending[7] = {"pdf": b"x", "text": "hello", "kind": "text"}
    cb = make_callback("print:7", message=None)

    await bot._cb_print(cb)

    bot.printer.print_pdf.assert_awaited_once_with(b"x")
    label = await bot.storage.get_label(1)
    assert label is not None
    assert label["chat_id"] == 0
    assert label["text"] == "hello"


# ── _cb_cancel ───────────────────────────────────────────────────────────────

async def test_cb_cancel_pops_job_and_marks_cancelled(bot):
    """Cancel pops the pending job, sets a 'Отменено' caption and answers."""
    bot._pending[7] = {"pdf": b"x", "text": "hello", "kind": "text"}
    cb = make_callback("cancel:7")

    await bot._cb_cancel(cb)

    assert 7 not in bot._pending
    cb.message.edit_caption.assert_awaited_once()
    caption = cb.message.edit_caption.await_args.kwargs["caption"]
    assert "Отменено" in caption
    assert "hello" in caption
    cb.answer.assert_awaited_once_with()
    bot.printer.print_pdf.assert_not_awaited()


# ── _cb_reprint ──────────────────────────────────────────────────────────────

async def test_cb_reprint_unknown_id_alerts_not_found(bot):
    """Reprinting a non-existent label answers 'не найдена' with show_alert."""
    cb = make_callback("reprint:99999")

    await bot._cb_reprint(cb)

    cb.answer.assert_awaited_once_with("Этикетка не найдена", show_alert=True)
    bot.printer.print_pdf.assert_not_awaited()


async def test_cb_reprint_text_renders_prints_and_confirms(bot):
    """A 'text' label re-renders, prints and posts a 🔁 confirmation."""
    label_id = await bot.storage.add_label("hello", 1, 2)
    cb = make_callback(f"reprint:{label_id}")

    await bot._cb_reprint(cb)

    bot.label_maker.render.assert_awaited_once_with("hello")
    bot.printer.print_pdf.assert_awaited_once()
    # Glaze path must not be touched for a text label.
    bot.grist.find_glaze.assert_not_awaited()
    cb.message.answer.assert_awaited_once()
    assert "🔁" in cb.message.answer.await_args.args[0]


async def test_cb_reprint_glaze_requeries_grist_and_prints(bot):
    """A 'glaze' label re-queries Grist, renders entities and prints."""
    label_id = await bot.storage.add_label("Лазурит Синий", 1, 2, kind="glaze")
    cb = make_callback(f"reprint:{label_id}")

    await bot._cb_reprint(cb)

    bot.grist.find_glaze.assert_awaited_once_with("Лазурит Синий")
    bot.glaze_maker.render_entities.assert_awaited_once()
    bot.printer.print_pdf.assert_awaited_once()
    # The plain text render path must not be used for a glaze label.
    bot.label_maker.render.assert_not_awaited()
    cb.message.answer.assert_awaited_once()
    assert "🔁" in cb.message.answer.await_args.args[0]


async def test_cb_reprint_glaze_missing_in_grist_reports_error(bot):
    """If Grist no longer has the glaze, the user gets an error message."""
    label_id = await bot.storage.add_label("Исчезла", 1, 2, kind="glaze")
    bot.grist.find_glaze = AsyncMock(return_value=None)
    cb = make_callback(f"reprint:{label_id}")

    await bot._cb_reprint(cb)

    bot.printer.print_pdf.assert_not_awaited()
    cb.message.answer.assert_awaited_once()
    err = cb.message.answer.await_args.args[0]
    assert "❌" in err
    assert "перепечатать" in err


# ── _cb_authorized ───────────────────────────────────────────────────────────

def test_cb_authorized_no_from_user_is_false(bot):
    """A callback without from_user is never authorized."""
    cb = make_callback("print:7", user_id=None)
    assert bot._cb_authorized(cb) is False


def test_cb_authorized_accessible_message_authorized_user(bot):
    """An accessible message + an allowlisted user is authorized."""
    # Default make_message chat is 222 (authorized) but user 111 is allowed too.
    cb = make_callback("print:7", user_id=111)
    assert bot._cb_authorized(cb) is True


def test_cb_authorized_accessible_message_authorized_by_chat(bot):
    """Authorization passes via the chat allowlist even for a non-allowed user."""
    # Build a message in the authorized chat 222 with an unlisted user 999.
    msg = make_message(user_id=999, chat_id=222)
    cb = make_callback("print:7", user_id=999, message=msg)
    assert bot._cb_authorized(cb) is True


def test_cb_authorized_inaccessible_falls_back_to_user_allowlist_allowed(bot):
    """For a >48h message, authorization falls back to the per-user allowlist."""
    cb = make_callback("print:7", user_id=111, inaccessible=True)
    assert bot._cb_authorized(cb) is True


def test_cb_authorized_inaccessible_unlisted_user_is_false(bot):
    """A >48h message from a non-allowlisted user is denied (no chat fallback)."""
    cb = make_callback("print:7", user_id=999, inaccessible=True)
    assert bot._cb_authorized(cb) is False
