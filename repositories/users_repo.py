from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from .db import DB_PATH


USER_COLUMNS = {
    "baskets_opened_total",
    "candies",
    "candies_given_total",
    "candies_total",
    "profile_requests_answered_total",
    "profile_requests_sent_total",
    "trick_coins",
    "trick_coins_given_total",
    "trick_coins_total",
    "tricks_applied_total",
    "mute_used_total",
    "switch_used_total",
    "horror_questions_correct_total",
    "dice_count",
    "xp_bank",
    "locale",
    "last_daily_dice_at",
    "last_active_at",
    "board_rolls_today",
    "daily_mission",
    "season_stats",
}


class UsersRepo:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path

    async def init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    baskets_opened_total INTEGER NOT NULL DEFAULT 0,
                    candies INTEGER NOT NULL DEFAULT 0,
                    candies_given_total INTEGER NOT NULL DEFAULT 0,
                    candies_total INTEGER NOT NULL DEFAULT 0,
                    profile_requests_answered_total INTEGER NOT NULL DEFAULT 0,
                    profile_requests_sent_total INTEGER NOT NULL DEFAULT 0,
                    trick_coins INTEGER NOT NULL DEFAULT 0,
                    trick_coins_given_total INTEGER NOT NULL DEFAULT 0,
                    trick_coins_total INTEGER NOT NULL DEFAULT 0,
                    tricks_applied_total INTEGER NOT NULL DEFAULT 0,
                    mute_used_total INTEGER NOT NULL DEFAULT 0,
                    switch_used_total INTEGER NOT NULL DEFAULT 0,
                    horror_questions_correct_total INTEGER NOT NULL DEFAULT 0,
                    dice_count INTEGER NOT NULL DEFAULT 0,
                    xp_bank INTEGER NOT NULL DEFAULT 0,
                    locale TEXT NOT NULL DEFAULT 'pt-BR',
                    last_daily_dice_at TEXT,
                    last_active_at TEXT,
                    board_rolls_today INTEGER NOT NULL DEFAULT 0,
                    daily_mission TEXT,
                    season_stats TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            await self._ensure_column(db, "users", "baskets_opened_total", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "users", "mute_used_total", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "users", "switch_used_total", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "users", "horror_questions_correct_total", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "users", "candies_given_total", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "users", "trick_coins_given_total", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "users", "profile_requests_answered_total", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "users", "profile_requests_sent_total", "INTEGER NOT NULL DEFAULT 0")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory (
                    user_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, item_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS xp_bank_closures (
                    closure_id TEXT PRIMARY KEY,
                    guild_id INTEGER,
                    created_at TEXT NOT NULL,
                    total_xp_bank INTEGER NOT NULL,
                    entry_count INTEGER NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS xp_bank_closure_entries (
                    closure_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    xp_bank INTEGER NOT NULL,
                    PRIMARY KEY (closure_id, user_id),
                    FOREIGN KEY (closure_id) REFERENCES xp_bank_closures(closure_id)
                )
                """
            )
            await db.commit()
        await self._migrate_dado_extra_inventory()

    async def get_user(self, user_id: int) -> dict[str, Any]:
        await self._ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            inventory = await self._get_inventory(db, user_id)

        if row is None:
            raise RuntimeError("user was not created")
        return self._deserialize_user(row, inventory)

    async def update_user(self, user_id: int, mutator) -> Any:
        await self._ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                raise RuntimeError("user was not created")

            user = self._deserialize_user(row, await self._get_inventory(db, user_id))
            try:
                result = mutator(user)
            except Exception:
                await db.rollback()
                raise

            user_fields = {
                key: value
                for key, value in user.items()
                if key in USER_COLUMNS
            }
            if isinstance(user_fields.get("season_stats"), dict):
                user_fields["season_stats"] = json.dumps(
                    user_fields["season_stats"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            assignments = ", ".join(f"{key} = ?" for key in user_fields)
            values = [*user_fields.values(), user_id]
            if assignments:
                await db.execute(f"UPDATE users SET {assignments} WHERE user_id = ?", values)

            inventory = user.get("inventory", {})
            if isinstance(inventory, dict):
                await db.execute("DELETE FROM inventory WHERE user_id = ?", (user_id,))
                await db.executemany(
                    """
                    INSERT INTO inventory (user_id, item_id, quantity)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (user_id, item_id, int(quantity))
                        for item_id, quantity in inventory.items()
                        if int(quantity) > 0
                    ],
                )
            await db.commit()

        return result

    async def add_inventory_item(self, user_id: int, item_id: str, quantity: int) -> int:
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        await self._ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO inventory (user_id, item_id, quantity)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, item_id) DO UPDATE SET
                    quantity = quantity + excluded.quantity
                """,
                (user_id, item_id, quantity),
            )
            cursor = await db.execute(
                "SELECT quantity FROM inventory WHERE user_id = ? AND item_id = ?",
                (user_id, item_id),
            )
            row = await cursor.fetchone()
            await db.commit()
        return int(row[0]) if row else quantity

    async def remove_inventory_item(self, user_id: int, item_id: str, quantity: int) -> int:
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        await self._ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE inventory
                SET quantity = quantity - ?
                WHERE user_id = ?
                  AND item_id = ?
                  AND quantity >= ?
                """,
                (quantity, user_id, item_id, quantity),
            )
            if not cursor.rowcount:
                await db.rollback()
                raise ValueError("not_enough_items")

            cursor = await db.execute(
                "SELECT quantity FROM inventory WHERE user_id = ? AND item_id = ?",
                (user_id, item_id),
            )
            row = await cursor.fetchone()
            remaining = int(row[0]) if row else 0
            if remaining == 0:
                await db.execute(
                    "DELETE FROM inventory WHERE user_id = ? AND item_id = ?",
                    (user_id, item_id),
                )
            await db.commit()
        return remaining

    async def add_currency(self, user_id: int, field: str, amount: int) -> int:
        allowed = {"candies", "trick_coins", "dice_count", "xp_bank"}
        if field not in allowed:
            raise ValueError(f"Unsupported currency field: {field}")

        await self._ensure_user(user_id)
        total_field = None
        if amount > 0 and field == "candies":
            total_field = "candies_total"
        elif amount > 0 and field == "trick_coins":
            total_field = "trick_coins_total"

        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"UPDATE users SET {field} = MAX(0, {field} + ?) WHERE user_id = ?",
                (amount, user_id),
            )
            if total_field:
                await db.execute(
                    f"UPDATE users SET {total_field} = {total_field} + ? WHERE user_id = ?",
                    (amount, user_id),
                )
            cursor = await db.execute(f"SELECT {field} FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            await db.commit()
        return int(row[0]) if row else 0

    async def remove_currency_up_to(self, user_id: int, field: str, amount: int) -> tuple[int, int]:
        """Remove ate ``amount`` sem deixar o saldo negativo.

        Retorna ``(quantidade_removida, saldo_restante)`` usando uma unica
        transacao, para que a confirmacao administrativa reflita o valor real.
        """
        allowed = {"candies", "trick_coins", "dice_count", "xp_bank"}
        if field not in allowed:
            raise ValueError(f"Unsupported currency field: {field}")
        if amount <= 0:
            raise ValueError("amount must be positive")

        await self._ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(f"SELECT {field} FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            current = int(row[0]) if row else 0
            removed = min(current, amount)
            remaining = current - removed
            if removed:
                await db.execute(
                    f"UPDATE users SET {field} = ? WHERE user_id = ?",
                    (remaining, user_id),
                )
            await db.commit()
        return removed, remaining

    async def spend_currency(self, user_id: int, field: str, amount: int) -> int:
        allowed = {"candies", "trick_coins", "dice_count", "xp_bank"}
        if field not in allowed:
            raise ValueError(f"Unsupported currency field: {field}")
        if amount <= 0:
            raise ValueError("amount must be positive")

        await self._ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                f"""
                UPDATE users
                SET {field} = {field} - ?
                WHERE user_id = ?
                  AND {field} >= ?
                """,
                (amount, user_id, amount),
            )
            if not cursor.rowcount:
                await db.rollback()
                raise ValueError("not_enough_currency")
            cursor = await db.execute(f"SELECT {field} FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            await db.commit()
        return int(row[0]) if row else 0

    async def set_locale(self, user_id: int, locale: str) -> None:
        await self._ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET locale = ? WHERE user_id = ?", (locale, user_id))
            await db.commit()

    async def set_last_active_at(self, user_id: int, last_active_at: str | None = None) -> None:
        await self._ensure_user(user_id)
        value = last_active_at or datetime.now().astimezone().isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET last_active_at = ? WHERE user_id = ?", (value, user_id))
            await db.commit()

    async def increment_tricks_applied_total(self, user_id: int, amount: int = 1) -> int:
        if amount <= 0:
            raise ValueError("amount must be positive")

        await self._ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET tricks_applied_total = tricks_applied_total + ? WHERE user_id = ?",
                (amount, user_id),
            )
            cursor = await db.execute(
                "SELECT tricks_applied_total FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            await db.commit()
        return int(row[0]) if row else amount

    async def increment_event_counter(self, user_id: int, field: str, amount: int = 1) -> int:
        allowed = {
            "baskets_opened_total",
            "mute_used_total",
            "switch_used_total",
            "horror_questions_correct_total",
            "candies_given_total",
            "trick_coins_given_total",
            "profile_requests_answered_total",
            "profile_requests_sent_total",
        }
        if field not in allowed:
            raise ValueError(f"Unsupported counter field: {field}")
        if amount <= 0:
            raise ValueError("amount must be positive")

        await self._ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"UPDATE users SET {field} = {field} + ? WHERE user_id = ?",
                (amount, user_id),
            )
            cursor = await db.execute(f"SELECT {field} FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            await db.commit()
        return int(row[0]) if row else amount

    async def grant_daily_dice_if_needed(self, user_id: int, date_key: str, amount: int = 1) -> bool:
        if amount <= 0:
            raise ValueError("amount must be positive")

        await self._ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE users
                SET dice_count = dice_count + ?,
                    last_daily_dice_at = ?
                WHERE user_id = ?
                  AND (last_daily_dice_at IS NULL OR last_daily_dice_at != ?)
                """,
                (amount, date_key, user_id, date_key),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def list_xp_bank(
        self,
        *,
        only_nonzero: bool = True,
        guild_id: int | None = None,
        member_ids: Collection[int] | None = None,
    ) -> list[dict[str, int]]:
        await self.init_db()
        where_clause = "WHERE xp_bank > 0" if only_nonzero else ""
        async with aiosqlite.connect(self.path) as db:
            if guild_id is not None:
                return await self._select_guild_xp_rows(db, guild_id, member_ids)
            cursor = await db.execute(
                f"""
                SELECT user_id, xp_bank
                FROM users
                {where_clause}
                ORDER BY xp_bank DESC, user_id ASC
                """
            )
            rows = await cursor.fetchall()
        return [{"user_id": int(user_id), "xp_bank": int(xp_bank)} for user_id, xp_bank in rows]

    async def close_xp_bank(
        self,
        *,
        guild_id: int | None = None,
        member_ids: Collection[int] | None = None,
    ) -> list[dict[str, int]]:
        """Captura e zera o XP em uma transacao exclusiva.

        A lista retornada e exatamente a que foi zerada. Isso impede que dois
        fechamentos exportem o mesmo saldo e preserva XP recebido apos o commit.
        """
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if guild_id is None:
                cursor = await db.execute(
                    """
                    SELECT user_id, xp_bank
                    FROM users
                    WHERE xp_bank > 0
                    ORDER BY xp_bank DESC, user_id ASC
                    """
                )
                selected = [
                    {"user_id": int(user_id), "xp_bank": int(xp_bank)}
                    for user_id, xp_bank in await cursor.fetchall()
                ]
            else:
                selected = await self._select_guild_xp_rows(db, guild_id, member_ids)
            if selected:
                closure_id = uuid4().hex
                await db.execute(
                    """
                    INSERT INTO xp_bank_closures (
                        closure_id, guild_id, created_at, total_xp_bank, entry_count
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        closure_id,
                        guild_id,
                        datetime.now().astimezone().isoformat(),
                        sum(row["xp_bank"] for row in selected),
                        len(selected),
                    ),
                )
                await db.executemany(
                    """
                    INSERT INTO xp_bank_closure_entries (closure_id, user_id, xp_bank)
                    VALUES (?, ?, ?)
                    """,
                    [(closure_id, row["user_id"], row["xp_bank"]) for row in selected],
                )
                await self._after_xp_closure_snapshot(db, closure_id)
                await db.executemany(
                    "UPDATE users SET xp_bank = MAX(0, xp_bank - ?) WHERE user_id = ?",
                    [(row["xp_bank"], row["user_id"]) for row in selected],
                )
                if guild_id is not None and await self._table_exists(db, "user_guild_metrics"):
                    await db.executemany(
                        "UPDATE user_guild_metrics SET xp_bank = 0 WHERE guild_id = ? AND user_id = ?",
                        [(guild_id, row["user_id"]) for row in selected],
                    )
                if guild_id is not None and await self._table_exists(db, "guild_metrics"):
                    await db.execute(
                        "UPDATE guild_metrics SET xp_bank = MAX(0, xp_bank - ?) WHERE guild_id = ?",
                        (sum(row["xp_bank"] for row in selected), guild_id),
                    )
            await db.commit()
        return selected

    async def get_latest_xp_closure(self, guild_id: int) -> dict[str, Any] | None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT closure_id, guild_id, created_at, total_xp_bank, entry_count
                FROM xp_bank_closures
                WHERE guild_id = ?
                ORDER BY created_at DESC, closure_id DESC
                LIMIT 1
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            closure = dict(row)
            cursor = await db.execute(
                """
                SELECT user_id, xp_bank
                FROM xp_bank_closure_entries
                WHERE closure_id = ?
                ORDER BY xp_bank DESC, user_id ASC
                """,
                (closure["closure_id"],),
            )
            closure["entries"] = [
                {"user_id": int(user_id), "xp_bank": int(xp_bank)}
                for user_id, xp_bank in await cursor.fetchall()
            ]
        return closure

    async def _after_xp_closure_snapshot(
        self,
        db: aiosqlite.Connection,
        closure_id: str,
    ) -> None:
        return None

    async def reset_xp_bank(self) -> int:
        """Compatibilidade para chamadas antigas que fecham o saldo global."""
        rows = await self.close_xp_bank()
        return len(rows)

    async def _select_guild_xp_rows(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        member_ids: Collection[int] | None,
    ) -> list[dict[str, int]]:
        cached_member_ids = {int(user_id) for user_id in (member_ids or ())}
        cursor = await db.execute("SELECT user_id, xp_bank FROM users WHERE xp_bank > 0")
        global_balances = {int(user_id): int(xp_bank) for user_id, xp_bank in await cursor.fetchall()}
        if not global_balances:
            return []

        guild_balances: dict[int, int] = {}
        tracked_totals: dict[int, int] = {}
        if await self._table_exists(db, "user_guild_metrics"):
            cursor = await db.execute(
                "SELECT user_id, xp_bank FROM user_guild_metrics WHERE guild_id = ? AND xp_bank > 0",
                (guild_id,),
            )
            guild_balances = {int(user_id): int(xp_bank) for user_id, xp_bank in await cursor.fetchall()}
            cursor = await db.execute(
                "SELECT user_id, SUM(xp_bank) FROM user_guild_metrics WHERE xp_bank > 0 GROUP BY user_id"
            )
            tracked_totals = {int(user_id): int(xp_bank) for user_id, xp_bank in await cursor.fetchall()}

        selected: list[dict[str, int]] = []
        for user_id, global_balance in global_balances.items():
            guild_balance = guild_balances.get(user_id, 0)
            tracked_total = tracked_totals.get(user_id, 0)
            if guild_balance > 0:
                amount = min(global_balance, guild_balance)
                # Saldos antigos nao tinham origem por guild. Se toda a origem
                # conhecida pertence a esta guild, incorpora o residual legado.
                if user_id in cached_member_ids and tracked_total <= guild_balance:
                    amount = global_balance
            elif user_id in cached_member_ids and tracked_total == 0:
                amount = global_balance
            else:
                continue
            if amount > 0:
                selected.append({"user_id": user_id, "xp_bank": amount})
        return sorted(selected, key=lambda row: (-row["xp_bank"], row["user_id"]))

    async def _table_exists(self, db: aiosqlite.Connection, table_name: str) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        return await cursor.fetchone() is not None

    async def _ensure_user(self, user_id: int) -> None:
        await self.init_db()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            await db.commit()

    async def _migrate_dado_extra_inventory(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT user_id, quantity
                FROM inventory
                WHERE item_id = 'dado_extra'
                """
            )
            rows = await cursor.fetchall()
            if not rows:
                return

            for user_id, quantity in rows:
                if int(quantity) <= 0:
                    continue
                await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (int(user_id),))
                await db.execute(
                    "UPDATE users SET dice_count = dice_count + ? WHERE user_id = ?",
                    (int(quantity), int(user_id)),
                )
            await db.execute("DELETE FROM inventory WHERE item_id = 'dado_extra'")
            await db.commit()

    async def _get_inventory(self, db: aiosqlite.Connection, user_id: int) -> dict[str, int]:
        cursor = await db.execute(
            "SELECT item_id, quantity FROM inventory WHERE user_id = ? AND quantity > 0",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return {str(item_id): int(quantity) for item_id, quantity in rows}

    def _deserialize_user(self, row: Any, inventory: dict[str, int]) -> dict[str, Any]:
        user = dict(row)
        user.pop("user_id", None)
        season_stats = user.get("season_stats")
        if isinstance(season_stats, str):
            try:
                user["season_stats"] = json.loads(season_stats)
            except json.JSONDecodeError:
                user["season_stats"] = {}
        user["inventory"] = inventory
        user["board_position"] = 0
        user["board_laps"] = 0
        return user

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
