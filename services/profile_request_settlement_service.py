from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from repositories.db import DB_PATH
from repositories.guild_metrics_repo import GuildMetricsRepo
from repositories.profile_requests_repo import ProfileRequestSettlement, ProfileRequestsRepo
from repositories.users_repo import UsersRepo


class ProfileRequestSettlementService:
    def __init__(
        self,
        *,
        path: Path = DB_PATH,
        requests_repo: ProfileRequestsRepo | None = None,
        users_repo: UsersRepo | None = None,
        guild_metrics_repo: GuildMetricsRepo | None = None,
    ) -> None:
        self.requests_repo = requests_repo or ProfileRequestsRepo(path)
        self.users_repo = users_repo or UsersRepo(path)
        self.guild_metrics_repo = guild_metrics_repo or GuildMetricsRepo(path)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def settle_once(
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
        await self._ensure_tables()
        return await self.requests_repo.settle_request_once(
            guild_id=guild_id,
            requester_id=requester_id,
            target_id=target_id,
            request_created_at=request_created_at,
            outcome=outcome,
            amount=amount,
            generosity_milestones=generosity_milestones,
            cooldown_until=cooldown_until,
        )

    async def _ensure_tables(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self.users_repo.init_db()
            await self.guild_metrics_repo.init_db()
            await self.requests_repo.init_db()
            self._initialized = True
