import time
from pathlib import Path

import aiosqlite
from loguru import logger


class Storage:
    """SQLite store of printed labels, so any label can be reprinted later.

    Only the label text is kept (not the PDF) — reprinting re-renders it from
    the current template, which is deterministic.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS labels (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                text       TEXT    NOT NULL,
                chat_id    INTEGER,
                user_id    INTEGER,
                kind       TEXT    NOT NULL DEFAULT 'text',
                created_at INTEGER NOT NULL
            )
            """
        )
        # Migrate older DBs created before the `kind` column existed.
        cur = await self.db.execute("PRAGMA table_info(labels)")
        columns = {row["name"] for row in await cur.fetchall()}
        if "kind" not in columns:
            await self.db.execute(
                "ALTER TABLE labels ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'"
            )
        await self.db.commit()
        logger.info("Label store ready at {}", self.path)

    async def add_label(
        self, text: str, chat_id: int, user_id: int, kind: str = "text"
    ) -> int:
        cur = await self.db.execute(
            "INSERT INTO labels (text, chat_id, user_id, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (text, chat_id, user_id, kind, int(time.time())),
        )
        await self.db.commit()
        if cur.lastrowid is None:
            raise RuntimeError("INSERT did not return a row id")
        return int(cur.lastrowid)

    async def get_label(self, label_id: int) -> dict | None:
        cur = await self.db.execute("SELECT * FROM labels WHERE id = ?", (label_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
