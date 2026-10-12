"""Smoke gate for the asakusa-tg-print image, run on the CI runner against an already-built
image.

The outer half is this file: `python3 ci/smoke.py` on the runner, driving `docker` against the tag
in $SMOKE_IMAGE. It checks the container as an object: what `docker inspect` says its CMD and
WORKDIR are, how the image behaves with no environment, whether a container is still alive at the
end, and what the image logs when started with its real command.

The inner half is PROBE below, fed to `docker exec -i <name> python -u -`. It checks the code
inside the image: every module imports, the third-party symbols the code calls exist, the PDF
rasteriser works, the SQLite store opens, and `PrintBot` builds with its full handler set. The job
runs inside act_runner's container while `docker` drives a daemon outside it, so nothing here may
reach a container by address. No port is published: the bot long-polls Telegram and has no
listening socket. `docker exec` propagates the probe's exit status, which lets the outer half tell
"the probe reported failures" from "the probe died before reporting".

The runner image has `python3` but no bare `python`; inside the container it is `python`. The
outer side uses only the standard library.

The pytest suite already covers the code with every collaborator mocked; this gate covers the
artefact:

* (a) the declared contract: CMD, WORKDIR, no ENTRYPOINT. WORKDIR matters because the template
  paths in src/label_maker.py and the default `labels_db_path` (`data/labels.db`) are relative.
  Outer half.
* (b) the required-variable guard fires: src/settings.py builds `Settings()` at import time and
  seven fields have no default, so a missing variable must exit with a ValidationError naming it.
  Outer half, `docker run` with no environment.
* (c) `lp` is in the image and executes. The probe runs the real `Printer.print_pdf()` against a
  CUPS address that refuses connections and requires a genuine CUPS failure: neither the "not
  found" branch nor exit 126/127 (a binary that is present but cannot be loaded). Probe.
* (d) both label templates are in the image, non-empty and valid JSON. Probe.
* (e) every `src/*` module imports, the third-party symbols exist, a real pymupdf rasterisation,
  `Storage.init()` twice, and a `PrintBot` with its eight handlers registered. Probe.
* (f) the image's real CMD gets through its own startup. Outer half.
* (g) the probe container is still alive afterwards. Outer half.

Nothing here holds a credential or reaches an external service: a syntactically valid fake bot
token, `.test` (RFC 2606) hostnames for the HTTP services, and loopback ports nothing listens on
for the two connections that are meant to fail.

* Failures leave through SystemExit, never `assert`, on both sides: asserts vanish under
  PYTHONOPTIMIZE=1.
* Every check runs before anything is reported. A check that cannot run reports FAILED, never
  skipped. The probe's report lines are merged into this gate's report.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time

# The tag to test. Required rather than defaulted: a default would let a mistyped `env:` block
# in a workflow silently gate some other image that happens to be on the daemon.
IMAGE_ENV = "SMOKE_IMAGE"
# Base name for every container this gate starts. The workflows put the run id in it, because
# the runner has a single docker daemon shared by every repository in the fleet and two
# concurrent runs must not collide on a name.
# Required for the same reasons; see the message in main() where it is enforced.
NAME_ENV = "SMOKE_NAME"

# The three containers this gate starts, by suffix on $SMOKE_NAME:
#   ""      the long-lived one the probe runs inside, started with a sleeping command because the
#           image's own command talks to Telegram.
#   -guard  the short-lived one started with no environment for check (b).
#   -cmd    the one started with the image's real command for check (f), without `--rm` because
#           check (f) reads its filesystem with `docker cp` after it has exited.
# All three are named so a container that survives a subprocess timeout (which kills only the
# docker client) can still be removed, here and by the workflow's `if: always()` step.
# Kept in step with the suffix list in both workflows' cleanup steps.
GUARD_SUFFIX = "-guard"
CMD_SUFFIX = "-cmd"

# The Dockerfile's WORKDIR and CMD, and the paths that resolve against that WORKDIR. Hardcoded
# rather than read back out of the image: these are the contract between this repo and its
# image, so a Dockerfile that quietly moves them has to go red here and be looked at, not be
# politely followed.
APP_DIR = "/app"
EXPECTED_CMD = ["python", "main.py"]
DATA_DIR = "/app/data"
DB_FILENAME = "labels.db"
# The row title check (f) reports the database under; used by check_real_command() and
# check_database_file().
DB_TARGET = "{}/{} was created inside the container".format(DATA_DIR, DB_FILENAME)

# The seven fields `Settings` declares without a default, as pydantic reports them (lowercase
# field names). Check (b) requires each one by name, so a field that acquired a default is caught.
REQUIRED_FIELDS = [
    "telegram_bot_token",
    "label_maker_url",
    "grist_base_url",
    "grist_doc_id",
    "grist_api_key",
    "glaze_site_url",
    "cups_host",
]

# The environment of the probe and real-CMD containers. Nothing can reach anything:
#   * the token is fake but syntactically valid: aiogram checks its shape in `Bot.__init__`;
#   * the three HTTP services get `.test` names (RFC 2606) and are never requested;
#   * CUPS_HOST is 127.0.0.1:1, a port nothing binds: check (c) needs an immediate connection
#     failure without DNS in the loop;
#   * TELEGRAM_BOT_API_SERVER is set (to the discard port 9) because production sets it, so
#     `PrintBot.__init__` takes the custom `TelegramAPIServer` branch production runs.
SMOKE_ENV = [
    "TELEGRAM_BOT_TOKEN=123456789:AAFakeSmokeTokenNotReal_0123456789abc",
    "TELEGRAM_BOT_API_SERVER=http://127.0.0.1:9",
    "LABEL_MAKER_URL=http://label-maker.test",
    "GRIST_BASE_URL=https://grist.test",
    "GRIST_DOC_ID=smoke-doc",
    "GRIST_API_KEY=smoke-not-a-real-key",
    "GLAZE_SITE_URL=https://glaze.test",
    "CUPS_HOST=127.0.0.1:1",
]

# The probe container runs this instead of the image's CMD, which reaches Telegram within a few
# lines; check (f) covers the real command separately. 900 s exceeds this gate's worst case, and
# the container is removed in a `finally` and again by the workflow.
IDLE_COMMAND = ["python", "-c", "import time; time.sleep(900)"]

# The first line `main.py` logs, before anything else happens. Its presence proves the settings
# parsed — i.e. all seven required variables arrived and pydantic was satisfied — and that
# loguru was reconfigured at the declared LOG_LEVEL.
STARTUP_MARKER = "Starting Asakusa Label Printer Bot"
# Logged by `Storage.init()` once the database is open and the schema is in place, and by
# `LabelMaker._load_template()` once a template has been read off disk. Both are THIS REPO'S OWN
# wording, which is what makes them safe to match on: they are the log-side twins of the two
# things check (f) is really asking about — that SQLite could be opened under the real CMD, and
# that the relative template path resolved against the real WORKDIR.
STORAGE_MARKER = "Label store ready at"
TEMPLATE_MARKER = "Loaded label template"

# The poll in check_real_command() waits for all three, not just the first: they are written by
# separate statements with a disk commit in between, so judging the last two against the log
# snapshot taken at the first produces intermittent false reds on the runner.
STARTUP_MARKERS = (STARTUP_MARKER, STORAGE_MARKER, TEMPLATE_MARKER)

# Printed by the probe on its last line. Declared twice, here and inside PROBE, and the two must
# agree; PROBE is kept a plain string (no `.format()`) so it can be pasted into a container by
# hand. A drift causes a false red, never a false green.
PROBE_MARKER = "asakusa-tg-print smoke probe ok"

# Prefix for every row the probe reported, so the merged report shows where it was decided.
PROBE_ROW_PREFIX = "[in-container] "

# Every docker call is bounded: a hung gate holds a shared runner slot until the step timeout
# kills it, and a killed step never runs its cleanup. Worst-case sum, in call order, which the
# smoke step's `timeout-minutes` in both workflows must exceed:
#   30 (inspect config)   + 30 (rm guard)      + 90 (guard run)
# + 30 (rm probe)         + 60 (probe run -d)  + 120 (docker exec: the probe)
# + 30 (inspect probe state)
# + 30 (rm cmd)           + 60 (cmd run -d)    + 45 (startup poll: 30 budget + one 15 s `logs`)
# + 30 (docker cp)        + 30 (inspect cmd state)
# + 30 (rm cmd, finally)  + 30 (rm probe, finally)
#   = 645 s, a little under 11 minutes. Both workflows allow 12.
# The probe itself is dominated by its bounded `lp` call (20 s) and takes well under a minute.
# Each container is removed by name before it starts, so a UI re-run (same run id, same
# $SMOKE_NAME) does not fail on "name already in use".
INSPECT_TIMEOUT = 30
REMOVE_TIMEOUT = 30
GUARD_TIMEOUT = 90
START_TIMEOUT = 60
PROBE_TIMEOUT = 120
COPY_TIMEOUT = 30
LOGS_TIMEOUT = 15

# Bounded in wall clock rather than attempts, since each attempt is a `docker logs` call with its
# own 15 s timeout. The markers normally arrive within a second or two.
STARTUP_BUDGET = 30
STARTUP_PAUSE = 0.5

# How much of an unexpected output reaches the log. Container logs can run to thousands of lines
# when something loops, and an unbounded dump would bury the verdict.
EXCERPT_CHARS = 4000


# The program fed to `python -u -` inside the container, kept as a string so the gate is one file.
# `-u` keeps already-printed lines when PROBE_TIMEOUT kills the probe (a piped stdout is
# block-buffered). It takes no parameters: `docker exec` inherits the SMOKE_ENV the container was
# created with.
PROBE = r'''
"""Runs INSIDE a container of the asakusa-tg-print image, fed to `python -u -` over stdin.

