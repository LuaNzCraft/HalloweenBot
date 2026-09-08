from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from .db import DB_PATH


class ObserverArchivesRepo:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path

    async def init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS observer_archives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT NOT NULL UNIQUE,
                    guild_id INTEGER NOT NULL,
                    source_channel_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    author_display_name TEXT NOT NULL,
                    author_username TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    avatar_blob BLOB,
                    hidden_image_path TEXT NOT NULL,
                    revealed_image_path TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (guild_id, source_message_id)
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_observer_archives_active
                ON observer_archives (guild_id, active, id)
                """
            )
            await db.commit()

    async def create_archive(self, archive: dict[str, Any]) -> dict[str, Any]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO observer_archives (
                    public_id, guild_id, source_channel_id, source_message_id,
                    author_id, author_display_name, author_username, message_text,
                    aliases_json, avatar_blob, hidden_image_path, revealed_image_path,
                    created_by, created_at, active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    str(archive["public_id"]),
                    int(archive["guild_id"]),
                    int(archive["source_channel_id"]),
                    int(archive["source_message_id"]),
                    int(archive["author_id"]),
                    str(archive["author_display_name"]),
                    str(archive["author_username"]),
                    str(archive["message_text"]),
                    json.dumps(list(archive["aliases"]), ensure_ascii=False),
                    archive.get("avatar_blob"),
                    str(archive["hidden_image_path"]),
                    str(archive["revealed_image_path"]),
                    int(archive["created_by"]),
                    str(archive["created_at"]),
                ),
            )
            await db.commit()
            archive_id = int(cursor.lastrowid)
        created = await self.get_by_public_id(int(archive["guild_id"]), str(archive["public_id"]))
        if created is None:
            raise RuntimeError(f"observer archive {archive_id} was not persisted")
        return created

    async def get_by_public_id(self, guild_id: int, public_id: str) -> dict[str, Any] | None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM observer_archives WHERE guild_id = ? AND public_id = ?",
                (guild_id, public_id),
            )
            row = await cursor.fetchone()
        return self._row_to_archive(row) if row is not None else None

    async def get_by_question_id(self, guild_id: int, question_id: str | None) -> dict[str, Any] | None:
        prefix = "archive_"
        value = str(question_id or "")
        if not value.startswith(prefix):
            return None
        return await self.get_by_public_id(guild_id, value[len(prefix):])

    async def get_by_source_message(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM observer_archives WHERE guild_id = ? AND source_message_id = ?",
                (guild_id, message_id),
            )
            row = await cursor.fetchone()
        return self._row_to_archive(row) if row is not None else None

    async def list_active(self, guild_id: int) -> list[dict[str, Any]]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM observer_archives
                WHERE guild_id = ? AND active = 1
                ORDER BY id ASC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_archive(row) for row in rows]

    async def list_all(self, guild_id: int, *, limit: int = 25) -> list[dict[str, Any]]:
        await self.init_db()
        safe_limit = min(max(1, int(limit)), 100)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM observer_archives
                WHERE guild_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (guild_id, safe_limit),
            )
            rows = await cursor.fetchall()
        return [self._row_to_archive(row) for row in rows]

    async def set_active(self, guild_id: int, public_id: str, active: bool) -> bool:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE observer_archives
                SET active = ?
                WHERE guild_id = ? AND public_id = ?
                """,
                (int(active), guild_id, public_id),
            )
            await db.commit()
        return bool(cursor.rowcount)

    @staticmethod
    def _row_to_archive(row: aiosqlite.Row) -> dict[str, Any]:
        archive = dict(row)
        archive["active"] = bool(archive["active"])
        archive["aliases"] = json.loads(archive.pop("aliases_json") or "[]")
        return archive
