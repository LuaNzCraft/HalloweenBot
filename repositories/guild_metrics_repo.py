from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from .db import DB_PATH


DEFAULT_METRICS = {
    "candies_total": 0,
    "profile_requests_resolved_total": 0,
    "trick_coins_total": 0,
    "tricks_applied_total": 0,
    "horror_questions_correct_total": 0,
    "board_laps": 0,
    "xp_bank": 0,
}


class GuildMetricsRepo:
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
                CREATE TABLE IF NOT EXISTS guild_metrics (
                    guild_id INTEGER PRIMARY KEY,
                    candies_total INTEGER NOT NULL DEFAULT 0,
                    profile_requests_resolved_total INTEGER NOT NULL DEFAULT 0,
                    trick_coins_total INTEGER NOT NULL DEFAULT 0,
                    tricks_applied_total INTEGER NOT NULL DEFAULT 0,
                    horror_questions_correct_total INTEGER NOT NULL DEFAULT 0,
                    board_laps INTEGER NOT NULL DEFAULT 0,
                    xp_bank INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await self._ensure_column(db, "guild_metrics", "profile_requests_resolved_total", "INTEGER NOT NULL DEFAULT 0")
            await db.commit()
        self._initialized = True

    async def get_metrics(self, guild_id: int) -> dict[str, Any]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT candies_total, trick_coins_total, tricks_applied_total,
                       horror_questions_correct_total, board_laps, xp_bank,
                       profile_requests_resolved_total
                FROM guild_metrics
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return dict(DEFAULT_METRICS)
        return {key: int(value) for key, value in dict(row).items()}

    async def increment_metric(self, guild_id: int, metric: str, amount: int) -> int:
        if metric not in DEFAULT_METRICS:
            raise ValueError(f"Unsupported guild metric: {metric}")
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO guild_metrics (guild_id) VALUES (?)", (guild_id,))
            await db.execute(
                f"UPDATE guild_metrics SET {metric} = MAX(0, {metric} + ?) WHERE guild_id = ?",
                (amount, guild_id),
            )
            cursor = await db.execute(f"SELECT {metric} FROM guild_metrics WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()
            await db.commit()
        return int(row[0]) if row else 0

    async def _ensure_column(
        self,
        db: aiosqlite.Connection,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        cursor = await db.execute(f"PRAGMA table_info({table_name})")
        rows = await cursor.fetchall()
        existing_columns = {str(row[1]) for row in rows}
        if column_name not in existing_columns:
            await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