It is HERMETIC: not one line of it opens a socket to anything outside the container. That is a
requirement rather than a nicety, because this gate has to be runnable on a pull request, where
no credential for Telegram, Grist, the label service or the print server exists and none should.
What it therefore checks is everything about this image that can be decided without a peer: that
the code imports, that the third-party symbols it calls are really there, that the compiled PDF
rasteriser rasterises, that SQLite opens, that the bot object can be built with its full handler
set, and that the CUPS client binary is present and executes.

The single deliberate exception is the `lp` invocation, and it is not an exception at all on
inspection: it is pointed at 127.0.0.1:1, inside this container, where nothing listens. The
connection is refused locally and immediately, no name is resolved and no packet leaves. What is
being tested is that the BINARY runs, not that anything answers it.

Everything is checked before anything is reported, and a check that cannot run reports itself as
FAILED rather than being skipped. Failures leave through SystemExit, never `assert`.
"""

import asyncio
import io
import json
import os
import shutil
import tempfile

# Printed on the last line and nowhere else. This is the outer half's PROBE_MARKER, written out
# again because this text is handed to another interpreter and cannot import anything from the
# file it lives in. If you move one, move the other; the note there explains why a drift costs a
# false red rather than a false green.
PROBE_MARKER = "asakusa-tg-print smoke probe ok"

# The WORKDIR the image declares, checked here as well as in the outer half's `docker inspect`
# — and they are not the same statement. The outer half reads what the image DECLARES; this
# reads where a `docker exec` process actually LANDS, which is what makes the relative template
# path in src/label_maker.py resolve. Production depends on the same thing for its CMD.
APP_DIR = "/app"

# Every module in src/. Imported by name rather than by walking the directory, so a module that
# is silently missing from the image is a failure here instead of a shorter loop that passes.
SRC_MODULES = [
    "src.settings",
    "src.storage",
    "src.label_maker",
    "src.grist",
    "src.preview",
    "src.printer",
    "src.bot",
]

# The third-party symbols this repo actually reaches for, module by module. An import of the
# package alone proves much less than it looks: a wheel can import and still be missing the
# attribute a call site uses, which is what a major-version bump of aiogram or httpx looks like
# from in here. Every name below is taken from a real call site in src/ — grep for it before
# deleting one.
THIRD_PARTY_SYMBOLS = [
    # src/bot.py: the Bot/Dispatcher/Router trio, the magic filter, and every aiogram type the
    # handlers construct or annotate.
    ("aiogram", ["Bot", "Dispatcher", "F", "Router"]),
    ("aiogram.client.session.aiohttp", ["AiohttpSession"]),
    ("aiogram.client.telegram", ["TelegramAPIServer"]),
    ("aiogram.filters", ["Command", "CommandObject"]),
    ("aiogram.fsm.storage.memory", ["MemoryStorage"]),
    ("aiogram.types", [
        "BotCommand", "BufferedInputFile", "CallbackQuery",
        "InlineKeyboardButton", "InlineKeyboardMarkup", "Message",
    ]),
    # src/label_maker.py builds an AsyncClient and catches HTTPError.
    ("httpx", ["AsyncClient", "HTTPError"]),
    # src/grist.py wraps the shared sync client and re-exports its error.
    ("grist_client", ["GristClient", "GristError"]),
    # src/storage.py: connect() and the Row factory, plus the Connection it annotates with.
    ("aiosqlite", ["connect", "Row", "Connection"]),
    # Every module logs through the singleton.
    ("loguru", ["logger"]),
    # src/preview.py calls the module-level open(); the rest of the chain (Document.__getitem__,
    # Page.get_pixmap, Pixmap.tobytes) is exercised for real by the rasterisation check below,
    # which is the only way to find out whether the compiled half of this wheel works.
    ("pymupdf", ["open", "Document", "Page", "Pixmap"]),
]

# Where the label templates live in the image, and what src/label_maker.py names them. The
# constants there are RELATIVE, so the two checks below are complementary: the absolute paths
# say the files were copied into the image at all, and constructing a LabelMaker says the
# relative form resolves from the working directory a process actually gets.
TEMPLATE_PATHS = [
    "/app/templates/label_template.json",
    "/app/templates/label_template_glaze.json",
]

# The handler set src/bot.py registers on its router: one service-message handler for a group ->
# supergroup migration, four commands (/start, /print, /printglaze, /sudoprint) and three
# callback prefixes (print:, cancel:, reprint:). Counted from the real aiogram structures rather
# than by reading the source, so a decorator that silently stopped registering — a filter that
# raises during registration, a router that never gets included — shows up here. Per event type
# rather than as one total, so a drift says WHICH half moved.
# This is an exact match, not a lower bound: adding a handler is supposed to fail this check
# once, loudly, and be answered by updating this constant.
EXPECTED_HANDLERS = {"message": 5, "callback_query": 3}

# The distinctive half of the error src/printer.py raises when the CUPS client is missing:
#     raise PrinterError("`lp` not found — install the cups-client package")
# Matching on it is safe in a way that matching on a third party's wording would not be: this
# string is THIS REPO'S OWN, it is the one branch of print_pdf() that means "the binary is not
# in the image", and the check below is built as "the failure must NOT be this one" — the other
# branch carries lp's own message, which is CUPS's to change and is never matched here.
LP_MISSING_FRAGMENT = "install the cups-client package"

# The other failure that must NOT be counted as success, and the one the naive version of this
# check quietly passed. A binary that is on PATH but cannot actually RUN — the base image moved
# to a different libc, a layer lost a shared object, the file lost its executable bit — does not
# raise FileNotFoundError: `execve` succeeds and it is the loader that fails, printing
# `error while loading shared libraries` and exiting 127 (126 for "found but not executable").
# printer.py has no way to tell that apart from a CUPS error and folds it into the same
# `lp failed (<code>): ...` string, which is exactly the branch this check treats as proof that
# lp works. So the STATUS CODE is discriminated on, not the message: `create_subprocess_exec`
# runs the binary directly with no shell in between, so 126 and 127 cannot come from anywhere but
# the loader — CUPS's own lp exits 1 on its errors. The matched prefix is printer.py's own
# formatting, not CUPS's wording.
LP_LOADER_FRAGMENTS = ("lp failed (126", "lp failed (127")

# Bounds the real `lp` invocation. The connection it attempts is to 127.0.0.1:1 inside this
# container, which is refused instantly, so this is not a timeout that is expected to be
# approached — it is there so that an `lp` which somehow blocks cannot hang the probe and, with
# it, the whole gate.
LP_TIMEOUT = 20

# The PNG signature, so the rasterisation check looks at what came back rather than merely at
# whether the call returned.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def describe(error):
    return "{}: {}".format(type(error).__name__, error)


def one_line(text):
    """Fold text onto a single line for the report.

    The report's contract is one row per line — the outer half parses these lines back into its
    own report by matching the `ok `/`FAIL ` prefix — and a reason is free to be multi-line:
    `describe()` on a pydantic ValidationError prints a paragraph per field, and an lp error can
    carry an embedded newline of its own. Without this fold every line after the first fails to
    match that prefix on the way out and is dropped, so the reason arrives truncated at exactly
    the point where it started being useful.
    Nothing is actually lost by folding: the outer half prints this program's raw, unfolded
    output as a transcript above its report, so the original shape is still one screen away.
    """
    return " ".join(text.split())


def minimal_pdf():
    """A one-page PDF, assembled here byte by byte, with a correct cross-reference table.

    Written out by hand rather than produced with pymupdf's own writer, and that is the point of
    the exercise: the check this feeds is about the RENDERING half of a compiled extension, and a
    document produced by the same library would let a writer and a parser agree with each other
    while neither actually works. 165x114 points is the label's real page box (58x40 mm), so this
    is the shape the service rasterises in production.

    The page carries a real content stream that DRAWS TEXT with a Base-14 font, and that is not
    decoration either. An empty page rasterises to a uniform white bitmap through a code path
    that never touches font loading, glyph lookup or the text renderer — and every label this
    service prints is text. So the page says something, and the check that consumes this requires
    the resulting raster to be something other than blank.
    """
    # Base-14: /Helvetica needs no embedded font file, so this stays a self-contained document
    # while still forcing MuPDF down its font machinery.
    content = b"BT /F1 12 Tf 10 50 Td (smoke) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 165 114] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number)
        out.write(body)
        out.write(b"\nendobj\n")
    xref_at = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objects) + 1))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1))
    out.write(b"startxref\n%d\n%%%%EOF\n" % xref_at)
    return out.getvalue()


def count_handlers(dispatcher):
    """Handlers registered under a Dispatcher, per event type, as aiogram really stores them.

    Walks the ROUTERS the dispatcher includes and counts each observer's `handlers` list. The
    Dispatcher's own observers are deliberately excluded: aiogram registers one built-in handler
    on the dispatcher's `update` observer in its constructor, so counting from the dispatcher
    itself yields nine where eight were registered, and the extra one belongs to the framework
    rather than to this repo.
    """
    routers = []
    pending = list(dispatcher.sub_routers)
    while pending:
        router = pending.pop()
        routers.append(router)
        pending.extend(router.sub_routers)

    counts = {}
    for router in routers:
        for name, observer in router.observers.items():
            handlers = getattr(observer, "handlers", None)
            if handlers:
                counts[name] = counts.get(name, 0) + len(handlers)
    return counts


def check_working_directory():
    """The process a `docker exec` gets must land in /app, which is what makes relative paths work."""
    target = "the container's working directory is {}".format(APP_DIR)
    cwd = os.getcwd()
    if cwd == APP_DIR:
        return [(target, None)]
    return [(target, (
        "it is {!r}. src/label_maker.py resolves its templates from relative paths and "
        "`labels_db_path` defaults to the relative data/labels.db, so a process that starts "
        "anywhere else reads no template and writes its database outside the mounted "
        "volume".format(cwd)))]


def check_src_imports():
    """(e) Every module under src/ imports inside the image."""
    rows = []
    for name in SRC_MODULES:
        target = "import {}".format(name)
        try:
            __import__(name)
        except Exception as error:
            rows.append((target, (
                "it raised {}. The module is missing from the image, or one of its imports is: "
                "the Dockerfile copies src/ wholesale, so a partial copy or a wheel that did not "
                "install look the same from here".format(describe(error)))))
            continue
        rows.append((target, None))
    return rows


def check_third_party_symbols():
    """(e) The third-party names this repo's call sites use are present in the installed wheels."""
    rows = []
    for module_name, symbols in THIRD_PARTY_SYMBOLS:
        target = "{} provides {}".format(module_name, ", ".join(symbols))
        try:
            module = __import__(module_name, fromlist=["__name__"])
        except Exception as error:
            rows.append((target, "importing it raised {}".format(describe(error))))
            continue
        missing = [symbol for symbol in symbols if not hasattr(module, symbol)]
        if missing:
            rows.append((target, (
                "it is installed but does not provide {}. Every name here is used at a real call "
                "site in src/, so this is a wheel whose API moved under the pinned code".format(
                    ", ".join(missing)))))
            continue
        rows.append((target, None))
    return rows


