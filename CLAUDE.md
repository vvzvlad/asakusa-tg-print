# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Telegram bot (aiogram v3) that prints 58×40mm labels on a shared **Xprinter XP-365B**. It is glue between three external systems — it owns almost no rendering or printing logic itself:

1. **Label Maker** — a *separate* HTTP service (sibling project `../label_maker`) that turns a JSON template + row data into a PDF via headless Chromium. The bot POSTs to `{LABEL_MAKER_URL}/api/generate-pdf`. This service must be running, with `ENABLE_PDF_API=1`.
2. **CUPS print server** — the bot shells out to the `lp` client over IPP (`lp -h <CUPS_HOST> -d <PRINTER_NAME>`, PDF piped to stdin). The server runs its own filter chain; see `../home-network/instructions/xp365b-printserver-setup.md` for how that printer/queue is configured.
3. **Grist** — REST API, source of glaze data for `/printglaze`.

## Commands

```bash
# Run (uses .env via pydantic-settings; expects label_maker + CUPS reachable)
python main.py

# Dependencies
pip install -r requirements.txt          # a .venv/ exists in the repo

# Syntax check — there is NO test suite; this is the validation used in this repo
python -m py_compile main.py src/*.py

# Docker (what .github/workflows/ghcr-check-publish.yml builds & pushes to ghcr on push to main)
docker build . -f Dockerfile -t asakusa-tg-print
```

The container needs the `lp` binary — the Dockerfile installs `cups-client`. PyMuPDF must be `>=1.24.3` (the top-level `import pymupdf` alias used in `src/preview.py` only exists from that version).

## Architecture

Entry point `main.py` opens the SQLite store, constructs `PrintBot` (`src/bot.py`), and starts long polling. `PrintBot` wires all handlers and holds the four collaborators: `LabelMaker`, a lazy glaze `LabelMaker`, `GristClient`, `Printer`.

### The print pipeline (the core flow every command funnels into)
`text → LabelMaker.render()` (HTTP POST, substitutes the text into `%E1%` of the template) `→ PDF bytes → pdf_to_png()` (PyMuPDF rasterizes page 1 for an in-chat preview) `→ Telegram inline confirm → Printer.print_pdf()` (`lp` pipes the PDF to CUPS) `→ Storage.add_label()` `→` message gets a «🔁 Перепечатать» button.

### Commands
- **`/print <text>`** (or sent as a reply — uses the replied message's text): full preview → confirm flow above.
- **`/sudoprint <text>`**: same render+print but **skips confirmation** — prints immediately.
- **`/printglaze <name>`**: `GristClient.find_glaze()` looks up the row in the `For_print` table by `GeneralName` (exact case-insensitive, else substring), resolves the `Maker` reference into a `Makerlink` from the `GlazeMakers` table, then `glaze_entities()` (`src/grist.py`) maps the row's fields into the template's `%E1%..%E8%` placeholders. Rendered with the glaze template, then the standard preview/confirm/print flow.

### Reprint persistence (`src/storage.py`)
The SQLite `labels` table stores only `(text, chat_id, user_id, kind, created_at)` — **never the PDF**. Reprint re-renders from scratch (rendering is deterministic). `kind` is `"text"` or `"glaze"`; on reprint a `glaze` row **re-queries Grist** by its stored name, so glaze reprints reflect current Grist data, not a snapshot. `init()` self-migrates older DBs that lack the `kind` column.

### Authorization
`_authorized(user_id, chat_id)` = `user_id ∈ ALLOWED_USER_IDS` **OR** `chat_id ∈ ALLOWED_CHAT_IDS`. A trusted chat means **everyone in it may print** (including reprinting others' labels) — this is intentional. Empty lists = nobody can print. Every command and the print/reprint callbacks check this. For callbacks on messages older than 48h (`InaccessibleMessage`, where the chat context is unavailable) `_cb_authorized` falls back to the per-user allowlist only.

## Key constraints & gotchas

- **Templates are static code assets in `templates/`, NOT in `data/`.** Paths are hardcoded in `src/label_maker.py` (`templates/label_template.json`, `templates/label_template_glaze.json`) and deliberately not configurable: `data/` is the runtime volume and would shadow them. `label_template.json` is large (~222KB) because it embeds a base64 logo; both templates are 58×40mm.
- **`data/` must be a Docker volume** for reprint to survive restarts — otherwise `labels.db` is lost with the container.
- **`LABEL_ROTATE=false`** produces a landscape 58×40mm PDF, which is what direct PDF printing to the CUPS server expects. The macOS-via-PostScript path (in the printserver doc) needs the rotated variant; this bot does not use it.
- **Telegram limits are guarded in `src/bot.py`**: input is capped at `MAX_LABEL_CHARS` (500), all user text echoed into captions/messages goes through `_short()` (caption limit is 1024), and the in-memory pending-preview dict is bounded by `MAX_PENDING` (50, oldest evicted).
- **Glaze field mapping** in `glaze_entities()` hardcodes Russian Grist values (`"Глянцевая"` → glossy icon, `"Пищевая"` → food-grade icon) and the `$SITE/glaze/...` URL scheme built from `GLAZE_SITE_URL`. Changing the glaze template's placeholders requires updating this function in lockstep.

## Configuration

All config is env vars loaded by `src/settings.py` (`Settings`, pydantic-settings, reads `.env`); see `.env.example` for the full set. Notable groups: Telegram (`TELEGRAM_BOT_TOKEN`, optional self-hosted `TELEGRAM_API_SERVER`), auth (`ALLOWED_USER_IDS`/`ALLOWED_CHAT_IDS`, comma-separated, chat IDs are negative), Label Maker (`LABEL_MAKER_URL`), CUPS (`CUPS_HOST`, `PRINTER_NAME`, `PRINTER_MEDIA`, `LABEL_ROTATE`), and Grist (`GRIST_BASE_URL`, `GRIST_DOC_ID`, `GRIST_API_KEY`, `GLAZE_SITE_URL`). Code comments must be in English.
