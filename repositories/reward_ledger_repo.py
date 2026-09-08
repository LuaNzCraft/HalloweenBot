from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

import aiosqlite

from .db import DB_PATH


REWARD_CURRENCIES = {"candies", "trick_coins", "dice_count", "xp_bank"}
REWARD_COUNTERS = {
    "baskets_opened_total",
    "candies_given_total",
    "profile_requests_answered_total",
    "profile_requests_sent_total",
    "trick_coins_given_total",
    "tricks_applied_total",
    "mute_used_total",
    "switch_used_total",
    "horror_questions_correct_total",
}
REWARD_METRICS = {
    "candies": "candies_total",
    "trick_coins": "trick_coins_total",
    "xp_bank": "xp_bank",
}


@dataclass(frozen=True)
class RewardSettlement:
    newly_settled: bool
    currencies: dict[str, int]
    items: dict[str, int]
    counters: dict[str, int]


class RewardLedgerRepo:
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
                CREATE TABLE IF NOT EXISTS reward_settlements (
                    settlement_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    guild_id INTEGER,
                    user_id INTEGER NOT NULL,
                    currencies_json TEXT NOT NULL DEFAULT '{}',
                    items_json TEXT NOT NULL DEFAULT '{}',
                    counters_json TEXT NOT NULL DEFAULT '{}',
                    settled_at TEXT NOT NULL
                )
                """
            )
            await db.commit()
        self._initialized = True

    async def settle_once(
        self,
        settlement_key: str,
        source: str,
        user_id: int,
        guild_id: int | None,
        *,
        currencies: dict[str, int] | None = None,
        items: dict[str, int] | None = None,
        counters: dict[str, int] | None = None,
    ) -> RewardSettlement:
        key = str(settlement_key).strip()
        source_name = str(source).strip()
        if not key or not source_name:
            raise ValueError("settlement_key and source are required")
        normalized_currencies = self._normalize_named_amounts(currencies, REWARD_CURRENCIES, "currency")
        normalized_items = self._normalize_items(items)
        normalized_counters = self._normalize_named_amounts(counters, REWARD_COUNTERS, "counter")

        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT source, guild_id, user_id, currencies_json, items_json, counters_json
                FROM reward_settlements
                WHERE settlement_key = ?
                """,
                (key,),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                if (
                    str(existing["source"]) != source_name
                    or int(existing["user_id"]) != int(user_id)
                    or self._optional_int(existing["guild_id"]) != self._optional_int(guild_id)
                ):
                    await db.rollback()
                    raise ValueError("settlement_key_collision")
                await db.rollback()
                return RewardSettlement(
                    newly_settled=False,
                    currencies=self._load_amounts(existing["currencies_json"]),
                    items=self._load_amounts(existing["items_json"]),
                    counters=self._load_amounts(existing["counters_json"]),
                )

            await db.execute(
                """
                INSERT INTO reward_settlements (
                    settlement_key, source, guild_id, user_id,
                    currencies_json, items_json, counters_json, settled_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    source_name,
                    guild_id,
                    user_id,
                    json.dumps(normalized_currencies, sort_keys=True),
                    json.dumps(normalized_items, sort_keys=True),
                    json.dumps(normalized_counters, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            try:
                await self._apply_settlement(
                    db,
                    int(user_id),
                    self._optional_int(guild_id),
                    normalized_currencies,
                    normalized_items,
                    normalized_counters,
                )
            except BaseException:
                await db.rollback()
                raise
            await db.commit()

        return RewardSettlement(
            newly_settled=True,
            currencies=normalized_currencies,
            items=normalized_items,
            counters=normalized_counters,
        )

    async def get_settlement(self, settlement_key: str) -> RewardSettlement | None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT currencies_json, items_json, counters_json
                FROM reward_settlements
                WHERE settlement_key = ?
                """,
                (settlement_key,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return RewardSettlement(
            newly_settled=False,
            currencies=self._load_amounts(row["currencies_json"]),
            items=self._load_amounts(row["items_json"]),
            counters=self._load_amounts(row["counters_json"]),
        )

    async def _apply_settlement(
        self,
        db: aiosqlite.Connection,
        user_id: int,
        guild_id: int | None,
        currencies: dict[str, int],
        items: dict[str, int],
        counters: dict[str, int],
    ) -> None:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        for field, amount in currencies.items():
            if field == "candies":
                await db.execute(
                    "UPDATE users SET candies = candies + ?, candies_total = candies_total + ? WHERE user_id = ?",
                    (amount, amount, user_id),
                )
            elif field == "trick_coins":
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
                    f"UPDATE users SET {field} = {field} + ? WHERE user_id = ?",
                    (amount, user_id),
                )

        for item_id, amount in items.items():
            await db.execute(
                """
                INSERT INTO inventory (user_id, item_id, quantity)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, item_id) DO UPDATE SET quantity = quantity + excluded.quantity
                """,
                (user_id, item_id, amount),
            )

        for field, amount in counters.items():
            await db.execute(
                f"UPDATE users SET {field} = {field} + ? WHERE user_id = ?",
                (amount, user_id),
            )

        if guild_id is None:
            return
        await db.execute("INSERT OR IGNORE INTO guild_metrics (guild_id) VALUES (?)", (guild_id,))
        await db.execute(
            "INSERT OR IGNORE INTO user_guild_metrics (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        for field, amount in currencies.items():
            metric = REWARD_METRICS.get(field)
            if metric is not None:
                await self._increment_metrics(db, guild_id, user_id, metric, amount)
        for field, amount in counters.items():
            if field in REWARD_COUNTERS:
                await self._increment_metrics(db, guild_id, user_id, field, amount)

    async def _increment_metrics(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        user_id: int,
        metric: str,
        amount: int,
    ) -> None:
        guild_metrics = {
            "candies_total",
            "trick_coins_total",
            "xp_bank",
            "horror_questions_correct_total",
        }
        user_guild_metrics = guild_metrics | {
            "baskets_opened_total",
            "tricks_applied_total",
            "mute_used_total",
            "switch_used_total",
        }
        if metric in guild_metrics:
            await db.execute(
                f"UPDATE guild_metrics SET {metric} = {metric} + ? WHERE guild_id = ?",
                (amount, guild_id),
            )
        if metric in user_guild_metrics:
            await db.execute(
                f"""
                UPDATE user_guild_metrics
                SET {metric} = {metric} + ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (amount, guild_id, user_id),
            )

    def _normalize_named_amounts(
        self,
        values: dict[str, int] | None,
        allowed: set[str],
        label: str,
    ) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for raw_name, raw_amount in (values or {}).items():
            name = str(raw_name).strip()
            if name not in allowed:
                raise ValueError(f"Unsupported reward {label}: {name}")
            amount = int(raw_amount)
            if amount > 0:
                normalized[name] = amount
        return normalized

    def _normalize_items(self, items: dict[str, int] | None) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for raw_item_id, raw_amount in (items or {}).items():
            item_id = str(raw_item_id).strip()
            amount = int(raw_amount)
            if not item_id:
                raise ValueError("Reward item id is required")
            if amount > 0:
                normalized[item_id] = amount
        return normalized

    def _load_amounts(self, raw: str) -> dict[str, int]:
        parsed = json.loads(raw or "{}")
        return {str(name): int(amount) for name, amount in parsed.items()}

    def _optional_int(self, value: object) -> int | None:
        return None if value is None else int(value)
