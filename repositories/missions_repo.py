from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from .db import DB_PATH


MISSION_REWARD_FIELDS = {"candies", "trick_coins", "dice_count", "xp_bank"}
MISSION_REWARD_METRICS = {
    "candies": "candies_total",
    "trick_coins": "trick_coins_total",
    "xp_bank": "xp_bank",
}


class MissionsRepo:
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
                CREATE TABLE IF NOT EXISTS user_mission_state (
                    user_id INTEGER NOT NULL,
                    mission_type TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    mission_id TEXT NOT NULL,
                    baseline_json TEXT NOT NULL DEFAULT '{}',
                    completed_at TEXT,
                    reward_granted_at TEXT,
                    PRIMARY KEY (user_id, mission_type, period_key)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_mission_state (
                    guild_id INTEGER NOT NULL,
                    mission_type TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    mission_id TEXT NOT NULL,
                    baseline_json TEXT NOT NULL DEFAULT '{}',
                    completed_at TEXT,
                    PRIMARY KEY (guild_id, mission_type, period_key)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_mission_rewards (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    mission_type TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    granted_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id, mission_type, period_key)
                )
                """
            )
            await self._ensure_column(db, "user_mission_state", "reward_granted_at", "TEXT")
            await db.commit()
        self._initialized = True

    async def get_user_state(self, user_id: int, mission_type: str, period_key: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT mission_id, baseline_json, completed_at, reward_granted_at
                FROM user_mission_state
                WHERE user_id = ? AND mission_type = ? AND period_key = ?
                """,
                (user_id, mission_type, period_key),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        state = dict(row)
        state["baseline"] = json.loads(state.pop("baseline_json") or "{}")
        return state

    async def upsert_user_state(
        self,
        user_id: int,
        mission_type: str,
        period_key: str,
        mission_id: str,
        baseline: dict[str, int],
        completed_at: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO user_mission_state (
                    user_id, mission_type, period_key, mission_id, baseline_json, completed_at, reward_granted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(user_id, mission_type, period_key) DO UPDATE SET
                    mission_id = excluded.mission_id,
                    baseline_json = excluded.baseline_json,
                    completed_at = COALESCE(user_mission_state.completed_at, excluded.completed_at)
                """,
                (user_id, mission_type, period_key, mission_id, json.dumps(baseline), completed_at),
            )
            await db.commit()

    async def mark_user_completed(self, user_id: int, mission_type: str, period_key: str, completed_at: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE user_mission_state
                SET completed_at = COALESCE(completed_at, ?)
                WHERE user_id = ? AND mission_type = ? AND period_key = ?
                """,
                (completed_at, user_id, mission_type, period_key),
            )
            await db.commit()

    async def grant_user_reward_once(
        self,
        user_id: int,
        mission_type: str,
        period_key: str,
        rewards: dict[str, int],
        guild_id: int | None,
        granted_at: str,
    ) -> bool:
        normalized_rewards = self._normalize_rewards(rewards)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE user_mission_state
                SET reward_granted_at = ?
                WHERE user_id = ?
                  AND mission_type = ?
                  AND period_key = ?
                  AND reward_granted_at IS NULL
                """,
                (granted_at, user_id, mission_type, period_key),
            )
            if not cursor.rowcount:
                await db.rollback()
                return False
            try:
                await self._apply_reward_rows(db, user_id, guild_id, normalized_rewards)
            except BaseException:
                await db.rollback()
                raise
            await db.commit()
        return True

    async def get_guild_state(self, guild_id: int, mission_type: str, period_key: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT mission_id, baseline_json, completed_at
                FROM guild_mission_state
                WHERE guild_id = ? AND mission_type = ? AND period_key = ?
                """,
                (guild_id, mission_type, period_key),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        state = dict(row)
        state["baseline"] = json.loads(state.pop("baseline_json") or "{}")
        return state

    async def upsert_guild_state(
        self,
        guild_id: int,
        mission_type: str,
        period_key: str,
        mission_id: str,
        baseline: dict[str, int],
        completed_at: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO guild_mission_state (
                    guild_id, mission_type, period_key, mission_id, baseline_json, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, mission_type, period_key) DO UPDATE SET
                    mission_id = excluded.mission_id,
                    baseline_json = excluded.baseline_json,
                    completed_at = COALESCE(guild_mission_state.completed_at, excluded.completed_at)
                """,
                (guild_id, mission_type, period_key, mission_id, json.dumps(baseline), completed_at),
            )
            await db.commit()

    async def mark_guild_completed(self, guild_id: int, mission_type: str, period_key: str, completed_at: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE guild_mission_state
                SET completed_at = COALESCE(completed_at, ?)
                WHERE guild_id = ? AND mission_type = ? AND period_key = ?
                """,
                (completed_at, guild_id, mission_type, period_key),
            )
            await db.commit()

    async def has_guild_reward_granted(self, guild_id: int, user_id: int, mission_type: str, period_key: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT 1
                FROM guild_mission_rewards
                WHERE guild_id = ? AND user_id = ? AND mission_type = ? AND period_key = ?
                """,
                (guild_id, user_id, mission_type, period_key),
            )
            row = await cursor.fetchone()
        return row is not None

    async def grant_guild_reward_once(
        self,
        guild_id: int,
        user_id: int,
        mission_type: str,
        period_key: str,
        rewards: dict[str, int],
        granted_at: str,
    ) -> bool:
        normalized_rewards = self._normalize_rewards(rewards)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO guild_mission_rewards (
                    guild_id, user_id, mission_type, period_key, granted_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, mission_type, period_key, granted_at),
            )
            if not cursor.rowcount:
                await db.rollback()
                return False
            try:
                await self._apply_reward_rows(db, user_id, guild_id, normalized_rewards)
            except BaseException:
                await db.rollback()
                raise
            await db.commit()
        return True

    def _normalize_rewards(self, rewards: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for reward_type, raw_amount in rewards.items():
            if reward_type not in MISSION_REWARD_FIELDS:
                raise ValueError(f"Unsupported mission reward: {reward_type}")
            amount = int(raw_amount)
            if amount > 0:
                normalized[reward_type] = amount
        return normalized

    async def _apply_reward_rows(
        self,
        db: aiosqlite.Connection,
        user_id: int,
        guild_id: int | None,
        rewards: dict[str, int],
    ) -> None:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        for reward_type, amount in rewards.items():
            if reward_type == "candies":
                await db.execute(
                    """
                    UPDATE users
                    SET candies = candies + ?, candies_total = candies_total + ?
                    WHERE user_id = ?
                    """,
                    (amount, amount, user_id),
                )
            elif reward_type == "trick_coins":
                await db.execute(
                    """
                    UPDATE users
                    SET trick_coins = trick_coins + ?, trick_coins_total = trick_coins_total + ?
                    WHERE user_id = ?
                    """,
                    (amount, amount, user_id),
                )
            else:
                await db.execute(
                    f"UPDATE users SET {reward_type} = {reward_type} + ? WHERE user_id = ?",
                    (amount, user_id),
                )

        if guild_id is None:
            return

        await db.execute("INSERT OR IGNORE INTO guild_metrics (guild_id) VALUES (?)", (guild_id,))
        await db.execute(
            "INSERT OR IGNORE INTO user_guild_metrics (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        for reward_type, amount in rewards.items():
            metric = MISSION_REWARD_METRICS.get(reward_type)
            if metric is None:
                continue
            await db.execute(
                f"UPDATE guild_metrics SET {metric} = {metric} + ? WHERE guild_id = ?",
                (amount, guild_id),
            )
            await db.execute(
                f"""
                UPDATE user_guild_metrics
                SET {metric} = {metric} + ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (amount, guild_id, user_id),
            )

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
