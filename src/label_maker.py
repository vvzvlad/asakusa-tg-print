import json
from pathlib import Path

import httpx
from loguru import logger

from src.settings import settings

# Templates are static code assets shipped in templates/ (NOT data/, which is a
# runtime volume that would shadow them). Paths are fixed, not configurable.
TEMPLATE_PATH = "templates/label_template.json"
GLAZE_TEMPLATE_PATH = "templates/label_template_glaze.json"


class LabelMakerError(Exception):
    """Raised when the Label Maker service fails to produce a PDF."""


class LabelMaker:
    """HTTP client for the Label Maker service (POST /api/generate-pdf).

    Loads the label template once at startup and substitutes the user's text
    into the %E1% placeholder via the `rows` entities, mirroring the web UI.
    """

    def __init__(self, template_path: str = TEMPLATE_PATH, lazy: bool = False) -> None:
        self.base_url = settings.label_maker_url.rstrip("/")
        self.timeout = settings.label_maker_timeout
        self.rotate = settings.label_rotate
        self.template_path = template_path
        # Lazy makers (e.g. the optional glaze template) defer loading so a
        # missing file doesn't crash startup — it only fails on first render.
        self._template: dict | None = None
        if not lazy:
            self._template = self._load_template(self.template_path)

    @property
    def template(self) -> dict:
        if self._template is None:
            self._template = self._load_template(self.template_path)
        return self._template

    @staticmethod
    def _load_template(path: str) -> dict:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise LabelMakerError(f"Cannot read template {path}: {e}") from e
        tpl = json.loads(raw)
        if not isinstance(tpl.get("nodes"), list):
            raise LabelMakerError(f"Invalid template (no nodes array): {path}")
        logger.info(
            "Loaded label template {} ({}x{}mm, {} nodes)",
            path, tpl.get("widthMm"), tpl.get("heightMm"), len(tpl["nodes"]),
        )
        return tpl

    async def render(self, text: str) -> bytes:
        """Render the template with `text` in %E1% and return PDF bytes."""
        return await self.render_entities([text])

    async def render_entities(self, entities: list[str]) -> bytes:
        """Render the template, substituting entities positionally into %E1%, %E2%, …"""
        payload = {
            "template": self.template,
            "rows": [{"entities": entities}],
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