def check_templates():
    """(d) Both label templates are in the image, non-empty and parseable."""
    rows = []
    for path in TEMPLATE_PATHS:
        target = "{} is present, non-empty and valid JSON".format(path)
        if not os.path.isfile(path):
            rows.append((target, (
                "it is not there. The Dockerfile copies templates/ in a line of its own, so the "
                "templates can go missing without anything else in the image changing — and the "
                "text template is loaded eagerly at startup while the glaze one is loaded lazily, "
                "so a missing glaze template would surface only when somebody runs "
                "/printglaze")))
            continue
        size = os.path.getsize(path)
        if size == 0:
            rows.append((target, "it exists and is 0 bytes"))
            continue
        try:
            with open(path, "rb") as handle:
                document = json.loads(handle.read().decode("utf-8"))
        except Exception as error:
            rows.append((target, "it exists ({} bytes) but does not parse: {}".format(
                size, describe(error))))
            continue
        if not isinstance(document.get("nodes"), list):
            # The same shape src/label_maker.py insists on: a template without a `nodes` array
            # makes _load_template() raise, so an image carrying one would fail at startup.
            rows.append((target, (
                "it parses but has no `nodes` array, which is exactly what "
                "LabelMaker._load_template() rejects. Top-level keys: {!r}".format(
                    sorted(document) if isinstance(document, dict) else type(document).__name__))))
            continue
        rows.append(("{} ({} bytes, {} nodes)".format(target, size, len(document["nodes"])), None))
    return rows


