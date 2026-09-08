from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite

from .db import DB_PATH


class JulgamentoRepo:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._runtime_locks: dict[int, asyncio.Lock] = {}

    async def init_db(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS julgamento_runtime (
                        guild_id INTEGER PRIMARY KEY,
                        active INTEGER NOT NULL DEFAULT 0,
                        phase TEXT NOT NULL DEFAULT 'idle',
                        channel_id INTEGER,
                        thread_id INTEGER,
                        announcement_message_id INTEGER,
                        signup_message_channel_id INTEGER,
                        signup_message_id INTEGER,
                        round_number INTEGER NOT NULL DEFAULT 0,
                        current_player_id INTEGER,
                        current_question_id TEXT,
                        current_question_text TEXT,
                        current_answers_json TEXT NOT NULL DEFAULT '[]',
                        current_correct_order_json TEXT NOT NULL DEFAULT '[]',
                        current_match_mode TEXT,
                        answer_deadline_at TEXT,
                        started_at TEXT,
                        signup_ends_at TEXT,
                        spectator_ends_at TEXT,
                        winner_user_id INTEGER
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS julgamento_participants (
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        joined_at TEXT,
                        PRIMARY KEY (guild_id, user_id)
                    )
                    """
                )
                cursor = await db.execute("PRAGMA table_info(julgamento_runtime)")
                columns = {row[1] for row in await cursor.fetchall()}
                required_columns = {
                    "announcement_message_id": "INTEGER",
                    "signup_message_channel_id": "INTEGER",
                    "signup_message_id": "INTEGER",
                    "round_number": "INTEGER NOT NULL DEFAULT 0",
                    "current_player_id": "INTEGER",
                    "current_question_id": "TEXT",
                    "current_question_text": "TEXT",
                    "current_answers_json": "TEXT NOT NULL DEFAULT '[]'",
                    "current_correct_order_json": "TEXT NOT NULL DEFAULT '[]'",
                    "current_match_mode": "TEXT",
                    "answer_deadline_at": "TEXT",
                    "started_at": "TEXT",
                    "signup_ends_at": "TEXT",
                    "spectator_ends_at": "TEXT",
                    "winner_user_id": "INTEGER",
                }
                for column_name, definition in required_columns.items():
                    if column_name not in columns:
                        await db.execute(f"ALTER TABLE julgamento_runtime ADD COLUMN {column_name} {definition}")
                await db.commit()
            self._initialized = True

    def _get_runtime_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._runtime_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._runtime_locks[guild_id] = lock
        return lock

    async def get_runtime(self, guild_id: int) -> dict[str, Any]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT active, phase, channel_id, thread_id, announcement_message_id,
                       signup_message_channel_id, signup_message_id, round_number,
                       current_player_id, current_question_id, current_question_text, current_answers_json,
                       current_correct_order_json, current_match_mode, answer_deadline_at, started_at,
                       signup_ends_at, spectator_ends_at, winner_user_id
                FROM julgamento_runtime
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return {
                "active": False,
                "phase": "idle",
                "channel_id": None,
                "thread_id": None,
                "announcement_message_id": None,
                "signup_message_channel_id": None,
                "signup_message_id": None,
                "round_number": 0,
                "current_player_id": None,
                "current_question_id": None,
                "current_question_text": None,
                "current_answers": [],
                "current_correct_order": [],
                "current_match_mode": None,
                "answer_deadline_at": None,
                "started_at": None,
                "signup_ends_at": None,
                "spectator_ends_at": None,
                "winner_user_id": None,
            }
        state = dict(row)
        state["active"] = bool(state["active"])
        state["current_answers"] = json.loads(state.pop("current_answers_json") or "[]")
        state["current_correct_order"] = json.loads(state.pop("current_correct_order_json") or "[]")
        return state

    async def set_runtime(self, guild_id: int, **fields: Any) -> None:
        await self.init_db()
        async with self._get_runtime_lock(guild_id):
            current = await self.get_runtime(guild_id)
            current.update(fields)
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    """
                    INSERT INTO julgamento_runtime (
                        guild_id, active, phase, channel_id, thread_id, announcement_message_id,
                        signup_message_channel_id, signup_message_id, round_number, current_player_id,
                        current_question_id, current_question_text, current_answers_json, current_correct_order_json, current_match_mode, answer_deadline_at, started_at, signup_ends_at,
                        spectator_ends_at, winner_user_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        active = excluded.active,
                        phase = excluded.phase,
                        channel_id = excluded.channel_id,
                        thread_id = excluded.thread_id,
                        announcement_message_id = excluded.announcement_message_id,
                        signup_message_channel_id = excluded.signup_message_channel_id,
                        signup_message_id = excluded.signup_message_id,
                        round_number = excluded.round_number,
                        current_player_id = excluded.current_player_id,
                        current_question_id = excluded.current_question_id,
                        current_question_text = excluded.current_question_text,
                        current_answers_json = excluded.current_answers_json,
                        current_correct_order_json = excluded.current_correct_order_json,
                        current_match_mode = excluded.current_match_mode,
                        answer_deadline_at = excluded.answer_deadline_at,
                        started_at = excluded.started_at,
                        signup_ends_at = excluded.signup_ends_at,
                        spectator_ends_at = excluded.spectator_ends_at,
                        winner_user_id = excluded.winner_user_id
                    """,
                    (
                        guild_id,
                        int(bool(current["active"])),
                        current["phase"],
                        current["channel_id"],
                        current["thread_id"],
                        current["announcement_message_id"],
                        current["signup_message_channel_id"],
                        current["signup_message_id"],
                        current["round_number"],
                        current["current_player_id"],
                        current["current_question_id"],
                        current["current_question_text"],
                        json.dumps(current.get("current_answers", []), ensure_ascii=False),
                        json.dumps(current.get("current_correct_order", []), ensure_ascii=False),
                        current["current_match_mode"],
                        current["answer_deadline_at"],
                        current["started_at"],
                        current["signup_ends_at"],
                        current["spectator_ends_at"],
                        current["winner_user_id"],
                    ),
                )
                await db.commit()

    async def reset_runtime(self, guild_id: int) -> None:
        await self.set_runtime(
            guild_id,
            active=False,
            phase="idle",
            channel_id=None,
            thread_id=None,
            announcement_message_id=None,
            signup_message_channel_id=None,
            signup_message_id=None,
            round_number=0,
            current_player_id=None,
            current_question_id=None,
            current_question_text=None,
            current_answers=[],
            current_correct_order=[],
            current_match_mode=None,
            answer_deadline_at=None,
            started_at=None,
            signup_ends_at=None,
            spectator_ends_at=None,
            winner_user_id=None,
        )
        await self.clear_participants(guild_id)

    async def add_participant(self, guild_id: int, user_id: int, status: str, joined_at: str) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO julgamento_participants (guild_id, user_id, status, joined_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    status = excluded.status,
                    joined_at = excluded.joined_at
                """,
                (guild_id, user_id, status, joined_at),
            )
            await db.commit()

    async def set_participant_status(self, guild_id: int, user_id: int, status: str) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE julgamento_participants
                SET status = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (status, guild_id, user_id),
            )
            await db.commit()

    async def get_participants(self, guild_id: int) -> list[dict[str, Any]]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT user_id, status, joined_at
                FROM julgamento_participants
                WHERE guild_id = ?
                ORDER BY joined_at ASC, user_id ASC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def clear_participants(self, guild_id: int) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM julgamento_participants WHERE guild_id = ?", (guild_id,))
            await db.commit()
