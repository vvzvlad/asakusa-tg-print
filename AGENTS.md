# Agent Instructions — asakusa-tg-print

Telegram bot (aiogram v3) that prints 58×40mm labels on a shared Xprinter XP-365B. It is glue
between three external systems and owns almost no rendering or printing logic:

1. **Label Maker** — a separate HTTP service (sibling project `label_maker`) that renders a JSON
   template plus row data into a PDF. The bot POSTs to `{LABEL_MAKER_URL}/api/generate-pdf`; the
   service must run with `ENABLE_PDF_API=1`.
2. **CUPS** — the bot shells out to `lp -h <CUPS_HOST> -d <PRINTER_NAME>` with the PDF on stdin.
   The print server setup is in the «XP-365B printserver» guide (wiki `gitmost-vvzvlad`, space
   Netmap / «Инфра», «Руководства»).
3. **Grist** — glaze data for `/printglaze`, via the shared `grist-client` package.

## Layout
- `main.py` — entry point: open the store → build `PrintBot` → start long polling.
- `src/` — `settings`, `bot`, `label_maker`, `grist`, `printer`, `preview`, `storage`.
- `templates/` — static label templates, shipped inside the image (never in `data/`).
- `data/` — runtime state (SQLite label store); gitignored, a docker volume in production.
- `tests/` — pytest suite; `conftest.py` holds fixtures and aiogram stub factories and injects
  the required settings, so the suite needs no `.env`.
- `ci/smoke.py` — smoke gate run against the built image in CI.

## Run, test, deploy
Tests and the image are built in CI (`.gitea/workflows/`), not on the laptop:
- `tests.yml` (pull requests): pytest in `python:3.12-slim`, image build, `ci/smoke.py`. No push.
- `image-check-publish.yml` (push to `main`): the same, then pushes `:<sha>` and `:latest` to
  `gitea.vvzvlad.xyz/projects/asakusa-tg-print`.

```bash
pip install -r requirements-dev.txt
pytest
python main.py                    # reads .env; needs Label Maker and CUPS reachable
SMOKE_IMAGE=asakusa-tg-print SMOKE_NAME=local-smoke python3 ci/smoke.py   # needs a built image
```

Production runs `docker-compose.yml` with a volume on `/app/data`. Portainer's image updater
(`io.portainer.update.enable` label) redeploys on a new `:latest`; there is no watchtower and its
label must not be added. Long polling, no inbound port.

## Architecture
Print pipeline, which every command funnels into: `LabelMaker.render()` (text into `%E1%` of the
template) → PDF → `pdf_to_png()` (PyMuPDF preview of page 1) → inline confirm →
`Printer.print_pdf()` → `Storage.add_label()` → a «🔁 Перепечатать» button.

- `/print <text>` (or as a reply, using the replied message's text): preview → confirm → print.
- `/sudoprint <text>`: render and print without confirmation.
- `/printglaze <name>`: `GristClient.find_glaze()` looks up `Glazes` by `GeneralName` (exact
  case-insensitive, else substring), resolves `Maker` into `Makerlink` from `GlazeMakers`, then
  `glaze_entities()` (`src/grist.py`) maps the row into `%E1%..%E8%` of the glaze template.

Reprint (`src/storage.py`): the `labels` table stores `(text, chat_id, user_id, kind,
created_at)`, never the PDF; reprint re-renders. A `glaze` row re-queries Grist by name, so it
reflects current data. The same database holds `chat_migrations` (old chat id → new chat id).

Authorization (`_authorized`): user in `ALLOWED_USER_IDS`, or chat in `ALLOWED_CHAT_IDS`, or chat
is the migration target of an allowed chat (removing the old id revokes the new one). Everyone in
a trusted chat may print, including reprinting others' labels — intentional. Empty lists mean
nobody. For callbacks on messages older than 48h (`InaccessibleMessage`) `_cb_authorized` falls
back to the user allowlist only.

## Conventions and invariants
- Config comes only from env / `.env` via `src/settings.py`; `.env.example` lists every variable.
- Credentials and addresses of our own services have no defaults, so `Settings()` fails at
  startup when one is missing: `TELEGRAM_BOT_TOKEN`, `LABEL_MAKER_URL`, `CUPS_HOST`,
  `GRIST_BASE_URL`, `GRIST_DOC_ID`, `GRIST_API_KEY`, `GLAZE_SITE_URL`. Defaults are allowed only
  for non-secret device/behaviour settings. No example credentials in code, not even commented.
- Template paths are constants in `src/label_maker.py` and deliberately not configurable:
  `data/` is the runtime volume and would shadow them.
- `LABEL_ROTATE=false` gives the landscape PDF that direct printing to CUPS expects.
- Telegram limits in `src/bot.py`: `MAX_LABEL_CHARS` (500), `_short()` for echoed text (caption
  limit 1024), `MAX_PENDING` (50 pending previews, oldest evicted).
- `glaze_entities()` hardcodes Russian Grist values (`"Глянцевая"`, `"Пищевая"`) and the
  `$SITE/glaze/...` URL scheme; changing the glaze template's placeholders means changing it too.
- The image needs `lp` (the Dockerfile installs `cups-client`).
- Code comments in English. New code comes with tests.

## Limits for agents
- Tests: at most three times the code (`wc -l` of `tests/` against the code). Over the limit,
  still write the test you need, and in the same change go through the old tests: drop or merge
  duplicates and tests of implementation rather than behaviour.
- Comments explain the code as it is and why it is built this way. History ("used to", "after
  the review", "since the incident") goes into the commit message.
- This file stays under ~150 lines: only what an agent would get wrong without it. No decision
  history, incident logs or retelling of the code.