async def check_rasteriser():
    """(e) pymupdf really rasterises, not merely imports.

    This is the check that justifies its own paragraph. pymupdf is a compiled extension, and the
    failure mode worth catching is the one where the python half imports perfectly well and the
    rendering half does not work — a wheel resolved for the wrong platform or linked against a
    library that is not in this base image. The bot would then start, answer commands, reach
    Grist and the label service, and fail only at the moment it tries to show a preview, which is
    the step before every single print.

    So it goes through `src.preview.pdf_to_png` — the very function src/bot.py calls — rather
    than through pymupdf directly, and it checks the BYTES that come back.

    "It returned a PNG" is not by itself enough, which is why there is a second row: the input
    page draws text, and a renderer that produced a correct, well-formed, completely BLANK bitmap
    would satisfy every structural check while proving that nothing was ever drawn. So the PNG is
    decoded back into a pixmap and required to contain at least one non-white byte. A blank
    165x114 render comes back as 56430 bytes of 0xFF and exactly one distinct value; the same
    page with `(smoke)` on it comes back with over a hundred.
    """
    ink_target = "...and the raster is not blank, so the text was really drawn"
    target = "src.preview.pdf_to_png rasterises a real PDF to a real PNG"
    try:
        from src.preview import pdf_to_png
    except Exception as error:
        reason = "src.preview could not be imported: {}".format(describe(error))
        return [(target, reason), (ink_target, "not attempted: " + reason)]

    pdf = minimal_pdf()
    try:
        # 72 dpi keeps the pixmap tiny; the label is rendered at 300 in production, and the code
        # path is identical either way.
        png = await pdf_to_png(pdf, dpi=72)
    except Exception as error:
        reason = (
            "it raised {}. The module imported, so this is the compiled half of pymupdf failing "
            "at render time — the exact failure that an import check would have called green "
            "while every preview in production came back broken".format(describe(error)))
        return [(target, reason), (ink_target, "not attempted: the render raised")]

    if not isinstance(png, (bytes, bytearray)):
        reason = "it returned {}, not bytes".format(type(png).__name__)
        return [(target, reason), (ink_target, "not attempted: " + reason)]
    if not bytes(png).startswith(PNG_MAGIC):
        reason = "it returned {} bytes that are not a PNG (first bytes: {!r})".format(
            len(png), bytes(png)[:12])
        return [(target, reason), (ink_target, "not attempted: the output is not a PNG")]
    rows = [("{} ({} bytes in, {} bytes out)".format(target, len(pdf), len(png)), None)]

    # Decoded back into a pixmap so the SAMPLES can be looked at: a PNG's pixels are deflated, so
    # the bytes returned above say nothing about what is in the picture. `Pixmap(bytes)` is
    # pymupdf's own image reader, which means this row exercises a second compiled code path
    # rather than trusting the first one's word for it.
    try:
        import pymupdf
        samples = pymupdf.Pixmap(bytes(png)).samples
    except Exception as error:
        rows.append((ink_target, (
            "the PNG could not be decoded back for inspection: {}. pdf_to_png returned something "
            "with a PNG signature that pymupdf itself will not read".format(describe(error)))))
        return rows
    if not samples:
        rows.append((ink_target, "the decoded pixmap has no samples at all"))
        return rows
    darkest = min(samples)
    if darkest == 255:
        rows.append((ink_target, (
            "every one of the {} sample bytes is 0xFF, i.e. the page rasterised to pure white. "
            "The input draws `(smoke)` in Base-14 Helvetica, so the geometry was rendered and the "
            "TEXT was not: MuPDF's font machinery is not producing glyphs in this image, and "
            "every label — which is nothing but text — would print blank while this gate and the "
            "bot both reported success".format(len(samples)))))
        return rows
    rows.append(("{} (darkest sample 0x{:02X} of {} bytes)".format(
        ink_target, darkest, len(samples)), None))
    return rows


async def check_storage():
    """(e) The SQLite store opens, creates its file, and survives a second init().

    `init()` being idempotent is not a hypothetical: this repo has a commit that made it so,
    because the second call used to overwrite `self.db` without closing it and leak the
    aiosqlite connection's non-daemon background thread — which then blocked interpreter
    shutdown. That is the kind of regression that a unit test can catch and an image cannot lose
    on its own; what this adds is that the whole path works with the aiosqlite wheel that is
    really installed here, against a real file on the image's real filesystem.
    """
    rows = []
    open_target = "Storage.init() opens a database and creates its file"
    again_target = "...and a second Storage.init() is a no-op rather than a second connection"

    try:
        from src.storage import Storage
    except Exception as error:
        reason = "src.storage could not be imported: {}".format(describe(error))
        return [(open_target, reason), (again_target, reason)]

    directory = tempfile.mkdtemp(prefix="smoke-storage-")
    path = os.path.join(directory, "labels.db")
    store = Storage(path)
    try:
        try:
            await store.init()
        except Exception as error:
            rows.append((open_target, "it raised {}".format(describe(error))))
            rows.append((again_target, "not attempted: the first init() failed"))
            return rows

        if not os.path.isfile(path):
            rows.append((open_target, (
                "it returned without raising, but {!r} does not exist. In production this path "
                "is data/labels.db inside the mounted volume, so a store that silently writes "
                "nowhere loses every reprint button in the chat".format(path))))
            rows.append((again_target, "not attempted: the database file was never created"))
            return rows
        size = os.path.getsize(path)
        rows.append(("{} ({} bytes)".format(open_target, size), None))

        first_connection = store.db
        try:
            await store.init()
        except Exception as error:
            rows.append((again_target, (
                "it raised {}. init() is supposed to return immediately when a connection "
                "already exists".format(describe(error)))))
            return rows
        if store.db is not first_connection:
            rows.append((again_target, (
                "it opened a second connection and dropped the first without closing it. That "
                "leaks aiosqlite's background thread, which is not a daemon thread and will hold "
                "interpreter shutdown open")))
            return rows
        rows.append((again_target, None))
        return rows
    finally:
        try:
            await store.close()
        except Exception:
            # Best effort: this is teardown of the probe's own scratch database, and a failure
            # here must not become the verdict of a check about something else.
            pass
        shutil.rmtree(directory, ignore_errors=True)


