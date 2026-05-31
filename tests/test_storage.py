"""Tests for src.storage.Storage — schema creation, migration, round-trips.

The `storage` fixture provides an already-init()'d in-memory Storage for the
round-trip cases. The schema/migration cases build their own Storage so they can
control the exact pre-existing DB state.
"""

import time

import aiosqlite

from src.storage import Storage

_EXPECTED_COLUMNS = {"id", "text", "chat_id", "user_id", "kind", "created_at"}


async def _column_names(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("PRAGMA table_info(labels)")
    return {row[1] for row in await cur.fetchall()}


# ── schema creation ──────────────────────────────────────────────────────────

async def test_init_creates_labels_table_with_expected_columns():
    s = Storage(":memory:")
    await s.init()
    try:
        # The table exists in sqlite_master ...
        cur = await s.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='labels'"
        )
        assert await cur.fetchone() is not None
        # ... and carries exactly the documented columns.
        assert await _column_names(s.db) == _EXPECTED_COLUMNS
    finally:
        await s.close()


async def test_init_is_idempotent():
    s = Storage(":memory:")
    await s.init()
    try:
        # Calling init twice must not raise (CREATE TABLE IF NOT EXISTS etc.).
        await s.init()
        assert await _column_names(s.db) == _EXPECTED_COLUMNS
    finally:
        await s.close()


# ── migration of an old DB lacking the `kind` column ─────────────────────────

async def test_init_migrates_old_db_adding_kind_column(tmp_path):
    db_path = tmp_path / "old.db"

    # Build a pre-`kind` schema by hand and drop a row in it.
    raw = await aiosqlite.connect(str(db_path))
    await raw.execute(
        """
        CREATE TABLE labels (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            text       TEXT    NOT NULL,
            chat_id    INTEGER,
            user_id    INTEGER,
            created_at INTEGER NOT NULL
        )
        """
    )
    await raw.execute(
        "INSERT INTO labels (text, chat_id, user_id, created_at) VALUES (?, ?, ?, ?)",
        ("legacy", 10, 20, 1700000000),
    )
    await raw.commit()
    await raw.close()

    s = Storage(str(db_path))
    await s.init()
    try:
        # The migration added the missing column.
        assert "kind" in await _column_names(s.db)
        # And the pre-existing row gets the 'text' default for the new column.
        row = await s.get_label(1)
        assert row is not None
        assert row["text"] == "legacy"
        assert row["kind"] == "text"
    finally:
        await s.close()


async def test_migrated_db_default_kind_applies_to_new_rows(tmp_path):
    db_path = tmp_path / "old.db"

    raw = await aiosqlite.connect(str(db_path))
    await raw.execute(
        """
        CREATE TABLE labels (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            text       TEXT    NOT NULL,
            chat_id    INTEGER,
            user_id    INTEGER,
            created_at INTEGER NOT NULL
        )
        """
    )
    await raw.commit()
    await raw.close()

    s = Storage(str(db_path))
    await s.init()
    try:
        # After migration the column behaves like a normal default-bearing column.
        new_id = await s.add_label("fresh", chat_id=1, user_id=2)
        row = await s.get_label(new_id)
        assert row["kind"] == "text"
    finally:
        await s.close()


# ── round-trip (storage fixture) ─────────────────────────────────────────────

async def test_add_and_get_label_roundtrips_all_fields(storage):
    label_id = await storage.add_label("hello world", chat_id=42, user_id=7)
    row = await storage.get_label(label_id)
    assert row["id"] == label_id
    assert row["text"] == "hello world"
    assert row["chat_id"] == 42
    assert row["user_id"] == 7


async def test_add_label_default_kind_is_text(storage):
    label_id = await storage.add_label("a", chat_id=1, user_id=1)
    row = await storage.get_label(label_id)
    assert row["kind"] == "text"


async def test_add_label_glaze_kind_is_persisted(storage):
    label_id = await storage.add_label("Лазурит", chat_id=1, user_id=1, kind="glaze")
    row = await storage.get_label(label_id)
    assert row["kind"] == "glaze"


async def test_get_label_missing_id_returns_none(storage):
    assert await storage.get_label(999999) is None


async def test_add_label_returns_distinct_incrementing_ids(storage):
    id1 = await storage.add_label("first", chat_id=1, user_id=1)
    id2 = await storage.add_label("second", chat_id=1, user_id=1)
    assert id1 != id2
    row1 = await storage.get_label(id1)
    row2 = await storage.get_label(id2)
    assert row1["text"] == "first"
    assert row2["text"] == "second"


# ── created_at invariant ─────────────────────────────────────────────────────

async def test_created_at_is_positive_int(storage):
    before = int(time.time())
    label_id = await storage.add_label("ts", chat_id=1, user_id=1)
    row = await storage.get_label(label_id)
    created = row["created_at"]
    # Assert the invariant (an int timestamp in a sane range), not an exact value.
    assert isinstance(created, int)
    assert created > 0
    assert created >= before


# ── close() on an un-init()'d Storage ────────────────────────────────────────

async def test_close_without_init_is_noop():
    s = Storage(":memory:")
    # .init() was never called, so .db is None — close must be a safe no-op.
    assert s.db is None
    await s.close()  # must not raise
