from __future__ import annotations

import asyncio
from pathlib import Path

from repositories.db import DB_PATH
from repositories.guild_metrics_repo import GuildMetricsRepo
from repositories.reward_ledger_repo import RewardLedgerRepo, RewardSettlement
from repositories.user_guild_metrics_repo import UserGuildMetricsRepo
from repositories.users_repo import UsersRepo


class RewardSettlementService:
    def __init__(
        self,
        *,
        path: Path = DB_PATH,
        users_repo: UsersRepo | None = None,
        guild_metrics_repo: GuildMetricsRepo | None = None,
        user_guild_metrics_repo: UserGuildMetricsRepo | None = None,
        ledger_repo: RewardLedgerRepo | None = None,
    ) -> None:
        self.users_repo = users_repo or UsersRepo(path)
        self.guild_metrics_repo = guild_metrics_repo or GuildMetricsRepo(path)
        self.user_guild_metrics_repo = user_guild_metrics_repo or UserGuildMetricsRepo(path)
        self.ledger_repo = ledger_repo or RewardLedgerRepo(path)
        self._base_initialized = False
        self._guild_tables_initialized = False
        self._init_lock = asyncio.Lock()

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
        await self._ensure_tables(guild_id)
        return await self.ledger_repo.settle_once(
            settlement_key,
            source,
            user_id,
            guild_id,
            currencies=currencies,
            items=items,
            counters=counters,
        )

    async def get_settlement(self, settlement_key: str) -> RewardSettlement | None:
        await self._ensure_tables(None)
        return await self.ledger_repo.get_settlement(settlement_key)

    async def _ensure_tables(self, guild_id: int | None) -> None:
        if self._base_initialized and (guild_id is None or self._guild_tables_initialized):
            return
        async with self._init_lock:
            if self._base_initialized and (guild_id is None or self._guild_tables_initialized):
                return
            if not self._base_initialized:
                await self.users_repo.init_db()
                await self.ledger_repo.init_db()
                self._base_initialized = True
            if guild_id is not None and not self._guild_tables_initialized:
                await self.guild_metrics_repo.init_db()
                await self.user_guild_metrics_repo.init_db()
                self._guild_tables_initialized = True
