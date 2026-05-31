"""Tests for src/preview.py — PDF-to-PNG rasterization.

Covers the synchronous `_render` worker and the async `pdf_to_png` wrapper
that delegates to it via `asyncio.to_thread`. The `pdf_bytes` fixture (a real
one-page PDF) is used for the happy paths; deliberately malformed bytes drive
the error paths. Per the test-strategy report, DPI-range validation is NOT
tested (no such feature exists in the code).
"""

import pytest

from src.preview import _render, pdf_to_png

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# ── _render (synchronous worker) ─────────────────────────────────────────────

def test_render_valid_pdf_returns_png_bytes(pdf_bytes):
    """A valid one-page PDF rasterizes to non-empty PNG-signed bytes."""
    out = _render(pdf_bytes, 150)
    assert isinstance(out, bytes)
    assert out.startswith(PNG_SIGNATURE)
    assert len(out) > len(PNG_SIGNATURE)


def test_render_invalid_bytes_raises(pdf_bytes):
    """Non-PDF input cannot be opened by pymupdf and propagates an error."""
    with pytest.raises(Exception):
        _render(b"not a pdf", 150)


def test_render_empty_bytes_raises():
    """Empty input is not a valid PDF stream and raises."""
    with pytest.raises(Exception):
        _render(b"", 150)


# ── pdf_to_png (async wrapper over asyncio.to_thread) ────────────────────────

async def test_pdf_to_png_valid_pdf_returns_png(pdf_bytes):
    """The async wrapper returns PNG bytes for a valid PDF (default dpi)."""
    out = await pdf_to_png(pdf_bytes)
    assert isinstance(out, bytes)
    assert out.startswith(PNG_SIGNATURE)


async def test_pdf_to_png_garbage_propagates_exception():
    """An error raised inside the worker thread propagates out of the await."""
    with pytest.raises(Exception):
        await pdf_to_png(b"garbage")
