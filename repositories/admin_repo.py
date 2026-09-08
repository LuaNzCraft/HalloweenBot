from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .db import DB_PATH
from utils.event_config import get_guild_defaults


DEFAULT_RUNTIME = {
    "event_active": False,
    "event_started_at": None,
    "event_ended_at": None,
    "events_paused_until": None,
    "pause_reason": None,
}


class AdminRepo:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path

    async def init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id INTEGER PRIMARY KEY,
                    leaderboard_channel_id INTEGER,
                    log_channel_id INTEGER,
                    event_channel_ids TEXT NOT NULL DEFAULT '[]',
                    julgamento_channel_id INTEGER,
                    julgamento_thread_id INTEGER,
                    julgamento_base_message_id INTEGER,
                    event_role_id INTEGER,
                    shop_role_id INTEGER,
                    board_100_role_id INTEGER,
                    notification_role_id INTEGER,
                    cinefilo_role_id INTEGER,
                    julgamento_active_role_id INTEGER,
                    julgamento_muted_role_id INTEGER,
                    julgamento_spectator_role_id INTEGER
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS event_runtime (
                    guild_id INTEGER PRIMARY KEY,
                    event_active INTEGER NOT NULL DEFAULT 0,
                    event_started_at TEXT,
                    event_ended_at TEXT,
                    events_paused_until TEXT,
                    pause_reason TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    guild_id INTEGER,
                    actor_id INTEGER,
                    target_id INTEGER,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS leaderboard_messages (
                    guild_id INTEGER NOT NULL,
                    leaderboard_type TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    edit_count INTEGER NOT NULL DEFAULT 0,
                    last_recreated_at TEXT,
                    current_page TEXT NOT NULL DEFAULT 'tabuleiro',
                    PRIMARY KEY (guild_id, leaderboard_type)
                )
                """
            )
            await self._ensure_column(db, "leaderboard_messages", "edit_count", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "leaderboard_messages", "last_recreated_at", "TEXT")
            await self._ensure_column(db, "leaderboard_messages", "current_page", "TEXT NOT NULL DEFAULT 'tabuleiro'")
            await self._ensure_column(db, "guild_config", "julgamento_channel_id", "INTEGER")
            await self._ensure_column(db, "guild_config", "julgamento_thread_id", "INTEGER")
            await self._ensure_column(db, "guild_config", "julgamento_base_message_id", "INTEGER")
            await self._ensure_column(db, "guild_config", "shop_role_id", "INTEGER")
            await self._ensure_column(db, "guild_config", "board_100_role_id", "INTEGER")
            await self._ensure_column(db, "guild_config", "cinefilo_role_id", "INTEGER")
            await self._ensure_column(db, "guild_config", "julgamento_active_role_id", "INTEGER")
            await self._ensure_column(db, "guild_config", "julgamento_muted_role_id", "INTEGER")
            await self._ensure_column(db, "guild_config", "julgamento_spectator_role_id", "INTEGER")
            await db.commit()

    async def get_config(self, guild_id: int) -> dict[str, Any]:
        await self._ensure_guild(guild_id)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()

        config = get_guild_defaults()
        if row:
            config.update(dict(row))
        config.pop("guild_id", None)
        config["event_channel_ids"] = json.loads(config.get("event_channel_ids") or "[]")
        return config

    async def set_channel(self, guild_id: int, channel_type: str, channel_id: int) -> None:
        await self._ensure_guild(guild_id)
        if channel_type == "leaderboard":
            await self._update_config_field(guild_id, "leaderboard_channel_id", channel_id)
        elif channel_type == "logs":
            await self._update_config_field(guild_id, "log_channel_id", channel_id)
        elif channel_type == "eventos":
            config = await self.get_config(guild_id)
            channel_ids = list(dict.fromkeys([*config["event_channel_ids"], channel_id]))
            await self._update_config_field(guild_id, "event_channel_ids", json.dumps(channel_ids))
        elif channel_type == "julgamento":
            await self._update_config_field(guild_id, "julgamento_channel_id", channel_id)
        else:
            raise ValueError("unknown_channel_type")

    async def set_julgamento_structure(
        self,
        guild_id: int,
        *,
        channel_id: int,
        thread_id: int,
        base_message_id: int,
    ) -> None:
        await self._ensure_guild(guild_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE guild_config
                SET julgamento_channel_id = ?, julgamento_thread_id = ?, julgamento_base_message_id = ?
                WHERE guild_id = ?
                """,
                (channel_id, thread_id, base_message_id, guild_id),
            )
            await db.commit()

    async def remove_event_channel(self, guild_id: int, channel_id: int) -> bool:
        config = await self.get_config(guild_id)
        channel_ids = [current for current in config["event_channel_ids"] if current != channel_id]
        removed = len(channel_ids) != len(config["event_channel_ids"])
        if not removed:
            return False
        await self._update_config_field(guild_id, "event_channel_ids", json.dumps(channel_ids))
        return True

    async def set_role(self, guild_id: int, role_type: str, role_id: int) -> None:
        await self._ensure_guild(guild_id)
        if role_type == "loja":
            await self._update_config_field(guild_id, "shop_role_id", role_id)
        elif role_type == "tabuleiro_100":
            await self._update_config_field(guild_id, "board_100_role_id", role_id)
        elif role_type == "notificacao":
            await self._update_config_field(guild_id, "notification_role_id", role_id)
        elif role_type == "cinefilo":
            await self._update_config_field(guild_id, "cinefilo_role_id", role_id)
        elif role_type == "julgamento_ativo":
            await self._update_config_field(guild_id, "julgamento_active_role_id", role_id)
        elif role_type == "julgamento_mutado":
            await self._update_config_field(guild_id, "julgamento_muted_role_id", role_id)
        elif role_type == "julgamento_espectador":
            await self._update_config_field(guild_id, "julgamento_spectator_role_id", role_id)
        else:
            raise ValueError("unknown_role_type")

    async def get_runtime(self, guild_id: int) -> dict[str, Any]:
        await self._ensure_guild(guild_id)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM event_runtime WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()

        runtime = dict(DEFAULT_RUNTIME)
        if row:
            runtime.update(dict(row))
        runtime.pop("guild_id", None)
        runtime["event_active"] = bool(runtime["event_active"])
        return runtime

    async def set_runtime(self, guild_id: int, **fields: Any) -> None:
        await self._ensure_guild(guild_id)
        allowed = set(DEFAULT_RUNTIME)
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [self._serialize_runtime_value(value) for value in updates.values()]
        values.append(guild_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE event_runtime SET {assignments} WHERE guild_id = ?", values)
            await db.commit()

    async def add_audit_log(
        self,
        *,
        guild_id: int | None,
        actor_id: int | None,
        action: str,
        target_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO audit_log (created_at, guild_id, actor_id, target_id, action, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    guild_id,
                    actor_id,
                    target_id,
                    action,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            await db.commit()

    async def get_leaderboard_message(self, guild_id: int, leaderboard_type: str) -> dict[str, Any] | None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT channel_id, message_id, edit_count, last_recreated_at, current_page
                FROM leaderboard_messages
                WHERE guild_id = ? AND leaderboard_type = ?
                """,
                (guild_id, leaderboard_type),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_all_leaderboard_messages(self, leaderboard_type: str) -> list[dict[str, Any]]:
        """Retorna todos os painéis publicados de um tipo, de todas as guilds."""
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT guild_id, channel_id, message_id, edit_count, current_page
                FROM leaderboard_messages
                WHERE leaderboard_type = ?
                """,
                (leaderboard_type,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def set_leaderboard_message(
        self,
        guild_id: int,
        leaderboard_type: str,
        channel_id: int,
        message_id: int,
        edit_count: int = 0,
        current_page: str = "tabuleiro",
    ) -> None:
        await self.init_db()
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO leaderboard_messages (
                    guild_id, leaderboard_type, channel_id, message_id, edit_count, last_recreated_at, current_page
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, leaderboard_type) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id,
                    edit_count = excluded.edit_count,
                    last_recreated_at = excluded.last_recreated_at,
                    current_page = excluded.current_page
                """,
                (guild_id, leaderboard_type, channel_id, message_id, edit_count, now, current_page),
            )
            await db.commit()

    async def set_leaderboard_current_page(self, guild_id: int, leaderboard_type: str, current_page: str) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE leaderboard_messages
                SET current_page = ?
                WHERE guild_id = ? AND leaderboard_type = ?
                """,
                (current_page, guild_id, leaderboard_type),
            )
            await db.commit()

    async def increment_leaderboard_edit_count(self, guild_id: int, leaderboard_type: str) -> int:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE leaderboard_messages
                SET edit_count = edit_count + 1
                WHERE guild_id = ? AND leaderboard_type = ?
                """,
                (guild_id, leaderboard_type),
            )
            cursor = await db.execute(
                """
                SELECT edit_count
                FROM leaderboard_messages
                WHERE guild_id = ? AND leaderboard_type = ?
                """,
                (guild_id, leaderboard_type),
            )
            row = await cursor.fetchone()
            await db.commit()
        return int(row[0]) if row else 0

    async def _ensure_guild(self, guild_id: int) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)",
                (guild_id,),
            )
            await db.execute(
                "INSERT OR IGNORE INTO event_runtime (guild_id) VALUES (?)",
                (guild_id,),
            )
            await db.commit()

    async def _update_config_field(self, guild_id: int, field: str, value: Any) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE guild_config SET {field} = ? WHERE guild_id = ?", (value, guild_id))
            await db.commit()

    def _serialize_runtime_value(self, value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        return value

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