async def check_bot_construction():
    """(e) PrintBot can actually be built, with every handler registered.

    The most valuable check in this file, and the one that repays explaining. `PrintBot.__init__`
    touches no network at all — it builds a `Bot` and a `Dispatcher`, constructs the two
    LabelMakers, the Grist client and the Printer, and registers eight handlers — but it does a
    great deal of work that only succeeds if the image is right: the token has to satisfy
    aiogram's format check, the eager LabelMaker has to READ the text template off disk through
    its relative path, the lazy one has to be constructible without reading anything, and every
    filter has to build. All of that is unreachable from the outside of a container whose real
    command dies at the first Telegram call.

    The handler count is taken from aiogram's own structures rather than from the source text,
    so a registration that silently stopped happening is a finding here.
    """
    rows = []
    build_target = "PrintBot(storage) constructs without touching the network"
    handlers_target = "...and registers {} handlers ({})".format(
        sum(EXPECTED_HANDLERS.values()),
        ", ".join("{} {}".format(count, name) for name, count in sorted(EXPECTED_HANDLERS.items())))
    eager_target = "...and its eager LabelMaker has the text template loaded"
    lazy_target = "...and its lazy glaze LabelMaker defers loading instead"

    every_target = (build_target, handlers_target, eager_target, lazy_target)

    # Which of the four have had a verdict recorded. The report text is allowed to carry a suffix
    # (" (7 nodes)"), so "did this target get answered" cannot be decided by looking for it in the
    # rows — the fallback in the exception handler at the bottom checks this set instead.
    answered = set()

    def record(target, reason, shown=None):
        rows.append((shown or target, reason))
        answered.add(target)

    try:
        from src.bot import PrintBot
        from src.storage import Storage
    except Exception as error:
        reason = "the modules could not be imported: {}".format(describe(error))
        return [(target, reason) for target in every_target]

    directory = tempfile.mkdtemp(prefix="smoke-bot-")
    store = Storage(os.path.join(directory, "labels.db"))
    bot = None
    try:
        await store.init()
        try:
            bot = PrintBot(store)
        except Exception as error:
            record(build_target, (
                "it raised {}. Nothing in that constructor is allowed to need a peer, so this is "
                "the image: a template that cannot be read from the working directory, a wheel "
                "whose API moved, or a token the aiogram in this image will not "
                "accept".format(describe(error))))
            for target in every_target[1:]:
                record(target, "not attempted: the constructor raised")
            return rows
        record(build_target, None)

        counts = count_handlers(bot.dp)
        if counts != EXPECTED_HANDLERS:
            record(handlers_target, (
                "the dispatcher holds {!r} instead. One chat-migration service-message handler, "
                "four commands (/start, /print, /printglaze, /sudoprint) and three callback "
                "prefixes (print:, cancel:, reprint:) are supposed to be registered; a mismatch "
                "means either a handler stopped being registered — in which case that command "
                "silently does nothing in the chat — or one was added and this constant needs "
                "updating".format(counts)))
        else:
            record(handlers_target, None)

        # `_template` rather than the `template` property on purpose: the property LOADS on first
        # access, so reading it would make both makers look identical and would prove nothing
        # about which of them loads eagerly. The distinction matters — the text template is
        # required for the bot to be useful at all and must fail at startup if it is missing,
        # while the glaze one is optional by design and must not take startup down with it.
        eager = getattr(bot.label_maker, "_template", None)
        if not isinstance(eager, dict) or not eager.get("nodes"):
            record(eager_target, (
                "it is {!r}. LabelMaker() without lazy=True is supposed to read "
                "templates/label_template.json during construction, so this means the eager load "
                "stopped happening and a missing template would now surface at print time "
                "instead of at startup".format(type(eager).__name__)))
        else:
            record(eager_target, None,
                   shown="{} ({} nodes)".format(eager_target, len(eager["nodes"])))

        if getattr(bot.glaze_maker, "_template", "absent") is not None:
            record(lazy_target, (
                "it has already loaded one. The glaze template is deliberately lazy so that its "
                "absence cannot crash startup; an eager load here reverses that"))
        else:
            record(lazy_target, None)
        return rows
    except Exception as error:
        # A distinct target rather than build_target, so a failure here cannot print a second,
        # contradictory verdict against a row that has already been reported ok.
        rows.append(("the PrintBot construction check ran to completion",
                     "it raised {}".format(describe(error))))
        # Then fill in whatever never got a verdict. Without this the four specific rows collapse
        # into the single general one above whenever something between them raises — `await
        # store.init()` and count_handlers() are both outside the inner try — and this file's own
        # invariant is that a check which could not run is reported as FAILED rather than
        # silently dropped. The two blocks above already do this by hand; this is the same thing
        # for the paths nobody enumerated.
        for target in every_target:
            if target not in answered:
                record(target, "not attempted: the check raised before reaching it")
        return rows
    finally:
        if bot is not None:
            try:
                # aiogram opens no socket until a request is made, but it does allocate a
                # session object; closing it keeps the probe from printing an unclosed-session
                # warning into the middle of its own report.
                await bot.bot.session.close()
            except Exception:
                pass
        try:
            await store.close()
        except Exception:
            pass
        shutil.rmtree(directory, ignore_errors=True)


async def check_lp_binary():
    """(c) The CUPS client is in the image and executes.

    src/printer.py shells out to `lp`, and the Dockerfile installs cups-client in a step of its
    own for exactly that. A base image change that dropped it would produce a container that
    starts, talks to Telegram, renders previews — and raises PrinterError the first time somebody
    presses «Печать», which is the last place anybody wants to discover it.

    `shutil.which` alone would not settle it: a binary can be present and still fail to execute,
    which is what a base image with a different libc looks like. So this runs the REAL
    `Printer.print_pdf()` against a CUPS address that refuses connections, and then rules out
    BOTH ways of not really running:

      * the "lp not found" branch, which printer.py raises on FileNotFoundError;
      * an exit of 126 or 127, which printer.py cannot distinguish from a CUPS failure and words
        identically. That is the case this docstring used to claim to catch while the check
        counted it as a pass — see LP_LOADER_FRAGMENTS for why the status code is the honest
        discriminator and the message is not.

    Only THIS REPO'S OWN wording is matched, and only to rule those two out — lp's own error text
    belongs to CUPS and is never matched here.
    """
    rows = []
    which_target = "the `lp` binary is on PATH inside the image"
    run_target = "...and running it through Printer.print_pdf() reaches lp itself"

    path = shutil.which("lp")
    if path is None:
        rows.append((which_target, (
            "it is not. The Dockerfile installs the cups-client package for this one binary, so "
            "either that step is gone or the base image changed underneath it — and printing is "
            "the only thing this service exists to do")))
        rows.append((run_target, "not attempted: there is no lp to run"))
        return rows
    rows.append(("{} ({})".format(which_target, path), None))

    try:
        from src.printer import Printer, PrinterError
    except Exception as error:
        rows.append((run_target, "src.printer could not be imported: {}".format(describe(error))))
        return rows

    # Constructed with no arguments, so it reads the same settings production reads. CUPS_HOST is
    # 127.0.0.1:1 in this container's environment (see SMOKE_ENV in the outer half), which is why
    # nothing needs to be patched here and why the connection is refused instantly.
    printer = Printer()
    try:
        request_id = await asyncio.wait_for(
            printer.print_pdf(minimal_pdf(), title="smoke"), timeout=LP_TIMEOUT)
    except PrinterError as error:
        message = str(error)
        if LP_MISSING_FRAGMENT in message:
            rows.append((run_target, (
                "it raised the missing-binary branch: {!r}. `shutil.which` found lp on PATH a "
                "moment ago, so this means the subprocess could not be spawned at all — the path "
                "went stale between the two calls, or exec was refused".format(message))))
        elif any(fragment in message for fragment in LP_LOADER_FRAGMENTS):
            # See LP_LOADER_FRAGMENTS: this is the case the docstring promises to catch and the
            # naive form of this check counted as a pass, because printer.py words it exactly
            # like a real CUPS failure.
            rows.append((run_target, (
                "lp exited with a loader status: {!r}. The file is on PATH and printer.py spawned "
                "it, but it never got as far as running its own code — 126 is 'found and not "
                "executable', 127 is the dynamic linker giving up on a missing or incompatible "
                "shared library. Neither can come from CUPS here, because print_pdf() execs the "
                "binary directly with no shell in between. A base image change is the usual "
                "cause, and printing would be dead in production while the container looked "
                "healthy".format(message))))
        else:
            # The expected outcome: lp ran its own code, could not reach a CUPS server on a port
            # nothing listens on, and printer.py turned its non-zero exit into the other branch.
            rows.append(("{} ({})".format(run_target, message[:120]), None))
    except asyncio.TimeoutError:
        rows.append((run_target, (
            "lp did not return within {} s against 127.0.0.1:1, where a connection can only be "
            "refused. The binary is there and started, but something in the print path blocks — "
            "and print_pdf() is awaited from a Telegram handler, so a block there stalls the "
            "bot".format(LP_TIMEOUT))))
    except Exception as error:
        rows.append((run_target, (
            "it raised {} instead of PrinterError. printer.py is supposed to convert every "
            "failure of the lp subprocess into PrinterError, which is what the handlers "
            "catch".format(describe(error)))))
    else:
        # Not a pass: nothing may answer on 127.0.0.1:1, so a success means either something IS
        # listening in this container or lp accepted a job somewhere it should not have.
        rows.append((run_target, (
            "lp reported success ({!r}) against 127.0.0.1:1, where nothing can be listening. A "
            "print job may have been submitted somewhere, and this check can no longer tell the "
            "two branches apart".format(request_id))))
    return rows


