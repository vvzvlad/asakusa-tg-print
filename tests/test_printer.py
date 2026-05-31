"""Tests for src/printer.py — `Printer._build_args` (unit) and `print_pdf` (integration).

The `lp` subprocess boundary is replaced by monkeypatching
`asyncio.create_subprocess_exec` with a fake that yields a proc whose
`.communicate` is an AsyncMock and which exposes a `.returncode` attribute.
No real `lp` binary or CUPS server is touched.
"""

from unittest.mock import AsyncMock

import pytest

from src.printer import Printer, PrinterError


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_printer(*, host="cups.local", printer="XP-365B", media="", fit_to_page=False) -> Printer:
    """Build a Printer and override its instance attrs to control _build_args output."""
    p = Printer()
    p.host = host
    p.printer = printer
    p.media = media
    p.fit_to_page = fit_to_page
    return p


def _fake_subprocess_exec(*, stdout=b"", stderr=b"", returncode=0):
    """Return an async function suitable for monkeypatching create_subprocess_exec.

    The fake proc's `.communicate` accepts the `input=` kwarg and returns
    `(stdout, stderr)`; `.returncode` is set as an attribute.
    """
    captured = {}

    async def fake(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        proc = AsyncMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        captured["proc"] = proc
        return proc

    fake.captured = captured
    return fake


# ── _build_args unit tests ───────────────────────────────────────────────────

def test_build_args_no_media_no_fit():
    """media="" and fit_to_page=False -> just the base lp invocation."""
    p = _make_printer(host="h1", printer="P1", media="", fit_to_page=False)
    assert p._build_args("my-title") == ["lp", "-h", "h1", "-d", "P1", "-t", "my-title"]


def test_build_args_with_media_appends_media_option():
    """A set media appends `-o media=<media>`."""
    p = _make_printer(host="h1", printer="P1", media="Custom.165x114pt", fit_to_page=False)
    assert p._build_args("t") == [
        "lp", "-h", "h1", "-d", "P1", "-t", "t",
        "-o", "media=Custom.165x114pt",
    ]


def test_build_args_with_fit_to_page_appends_fit_option():
    """fit_to_page=True appends `-o fit-to-page=true`."""
    p = _make_printer(host="h1", printer="P1", media="", fit_to_page=True)
    assert p._build_args("t") == [
        "lp", "-h", "h1", "-d", "P1", "-t", "t",
        "-o", "fit-to-page=true",
    ]


def test_build_args_media_then_fit_to_page_order():
    """When both are set, the media option precedes the fit-to-page option."""
    p = _make_printer(host="h1", printer="P1", media="A4", fit_to_page=True)
    assert p._build_args("t") == [
        "lp", "-h", "h1", "-d", "P1", "-t", "t",
        "-o", "media=A4",
        "-o", "fit-to-page=true",
    ]


# ── print_pdf integration tests ──────────────────────────────────────────────

async def test_print_pdf_parses_request_id(monkeypatch):
    """returncode 0 with a normal lp message -> just the request id is returned."""
    fake = _fake_subprocess_exec(
        stdout=b"request id is XP-365B-42 (1 file(s))",
        returncode=0,
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)

    p = _make_printer()
    result = await p.print_pdf(b"%PDF-1.4 fake", title="job")
    assert result == "XP-365B-42"


async def test_print_pdf_empty_stdout_returns_empty_string(monkeypatch):
    """Empty stdout -> empty string."""
    fake = _fake_subprocess_exec(stdout=b"", returncode=0)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)

    p = _make_printer()
    result = await p.print_pdf(b"%PDF-1.4 fake")
    assert result == ""


async def test_print_pdf_unexpected_output_returns_stripped_text(monkeypatch):
    """stdout without the 'request id is' prefix -> the stripped raw text."""
    fake = _fake_subprocess_exec(stdout=b"  weird output  ", returncode=0)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)

    p = _make_printer()
    result = await p.print_pdf(b"%PDF-1.4 fake")
    assert result == "weird output"


async def test_print_pdf_nonzero_returncode_raises_with_stderr(monkeypatch):
    """returncode 1 with stderr -> PrinterError carrying the stderr text."""
    fake = _fake_subprocess_exec(stderr=b"boom", returncode=1)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)

    p = _make_printer()
    with pytest.raises(PrinterError) as exc:
        await p.print_pdf(b"%PDF-1.4 fake")
    assert "boom" in str(exc.value)


async def test_print_pdf_missing_lp_binary_raises(monkeypatch):
    """create_subprocess_exec raising FileNotFoundError -> PrinterError."""
    async def fake(*args, **kwargs):
        raise FileNotFoundError("lp")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)

    p = _make_printer()
    with pytest.raises(PrinterError) as exc:
        await p.print_pdf(b"%PDF-1.4 fake")
    assert "lp" in str(exc.value)


async def test_print_pdf_invalid_utf8_stderr_does_not_crash(monkeypatch):
    """Invalid-UTF8 stderr on failure is decoded with errors='replace' — still PrinterError."""
    fake = _fake_subprocess_exec(stderr=b"\xff\xfe", returncode=1)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)

    p = _make_printer()
    with pytest.raises(PrinterError):
        await p.print_pdf(b"%PDF-1.4 fake")


async def test_print_pdf_pipes_pdf_to_stdin(monkeypatch):
    """The PDF bytes are passed to communicate(input=...) and args come from _build_args."""
    fake = _fake_subprocess_exec(
        stdout=b"request id is XP-365B-7 (1 file(s))",
        returncode=0,
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)

    p = _make_printer(host="h9", printer="P9", media="A4", fit_to_page=True)
    pdf = b"%PDF-1.4 payload"
    result = await p.print_pdf(pdf, title="mine")

    assert result == "XP-365B-7"
    # the exec args are exactly the _build_args output
    assert list(fake.captured["args"]) == [
        "lp", "-h", "h9", "-d", "P9", "-t", "mine",
        "-o", "media=A4",
        "-o", "fit-to-page=true",
    ]
    # the PDF bytes are piped to lp via communicate(input=...)
    fake.captured["proc"].communicate.assert_awaited_once_with(input=pdf)
