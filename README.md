# asakusa-tg-print

Telegram bot (aiogram v3) that prints **58×40mm labels** on a shared **Xprinter
XP-365B**. It glues together three systems and owns almost no rendering/printing
logic itself:

- **Label Maker** — separate HTTP service that turns a JSON template + row data into
  a PDF (the bot POSTs to `{LABEL_MAKER_URL}/api/generate-pdf`).
- **CUPS print server** — the bot pipes the PDF to the `lp` client over IPP.
- **Grist** — REST API, source of glaze data for `/printglaze`.

## Commands

| Command | What it does |
|---|---|
| `/print <text>` | Render a label, show a preview, print on confirmation. Works as a reply too. |
| `/sudoprint <text>` | Render and print immediately, no confirmation. |
| `/printglaze <name>` | Look the glaze up in Grist by `GeneralName`, render and print it. |
| `/start` | Help. |

Every printed label gets a «🔁 Перепечатать» button — the text is stored (not the
PDF) and re-rendered on demand.

## Quick start (local)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # fill in real values
.venv/bin/python main.py      # requires Label Maker + CUPS reachable
```

Run the tests:

```bash
.venv/bin/pytest
```

## Configuration

All config is environment variables (loaded from `.env` via `pydantic-settings`).
**Credentials and own-service addresses have no defaults — a missing one makes the
bot fail on startup.** See [`.env.example`](.env.example) for the full list; the
required ones are:

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `LABEL_MAKER_URL` | Label Maker service base URL |
| `CUPS_HOST` | CUPS print server host |
| `GRIST_BASE_URL`, `GRIST_DOC_ID`, `GRIST_API_KEY` | Grist source for `/printglaze` |
| `GLAZE_SITE_URL` | Public glaze site URL printed into labels |

Authorization: `ALLOWED_USER_IDS` / `ALLOWED_CHAT_IDS` (comma-separated). Empty =
nobody can print. A trusted chat means everyone in it may print.

## Deploy

CI lives in `.gitea/workflows/`. On a push to `main`,
[`image-check-publish.yml`](.gitea/workflows/image-check-publish.yml) runs the tests,
builds the image, smoke-tests the built image with [`ci/smoke.py`](ci/smoke.py), and
only then pushes `gitea.vvzvlad.xyz/projects/asakusa-tg-print` as `:<sha>` and
`:latest`, in that order. Each stage gates the next, so red tests or a red smoke mean
nothing reaches the registry. Pull requests run
[`tests.yml`](.gitea/workflows/tests.yml), which runs the same tests, the same build
and the same gate but never logs in and never pushes.

Production runs the prebuilt image via [`docker-compose.yml`](docker-compose.yml) with
a volume on `/app/data` and follows `:latest`; Portainer's own image updater redeploys
it, which is what the `io.portainer.update.enable` label turns on (there is no
watchtower on this stack). The bot uses long polling (no inbound port).

See [`AGENTS.md`](AGENTS.md) for contributor/agent conventions and
[`CLAUDE.md`](CLAUDE.md) for architecture details.