async def run_check(name, function):
    """Run one check, converting an unexpected exception into a row instead of a dead probe.

    Without this a single unforeseen error — an aiogram that raises something new during
    construction, a tempdir that cannot be made — would take the whole probe down and cost the
    run every other verdict, in exactly the situation where the rest of the report is what tells
    somebody which change did it.

    The check is passed as a FUNCTION and called in here, not called at the call site and passed
    as a result. That distinction is the whole safety net for the synchronous checks: calling
    `check_templates()` in the argument list would run its body BEFORE this function is entered,
    so an exception from it would escape past the `except` below and take the probe down anyway.
    (An async check would survive either way, because calling it only builds a coroutine — which
    is exactly the kind of asymmetry that makes a net look like it works until it is needed.)
    """
    try:
        result = function()
        if hasattr(result, "__await__"):
            result = await result
        return result
    except Exception as error:
        return [("the {} check".format(name), "it raised {}".format(describe(error)))]


async def main():
    rows = []
    rows.extend(await run_check("working directory", check_working_directory))
    rows.extend(await run_check("src imports", check_src_imports))
    rows.extend(await run_check("third-party symbols", check_third_party_symbols))
    rows.extend(await run_check("templates", check_templates))
    rows.extend(await run_check("rasteriser", check_rasteriser))
    rows.extend(await run_check("storage", check_storage))
    rows.extend(await run_check("bot construction", check_bot_construction))
    rows.extend(await run_check("lp binary", check_lp_binary))

    failures = []
    for target, reason in rows:
        # Both halves of the row go through one_line(): a target can pick up an lp message with a
        # newline in it just as easily as a reason can be a multi-paragraph ValidationError, and
        # either one would break the one-row-per-line contract the outer half parses on.
        if reason is None:
            print("ok   {}".format(one_line(target)))
        else:
            print("FAIL {} -> {}".format(one_line(target), one_line(reason)))
            failures.append(target)

    if failures:
        print("probe FAILED: {}/{} targets broken".format(len(failures), len(rows)))
        raise SystemExit(1)

    print("{}: {}/{} targets".format(PROBE_MARKER, len(rows), len(rows)))


if __name__ == "__main__":
    asyncio.run(main())
