from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .db import DB_PATH


class EffectsRepo:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path

    async def init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS active_effects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    effect_type TEXT NOT NULL,
                    guild_id INTEGER,
                    actor_id INTEGER,
                    target_id INTEGER NOT NULL,
                    starts_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_active_effects_lookup
                ON active_effects (active, effect_type, guild_id, target_id)
                """
            )
            await db.commit()

    async def create_effect(
        self,
        *,
        effect_type: str,
        guild_id: int | None,
        actor_id: int | None,
        target_id: int,
        starts_at: datetime,
        expires_at: datetime,
        payload: dict[str, Any] | None = None,
    ) -> int:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO active_effects (
                    effect_type, guild_id, actor_id, target_id, starts_at, expires_at, payload_json, active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    effect_type,
                    guild_id,
                    actor_id,
                    target_id,
                    starts_at.astimezone(timezone.utc).isoformat(),
                    expires_at.astimezone(timezone.utc).isoformat(),
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def list_active_effects(self, *, effect_type: str | None = None, guild_id: int | None = None) -> list[dict[str, Any]]:
        await self.init_db()
        filters = ["active = 1"]
        values: list[Any] = []
        if effect_type is not None:
            filters.append("effect_type = ?")
            values.append(effect_type)
        if guild_id is not None:
            filters.append("guild_id = ?")
            values.append(guild_id)

        query = f"SELECT * FROM active_effects WHERE {' AND '.join(filters)} ORDER BY expires_at ASC"
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, values)
            rows = await cursor.fetchall()

        return [self._row_to_effect(row) for row in rows]

    async def deactivate_effect(self, effect_id: int) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE active_effects SET active = 0 WHERE id = ?", (effect_id,))
            await db.commit()

    async def update_effect_payload(self, effect_id: int, payload: dict[str, Any]) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE active_effects SET payload_json = ? WHERE id = ?",
                (json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), effect_id),
            )
            await db.commit()

    async def deactivate_expired(self, now: datetime | None = None) -> int:
        await self.init_db()
        now = now or datetime.now(timezone.utc)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "UPDATE active_effects SET active = 0 WHERE active = 1 AND expires_at <= ?",
                (now.astimezone(timezone.utc).isoformat(),),
            )
            await db.commit()
            return int(cursor.rowcount)

    def _row_to_effect(self, row: aiosqlite.Row) -> dict[str, Any]:
        effect = dict(row)
        effect["active"] = bool(effect["active"])
        effect["payload"] = json.loads(effect.pop("payload_json") or "{}")
        return effect
