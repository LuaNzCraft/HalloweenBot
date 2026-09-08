from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from .db import DB_PATH


DEFAULT_USER_GUILD_METRICS = {
    "candies_total": 0,
    "trick_coins_total": 0,
    "tricks_applied_total": 0,
    "horror_questions_correct_total": 0,
    "board_laps": 0,
    "xp_bank": 0,
    "baskets_opened_total": 0,
    "mute_used_total": 0,
    "switch_used_total": 0,
}


class UserGuildMetricsRepo:
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
                CREATE TABLE IF NOT EXISTS user_guild_metrics (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    candies_total INTEGER NOT NULL DEFAULT 0,
                    trick_coins_total INTEGER NOT NULL DEFAULT 0,
                    tricks_applied_total INTEGER NOT NULL DEFAULT 0,
                    horror_questions_correct_total INTEGER NOT NULL DEFAULT 0,
                    board_laps INTEGER NOT NULL DEFAULT 0,
                    xp_bank INTEGER NOT NULL DEFAULT 0,
                    baskets_opened_total INTEGER NOT NULL DEFAULT 0,
                    mute_used_total INTEGER NOT NULL DEFAULT 0,
                    switch_used_total INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            await db.commit()
        self._initialized = True

    async def increment_metric(self, guild_id: int, user_id: int, metric: str, amount: int) -> int:
        if metric not in DEFAULT_USER_GUILD_METRICS:
            raise ValueError(f"Unsupported user guild metric: {metric}")
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO user_guild_metrics (guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
            await db.execute(
                f"UPDATE user_guild_metrics SET {metric} = MAX(0, {metric} + ?) WHERE guild_id = ? AND user_id = ?",
                (amount, guild_id, user_id),
            )
            cursor = await db.execute(
                f"SELECT {metric} FROM user_guild_metrics WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            row = await cursor.fetchone()
            await db.commit()
        return int(row[0]) if row else 0

    async def get_top_rows(self, guild_id: int, metric: str, limit: int) -> list[dict[str, Any]]:
        if metric not in DEFAULT_USER_GUILD_METRICS:
            raise ValueError(f"Unsupported user guild metric: {metric}")
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT user_id, {metric} AS value
                FROM user_guild_metrics
                WHERE guild_id = ? AND {metric} > 0
                ORDER BY {metric} DESC, user_id ASC
                LIMIT ?
                """,
                (guild_id, limit),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