'''


def excerpt(text):
    """Bound what reaches the log, and say so when something was cut."""
    if text is None:
        return ""
    if len(text) <= EXCERPT_CHARS:
        return text
    return text[:EXCERPT_CHARS] + "\n[... truncated at {} characters]".format(EXCERPT_CHARS)


def docker(args, timeout, stdin_text=None):
    """Run a docker command.

    Returns (status, output) with stderr folded into stdout, because everything here is read by
    a human out of a CI log where the interleaving is the useful part. A status of None means
    the command produced no exit code at all — it timed out, or docker is not there — and
    `output` then explains which.
    """
    argv = ["docker"] + args
    try:
        completed = subprocess.run(
            argv,
            input=stdin_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True)
    except FileNotFoundError:
        return None, (
            "`docker` is not on PATH. This gate drives the daemon from the runner, so it cannot "
            "run anywhere the docker CLI is missing")
    except subprocess.TimeoutExpired as error:
        return None, "`{}` did not finish within {} s. Output so far:\n{}".format(
            " ".join(argv), timeout, excerpt(error.output))
    return completed.returncode, completed.stdout or ""


def remove_container(name):
    """Best effort. Never the reason a check fails; the workflow cleans up too."""
    docker(["rm", "-f", name], REMOVE_TIMEOUT)


def environment_flags():
    """SMOKE_ENV as `-e VAR=value` pairs for a `docker run` argument list."""
    flags = []
    for pair in SMOKE_ENV:
        flags.extend(["-e", pair])
    return flags


def check_image_contract(image):
    """(a) What the image DECLARES: its command, where it runs, and that nothing wraps them."""
    rows = []
    target = "docker inspect {}".format(image)
    status, output = docker(
        ["inspect", "--format", "{{json .Config}}", image], INSPECT_TIMEOUT)
    if status is None:
        # No exit code at all: the docker CLI is missing or the call ran out its timeout. The
        # message from docker() already says which, and neither of them means what a non-zero
        # exit means, so they must not share its wording.
        rows.append((target, output))
        return rows
    if status != 0:
        rows.append((target, (
            "it exited {} — the tag does not exist on this daemon, so the build step and this "
            "step disagree about what was built. Output:\n{}".format(status, excerpt(output)))))
        return rows
    try:
        config = json.loads(output)
    except ValueError as error:
        rows.append((
            "parse the image config of {}".format(image),
            "docker inspect returned something that is not JSON ({}): {}".format(
                error, excerpt(output))))
        return rows

    cmd = config.get("Cmd")
    rows.append((
        "the image's CMD is {}".format(EXPECTED_CMD),
        None if cmd == EXPECTED_CMD else (
            "it is {!r}. Production runs the image's own command and so does check (f) below — a "
            "silent change here means the gate and the deployment are describing two different "
            "programs".format(cmd))))

    # An ENTRYPOINT would prepend itself to the CMD above, so `docker run <image>` would no
    # longer be `python main.py` and check (b)'s verdict would be about something else entirely.
    # There is none today, and this is what keeps it that way.
    entrypoint = config.get("Entrypoint")
    rows.append((
        "the image declares no ENTRYPOINT, so its CMD is the whole command line",
        None if not entrypoint else (
            "it declares {!r}, which is prepended to the CMD. Every `docker run` in this gate "
            "now executes something other than what it reports".format(entrypoint))))

    working_dir = config.get("WorkingDir")
    rows.append((
        "the image's WORKDIR is {}".format(APP_DIR),
        None if working_dir == APP_DIR else (
            "it is {!r}, and that is not cosmetic. src/label_maker.py declares its template "
            "paths as the RELATIVE constants templates/label_template.json and "
            "templates/label_template_glaze.json, and `labels_db_path` defaults to the relative "
            "data/labels.db — all three resolve against the working directory. Move it and the "
            "bot either fails to load a template at startup or quietly keeps its label history "
            "outside the mounted volume, where every reprint button stops working after the next "
            "redeploy".format(working_dir))))

    return rows


def check_missing_env_guard(image, name):
    """(b) Run the image with no environment: it must die and name all seven variables.

    `settings = Settings()` in src/settings.py runs at import time and pydantic raises before
    main() logs anything. This relies on the image carrying no `.env` file (`SettingsConfigDict`
    names one): the Dockerfile copies only specific paths and .dockerignore lists `.env`.

    The image's own CMD is kept; the guard makes it terminate. `--rm` covers the container that
    exits, the name covers the one that does not.
    """
    rows = []
    target = "`docker run --rm {}` with no environment at all".format(image)
    # A re-run from the UI reuses the run id, so it reuses this name too, and a previous attempt
    # killed by the step timeout may have left the container behind. A no-op when it did not.
    remove_container(name)
    status, output = docker(["run", "--rm", "--name", name, image], GUARD_TIMEOUT)
    if status is None:
        rows.append((target, output))
        return rows, output
    if status == 0:
        rows.append((target, (
            "it exited 0. The required-variable guard did not fire, so a stack redeployed "
            "without TELEGRAM_BOT_TOKEN or CUPS_HOST would come up looking perfectly healthy and "
            "fail silently at the first print instead. Output:\n{}".format(excerpt(output)))))
        return rows, output
    rows.append(("{} exits non-zero (got {})".format(target, status), None))

    # A non-zero exit alone proves nothing (an import error exits non-zero too), so the output
    # must name each missing field, one row per field.
    for field in REQUIRED_FIELDS:
        rows.append((
            "...and its output names the missing field {!r}".format(field),
            None if field in output else (
                "nothing in the output mentions it. The image exited {}, but either this is not "
                "the validation guard firing — it is the app dying of something else before it "
                "got there — or {!r} has quietly acquired a default and is no longer required at "
                "all. Output:\n{}".format(status, field, excerpt(output)))))
    return rows, output


def start_probe_container(image, name):
    """Start the container the probe runs inside.

    The image's CMD is replaced with a sleeping command because the real one reaches Telegram a
    few lines into startup; check (f) runs the real command in its own container.
    """
    rows = []
    target = "`docker run -d` starts a probe container of {}".format(image)
    remove_container(name)
    status, output = docker(
        ["run", "-d", "--name", name] + environment_flags() + [image] + IDLE_COMMAND,
        START_TIMEOUT)
    if status is None:
        rows.append((target, output))
        return rows, False, output
    if status != 0:
        rows.append((target, "`docker run -d` exited {}:\n{}".format(status, excerpt(output))))
        return rows, False, output
    rows.append((target, None))
    return rows, True, output


def probe_report_rows(output):
    """Parse the probe's own report lines back into rows of this gate's report.

    The probe prints exactly the same `ok   <target>` / `FAIL <target> -> <reason>` shape this
    file does, so its verdicts can be merged into one list instead of arriving as a single opaque
    "the probe failed". That matters for the same reason the guard check has one row per
    variable: a run that breaks four things should say four things.
    """
    rows = []
    for line in output.splitlines():
        if line.startswith("ok   "):
            rows.append((PROBE_ROW_PREFIX + line[5:], None))
        elif line.startswith("FAIL "):
            target, _, reason = line[5:].partition(" -> ")
            rows.append((PROBE_ROW_PREFIX + target,
                         reason or "the probe reported FAIL with no reason"))
    return rows


def check_probe(name, started):
    """(c)-(e) Run the in-container probe and merge its verdicts into ours.

    `docker exec -i` feeds the program over stdin without a tty and propagates its exit status,
    which lets the rows below tell "the probe reported failures" from "the probe died first".
    """
    target = "the in-container probe's exit status agrees with its own report"
    marker_target = "the in-container probe ran to its end"
    if not started:
        return [("the in-container probe", "not attempted: the container never started")], ""

    status, output = docker(
        ["exec", "-i", name, "python", "-u", "-"], PROBE_TIMEOUT, stdin_text=PROBE)
    if status is None:
        # Timed out, or docker is missing. Either way there are no verdicts to merge.
        return [("the in-container probe", output)], output

    rows = probe_report_rows(output)
    if not rows:
        # Exit status alone cannot be read here: a probe that printed nothing has told us
        # nothing, whatever it exited with. This is what a `docker exec` that could not start the
        # interpreter looks like, and what a truncated stdin looks like.
        return [("the in-container probe", (
            "it produced no report lines at all, so none of the in-container checks ran. "
            "`docker exec` exited {}. Output:\n{}".format(status, excerpt(output))))], output

    reported_failures = any(reason is not None for _, reason in rows)
    if status != 0 and not reported_failures:
        rows.append((target, (
            "`docker exec` exited {} but every line the probe printed says ok — so it died after "
            "reporting and before finishing, and the report above is incomplete".format(status))))
    elif status == 0 and reported_failures:
        rows.append((target, (
            "the probe printed FAIL lines yet exited 0. Its failures are supposed to leave "
            "through SystemExit(1); an exit of 0 here means they no longer do, and this gate "
            "would have gone green on them")))
    else:
        rows.append((target, None))

    rows.append((marker_target, None if PROBE_MARKER in output else (
        "it never printed {!r}. The marker is on the probe's last line, so its absence means the "
        "program did not run to the end — a truncated stdin, or something that killed the "
        "interpreter mid-report".format(PROBE_MARKER))))
    return rows, output


def check_container_alive(name, started):
    """(g) The probe container is still running after everything above.

    It was started with a sleeping command, so there is exactly one correct answer. A container
    that is gone means something killed it — the daemon's OOM killer being the realistic one, on
    a runner shared with every other repository — and that would also mean the probe's verdicts
    above were collected from a process that was being torn down underneath it.
    """
    target = "the probe container is still running after every check above"
    if not started:
        return [(target, "not attempted: the container never started")]
    status, output = docker(
        ["inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", name], INSPECT_TIMEOUT)
    if status != 0:
        return [(target, "docker inspect exited {}: {}".format(status, excerpt(output)))]
    state = output.strip()
    return [(target, None if state.startswith("true") else (
        "docker reports `Running ExitCode` = {!r}. It was started with a command that does "
        "nothing but sleep, so it cannot have finished on its own".format(state)))]


def check_real_command(image, name):
    """(f) The image's real CMD starts and gets through its own startup.

    main.py configures the logger and logs STARTUP_MARKER, awaits `Storage.init()`, constructs
    `PrintBot` (which reads the text template), and calls `bot.start()`. `PrintBot.start()` awaits
    `set_my_commands` (a request to Telegram) before logging "Starting polling", so that marker is
    unreachable here and must not be waited for.

    Checked: the startup marker (the seven variables parsed), the database file (SQLite opened
    under the real CMD and WORKDIR), and the storage and template log markers. Not checked: the
    container's fate after the Telegram call, which depends on third-party wording and on retry
    behaviour that may change; its final state is printed for diagnosis only.

    Started detached and without `--rm`, because `docker cp` reads its filesystem afterwards.
    """
    rows = []
    start_target = "`docker run -d` starts a container of {} with the image's own command".format(
        image)
    marker_target = "its log carries {!r} (bounded at {} s)".format(STARTUP_MARKER, STARTUP_BUDGET)
    storage_target = "...and {!r}, so the label store opened".format(STORAGE_MARKER)
    template_target = "...and {!r}, so the relative template path resolved".format(TEMPLATE_MARKER)
    db_target = DB_TARGET

    remove_container(name)
    status, output = docker(
        ["run", "-d", "--name", name] + environment_flags() + [image], START_TIMEOUT)
    if status is None:
        rows.append((start_target, output))
        for target in (marker_target, storage_target, template_target, db_target):
            rows.append((target, "not attempted: the container could not be started"))
        return rows, ""
    if status != 0:
        rows.append((start_target, "`docker run -d` exited {}:\n{}".format(status, excerpt(output))))
        for target in (marker_target, storage_target, template_target, db_target):
            rows.append((target, "not attempted: the container could not be started"))
        return rows, ""
    rows.append((start_target, None))

    # Polled rather than slept on, and bounded in wall clock: `docker logs` works on a container
    # that has already exited, so this reads the same log either way and does not care whether
    # the process is still alive by the time the marker is looked for.
    # The exit condition is ALL of STARTUP_MARKERS, not just the first one — see the note on that
    # constant. Every verdict below is decided against the single snapshot this loop leaves in
    # `logs`, so a loop that returned as soon as the first line appeared would be judging the
    # other two on a log that had not been given the chance to contain them yet.
    deadline = time.monotonic() + STARTUP_BUDGET
    logs = ""
    log_status = None
    while True:
        log_status, logs = docker(["logs", name], LOGS_TIMEOUT)
        if log_status == 0 and all(marker in logs for marker in STARTUP_MARKERS):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(STARTUP_PAUSE)

    if log_status != 0:
        for target in (marker_target, storage_target, template_target):
            rows.append((target, "`docker logs` exited {}: {}".format(log_status, excerpt(logs))))
    else:
        rows.append((marker_target, None if STARTUP_MARKER in logs else (
            "it never appeared in {} s. That line is the first thing main.py logs, before "
            "anything else happens, so its absence means the process did not get that far: "
            "`settings = Settings()` raised at import — a required variable is missing or "
            "malformed — or the interpreter never started at all. Logs:\n{}".format(
                STARTUP_BUDGET, excerpt(logs)))))
        rows.append((storage_target, None if STORAGE_MARKER in logs else (
            "it never appeared in {} s. Storage.init() logs it once the database is open and the "
            "schema is in place, so the store did not finish opening under the real command. "
            "Logs:\n{}".format(STARTUP_BUDGET, excerpt(logs)))))
        rows.append((template_target, None if TEMPLATE_MARKER in logs else (
            "it never appeared in {} s. LabelMaker logs it after reading a template through its "
            "relative path, which only resolves because the image's WORKDIR is {}. Its absence "
            "means PrintBot never got that far, or the templates are not where the code looks "
            "for them. Logs:\n{}".format(STARTUP_BUDGET, APP_DIR, excerpt(logs)))))

    rows.extend(check_database_file(name))

    # Informational only, and deliberately not a verdict — see the docstring above. Printed so
    # that a human reading a failure has the container's fate in front of them without having to
    # go and ask the daemon for it.
    state_status, state = docker(
        ["inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", name], INSPECT_TIMEOUT)
    footer = "\n[container state after the checks above — `Running ExitCode` = {}]".format(
        state.strip() if state_status == 0 else "unavailable")
    return rows, (logs or "") + footer


def check_database_file(name):
    """(f, second half) The label database really exists inside the container.

    Read with `docker cp` rather than `docker exec`: the container dies at its first Telegram call,
    and `docker cp` reads a stopped container's filesystem while `docker exec` needs a running one
    (an exec would race the exit and flake red on a healthy image). The whole directory is copied
    so a failure carries the listing that explains it.
    """
    target = DB_TARGET
    destination = tempfile.mkdtemp(prefix="smoke-datadir-")
    try:
        status, output = docker(["cp", "{}:{}".format(name, DATA_DIR), destination], COPY_TIMEOUT)
        if status is None:
            return [(target, output)]
        if status != 0:
            return [(target, (
                "`docker cp` of {} exited {}. The directory is created by the Dockerfile and is "
                "the mount point of the production volume, so it not being there is a change to "
                "the image itself. Output:\n{}".format(DATA_DIR, status, excerpt(output))))]

        local_dir = os.path.join(destination, os.path.basename(DATA_DIR))
        if not os.path.isdir(local_dir):
            return [(target, (
                "`docker cp` succeeded but produced no {!r} directory locally, so there is "
                "nothing to look at. Copied into: {!r}".format(
                    os.path.basename(DATA_DIR), sorted(os.listdir(destination)))))]
        entries = sorted(os.listdir(local_dir))
        local_db = os.path.join(local_dir, DB_FILENAME)
        if not os.path.isfile(local_db):
            return [(target, (
                "it is not there. main.py awaits Storage.init() before it builds the bot, and "
                "init() creates the file, so an empty data directory means startup stopped "
                "before that — or `labels_db_path` no longer resolves to this directory, in "
                "which case production would be writing its label history outside the mounted "
                "volume and losing it on every redeploy. {} contains: {!r}".format(
                    DATA_DIR, entries)))]
        size = os.path.getsize(local_db)
        if size == 0:
            return [(target, (
                "it exists and is 0 bytes. SQLite writes its header as soon as the schema is "
                "created, so a zero-length file means the connection was opened and nothing was "
                "ever executed on it"))]
        return [("{} ({} bytes)".format(target, size), None)]
    finally:
        shutil.rmtree(destination, ignore_errors=True)


def main():
    image = os.environ.get(IMAGE_ENV)
    if not image:
        print("{} is not set: this gate tests the image it is given and has no default, because "
              "a default would silently gate whichever image happened to be on the daemon".format(
                  IMAGE_ENV))
        raise SystemExit(1)
    name = os.environ.get(NAME_ENV)
    if not name:
        # No default: the workflow's cleanup step builds the container names from the same
        # variable, so a fallback would hide containers from it, and a constant would make two
        # concurrent runs on the shared daemon remove each other's containers.
        print("{} is not set: this gate names every container it starts after it, and a default "
              "would both hide containers from the workflow's cleanup step and make two "
              "concurrent runs collide on one name".format(NAME_ENV))
        raise SystemExit(1)

    rows = []
    transcripts = []

    rows.extend(check_image_contract(image))

    guard_rows, guard_output = check_missing_env_guard(image, name + GUARD_SUFFIX)
    transcripts.append(("the image run with no environment at all", guard_output))
    rows.extend(guard_rows)

    try:
        probe_start_rows, started, start_output = start_probe_container(image, name)
        transcripts.append(("`docker run -d` of the probe container", start_output))
        rows.extend(probe_start_rows)

        probe_rows, probe_output = check_probe(name, started)
        transcripts.append(("the probe, from inside the container", probe_output))
        rows.extend(probe_rows)

        # After the probe on purpose: "the container is still up" is only worth anything once
        # everything else has had its turn at it.
        rows.extend(check_container_alive(name, started))

        cmd_rows, cmd_logs = check_real_command(image, name + CMD_SUFFIX)
        transcripts.append(("the image started with its own CMD", cmd_logs))
        rows.extend(cmd_rows)
    finally:
        # The workflow removes both again under `if: always()`, which covers the case where this
        # whole script is killed by the step timeout and never gets here.
        remove_container(name + CMD_SUFFIX)
        remove_container(name)

    # The transcripts first, the verdicts last: in a CI log the verdicts are what somebody
    # scrolls to the bottom for, and the containers' own output is what turns a one-line verdict
    # into a diagnosis.
    for label, text in transcripts:
        print("")
        print("--- {} ---".format(label))
        print(excerpt(text).rstrip() or "(no output)")
    print("")
    print("--- results ---")

    failures = []
    for target, reason in rows:
        if reason is None:
            print("ok   {}".format(target))
        else:
            print("FAIL {} -> {}".format(target, reason))
            failures.append(target)

    if failures:
        print("")
        print("smoke FAILED: {}/{} targets broken:".format(len(failures), len(rows)))
        for target in failures:
            print("  - {}".format(target))
        raise SystemExit(1)

    print("")
    print("smoke ok: {}/{} targets".format(len(rows), len(rows)))


if __name__ == "__main__":
    main()
