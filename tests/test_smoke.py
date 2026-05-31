"""Smoke test: verifies the conftest foundation works end-to-end.

This is intentionally minimal — it exercises the riskiest fixture mechanics
(stub isinstance behaviour, async spies, the bot fixture) so that the per-module
test suites can rely on them. Safe to keep as a sanity check.
"""

from aiogram.filters import CommandObject
from aiogram.types import Message

from tests.conftest import make_callback, make_message


def test_settings_imports():
    from src.settings import settings
    assert settings.telegram_bot_token  # dummy token injected by conftest


def test_message_stub_is_message_instance():
    msg = make_message("hi", user_id=5, chat_id=7)
    assert isinstance(msg, Message)
    assert msg.from_user.id == 5
    assert msg.chat.id == 7


def test_inaccessible_callback_message_is_not_a_message():
    cb = make_callback("print:1", inaccessible=True)
    # the >48h fallback in _cb_authorized relies on this being False
    assert not isinstance(cb.message, Message)


async def test_message_answer_is_async_spy():
    msg = make_message()
    status = await msg.answer("hello")
    await status.edit_text("edited")
    await status.delete()
    msg.answer.assert_awaited_once_with("hello")
    status.edit_text.assert_awaited_once_with("edited")


async def test_storage_roundtrip(storage):
    label_id = await storage.add_label("hello", chat_id=1, user_id=2)
    row = await storage.get_label(label_id)
    assert row["text"] == "hello"
    assert row["kind"] == "text"


async def test_bot_cmd_print_happy_path(bot):
    msg = make_message("Test label", user_id=111, chat_id=222)
    await bot._cmd_print(msg, CommandObject(command="print", args="Test label"))
    # a pending preview was created and a photo with a confirm keyboard sent
    assert len(bot._pending) == 1
    msg.answer_photo.assert_awaited_once()
    bot.label_maker.render.assert_awaited_once_with("Test label")
