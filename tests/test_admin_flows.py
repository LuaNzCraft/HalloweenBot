from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

import aiosqlite

from cogs.evento import EventoCog
from repositories.guild_metrics_repo import GuildMetricsRepo
from repositories.missions_repo import MissionsRepo
from repositories.user_guild_metrics_repo import UserGuildMetricsRepo
from repositories.users_repo import UsersRepo
from services.public_event_runtime import PublicEventRuntimeService


class FailingXpClosureRepo(UsersRepo):
    async def _after_xp_closure_snapshot(self, db: aiosqlite.Connection, closure_id: str) -> None:
        raise RuntimeError("injected closure failure")


class UsersRepoAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "admin-tests.sqlite3"
        self.repo = UsersRepo(self.db_path)
        await self.repo.init_db()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_remove_currency_reports_only_the_amount_actually_removed(self) -> None:
        await self.repo.add_currency(10, "candies", 5)

        removed, remaining = await self.repo.remove_currency_up_to(10, "candies", 100)

        self.assertEqual((removed, remaining), (5, 0))

    async def test_close_xp_bank_is_scoped_to_guild_activity(self) -> None:
        metrics = UserGuildMetricsRepo(self.db_path)
        await self.repo.add_currency(10, "xp_bank", 50)
        await self.repo.add_currency(20, "xp_bank", 70)
        await metrics.increment_metric(1, 10, "xp_bank", 50)
        await metrics.increment_metric(2, 20, "xp_bank", 70)

        closed = await self.repo.close_xp_bank(guild_id=1, member_ids=set())

        self.assertEqual(closed, [{"user_id": 10, "xp_bank": 50}])
        self.assertEqual((await self.repo.get_user(10))["xp_bank"], 0)
        self.assertEqual((await self.repo.get_user(20))["xp_bank"], 70)

    async def test_two_concurrent_closures_cannot_export_the_same_xp(self) -> None:
        await self.repo.add_currency(10, "xp_bank", 50)

        first, second = await asyncio.gather(
            self.repo.close_xp_bank(guild_id=1, member_ids={10}),
            self.repo.close_xp_bank(guild_id=1, member_ids={10}),
        )

        exported = [rows for rows in (first, second) if rows]
        self.assertEqual(exported, [[{"user_id": 10, "xp_bank": 50}]])

    async def test_close_xp_bank_persists_durable_snapshot_with_the_reset(self) -> None:
        await self.repo.add_currency(10, "xp_bank", 50)
        await self.repo.add_currency(20, "xp_bank", 70)

        closed = await self.repo.close_xp_bank(guild_id=1, member_ids={10, 20})
        snapshot = await self.repo.get_latest_xp_closure(1)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["entries"], closed)
        self.assertEqual(snapshot["total_xp_bank"], 120)
        self.assertEqual(snapshot["entry_count"], 2)
        self.assertEqual((await self.repo.get_user(10))["xp_bank"], 0)
        self.assertEqual((await self.repo.get_user(20))["xp_bank"], 0)

    async def test_closure_failure_rolls_back_snapshot_and_xp_reset(self) -> None:
        failing_repo = FailingXpClosureRepo(self.db_path)
        await failing_repo.add_currency(10, "xp_bank", 50)

        with self.assertRaisesRegex(RuntimeError, "injected closure failure"):
            await failing_repo.close_xp_bank(guild_id=1, member_ids={10})

        self.assertEqual((await failing_repo.get_user(10))["xp_bank"], 50)
        self.assertIsNone(await failing_repo.get_latest_xp_closure(1))

    async def test_shared_member_keeps_xp_tracked_in_another_guild(self) -> None:
        metrics = UserGuildMetricsRepo(self.db_path)
        await self.repo.add_currency(10, "xp_bank", 120)
        await metrics.increment_metric(1, 10, "xp_bank", 50)
        await metrics.increment_metric(2, 10, "xp_bank", 70)

        closed = await self.repo.close_xp_bank(guild_id=1, member_ids={10})

        self.assertEqual(closed, [{"user_id": 10, "xp_bank": 50}])
        self.assertEqual((await self.repo.get_user(10))["xp_bank"], 70)
        self.assertEqual(await metrics.get_top_rows(1, "xp_bank", 10), [])
        self.assertEqual(await metrics.get_top_rows(2, "xp_bank", 10), [{"user_id": 10, "value": 70}])

    async def test_mission_xp_reward_tracks_its_guild_for_future_closures(self) -> None:
        missions = MissionsRepo(self.db_path)
        guild_metrics = GuildMetricsRepo(self.db_path)
        user_guild_metrics = UserGuildMetricsRepo(self.db_path)
        await missions.init_db()
        await guild_metrics.init_db()
        await user_guild_metrics.init_db()

        granted = await missions.grant_guild_reward_once(
            1,
            10,
            "daily_global",
            "test-period",
            {"xp_bank": 25},
            "2026-09-06T00:00:00+00:00",
        )

        self.assertTrue(granted)
        self.assertEqual((await self.repo.get_user(10))["xp_bank"], 25)
        self.assertEqual(
            await UserGuildMetricsRepo(self.db_path).get_top_rows(1, "xp_bank", 10),
            [{"user_id": 10, "value": 25}],
        )
        self.assertEqual((await GuildMetricsRepo(self.db_path).get_metrics(1))["xp_bank"], 25)


class PublicEventRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ghostface_start_returns_a_failure_reason_without_channels(self) -> None:
        cog = SimpleNamespace(_get_event_channels=AsyncMock(return_value=[]))
        guild = SimpleNamespace(id=1)

        ok, reason = await PublicEventRuntimeService().start_ghostface(cog, guild, {})

        self.assertFalse(ok)
        self.assertIn("canal", reason.lower())


class EventoLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_clears_an_old_pause(self) -> None:
        cog = object.__new__(EventoCog)
        cog.admin_repo = SimpleNamespace(
            get_runtime=AsyncMock(return_value={"event_active": False}),
            set_runtime=AsyncMock(),
        )
        cog.bot = SimpleNamespace(get_cog=MagicMock(return_value=None))
        cog._audit = AsyncMock()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await cog._comecar(interaction)

        kwargs = cog.admin_repo.set_runtime.await_args.kwargs
        self.assertIsNone(kwargs["events_paused_until"])
        self.assertIsNone(kwargs["pause_reason"])

    async def test_end_cancels_the_active_public_event(self) -> None:
        public_events_cog = SimpleNamespace(
            reset_chat_bonus_window=MagicMock(),
            cancel_active_event=AsyncMock(return_value=True),
        )
        cog = object.__new__(EventoCog)
        cog.admin_repo = SimpleNamespace(set_runtime=AsyncMock())
        cog.public_events_repo = SimpleNamespace(
            reset_ghostface_state=AsyncMock(),
            reset_spider_state=AsyncMock(),
        )
        cog.julgamento_repo = SimpleNamespace(reset_runtime=AsyncMock())
        cog.bot = SimpleNamespace(get_cog=MagicMock(return_value=public_events_cog))
        cog._audit = AsyncMock()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await cog._encerrar(interaction)

        public_events_cog.cancel_active_event.assert_awaited_once_with(interaction.guild)


if __name__ == "__main__":
    unittest.main()
