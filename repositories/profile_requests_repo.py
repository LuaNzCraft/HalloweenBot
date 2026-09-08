from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import aiosqlite

from .db import DB_PATH


@dataclass(frozen=True)
class ProfileRequestSettlement:
    newly_settled: bool
    outcome: str
    amount: int
    milestone_items: dict[str, int]


class ProfileRequestNotPending(ValueError):
    pass


class ProfileRequestInsufficientFunds(ValueError):
    pass


class ProfileRequestsRepo:
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
                CREATE TABLE IF NOT EXISTS profile_requests (
                    requester_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    date_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (requester_id, target_id, date_key)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_requests_guild (
                    guild_id INTEGER NOT NULL,
                    requester_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    date_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (guild_id, requester_id, target_id, date_key)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_request_runtime_guild (
                    guild_id INTEGER NOT NULL,
                    requester_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    channel_id INTEGER,
                    message_id INTEGER,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, requester_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_request_cooldowns_guild (
                    guild_id INTEGER NOT NULL,
                    requester_id INTEGER NOT NULL,
                    available_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, requester_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_request_activity (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    available_at TEXT NOT NULL
                    ,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_request_settlements (
                    settlement_key TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    requester_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    request_created_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0,
                    milestone_items_json TEXT NOT NULL DEFAULT '{}',
                    settled_at TEXT NOT NULL
                )
                """
            )
            await self._ensure_column(
                db,
                "profile_requests_guild",
                "request_count",
                "INTEGER NOT NULL DEFAULT 1",
            )
            await self._ensure_column(db, "profile_request_runtime_guild", "channel_id", "INTEGER")
            await self._ensure_column(db, "profile_request_runtime_guild", "message_id", "INTEGER")
            await db.commit()
        self._initialized = True

    async def register_daily_request(
        self,
        guild_id: int,
        requester_id: int,
        target_id: int,
        date_key: str,
        created_at: str,
        daily_limit: int = 1,
    ) -> bool:
        if daily_limit <= 0:
            raise ValueError("daily_limit must be positive")
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO profile_requests_guild (
                    guild_id,
                    requester_id,
                    target_id,
                    date_key,
                    created_at,
                    request_count
                )
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(guild_id, requester_id, target_id, date_key) DO UPDATE SET
                    created_at = excluded.created_at,
                    request_count = profile_requests_guild.request_count + 1
                WHERE profile_requests_guild.request_count < ?
                """,
                (guild_id, requester_id, target_id, date_key, created_at, daily_limit),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def delete_daily_request(self, guild_id: int, requester_id: int, target_id: int, date_key: str) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                DELETE FROM profile_requests_guild
                WHERE guild_id = ? AND requester_id = ? AND target_id = ? AND date_key = ?
                """,
                (guild_id, requester_id, target_id, date_key),
            )
            await db.commit()

    async def try_create_active_request(
        self,
        guild_id: int,
        requester_id: int,
        target_id: int,
        created_at: str,
        expires_at: str,
        now_iso: str,
        channel_id: int | None = None,
    ) -> bool:
        await self.init_db()
        await self._cleanup_expired_active_requests(guild_id, now_iso)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO profile_request_runtime_guild (
                    guild_id, requester_id, target_id, channel_id, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (guild_id, requester_id, target_id, channel_id, created_at, expires_at),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def mark_active_request_published(
        self,
        guild_id: int,
        requester_id: int,
        created_at: str,
        message_id: int,
    ) -> bool:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE profile_request_runtime_guild
                SET message_id = ?
                WHERE guild_id = ? AND requester_id = ? AND created_at = ?
                """,
                (message_id, guild_id, requester_id, created_at),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def list_active_requests(self) -> list[dict[str, Any]]:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT guild_id, requester_id, target_id, channel_id, message_id,
                       created_at, expires_at
                FROM profile_request_runtime_guild
                ORDER BY created_at ASC
                """
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def delete_active_request(
        self,
        guild_id: int,
        requester_id: int,
        *,
        created_at: str | None = None,
    ) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            if created_at is None:
                await db.execute(
                    """
                    DELETE FROM profile_request_runtime_guild
                    WHERE guild_id = ? AND requester_id = ?
                    """,
                    (guild_id, requester_id),
                )
            else:
                await db.execute(
                    """
                    DELETE FROM profile_request_runtime_guild
                    WHERE guild_id = ? AND requester_id = ? AND created_at = ?
                    """,
                    (guild_id, requester_id, created_at),
                )
            await db.commit()

    async def settle_request_once(
        self,
        *,
        guild_id: int,
        requester_id: int,
        target_id: int,
        request_created_at: str,
        outcome: str,
        amount: int,
        generosity_milestones: list[dict[str, Any]] | None = None,
        cooldown_until: str | None = None,
    ) -> ProfileRequestSettlement:
        if outcome not in {"candy", "trick", "ignore"}:
            raise ValueError("unsupported_profile_request_outcome")
        normalized_amount = int(amount)
        if outcome == "ignore":
            normalized_amount = 0
        elif normalized_amount <= 0:
            raise ValueError("profile_request_amount_must_be_positive")
        if requester_id == target_id:
            raise ValueError("profile_request_participants_must_differ")

        created_at = str(request_created_at).strip()
        if not created_at:
            raise ValueError("request_created_at_is_required")
        settlement_key = self._settlement_key(
            guild_id,
            requester_id,
            target_id,
            created_at,
        )

        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            existing = await self._get_settlement_row(db, settlement_key)
            if existing is not None:
                self._validate_settlement_identity(
                    existing,
                    guild_id=guild_id,
                    requester_id=requester_id,
                    target_id=target_id,
                    request_created_at=created_at,
                )
                await db.rollback()
                return self._settlement_from_row(existing, newly_settled=False)

            cursor = await db.execute(
                """
                SELECT 1
                FROM profile_request_runtime_guild
                WHERE guild_id = ?
                  AND requester_id = ?
                  AND target_id = ?
                  AND created_at = ?
                """,
                (guild_id, requester_id, target_id, created_at),
            )
            if await cursor.fetchone() is None:
                await db.rollback()
                raise ProfileRequestNotPending("profile_request_not_pending")

            try:
                await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (requester_id,))
                await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (target_id,))

                milestone_items: dict[str, int] = {}
                if outcome in {"candy", "trick"}:
                    currency = "candies" if outcome == "candy" else "trick_coins"
                    given_counter = "candies_given_total" if outcome == "candy" else "trick_coins_given_total"
                    cursor = await db.execute(
                        f"""
                        UPDATE users
                        SET {currency} = {currency} - ?,
                            profile_requests_answered_total = profile_requests_answered_total + 1,
                            {given_counter} = {given_counter} + ?
                        WHERE user_id = ? AND {currency} >= ?
                        """,
                        (normalized_amount, normalized_amount, target_id, normalized_amount),
                    )
                    if not cursor.rowcount:
                        raise ProfileRequestInsufficientFunds("not_enough_currency")

                    total_field = "candies_total" if outcome == "candy" else "trick_coins_total"
                    await db.execute(
                        f"""
                        UPDATE users
                        SET {currency} = {currency} + ?,
                            {total_field} = {total_field} + ?
                        WHERE user_id = ?
                        """,
                        (normalized_amount, normalized_amount, requester_id),
                    )
                    if outcome == "candy":
                        milestone_items = await self._apply_generosity_milestones(
                            db,
                            target_id,
                            generosity_milestones or [],
                        )
                else:
                    await db.execute(
                        """
                        UPDATE users
                        SET profile_requests_answered_total = profile_requests_answered_total + 1
                        WHERE user_id = ?
                        """,
                        (target_id,),
                    )
                    if cooldown_until:
                        await db.execute(
                            """
                            INSERT INTO profile_request_cooldowns_guild (guild_id, requester_id, available_at)
                            VALUES (?, ?, ?)
                            ON CONFLICT(guild_id, requester_id) DO UPDATE SET
                                available_at = excluded.available_at
                            """,
                            (guild_id, requester_id, cooldown_until),
                        )

                await db.execute("INSERT OR IGNORE INTO guild_metrics (guild_id) VALUES (?)", (guild_id,))
                await db.execute(
                    """
                    UPDATE guild_metrics
                    SET profile_requests_resolved_total = profile_requests_resolved_total + 1
                    WHERE guild_id = ?
                    """,
                    (guild_id,),
                )
                await self._after_economy_applied(db, settlement_key)
                await db.execute(
                    """
                    INSERT INTO profile_request_settlements (
                        settlement_key, guild_id, requester_id, target_id,
                        request_created_at, outcome, amount,
                        milestone_items_json, settled_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        settlement_key,
                        guild_id,
                        requester_id,
                        target_id,
                        created_at,
                        outcome,
                        normalized_amount,
                        json.dumps(milestone_items, ensure_ascii=False, sort_keys=True),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                await db.execute(
                    """
                    DELETE FROM profile_request_runtime_guild
                    WHERE guild_id = ?
                      AND requester_id = ?
                      AND target_id = ?
                      AND created_at = ?
                    """,
                    (guild_id, requester_id, target_id, created_at),
                )
            except BaseException:
                await db.rollback()
                raise
            await db.commit()

        return ProfileRequestSettlement(
            newly_settled=True,
            outcome=outcome,
            amount=normalized_amount,
            milestone_items=milestone_items,
        )

    async def get_cooldown_remaining_seconds(self, guild_id: int, requester_id: int, now_iso: str) -> int:
        await self.init_db()
        await self._cleanup_expired_cooldowns(guild_id, now_iso)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT available_at
                FROM profile_request_cooldowns_guild
                WHERE guild_id = ? AND requester_id = ?
                """,
                (guild_id, requester_id),
            )
            row = await cursor.fetchone()
        if not row:
            return 0
        available_at = datetime.fromisoformat(str(row[0]))
        now_dt = datetime.fromisoformat(now_iso)
        remaining = int((available_at - now_dt).total_seconds())
        return max(0, remaining)

    async def set_cooldown(self, guild_id: int, requester_id: int, available_at: str) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO profile_request_cooldowns_guild (guild_id, requester_id, available_at)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, requester_id) DO UPDATE SET
                    available_at = excluded.available_at
                """,
                (guild_id, requester_id, available_at),
            )
            await db.commit()

    async def set_last_active_at(self, guild_id: int, user_id: int, last_active_at: str) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO profile_request_activity (guild_id, user_id, available_at)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    available_at = excluded.available_at
                """,
                (guild_id, user_id, last_active_at),
            )
            await db.commit()

    async def get_last_active_at(self, guild_id: int, user_id: int) -> str | None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT available_at
                FROM profile_request_activity
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            row = await cursor.fetchone()
        return str(row[0]) if row else None

    async def _cleanup_expired_active_requests(self, guild_id: int, now_iso: str) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                DELETE FROM profile_request_runtime_guild
                WHERE guild_id = ? AND expires_at <= ?
                """,
                (guild_id, now_iso),
            )
            await db.commit()

    async def _cleanup_expired_cooldowns(self, guild_id: int, now_iso: str) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                DELETE FROM profile_request_cooldowns_guild
                WHERE guild_id = ? AND available_at <= ?
                """,
                (guild_id, now_iso),
            )
            await db.commit()

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

    async def _apply_generosity_milestones(
        self,
        db: aiosqlite.Connection,
        target_id: int,
        milestones: list[dict[str, Any]],
    ) -> dict[str, int]:
        cursor = await db.execute(
            "SELECT candies_given_total, season_stats FROM users WHERE user_id = ?",
            (target_id,),
        )
        row = await cursor.fetchone()
        current_total = int(row[0]) if row else 0
        try:
            season_stats = json.loads(str(row[1] or "{}")) if row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            season_stats = {}
        if not isinstance(season_stats, dict):
            season_stats = {}
        claimed = {
            int(value)
            for value in (season_stats.get("generosity_candies_milestones_claimed") or [])
            if str(value).isdigit()
        }
        granted: dict[str, int] = {}
        for raw_entry in milestones:
            if not isinstance(raw_entry, dict):
                continue
            target = int(raw_entry.get("target") or 0)
            rewards = raw_entry.get("rewards") or {}
            if target <= 0 or target in claimed or current_total < target or not isinstance(rewards, dict):
                continue
            for raw_item_id, raw_quantity in rewards.items():
                item_id = str(raw_item_id).strip()
                quantity = int(raw_quantity or 0)
                if not item_id or quantity <= 0:
                    continue
                granted[item_id] = granted.get(item_id, 0) + quantity
            claimed.add(target)

        for item_id, quantity in granted.items():
            await db.execute(
                """
                INSERT INTO inventory (user_id, item_id, quantity)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, item_id) DO UPDATE SET
                    quantity = quantity + excluded.quantity
                """,
                (target_id, item_id, quantity),
            )
        if granted:
            season_stats["generosity_candies_milestones_claimed"] = sorted(claimed)
            await db.execute(
                "UPDATE users SET season_stats = ? WHERE user_id = ?",
                (json.dumps(season_stats, ensure_ascii=False, sort_keys=True), target_id),
            )
        return granted

    async def _get_settlement_row(
        self,
        db: aiosqlite.Connection,
        settlement_key: str,
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            """
            SELECT guild_id, requester_id, target_id, request_created_at,
                   outcome, amount, milestone_items_json
            FROM profile_request_settlements
            WHERE settlement_key = ?
            """,
            (settlement_key,),
        )
        return await cursor.fetchone()

    def _settlement_key(
        self,
        guild_id: int,
        requester_id: int,
        target_id: int,
        request_created_at: str,
    ) -> str:
        return f"profile_request:{guild_id}:{requester_id}:{target_id}:{request_created_at}"

    def _validate_settlement_identity(
        self,
        row: aiosqlite.Row,
        *,
        guild_id: int,
        requester_id: int,
        target_id: int,
        request_created_at: str,
    ) -> None:
        if (
            int(row["guild_id"]) != int(guild_id)
            or int(row["requester_id"]) != int(requester_id)
            or int(row["target_id"]) != int(target_id)
            or str(row["request_created_at"]) != request_created_at
        ):
            raise ValueError("profile_request_settlement_key_collision")

    def _settlement_from_row(
        self,
        row: aiosqlite.Row,
        *,
        newly_settled: bool,
    ) -> ProfileRequestSettlement:
        raw_items = json.loads(str(row["milestone_items_json"] or "{}"))
        return ProfileRequestSettlement(
            newly_settled=newly_settled,
            outcome=str(row["outcome"]),
            amount=int(row["amount"]),
            milestone_items={str(item_id): int(quantity) for item_id, quantity in raw_items.items()},
        )

    async def _after_economy_applied(
        self,
        db: aiosqlite.Connection,
        settlement_key: str,
    ) -> None:
        return None
