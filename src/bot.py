from itertools import count

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from src.label_maker import LabelMaker, LabelMakerError
from src.preview import pdf_to_png
from src.printer import Printer, PrinterError
from src.settings import settings
from src.storage import Storage

HELP_TEXT = """Asakusa Label Printer

/print <текст> — отрендерить этикетку 58×40мм и показать превью перед печатью
/print (реплаем) — напечатать текст сообщения, на которое отвечаешь
/sudoprint <текст> — напечатать сразу, без подтверждения
/start — эта справка

После /print приходит превью с кнопками «Печать» / «Отмена» — на принтер ничего
не уйдёт, пока не подтвердишь. У каждой напечатанной этикетки есть кнопка
«🔁 Перепечатать» — её можно нажать в любой момент, чтобы напечатать ещё раз."""


def _confirm_keyboard(token: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🖨 Печать", callback_data=f"print:{token}"),
        InlineKeyboardButton(text="✖ Отмена", callback_data=f"cancel:{token}"),
    ]])


def _reprint_keyboard(label_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Перепечатать", callback_data=f"reprint:{label_id}"),
    ]])


def _reply_text(message: Message) -> str:
    reply = message.reply_to_message
    if reply is None:
        return ""
    return (reply.text or reply.caption or "").strip()


class PrintBot:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

        # Use a self-hosted Bot API server instead of api.telegram.org when set
        if settings.telegram_api_server:
            session = AiohttpSession(
                api=TelegramAPIServer.from_base(settings.telegram_api_server)
            )
            self.bot = Bot(token=settings.telegram_bot_token, session=session)
        else:
            self.bot = Bot(token=settings.telegram_bot_token)
        self.dp = Dispatcher(storage=MemoryStorage())

        self.label_maker = LabelMaker()
        self.printer = Printer()

        # Previews awaiting confirmation: token -> {"pdf", "text"} (transient, in-memory)
        self._pending: dict[int, dict] = {}
        self._tokens = count(1)

        router = Router()
        router.message.register(self._cmd_start, Command("start"))
        router.message.register(self._cmd_print, Command("print"))
        router.message.register(self._cmd_sudoprint, Command("sudoprint"))
        router.callback_query.register(self._cb_print, F.data.startswith("print:"))
        router.callback_query.register(self._cb_cancel, F.data.startswith("cancel:"))
        router.callback_query.register(self._cb_reprint, F.data.startswith("reprint:"))
        self.dp.include_router(router)

    async def start(self) -> None:
        await self.bot.set_my_commands([
            BotCommand(command="print", description="Напечатать этикетку (с подтверждением)"),
            BotCommand(command="sudoprint", description="Напечатать сразу, без подтверждения"),
            BotCommand(command="start", description="Справка"),
        ])
        logger.info("Starting polling")
        await self.dp.start_polling(self.bot)

    # ── auth ──────────────────────────────────────────────────────

    @staticmethod
    def _authorized(user_id: int, chat_id: int) -> bool:
        return user_id in settings.allowed_ids or chat_id in settings.allowed_chats

    # ── rendering ─────────────────────────────────────────────────

    async def _render(self, text: str) -> tuple[bytes, bytes]:
        pdf = await self.label_maker.render(text)
        png = await pdf_to_png(pdf)
        return pdf, png

    # ── commands ──────────────────────────────────────────────────

    async def _cmd_start(self, message: Message) -> None:
        try:
            if message.from_user is None:
                return
            if self._authorized(message.from_user.id, message.chat.id):
                await message.answer(HELP_TEXT)
            else:
                await message.answer(
                    f"{HELP_TEXT}\n\n⚠️ Печать недоступна.\n"
                    f"user_id: {message.from_user.id}\nchat_id: {message.chat.id}\n"
                    "Добавьте их в ALLOWED_USER_IDS / ALLOWED_CHAT_IDS."
                )
        except Exception as e:
            logger.warning("Error in /start: {}", e)

    async def _cmd_print(self, message: Message, command: CommandObject) -> None:
        try:
            if message.from_user is None:
                return
            if not self._authorized(message.from_user.id, message.chat.id):
                logger.info("Denied /print: user={} chat={}", message.from_user.id, message.chat.id)
                await message.answer(
                    f"⛔ Печать запрещена.\nuser_id: {message.from_user.id}\nchat_id: {message.chat.id}"
                )
                return

            text = (command.args or "").strip() or _reply_text(message)
            if not text:
                await message.answer("Использование: /print <текст> или /print реплаем на сообщение")
                return

            status = await message.answer("⏳ Генерирую этикетку…")
            try:
                pdf, png = await self._render(text)
            except LabelMakerError as e:
                logger.warning("Render failed: {}", e)
                await status.edit_text(f"❌ Не удалось сгенерировать этикетку: {e}")
                return

            token = next(self._tokens)
            self._pending[token] = {"pdf": pdf, "text": text}
            await status.delete()
            await message.answer_photo(
                BufferedInputFile(png, filename="label.png"),
                caption=f"Этикетка:\n«{text}»\n\nПечатать?",
                reply_markup=_confirm_keyboard(token),
            )
        except Exception as e:
            logger.warning("Error in /print: {}", e)
            await message.answer("Произошла ошибка.")

    async def _cmd_sudoprint(self, message: Message, command: CommandObject) -> None:
        try:
            if message.from_user is None:
                return
            chat_id, user_id = message.chat.id, message.from_user.id
            if not self._authorized(user_id, chat_id):
                logger.info("Denied /sudoprint: user={} chat={}", user_id, chat_id)
                await message.answer(f"⛔ Печать запрещена.\nuser_id: {user_id}\nchat_id: {chat_id}")
                return

            text = (command.args or "").strip() or _reply_text(message)
            if not text:
                await message.answer("Использование: /sudoprint <текст> или реплаем на сообщение")
                return

            status = await message.answer("⏳ Печатаю…")
            try:
                pdf, png = await self._render(text)
                request_id = await self.printer.print_pdf(pdf)
            except LabelMakerError as e:
                logger.warning("Render failed: {}", e)
                await status.edit_text(f"❌ Не удалось сгенерировать этикетку: {e}")
                return
            except PrinterError as e:
                logger.warning("Print failed: {}", e)
                await status.edit_text(f"❌ Ошибка печати: {e}")
                return

            label_id = await self.storage.add_label(text, chat_id, user_id)
            await status.delete()
            suffix = f" (задание {request_id})" if request_id else ""
            await message.answer_photo(
                BufferedInputFile(png, filename="label.png"),
                caption=f"«{text}»\n\n✅ Отправлено на печать{suffix}",
                reply_markup=_reprint_keyboard(label_id),
            )
        except Exception as e:
            logger.warning("Error in /sudoprint: {}", e)
            await message.answer("Произошла ошибка.")

    # ── callbacks ─────────────────────────────────────────────────

    async def _cb_print(self, callback: CallbackQuery) -> None:
        try:
            if not self._cb_authorized(callback):
                await callback.answer("Печать запрещена", show_alert=True)
                return
            token = int(callback.data.split(":", 1)[1])
            job = self._pending.pop(token, None)
            if job is None:
                await callback.answer("Задание устарело, отправьте /print заново")
                await self._clear_markup(callback)
                return

            await callback.answer("Отправляю на принтер…")
            try:
                request_id = await self.printer.print_pdf(job["pdf"])
            except PrinterError as e:
                logger.warning("Print failed: {}", e)
                await self._set_caption(callback, f"«{job['text']}»\n\n❌ Ошибка печати: {e}", None)
                return

            chat_id = callback.message.chat.id if callback.message else 0
            label_id = await self.storage.add_label(job["text"], chat_id, callback.from_user.id)
            suffix = f" (задание {request_id})" if request_id else ""
            await self._set_caption(
                callback,
                f"«{job['text']}»\n\n✅ Отправлено на печать{suffix}",
                _reprint_keyboard(label_id),
            )
        except Exception as e:
            logger.warning("Error in print callback: {}", e)
            await callback.answer("Ошибка")

    async def _cb_cancel(self, callback: CallbackQuery) -> None:
        try:
            token = int(callback.data.split(":", 1)[1])
            job = self._pending.pop(token, None)
            text = job["text"] if job else ""
            await self._set_caption(callback, f"«{text}»\n\n✖ Отменено", None)
            await callback.answer()
        except Exception as e:
            logger.warning("Error in cancel callback: {}", e)
            await callback.answer("Ошибка")

    async def _cb_reprint(self, callback: CallbackQuery) -> None:
        try:
            if not self._cb_authorized(callback):
                await callback.answer("Печать запрещена", show_alert=True)
                return
            label_id = int(callback.data.split(":", 1)[1])
            label = await self.storage.get_label(label_id)
            if label is None:
                await callback.answer("Этикетка не найдена", show_alert=True)
                return

            await callback.answer("⏳ Перепечатываю…")
            text = label["text"]
            try:
                pdf = await self.label_maker.render(text)
                request_id = await self.printer.print_pdf(pdf)
            except (LabelMakerError, PrinterError) as e:
                logger.warning("Reprint failed: {}", e)
                if callback.message:
                    await callback.message.answer(f"❌ Не удалось перепечатать «{text}»: {e}")
                return

            suffix = f" (задание {request_id})" if request_id else ""
            if callback.message:
                await callback.message.answer(f"🔁 «{text}» — отправлено на печать заново{suffix}")
        except Exception as e:
            logger.warning("Error in reprint callback: {}", e)
            await callback.answer("Ошибка")

    # ── helpers ───────────────────────────────────────────────────

    def _cb_authorized(self, callback: CallbackQuery) -> bool:
        if callback.from_user is None:
            return False
        chat_id = callback.message.chat.id if callback.message else callback.from_user.id
        return self._authorized(callback.from_user.id, chat_id)

    @staticmethod
    async def _set_caption(callback: CallbackQuery, caption: str, markup) -> None:
        if callback.message is not None:
            await callback.message.edit_caption(caption=caption, reply_markup=markup)

    @staticmethod
    async def _clear_markup(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
