from __future__ import annotations

from datetime import datetime, timezone

from repositories.admin_repo import AdminRepo


class EventRuntimeService:
    def __init__(self, admin_repo: AdminRepo | None = None) -> None:
        self.admin_repo = admin_repo or AdminRepo()

    async def is_event_active(self, guild_id: int) -> bool:
        runtime = await self.admin_repo.get_runtime(guild_id)
        return bool(runtime["event_active"])

    async def are_public_events_paused(self, guild_id: int) -> bool:
        runtime = await self.admin_repo.get_runtime(guild_id)
        paused_until = runtime.get("events_paused_until")
        if not paused_until:
            return False

        try:
            paused_until_dt = datetime.fromisoformat(paused_until)
        except ValueError:
            return False

        if paused_until_dt > datetime.now(timezone.utc):
            return True

        await self.admin_repo.set_runtime(guild_id, events_paused_until=None, pause_reason=None)
        return False

    async def can_run_public_event(self, guild_id: int) -> bool:
        return await self.is_event_active(guild_id) and not await self.are_public_events_paused(guild_id)
