"""Integration tests for PrintBot's command handlers in src.bot.

Uses the `bot` fixture (collaborators mocked, pdf_to_png stubbed, user 111 /
chat 222 authorized). Each handler is driven directly with a stub `Message`
and a real `CommandObject`. Failure paths are exercised by setting `.side_effect`
on the relevant mocked collaborator.

Covers:
- _cmd_print      : auth denial, no-text usage hint, over-length rejection,
                    happy path (pending stored + confirm keyboard + render args),
                    LabelMakerError surfaced on the status message.
- _cmd_printglaze : GristError, glaze-not-found, happy path (kind=='glaze').
- _cmd_sudoprint  : auth denial, happy path (print + persist + reprint keyboard),
                    PrinterError surfaced on the status message.
- _cmd_start      : authorized help, denied help + ids hint.
"""

from aiogram.filters import CommandObject

from src.bot import HELP_TEXT, MAX_LABEL_CHARS
from src.grist import GristError
from src.label_maker import LabelMakerError
from src.printer import PrinterError
from tests.conftest import make_glaze_fields, make_message


def _print_cmd(args):
    return CommandObject(command="print", args=args)


def _glaze_cmd(args):
    return CommandObject(command="printglaze", args=args)


def _sudo_cmd(args):
    return CommandObject(command="sudoprint", args=args)


# ── _cmd_print ───────────────────────────────────────────────────────────────

async def test_print_denied_user_gets_ban_message_and_does_not_render(bot):
    # Unauthorized user (not 111, chat not 222): refusal, render never awaited.
    msg = make_message("/print x", user_id=999, chat_id=888)
    await bot._cmd_print(msg, _print_cmd("x"))

    msg.answer.assert_awaited_once()
    sent = msg.answer.await_args.args[0]
    assert sent.startswith("⛔")
    bot.label_maker.render.assert_not_awaited()
    assert bot._pending == {}


async def test_print_no_text_shows_usage_hint(bot):
    # args=None and no reply -> usage hint, nothing rendered.
    msg = make_message("/print")
    await bot._cmd_print(msg, _print_cmd(None))

    msg.answer.assert_awaited_once()
    assert "Использование" in msg.answer.await_args.args[0]
    bot.label_maker.render.assert_not_awaited()


async def test_print_too_long_text_rejected(bot):
    # Text over MAX_LABEL_CHARS -> rejection, nothing rendered.
    long_text = "a" * (MAX_LABEL_CHARS + 1)
    msg = make_message("/print " + long_text)
    await bot._cmd_print(msg, _print_cmd(long_text))

    msg.answer.assert_awaited_once()
    assert "слишком длинный" in msg.answer.await_args.args[0]
    bot.label_maker.render.assert_not_awaited()


async def test_print_happy_path_stores_pending_and_shows_confirm(bot):
    # Authorized + valid text: render with that text, one pending preview,
    # answer_photo with a confirm keyboard.
    msg = make_message("/print Hello")
    await bot._cmd_print(msg, _print_cmd("Hello"))

    bot.label_maker.render.assert_awaited_once_with("Hello")
    assert len(bot._pending) == 1
    job = next(iter(bot._pending.values()))
    assert job["text"] == "Hello"
    assert job["kind"] == "text"

    msg.answer_photo.assert_awaited_once()
    kb = msg.answer_photo.await_args.kwargs["reply_markup"]
    # Confirm keyboard routes via print:/cancel: callback prefixes.
    assert kb.inline_keyboard[0][0].callback_data.startswith("print:")
    assert kb.inline_keyboard[0][1].callback_data.startswith("cancel:")


async def test_print_render_error_edits_status_with_failure(bot):
    # LabelMakerError during render -> status (msg.answer.return_value) edited with ❌,
    # no preview shown, nothing left pending.
    bot.label_maker.render.side_effect = LabelMakerError("boom")
    msg = make_message("/print Hello")
    await bot._cmd_print(msg, _print_cmd("Hello"))

    status = msg.answer.return_value
    status.edit_text.assert_awaited_once()
    assert status.edit_text.await_args.args[0].startswith("❌")
    msg.answer_photo.assert_not_awaited()
    assert bot._pending == {}


# ── _cmd_printglaze ──────────────────────────────────────────────────────────

