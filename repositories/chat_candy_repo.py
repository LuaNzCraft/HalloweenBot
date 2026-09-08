from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

import aiosqlite

from .db import DB_PATH


class ChatCandyRepo:
    """Persistencia das aparicoes de doces no chat.

    A tentativa e o encerramento do spawn vencedor acontecem na mesma
    transacao. Isso garante uma tentativa por jogador e apenas um vencedor,
    mesmo quando varias reacoes chegam praticamente juntas.
    """

    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self._initialized = False

    async def init_db(self) -> None:
        if self._initialized:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_candy_guild_state (
                    guild_id INTEGER PRIMARY KEY,
                    last_spawn_at TEXT,
                    spawn_day TEXT,
                    daily_spawns INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_candy_spawns (
                    spawn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL UNIQUE,
                    correct_emoji_id INTEGER NOT NULL,
                    option_emoji_ids_json TEXT NOT NULL,
                    reward_candies INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1,
                    winner_user_id INTEGER,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    closed_at TEXT,
                    reward_settled_at TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_candy_attempts (
                    spawn_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    emoji_id INTEGER NOT NULL,
                    correct INTEGER NOT NULL DEFAULT 0,
                    attempted_at TEXT NOT NULL,
                    PRIMARY KEY (spawn_id, user_id),
                    FOREIGN KEY (spawn_id) REFERENCES chat_candy_spawns(spawn_id)
                )
                """
            )
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS chat_candy_one_active_per_guild
                ON chat_candy_spawns(guild_id)
                WHERE active = 1
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS chat_candy_message_lookup
                ON chat_candy_spawns(guild_id, message_id)
                """
            )
            await self._ensure_column(
                db,
                "chat_candy_spawns",
                "reward_candies",
                "INTEGER NOT NULL DEFAULT 1",
            )
            await self._ensure_column(db, "chat_candy_spawns", "reward_settled_at", "TEXT")
            await db.commit()
        self._initialized = True

    async def can_spawn(
        self,
        guild_id: int,
        *,
        now: datetime,
        spawn_day: str,
        cooldown_seconds: float,
        daily_cap: int,
    ) -> bool:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            active_cursor = await db.execute(
                "SELECT 1 FROM chat_candy_spawns WHERE guild_id = ? AND active = 1 LIMIT 1",
                (guild_id,),
            )
            if await active_cursor.fetchone() is not None:
                return False

            state_cursor = await db.execute(
                "SELECT last_spawn_at, spawn_day, daily_spawns FROM chat_candy_guild_state WHERE guild_id = ?",
                (guild_id,),
            )
            row = await state_cursor.fetchone()

        if row is None:
            return True
        last_spawn_at, stored_day, daily_spawns = row
        if str(stored_day or "") == spawn_day and daily_cap > 0 and int(daily_spawns or 0) >= daily_cap:
            return False
        if last_spawn_at and cooldown_seconds > 0:
            try:
                elapsed = (now - datetime.fromisoformat(str(last_spawn_at))).total_seconds()
            except (TypeError, ValueError):
                elapsed = cooldown_seconds
            if elapsed < cooldown_seconds:
                return False
        return True

    async def create_spawn(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        correct_emoji_id: int,
        option_emoji_ids: list[int],
        reward_candies: int,
        *,
        created_at: datetime,
        expires_at: datetime,
        spawn_day: str,
        cooldown_seconds: float,
        daily_cap: int,
    ) -> int | None:
        await self.init_db()
        now_iso = created_at.isoformat()
        options_json = json.dumps(option_emoji_ids)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            active_cursor = await db.execute(
                "SELECT 1 FROM chat_candy_spawns WHERE guild_id = ? AND active = 1 LIMIT 1",
                (guild_id,),
            )
            if await active_cursor.fetchone() is not None:
                await db.rollback()
                return None

            state_cursor = await db.execute(
                "SELECT last_spawn_at, spawn_day, daily_spawns FROM chat_candy_guild_state WHERE guild_id = ?",
                (guild_id,),
            )
            state = await state_cursor.fetchone()
            current_daily = 0
            if state is not None:
                last_spawn_at, stored_day, stored_daily = state
                current_daily = int(stored_daily or 0) if str(stored_day or "") == spawn_day else 0
                if daily_cap > 0 and current_daily >= daily_cap:
                    await db.rollback()
                    return None
                if last_spawn_at and cooldown_seconds > 0:
                    try:
                        elapsed = (created_at - datetime.fromisoformat(str(last_spawn_at))).total_seconds()
                    except (TypeError, ValueError):
                        elapsed = cooldown_seconds
                    if elapsed < cooldown_seconds:
                        await db.rollback()
                        return None

            cursor = await db.execute(
                """
                INSERT INTO chat_candy_spawns (
                    guild_id, channel_id, message_id, correct_emoji_id,
                    option_emoji_ids_json, reward_candies, active, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    message_id,
                    correct_emoji_id,
                    options_json,
                    reward_candies,
                    now_iso,
                    expires_at.isoformat(),
                ),
            )
            await db.execute(
                """
                INSERT INTO chat_candy_guild_state (guild_id, last_spawn_at, spawn_day, daily_spawns)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    last_spawn_at = excluded.last_spawn_at,
                    spawn_day = excluded.spawn_day,
                    daily_spawns = excluded.daily_spawns
                """,
                (guild_id, now_iso, spawn_day, current_daily + 1),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def submit_attempt(
        self,
        guild_id: int,
        message_id: int,
        user_id: int,
        emoji_id: int,
        *,
        attempted_at: datetime,
    ) -> tuple[str, dict[str, Any] | None]:
        """Registra uma escolha e retorna seu resultado.

        Status possiveis: ``no_spawn``, ``expired``, ``invalid_option``,
        ``already_attempted``, ``incorrect`` e ``won``.
        """

        await self.init_db()
        attempted_iso = attempted_at.isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT spawn_id, guild_id, channel_id, message_id, correct_emoji_id,
                       option_emoji_ids_json, reward_candies, active, winner_user_id,
                       created_at, expires_at
                FROM chat_candy_spawns
                WHERE guild_id = ? AND message_id = ?
                LIMIT 1
                """,
                (guild_id, message_id),
            )
            row = await cursor.fetchone()
            if row is None or not bool(row["active"]):
                await db.rollback()
                return "no_spawn", None

            spawn = dict(row)
            if str(spawn["expires_at"]) <= attempted_iso:
                await db.execute(
                    "UPDATE chat_candy_spawns SET active = 0, closed_at = ? WHERE spawn_id = ? AND active = 1",
                    (attempted_iso, int(spawn["spawn_id"])),
                )
                await db.commit()
                return "expired", spawn

            try:
                option_ids = {int(value) for value in json.loads(spawn["option_emoji_ids_json"] or "[]")}
            except (TypeError, ValueError, json.JSONDecodeError):
                option_ids = set()
            if emoji_id not in option_ids:
                await db.rollback()
                return "invalid_option", spawn

            is_correct = emoji_id == int(spawn["correct_emoji_id"])
            attempt_cursor = await db.execute(
                """
                INSERT OR IGNORE INTO chat_candy_attempts (
                    spawn_id, user_id, emoji_id, correct, attempted_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (int(spawn["spawn_id"]), user_id, emoji_id, int(is_correct), attempted_iso),
            )
            if int(attempt_cursor.rowcount) <= 0:
                await db.rollback()
                return "already_attempted", spawn

            if not is_correct:
                await db.commit()
                return "incorrect", spawn

            claim_cursor = await db.execute(
                """
                UPDATE chat_candy_spawns
                SET active = 0, winner_user_id = ?, closed_at = ?
                WHERE spawn_id = ? AND active = 1
                """,
                (user_id, attempted_iso, int(spawn["spawn_id"])),
            )
            if int(claim_cursor.rowcount) <= 0:
                await db.rollback()
                return "no_spawn", spawn
            await db.commit()
            spawn["winner_user_id"] = user_id
            return "won", spawn

    async def close_spawn(self, spawn_id: int, *, closed_at: datetime) -> bool:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE chat_candy_spawns
                SET active = 0, closed_at = ?
                WHERE spawn_id = ? AND active = 1
                """,
                (closed_at.isoformat(), spawn_id),
            )
            await db.commit()
            return int(cursor.rowcount) > 0

    async def list_pending_reward_spawns(self) -> list[dict[str, Any]]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT spawn_id, guild_id, channel_id, message_id, reward_candies,
                       winner_user_id, reward_settled_at
                FROM chat_candy_spawns
                WHERE winner_user_id IS NOT NULL AND reward_settled_at IS NULL
                ORDER BY spawn_id ASC
                """
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_reward_settled(self, spawn_id: int, winner_user_id: int, settled_at: datetime) -> bool:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE chat_candy_spawns
                SET reward_settled_at = ?
                WHERE spawn_id = ?
                  AND winner_user_id = ?
                  AND reward_settled_at IS NULL
                """,
                (settled_at.isoformat(), spawn_id, winner_user_id),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def list_active_spawns(self) -> list[dict[str, Any]]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT spawn_id, guild_id, channel_id, message_id, correct_emoji_id,
                       option_emoji_ids_json, reward_candies, created_at, expires_at
                FROM chat_candy_spawns
                WHERE active = 1
                ORDER BY spawn_id ASC
                """
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_active_spawn_for_guild(self, guild_id: int) -> dict[str, Any] | None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT spawn_id, guild_id, channel_id, message_id, correct_emoji_id,
                       option_emoji_ids_json, reward_candies, created_at, expires_at
                FROM chat_candy_spawns
                WHERE guild_id = ? AND active = 1
                LIMIT 1
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_daily_spawns(self, guild_id: int, spawn_day: str) -> int:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT spawn_day, daily_spawns FROM chat_candy_guild_state WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
        if row is None or str(row[0] or "") != spawn_day:
            return 0
        return int(row[1] or 0)

    async def _ensure_column(
        self,
        db: aiosqlite.Connection,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        cursor = await db.execute(f"PRAGMA table_info({table_name})")
        existing = {str(row[1]) for row in await cursor.fetchall()}
        if column_name not in existing:
            await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
