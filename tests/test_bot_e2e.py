"""End-to-end user journeys through PrintBot (src.bot).

These exercise whole command -> preview -> confirm -> print -> persist flows
against the `bot` fixture, whose collaborators (label_maker / glaze_maker /
grist / printer) and `pdf_to_png` are all mocked, so no real rendering, HTTP
or printing happens. User 111 / chat 222 are authorized by the fixture.

Covers:
- E2E-1 «Печать с подтверждением»: /print -> pending token -> print callback
  prints, persists a label and sends a reprint keyboard; the denied branch
  prints nothing.
- E2E-2 «Мгновенная печать + перепечатка»: /sudoprint prints immediately and
  saves a label with a 🔁 keyboard; the reprint callback prints a SECOND time.
- E2E-3 «Печать глазури из Grist»: /printglaze (found) -> pending kind 'glaze'
  -> confirm prints and persists a glaze label; the not-found branch creates
  no pending job.
"""

from unittest.mock import AsyncMock

from aiogram.filters import CommandObject
from aiogram.types import InlineKeyboardMarkup

from tests.conftest import make_callback, make_message, make_glaze_fields


# ── E2E-1: /print with confirmation ──────────────────────────────────────────

async def test_print_confirm_flow_prints_persists_and_offers_reprint(bot):
    """/print (authorized) -> a pending token, then confirming it prints the
    PDF, saves the label, and sends a «🔁 Перепечатать» keyboard."""
    msg = make_message("/print Привет мир")
    await bot._cmd_print(msg, CommandObject(args="Привет мир"))

    # A preview was sent and exactly one job is pending awaiting confirmation.
    msg.answer_photo.assert_awaited_once()
    assert len(bot._pending) == 1
    assert bot.printer.print_pdf.await_count == 0  # nothing printed before confirm

    token = next(iter(bot._pending))
    cb = make_callback(f"print:{token}")
    await bot._cb_print(cb)

    # The pending job was consumed and the PDF was sent to the printer.
    assert token not in bot._pending
    bot.printer.print_pdf.assert_awaited_once()
    pdf_sent = bot.printer.print_pdf.await_args.args[0]
    assert pdf_sent == b"%PDF-1.4 fake"

    # The label was persisted as a 'text' label (fresh in-memory store -> id 1).
    label = await bot.storage.get_label(1)
    assert label is not None
    assert label["text"] == "Привет мир"
    assert label["kind"] == "text"
    assert label["chat_id"] == 222
    assert label["user_id"] == 111

    # The confirmation edit carries a reprint keyboard pointing at label id 1.
    edit_kwargs = cb.message.edit_caption.call_args.kwargs
    markup = edit_kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[0][0].callback_data == "reprint:1"
    assert "Отправлено на печать" in edit_kwargs["caption"]


async def test_print_denied_for_unauthorized_user_prints_nothing(bot):
    """An unauthorized user (not in allowlist, foreign chat) gets a refusal and
    nothing is rendered, printed or made pending."""
    msg = make_message("/print secret", user_id=999, chat_id=888)
    await bot._cmd_print(msg, CommandObject(args="secret"))

    msg.answer.assert_awaited_once()
    refusal = msg.answer.await_args.args[0]
    assert "запрещена" in refusal
    assert bot.label_maker.render.await_count == 0
    assert bot.printer.print_pdf.await_count == 0
    assert bot._pending == {}
    # No status message / preview was produced.
    msg.answer_photo.assert_not_awaited()


# ── E2E-2: /sudoprint immediate print + reprint ──────────────────────────────

async def test_sudoprint_then_reprint_prints_twice_across_journey(bot):
    """/sudoprint prints immediately + persists a 🔁-labelled photo; pressing
    that reprint button re-renders and prints a SECOND time."""
    msg = make_message("/sudoprint Этикетка")
    await bot._cmd_sudoprint(msg, CommandObject(args="Этикетка"))

    # Printed immediately (no confirmation step) and nothing left pending.
    bot.printer.print_pdf.assert_awaited_once()
    assert bot._pending == {}

    # A label was saved (id 1) and a reprint keyboard was attached to the photo.
    label = await bot.storage.get_label(1)
    assert label is not None
    assert label["text"] == "Этикетка"
    assert label["kind"] == "text"

    photo_kwargs = msg.answer_photo.call_args.kwargs
    markup = photo_kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    reprint_data = markup.inline_keyboard[0][0].callback_data
    assert reprint_data == "reprint:1"
    assert "✅ Отправлено на печать" in photo_kwargs["caption"]

    # Recover the persisted label id from the keyboard and fire the reprint.
    label_id = int(reprint_data.split(":", 1)[1])
    cb = make_callback(f"reprint:{label_id}")
    await bot._cb_reprint(cb)

    # Re-rendered from text (not glaze) and printed a SECOND time.
    bot.label_maker.render.assert_awaited()  # used for the original render and reprint
    assert bot.printer.print_pdf.await_count == 2
    bot.grist.find_glaze.assert_not_awaited()  # 'text' kind must not touch Grist
    # A confirmation message about reprinting was sent into the chat.
    cb.message.answer.assert_awaited()
    assert "отправлено на печать заново" in cb.message.answer.await_args.args[0]


# ── E2E-3: /printglaze from Grist ─────────────────────────────────────────────

async def test_printglaze_found_confirm_flow_persists_glaze_label(bot):
    """/printglaze with a Grist hit -> a pending 'glaze' job; confirming prints
    the glaze PDF and persists a label whose kind is 'glaze'."""
    fields = make_glaze_fields(GeneralName="Лазурит Синий")
    bot.grist.find_glaze = AsyncMock(return_value=fields)

    msg = make_message("/printglaze Лазурит")
    await bot._cmd_printglaze(msg, CommandObject(args="Лазурит"))

    # A preview is pending and it was rendered via the glaze maker, not the
    # plain text maker.
    assert len(bot._pending) == 1
    token = next(iter(bot._pending))
    assert bot._pending[token]["kind"] == "glaze"
    bot.glaze_maker.render_entities.assert_awaited_once()
    bot.label_maker.render.assert_not_awaited()
    assert bot.printer.print_pdf.await_count == 0

    cb = make_callback(f"print:{token}")
    await bot._cb_print(cb)

    # Printed once and persisted as a glaze label keyed by its GeneralName.
    bot.printer.print_pdf.assert_awaited_once()
    label = await bot.storage.get_label(1)
    assert label is not None
    assert label["kind"] == "glaze"
    assert label["text"] == "Лазурит Синий"


async def test_printglaze_not_found_sends_message_and_no_pending(bot):
    """When Grist returns None, /printglaze reports «не найдена», renders/prints
    nothing, and leaves no pending job."""
    bot.grist.find_glaze = AsyncMock(return_value=None)

    msg = make_message("/printglaze nonexistent")
    await bot._cmd_printglaze(msg, CommandObject(args="nonexistent"))

    # The status message was edited to the not-found notice.
    status = msg.answer.return_value
    status.edit_text.assert_awaited_once()
    assert "не найдена" in status.edit_text.await_args.args[0]

    # Nothing was rendered, printed, or queued for confirmation.
    bot.glaze_maker.render_entities.assert_not_awaited()
    bot.printer.print_pdf.assert_not_awaited()
    assert bot._pending == {}