async def test_printglaze_grist_error_edits_status_with_failure(bot):
    # GristError from find_glaze -> status edited with ❌, no preview.
    bot.grist.find_glaze.side_effect = GristError("api down")
    msg = make_message("/printglaze Лазурит")
    await bot._cmd_printglaze(msg, _glaze_cmd("Лазурит"))

    status = msg.answer.return_value
    status.edit_text.assert_awaited_once()
    assert status.edit_text.await_args.args[0].startswith("❌")
    msg.answer_photo.assert_not_awaited()


async def test_printglaze_not_found_edits_status(bot):
    # find_glaze returns None -> "не найдена".
    bot.grist.find_glaze.return_value = None
    msg = make_message("/printglaze Нечто")
    await bot._cmd_printglaze(msg, _glaze_cmd("Нечто"))

    status = msg.answer.return_value
    status.edit_text.assert_awaited_once()
    assert "не найдена" in status.edit_text.await_args.args[0]
    msg.answer_photo.assert_not_awaited()


async def test_printglaze_happy_path_stores_glaze_pending(bot):
    # find_glaze returns fields -> a pending entry of kind 'glaze', preview shown.
    bot.grist.find_glaze.return_value = make_glaze_fields()
    msg = make_message("/printglaze Лазурит")
    await bot._cmd_printglaze(msg, _glaze_cmd("Лазурит"))

    assert len(bot._pending) == 1
    job = next(iter(bot._pending.values()))
    assert job["kind"] == "glaze"
    msg.answer_photo.assert_awaited_once()
    kb = msg.answer_photo.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data.startswith("print:")


# ── _cmd_sudoprint ───────────────────────────────────────────────────────────

async def test_sudoprint_denied_user_gets_ban_and_does_not_print(bot):
    # Unauthorized user: refusal, printer never invoked.
    msg = make_message("/sudoprint x", user_id=999, chat_id=888)
    await bot._cmd_sudoprint(msg, _sudo_cmd("x"))

    msg.answer.assert_awaited_once()
    assert msg.answer.await_args.args[0].startswith("⛔")
    bot.printer.print_pdf.assert_not_awaited()


async def test_sudoprint_happy_path_prints_persists_and_offers_reprint(bot):
    # Authorized: print immediately, persist a label, offer a reprint keyboard.
    msg = make_message("/sudoprint Now")
    await bot._cmd_sudoprint(msg, _sudo_cmd("Now"))

    bot.printer.print_pdf.assert_awaited_once()
    # First inserted label gets id 1; it must have been persisted.
    stored = await bot.storage.get_label(1)
    assert stored is not None
    assert stored["text"] == "Now"

    msg.answer_photo.assert_awaited_once()
    kb = msg.answer_photo.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data.startswith("reprint:")


async def test_sudoprint_printer_error_edits_status_and_does_not_persist(bot):
    # PrinterError -> status edited with ❌ печати, nothing persisted, no preview.
    bot.printer.print_pdf.side_effect = PrinterError("offline")
    msg = make_message("/sudoprint Now")
    await bot._cmd_sudoprint(msg, _sudo_cmd("Now"))

    status = msg.answer.return_value
    status.edit_text.assert_awaited_once()
    edited = status.edit_text.await_args.args[0]
    assert edited.startswith("❌") and "печати" in edited
    msg.answer_photo.assert_not_awaited()
    assert await bot.storage.get_label(1) is None


# ── _cmd_start ───────────────────────────────────────────────────────────────

async def test_start_authorized_sends_help(bot):
    # Authorized user gets the plain help text (no denial hint).
    msg = make_message("/start")
    await bot._cmd_start(msg)

    msg.answer.assert_awaited_once()
    sent = msg.answer.await_args.args[0]
    assert sent == HELP_TEXT


async def test_start_denied_sends_help_plus_ids_hint(bot):
    # Unauthorized user gets help plus the ⚠️ block with their user/chat ids.
    msg = make_message("/start", user_id=999, chat_id=888)
    await bot._cmd_start(msg)

    msg.answer.assert_awaited_once()
    sent = msg.answer.await_args.args[0]
    assert HELP_TEXT in sent
    assert "⚠️" in sent
    assert "user_id: 999" in sent
    assert "chat_id: 888" in sent
