import asyncio

from loguru import logger

from src.settings import settings


class PrinterError(Exception):
    """Raised when the print job could not be submitted."""


class Printer:
    """Submits PDF jobs to a shared CUPS printer via the `lp` client over IPP.

    Equivalent to:
        lp -h <cups_host> -d <printer> -o media=<media> -o fit-to-page=true -
    The PDF is piped to stdin; CUPS on the server runs its own filter chain.
    """

    def __init__(self) -> None:
        self.host = settings.cups_host
        self.printer = settings.printer_name
        self.media = settings.printer_media
        self.fit_to_page = settings.printer_fit_to_page

    def _build_args(self, title: str) -> list[str]:
        args = ["lp", "-h", self.host, "-d", self.printer, "-t", title]
        if self.media:
            args += ["-o", f"media={self.media}"]
        if self.fit_to_page:
            args += ["-o", "fit-to-page=true"]
        return args

    async def print_pdf(self, pdf: bytes, title: str = "asakusa-print") -> str:
        """Send the PDF to the printer. Returns the CUPS request id (e.g. XP-365B-42)."""
        args = self._build_args(title)
        logger.info("Submitting print job: {}", " ".join(args))
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise PrinterError("`lp` not found — install the cups-client package") from e

        stdout, stderr = await proc.communicate(input=pdf)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip() or "unknown lp error"
            raise PrinterError(f"lp failed ({proc.returncode}): {err}")

        # lp prints e.g. "request id is XP-365B-42 (1 file(s))"
        out = stdout.decode(errors="replace").strip()
        request_id = out.split("request id is", 1)[-1].split("(", 1)[0].strip() if out else ""
        logger.info("Print job accepted: {}", request_id or out)
        return request_id or out
