from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from .db import DB_PATH


class PublicEventsRepo:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path

    async def init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS public_event_scheduler (
                    guild_id INTEGER PRIMARY KEY,
                    next_run_at TEXT,
                    next_event_name TEXT,
                    last_public_event_name TEXT,
                    last_announced_at TEXT,
                    last_triggered_at TEXT
                )
                """
            )
            scheduler_columns_cursor = await db.execute("PRAGMA table_info(public_event_scheduler)")
            scheduler_columns = {row[1] for row in await scheduler_columns_cursor.fetchall()}
            if "next_event_name" not in scheduler_columns:
                await db.execute(
                    """
                    ALTER TABLE public_event_scheduler
                    ADD COLUMN next_event_name TEXT
                    """
                )
            if "last_public_event_name" not in scheduler_columns:
                await db.execute(
                    """
                    ALTER TABLE public_event_scheduler
                    ADD COLUMN last_public_event_name TEXT
                    """
                )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS ghostface_runtime (
                    guild_id INTEGER PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 0,
                    channel_id INTEGER,
                    question_id TEXT,
                    question_text TEXT,
                    accepted_answers_json TEXT NOT NULL DEFAULT '[]',
                    answer_match_mode TEXT NOT NULL DEFAULT 'normalized',
                    question_difficulty TEXT NOT NULL DEFAULT 'easy',
                    started_at TEXT,
                    expires_at TEXT,
                    message_id INTEGER,
                    winner_user_id INTEGER,
                    reward_settled_at TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS ghostface_rotation (
                    guild_id INTEGER PRIMARY KEY,
                    used_question_ids_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS spider_runtime (
                    guild_id INTEGER PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    ends_at TEXT,
                    total_spawns INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS spider_spawns (
                    spawn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    emoji_id INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS spider_captures (
                    guild_id INTEGER NOT NULL,
                    spawn_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, spawn_id, user_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS ghostface_hint_uses (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    question_id TEXT NOT NULL,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id, question_id)
                )
                """
            )
            columns_cursor = await db.execute("PRAGMA table_info(ghostface_runtime)")
            columns = {row[1] for row in await columns_cursor.fetchall()}
            if "answer_match_mode" not in columns:
                await db.execute(
                    """
                    ALTER TABLE ghostface_runtime
                    ADD COLUMN answer_match_mode TEXT NOT NULL DEFAULT 'normalized'
                    """
                )
            if "question_id" not in columns:
                await db.execute(
                    """
                    ALTER TABLE ghostface_runtime
                    ADD COLUMN question_id TEXT
                    """
                )
            if "question_difficulty" not in columns:
                await db.execute(
                    """
                    ALTER TABLE ghostface_runtime
                    ADD COLUMN question_difficulty TEXT NOT NULL DEFAULT 'easy'
                    """
                )
            if "reward_settled_at" not in columns:
                await db.execute(
                    """
                    ALTER TABLE ghostface_runtime
                    ADD COLUMN reward_settled_at TEXT
                    """
                )
            await db.commit()

    async def get_state(self, guild_id: int) -> dict[str, Any]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT next_run_at, next_event_name, last_announced_at, last_triggered_at
                , last_public_event_name
                FROM public_event_scheduler
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return {
                "next_run_at": None,
                "next_event_name": None,
                "last_public_event_name": None,
                "last_announced_at": None,
                "last_triggered_at": None,
            }
        return dict(row)

    async def set_state(
        self,
        guild_id: int,
        *,
        next_run_at: str | None = None,
        next_event_name: str | None = None,
        last_public_event_name: str | None = None,
        last_announced_at: str | None = None,
        last_triggered_at: str | None = None,
    ) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO public_event_scheduler (
                    guild_id, next_run_at, next_event_name, last_public_event_name, last_announced_at, last_triggered_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    next_run_at = excluded.next_run_at,
                    next_event_name = excluded.next_event_name,
                    last_public_event_name = excluded.last_public_event_name,
                    last_announced_at = excluded.last_announced_at,
                    last_triggered_at = excluded.last_triggered_at
                """,
                (guild_id, next_run_at, next_event_name, last_public_event_name, last_announced_at, last_triggered_at),
            )
            await db.commit()

    async def get_ghostface_state(self, guild_id: int) -> dict[str, Any]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT active, channel_id, question_id, question_text, accepted_answers_json,
                       answer_match_mode, question_difficulty, started_at, expires_at,
                       message_id, winner_user_id, reward_settled_at
                FROM ghostface_runtime
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return {
                "active": False,
                "channel_id": None,
                "question_id": None,
                "question_text": None,
                "accepted_answers": [],
                "answer_match_mode": "normalized",
                "question_difficulty": "easy",
                "started_at": None,
                "expires_at": None,
                "message_id": None,
                "winner_user_id": None,
                "reward_settled_at": None,
            }
        state = dict(row)
        state["active"] = bool(state["active"])
        import json
        state["accepted_answers"] = json.loads(state.pop("accepted_answers_json") or "[]")
        return state

    async def set_ghostface_state(
        self,
        guild_id: int,
        *,
        active: bool,
        channel_id: int | None,
        question_text: str | None,
        accepted_answers: list[str],
        answer_match_mode: str,
        question_difficulty: str,
        started_at: str | None,
        expires_at: str | None,
        message_id: int | None,
        winner_user_id: int | None,
        question_id: str | None = None,
        reward_settled_at: str | None = None,
    ) -> None:
        await self.init_db()
        import json
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO ghostface_runtime (
                    guild_id, active, channel_id, question_id, question_text,
                    accepted_answers_json, answer_match_mode, question_difficulty,
                    started_at, expires_at, message_id, winner_user_id, reward_settled_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    active = excluded.active,
                    channel_id = excluded.channel_id,
                    question_id = excluded.question_id,
                    question_text = excluded.question_text,
                    accepted_answers_json = excluded.accepted_answers_json,
                    answer_match_mode = excluded.answer_match_mode,
                    question_difficulty = excluded.question_difficulty,
                    started_at = excluded.started_at,
                    expires_at = excluded.expires_at,
                    message_id = excluded.message_id,
                    winner_user_id = excluded.winner_user_id,
                    reward_settled_at = excluded.reward_settled_at
                """,
                (
                    guild_id,
                    int(active),
                    channel_id,
                    question_id,
                    question_text,
                    json.dumps(accepted_answers, ensure_ascii=False),
                    answer_match_mode,
                    question_difficulty,
                    started_at,
                    expires_at,
                    message_id,
                    winner_user_id,
                    reward_settled_at,
                ),
            )
            await db.commit()

    async def list_pending_ghostface_rewards(self) -> list[dict[str, Any]]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT guild_id, active, channel_id, question_id, question_text,
                       answer_match_mode, question_difficulty, started_at, expires_at,
                       message_id, winner_user_id, reward_settled_at
                FROM ghostface_runtime
                WHERE winner_user_id IS NOT NULL AND reward_settled_at IS NULL
                """
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_active_ghostface_states(self) -> list[dict[str, Any]]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT guild_id, expires_at
                FROM ghostface_runtime
                WHERE active = 1
                """
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def claim_ghostface_winner(self, guild_id: int, winner_user_id: int) -> bool:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE ghostface_runtime
                SET active = 0,
                    winner_user_id = ?,
                    reward_settled_at = NULL
                WHERE guild_id = ? AND active = 1
                """,
                (winner_user_id, guild_id),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def mark_ghostface_reward_settled(
        self,
        guild_id: int,
        winner_user_id: int,
        settled_at: str,
    ) -> bool:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE ghostface_runtime
                SET reward_settled_at = ?
                WHERE guild_id = ?
                  AND winner_user_id = ?
                  AND reward_settled_at IS NULL
                """,
                (settled_at, guild_id, winner_user_id),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def reset_ghostface_state(self, guild_id: int) -> None:
        await self.set_ghostface_state(
            guild_id,
            active=False,
            channel_id=None,
            question_id=None,
            question_text=None,
            accepted_answers=[],
            answer_match_mode="normalized",
            question_difficulty="easy",
            started_at=None,
            expires_at=None,
            message_id=None,
            winner_user_id=None,
        )
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM ghostface_hint_uses WHERE guild_id = ?", (guild_id,))
            await db.commit()

    async def increment_ghostface_hint_use(self, guild_id: int, user_id: int, question_id: str) -> int:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO ghostface_hint_uses (guild_id, user_id, question_id, use_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(guild_id, user_id, question_id) DO UPDATE SET
                    use_count = use_count + 1
                """,
                (guild_id, user_id, question_id),
            )
            cursor = await db.execute(
                """
                SELECT use_count
                FROM ghostface_hint_uses
                WHERE guild_id = ? AND user_id = ? AND question_id = ?
                """,
                (guild_id, user_id, question_id),
            )
            row = await cursor.fetchone()
            await db.commit()
        return int(row[0]) if row else 1

    async def get_spider_state(self, guild_id: int) -> dict[str, Any]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT active, started_at, ends_at, total_spawns
                FROM spider_runtime
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return {"active": False, "started_at": None, "ends_at": None, "total_spawns": 0}
        state = dict(row)
        state["active"] = bool(state["active"])
        return state

    async def set_spider_state(
        self,
        guild_id: int,
        *,
        active: bool,
        started_at: str | None,
        ends_at: str | None,
        total_spawns: int,
    ) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO spider_runtime (guild_id, active, started_at, ends_at, total_spawns)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    active = excluded.active,
                    started_at = excluded.started_at,
                    ends_at = excluded.ends_at,
                    total_spawns = excluded.total_spawns
                """,
                (
                    guild_id,
                    int(active),
                    started_at,
                    ends_at,
                    int(total_spawns),
                ),
            )
            await db.commit()

    async def reset_spider_state(self, guild_id: int) -> None:
        await self.set_spider_state(
            guild_id,
            active=False,
            started_at=None,
            ends_at=None,
            total_spawns=0,
        )
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE spider_spawns SET active = 0 WHERE guild_id = ?", (guild_id,))
            await db.execute("DELETE FROM spider_captures WHERE guild_id = ?", (guild_id,))
            await db.commit()

    async def increment_spider_spawns(self, guild_id: int) -> int:
        state = await self.get_spider_state(guild_id)
        total_spawns = int(state.get("total_spawns") or 0) + 1
        await self.set_spider_state(
            guild_id,
            active=bool(state.get("active")),
            started_at=state.get("started_at"),
            ends_at=state.get("ends_at"),
            total_spawns=total_spawns,
        )
        return total_spawns

    async def create_spider_spawn(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        emoji_id: int,
        created_at: str,
        expires_at: str,
    ) -> int:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO spider_spawns (
                    guild_id, channel_id, message_id, emoji_id, active, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (guild_id, channel_id, message_id, emoji_id, created_at, expires_at),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def close_spider_spawn(self, guild_id: int, spawn_id: int) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE spider_spawns SET active = 0 WHERE guild_id = ? AND spawn_id = ?",
                (guild_id, spawn_id),
            )
            await db.commit()

    async def get_active_spider_spawn(self, guild_id: int, message_id: int, emoji_id: int) -> dict[str, Any] | None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT spawn_id, guild_id, channel_id, message_id, emoji_id, active, created_at, expires_at
                FROM spider_spawns
                WHERE guild_id = ? AND message_id = ? AND emoji_id = ? AND active = 1
                ORDER BY spawn_id DESC
                LIMIT 1
                """,
                (guild_id, message_id, emoji_id),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def add_spider_capture(self, guild_id: int, spawn_id: int, user_id: int, captured_at: str) -> bool:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO spider_captures (guild_id, spawn_id, user_id, captured_at)
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, spawn_id, user_id, captured_at),
            )
            await db.commit()
            return int(cursor.rowcount) > 0

    async def count_spider_captures_for_user(self, guild_id: int, user_id: int) -> int:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM spider_captures WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def list_spider_capture_counts(self, guild_id: int) -> list[dict[str, int]]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT user_id, COUNT(*) AS captures
                FROM spider_captures
                WHERE guild_id = ?
                GROUP BY user_id
                ORDER BY captures DESC, user_id ASC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
        return [{"user_id": int(user_id), "captures": int(captures)} for user_id, captures in rows]

    async def get_ghostface_used_question_ids(self, guild_id: int) -> list[str]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT used_question_ids_json
                FROM ghostface_rotation
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
        if row is None or not row[0]:
            return []
        import json
        return [str(value) for value in json.loads(row[0])]

    async def set_ghostface_used_question_ids(self, guild_id: int, question_ids: list[str]) -> None:
        await self.init_db()
        import json
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO ghostface_rotation (guild_id, used_question_ids_json)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    used_question_ids_json = excluded.used_question_ids_json
                """,
                (guild_id, json.dumps(question_ids, ensure_ascii=False)),
            )
            await db.commit()
