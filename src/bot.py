from itertools import count

from aiogram import Bot, Dispatcher, F, Router
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

HELP_TEXT = """Asakusa Label Printer

/print <text> — render a 58×40mm label and preview it before printing
/start — show this help

After /print you get a preview with «Print» / «Cancel» buttons; nothing is
sent to the printer until you confirm."""


def _confirm_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🖨 Печать", callback_data=f"print:{job_id}"),
        InlineKeyboardButton(text="✖ Отмена", callback_data=f"cancel:{job_id}"),
    ]])


class PrintBot:
    def __init__(self) -> None:
        bot_kwargs = {"token": settings.telegram_bot_token}
        if settings.telegram_api_server:
            bot_kwargs["base_url"] = settings.telegram_api_server
        self.bot = Bot(**bot_kwargs)
        self.dp = Dispatcher(storage=MemoryStorage())

        self.label_maker = LabelMaker()
        self.printer = Printer()

        # Pending jobs awaiting confirmation: job_id -> {"pdf", "text", "user_id"}
        self._jobs: dict[int, dict] = {}
        self._job_ids = count(1)

        router = Router()
        router.message.register(self._cmd_start, Command("start"))
        router.message.register(self._cmd_print, Command("print"))
        router.callback_query.register(self._cb_print, F.data.startswith("print:"))
        router.callback_query.register(self._cb_cancel, F.data.startswith("cancel:"))
        self.dp.include_router(router)

    async def start(self) -> None:
        await self.bot.set_my_commands([
            BotCommand(command="print", description="Print a text label"),
            BotCommand(command="start", description="Show help"),
        ])
        logger.info("Starting polling")
        await self.dp.start_polling(self.bot)

    def _authorized(self, user_id: int) -> bool:
        return user_id in settings.allowed_ids

    async def _cmd_start(self, message: Message) -> None:
        try:
            if message.from_user is None:
                return
            user_id = message.from_user.id
            if self._authorized(user_id):
                await message.answer(HELP_TEXT)
            else:
                await message.answer(
                    f"{HELP_TEXT}\n\n⚠️ Ваш ID {user_id} не в списке разрешённых. "
                    "Попросите администратора добавить его в ALLOWED_USER_IDS."
                )
        except Exception as e:
            logger.warning("Error in /start: {}", e)

    async def _cmd_print(self, message: Message, command: CommandObject) -> None:
        try:
            if message.from_user is None:
                return
            user_id = message.from_user.id
            if not self._authorized(user_id):
                logger.info("Denied /print for unauthorized user {}", user_id)
                await message.answer(f"⛔ Печать запрещена. Ваш ID: {user_id}")
                return

            text = (command.args or "").strip()
            if not text:
                await message.answer("Использование: /print <текст этикетки>")
                return

            status = await message.answer("⏳ Генерирую этикетку…")
            try:
                pdf = await self.label_maker.render(text)
                png = await pdf_to_png(pdf)
            except LabelMakerError as e:
                logger.warning("Render failed: {}", e)
                await status.edit_text(f"❌ Не удалось сгенерировать этикетку: {e}")
                return

            job_id = next(self._job_ids)
            self._jobs[job_id] = {"pdf": pdf, "text": text, "user_id": user_id}

            await status.delete()
            await message.answer_photo(
                BufferedInputFile(png, filename="label.png"),
                caption=f'Этикетка:\n«{text}»\n\nПечатать?',
                reply_markup=_confirm_keyboard(job_id),
            )
        except Exception as e:
            logger.warning("Error in /print: {}", e)
            await message.answer("Произошла ошибка.")

    async def _cb_print(self, callback: CallbackQuery) -> None:
        try:
            if callback.from_user is None:
                await callback.answer()
                return
            job_id = int(callback.data.split(":", 1)[1])
            job = self._jobs.get(job_id)
            if job is None:
                await callback.answer("Задание устарело, отправьте /print заново")
                await self._clear_markup(callback)
                return
            if job["user_id"] != callback.from_user.id:
                await callback.answer("Это не ваше задание")
                return

            await callback.answer("Отправляю на принтер…")
            try:
                request_id = await self.printer.print_pdf(job["pdf"])
            except PrinterError as e:
                logger.warning("Print failed: {}", e)
                await self._set_caption(callback, f'«{job["text"]}»\n\n❌ Ошибка печати: {e}')
                return
            finally:
                self._jobs.pop(job_id, None)

            suffix = f" (задание {request_id})" if request_id else ""
            await self._set_caption(callback, f'«{job["text"]}»\n\n✅ Отправлено на печать{suffix}')
        except Exception as e:
            logger.warning("Error in print callback: {}", e)
            await callback.answer("Ошибка")

    async def _cb_cancel(self, callback: CallbackQuery) -> None:
        try:
            if callback.from_user is None:
                await callback.answer()
                return
            job_id = int(callback.data.split(":", 1)[1])
            job = self._jobs.get(job_id)
            if job is not None and job["user_id"] != callback.from_user.id:
                await callback.answer("Это не ваше задание")
                return
            self._jobs.pop(job_id, None)
            text = job["text"] if job else ""
            await self._set_caption(callback, f'«{text}»\n\n✖ Отменено')
            await callback.answer()
        except Exception as e:
            logger.warning("Error in cancel callback: {}", e)
            await callback.answer("Ошибка")

    @staticmethod
    async def _set_caption(callback: CallbackQuery, caption: str) -> None:
        if callback.message is not None:
            await callback.message.edit_caption(caption=caption, reply_markup=None)

    @staticmethod
    async def _clear_markup(callback: CallbackQuery) -> None:
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
