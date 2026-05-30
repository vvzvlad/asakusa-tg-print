import json
from pathlib import Path

import httpx
from loguru import logger

from src.settings import settings


class LabelMakerError(Exception):
    """Raised when the Label Maker service fails to produce a PDF."""


class LabelMaker:
    """HTTP client for the Label Maker service (POST /api/generate-pdf).

    Loads the label template once at startup and substitutes the user's text
    into the %E1% placeholder via the `rows` entities, mirroring the web UI.
    """

    def __init__(self) -> None:
        self.base_url = settings.label_maker_url.rstrip("/")
        self.timeout = settings.label_maker_timeout
        self.rotate = settings.label_rotate
        self.template = self._load_template(settings.template_path)

    @staticmethod
    def _load_template(path: str) -> dict:
        tpl = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(tpl.get("nodes"), list):
            raise LabelMakerError(f"Invalid template (no nodes array): {path}")
        logger.info(
            "Loaded label template {} ({}x{}mm, {} nodes)",
            path, tpl.get("widthMm"), tpl.get("heightMm"), len(tpl["nodes"]),
        )
        return tpl

    async def render(self, text: str) -> bytes:
        """Render the template with `text` in %E1% and return PDF bytes."""
        payload = {
            "template": self.template,
            "rows": [{"entities": [text]}],
            "rotate": self.rotate,
        }
        url = f"{self.base_url}/api/generate-pdf"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
        except httpx.HTTPError as e:
            raise LabelMakerError(f"Label Maker unreachable: {e}") from e

        if resp.status_code != 200:
            detail = resp.text[:200]
            raise LabelMakerError(f"Label Maker returned {resp.status_code}: {detail}")

        pdf = resp.content
        if not pdf.startswith(b"%PDF"):
            raise LabelMakerError("Label Maker did not return a PDF")
        logger.info("Rendered label PDF ({} bytes)", len(pdf))
        return pdf
