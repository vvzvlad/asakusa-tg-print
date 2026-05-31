"""Shared pytest fixtures and factories for the asakusa-tg-print test suite.

IMPORTANT ordering note: a dummy TELEGRAM_BOT_TOKEN is injected into the
environment *before* any `src.*` module is imported, because importing
`src.settings` constructs `Settings()` at module load and the token field is
required (no default). os.environ takes priority over the real `.env` in
pydantic-settings, so the suite is hermetic regardless of a developer's .env.

Fixtures provided to test modules
---------------------------------
- `storage`           : a fresh in-memory `Storage` (already `init()`-ed), closed on teardown.
- `pdf_bytes`         : a real one-page PDF (valid, parseable by PyMuPDF) for preview tests.
- `bot`               : a `PrintBot` wired to an in-memory store, with its four
                        collaborators (label_maker / glaze_maker / grist / printer)
                        and `pdf_to_png` replaced by mocks. User 111 / chat 222 are
                        authorized. Each test gets a brand-new instance (no _pending
                        / _tokens leakage between tests).
- `live_settings`     : the live `src.settings.settings` object (mutate via monkeypatch).

Factories (plain functions, import from `tests.conftest`)
---------------------------------------------------------
- `make_message(text="", *, user_id=111, chat_id=222, reply_text=None, reply_caption=None)`
- `make_callback(data, *, user_id=111, message=_DEFAULT, inaccessible=False)`
- `make_glaze_fields(**overrides)`   -> a `For_print` row's `fields` dict (for glaze_entities)
- `make_glaze_record(**overrides)`   -> a full Grist record `{"id":.., "fields": {..}}`

Stub conventions
----------------
- Message/CallbackQuery stubs are `MagicMock(spec=...)`, so `isinstance(x, Message)`
  passes and async methods (`answer`, `answer_photo`, `edit_text`, `delete`,
  `edit_caption`, `edit_reply_markup`) are `AsyncMock`es.
- `message.answer(...)` returns a *status* Message stub. To assert on what the bot
  did to it (e.g. `status.edit_text(...)` / `status.delete()`), read it back via
  `message.answer.return_value`.
- The `bot` fixture's mocks return fake PDF/PNG bytes; set `.side_effect` to raise
  `LabelMakerError` / `PrinterError` / `GristError` to exercise failure branches.
"""

import os

# --- must run before importing anything under src/ ---------------------------
# Settings() runs at import of src.settings and these fields are required (no
# defaults). Inject hermetic dummies so the suite never depends on a real .env;
# setdefault → os.environ wins over .env, so values stay deterministic in CI too.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:AAFakeTestTokenNotReal_0123456789abcdef")
os.environ.setdefault("LABEL_MAKER_URL", "http://label-maker.test")
os.environ.setdefault("CUPS_HOST", "cups.test")
os.environ.setdefault("GRIST_BASE_URL", "https://grist.test")
os.environ.setdefault("GRIST_DOC_ID", "testdoc")
os.environ.setdefault("GRIST_API_KEY", "test-grist-key")
os.environ.setdefault("GLAZE_SITE_URL", "https://glaze.test")

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pymupdf  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    Message,
    User,
)

_DEFAULT = object()  # sentinel: "build a default Message stub"


# ── aiogram stub factories ───────────────────────────────────────────────────

def _status_message() -> MagicMock:
    """A Message stub as returned by `message.answer(...)` — async edit/delete spies."""
    status = MagicMock(spec=Message)
    status.edit_text = AsyncMock(return_value=status)
    status.edit_caption = AsyncMock(return_value=status)
    status.edit_reply_markup = AsyncMock(return_value=status)
    status.delete = AsyncMock(return_value=True)
    status.answer = AsyncMock(return_value=status)
    return status


def make_message(text="", *, user_id=111, chat_id=222, reply_text=None, reply_caption=None) -> MagicMock:
    """Build a stub aiogram `Message`.

    `user_id=None` simulates a service message with no `from_user`.
    Pass `reply_text`/`reply_caption` to attach a `reply_to_message`.
    """
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.caption = None

    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = chat_id

    if user_id is None:
        msg.from_user = None
    else:
        msg.from_user = MagicMock(spec=User)
        msg.from_user.id = user_id

    if reply_text is None and reply_caption is None:
        msg.reply_to_message = None
    else:
        reply = MagicMock(spec=Message)
        reply.text = reply_text
        reply.caption = reply_caption
        msg.reply_to_message = reply

    # answer(...) returns a persistent status stub so a test can assert on it
    # afterwards via `msg.answer.return_value`.
    status = _status_message()
    msg.answer = AsyncMock(return_value=status)
    msg.answer_photo = AsyncMock(return_value=_status_message())
    msg.edit_text = AsyncMock(return_value=status)
    msg.edit_caption = AsyncMock(return_value=status)
    msg.edit_reply_markup = AsyncMock(return_value=status)
    msg.delete = AsyncMock(return_value=True)
    return msg


