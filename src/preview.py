import asyncio

import pymupdf


def _render(pdf: bytes, dpi: int) -> bytes:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        page = doc[0]
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")
    finally:
        doc.close()


async def pdf_to_png(pdf: bytes, dpi: int = 300) -> bytes:
    """Rasterize the first PDF page to PNG bytes for an in-chat preview."""
    return await asyncio.to_thread(_render, pdf, dpi)
