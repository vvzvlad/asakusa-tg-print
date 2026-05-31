# Agent Instructions — asakusa-tg-print

Telegram bot (aiogram v3) that prints 58×40mm labels on a shared Xprinter XP-365B.
It is glue between three external systems: the **Label Maker** HTTP service (renders
the PDF), a **CUPS** print server (reached via the `lp` client over IPP), and
**Grist** (glaze data for `/printglaze`).

## Project structure
- `src/` — application code (settings, bot, label_maker, grist, printer, preview, storage)
- `main.py` — thin entry point: open store → build `PrintBot` → start long polling
- `tests/` — pytest suite (`conftest.py` holds fixtures + aiogram stub factories)
- `templates/` — static label templates (ship inside the image; **never** in `data/`)
- `data/` — runtime state (SQLite label store); gitignored, mounted as a docker volume

For deeper architecture notes see `CLAUDE.md`.

## Setup
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # then fill in real values
```

## Running tests
```bash
.venv/bin/pytest                                   # always from the venv
.venv/bin/pytest --cov=src --cov-report=term-missing
```

## Running the app
```bash
.venv/bin/python main.py        # needs label_maker + CUPS reachable
python -m py_compile main.py src/*.py   # quick syntax check
```

## Conventions
- All mutable state lives in `data/`. Nothing else holds runtime state.
- All config comes from ENV / `.env`, read via `src/settings.py` (`pydantic-settings`).
  See `.env.example` for the full variable list.
- **Credentials and the addresses of our own services have NO defaults** — if the
  env var is missing, `Settings()` fails on startup (loud, not silent). This covers
  `TELEGRAM_BOT_TOKEN`, `LABEL_MAKER_URL`, `CUPS_HOST`, `GRIST_BASE_URL`,
  `GRIST_DOC_ID`, `GRIST_API_KEY`, `GLAZE_SITE_URL`. Defaults are only allowed for
  non-secret device/behaviour config (printer name/media, log level, db path, …).
- Creds/addresses the user provides go ONLY into `.env` (never into code, never via
  inline `VAR=... python main.py`), and are read through `Settings`.
- No default/example credentials anywhere in code, not even commented out.
- Code comments in English.
- Tests are required for new code. In CI `build` depends on `test`, so red tests
  block the image build/push.

## Deploy
Production pulls `ghcr.io/vvzvlad/asakusa-tg-print:latest` (built & pushed by CI),
runs it via `docker-compose.yml` with a volume on `/app/data`; watchtower
auto-updates on new `:latest`. The bot uses long polling — no inbound port.