def make_callback(data, *, user_id=111, message=_DEFAULT, inaccessible=False) -> MagicMock:
    """Build a stub aiogram `CallbackQuery`.

    - `inaccessible=True` makes `callback.message` an `InaccessibleMessage` stub
      (NOT a `Message` instance), exercising the >48h fallback in `_cb_authorized`.
    - `message=None` sets `callback.message = None`.
    - otherwise a default (or the passed) Message stub is used.
    """
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data

    if user_id is None:
        cb.from_user = None
    else:
        cb.from_user = MagicMock(spec=User)
        cb.from_user.id = user_id

    if inaccessible:
        cb.message = MagicMock(spec=InaccessibleMessage)
    elif message is None:
        cb.message = None
    elif message is _DEFAULT:
        cb.message = make_message(user_id=user_id)
    else:
        cb.message = message

    cb.answer = AsyncMock()
    return cb


# ── Grist data factories ─────────────────────────────────────────────────────

def make_glaze_fields(**overrides) -> dict:
    """A `For_print` row's `fields` dict, suitable for `glaze_entities(fields)`."""
    fields = {
        "GeneralName": "Лазурит Синий",
        "Surface": "Глянцевая",
        "Foodgrade": "Пищевая",
        "Maker": 5,
        "Makerlink": "https://example.com/maker",
        "Type": "Порошок",
        "Description": "Тестовое описание",
    }
    fields.update(overrides)
    return fields


def make_glaze_record(*, record_id=1, **field_overrides) -> dict:
    """A full Grist record: `{"id": <int>, "fields": {...}}`."""
    return {"id": record_id, "fields": make_glaze_fields(**field_overrides)}


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def live_settings():
    """The live settings singleton. Mutate fields via `monkeypatch.setattr`."""
    from src.settings import settings
    return settings


@pytest.fixture
def pdf_bytes() -> bytes:
    """A real, parseable one-page PDF (≈58×40mm in points)."""
    doc = pymupdf.open()
    doc.new_page(width=165, height=114)
    data = doc.tobytes()
    doc.close()
    return data


@pytest_asyncio.fixture
async def storage():
    """A fresh in-memory Storage, initialised and closed around each test."""
    from src.storage import Storage
    s = Storage(":memory:")
    await s.init()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def bot(storage, monkeypatch):
    """A PrintBot with mocked collaborators and an in-memory store.

    User 111 and chat 222 are authorized. `label_maker`/`glaze_maker`/`grist`/
    `printer` and the module-level `pdf_to_png` are replaced by mocks so no real
    rendering, HTTP, printing or PDF rasterization happens. Tweak return values /
    `side_effect` per test to drive happy and failure paths.
    """
    from src.settings import settings as st
    monkeypatch.setattr(st, "allowed_user_ids", "111")
    monkeypatch.setattr(st, "allowed_chat_ids", "222")

    # Avoid PyMuPDF on fake PDF bytes by stubbing the rasterizer.
    monkeypatch.setattr("src.bot.pdf_to_png", AsyncMock(return_value=b"\x89PNG\r\n\x1a\nFAKE"))

    from src.bot import PrintBot
    b = PrintBot(storage)

    b.label_maker = MagicMock()
    b.label_maker.render = AsyncMock(return_value=b"%PDF-1.4 fake")
    b.label_maker.render_entities = AsyncMock(return_value=b"%PDF-1.4 fake")

    b.glaze_maker = MagicMock()
    b.glaze_maker.render_entities = AsyncMock(return_value=b"%PDF-1.4 fake")

    b.grist = MagicMock()
    b.grist.find_glaze = AsyncMock(return_value=make_glaze_fields())

    b.printer = MagicMock()
    b.printer.print_pdf = AsyncMock(return_value="XP-365B-1")

    yield b

    await b.bot.session.close()
