from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import aiosqlite

from cogs.profile import ProfileCog, TrickOrTreatRequestView
from repositories.guild_metrics_repo import GuildMetricsRepo
from repositories.profile_requests_repo import (
    ProfileRequestInsufficientFunds,
    ProfileRequestsRepo,
)
from repositories.users_repo import UsersRepo
from services.i18n_service import I18nService
from services.profile_request_settlement_service import ProfileRequestSettlementService


class FailingProfileRequestsRepo(ProfileRequestsRepo):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.fail_once = True

    async def _after_economy_applied(self, db: aiosqlite.Connection, settlement_key: str) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("injected settlement failure")


class ProfileRequestSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "halloween.sqlite3"
        self.users_repo = UsersRepo(self.path)
        self.requests_repo = ProfileRequestsRepo(self.path)
        self.guild_metrics_repo = GuildMetricsRepo(self.path)
        self.service = ProfileRequestSettlementService(
            path=self.path,
            requests_repo=self.requests_repo,
            users_repo=self.users_repo,
            guild_metrics_repo=self.guild_metrics_repo,
        )
        await self.users_repo.init_db()
        await self.requests_repo.init_db()
        await self.guild_metrics_repo.init_db()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _create_request(self, created_at: str = "2026-09-07T12:00:00+00:00") -> None:
        created = await self.requests_repo.try_create_active_request(
            guild_id=100,
            requester_id=10,
            target_id=20,
            created_at=created_at,
            expires_at="2099-09-07T12:30:00+00:00",
            now_iso=created_at,
        )
        self.assertTrue(created)

    async def _settle_candy(
        self,
        service: ProfileRequestSettlementService | None = None,
        *,
        milestones: list[dict] | None = None,
    ):
        return await (service or self.service).settle_once(
            guild_id=100,
            requester_id=10,
            target_id=20,
            request_created_at="2026-09-07T12:00:00+00:00",
            outcome="candy",
            amount=10,
            generosity_milestones=milestones,
        )

    async def test_two_instances_transfer_and_count_the_request_once(self) -> None:
        await self.users_repo.add_currency(20, "candies", 30)
        await self._create_request()
        second_service = ProfileRequestSettlementService(path=self.path)

        first, second = await asyncio.gather(
            self._settle_candy(),
            self._settle_candy(second_service),
        )

        self.assertEqual({first.newly_settled, second.newly_settled}, {False, True})
        requester = await self.users_repo.get_user(10)
        target = await self.users_repo.get_user(20)
        metrics = await self.guild_metrics_repo.get_metrics(100)
        self.assertEqual(requester["candies"], 10)
        self.assertEqual(requester["candies_total"], 10)
        self.assertEqual(target["candies"], 20)
        self.assertEqual(target["profile_requests_answered_total"], 1)
        self.assertEqual(target["candies_given_total"], 10)
        self.assertEqual(metrics["profile_requests_resolved_total"], 1)
        self.assertEqual(await self._table_count("profile_request_settlements"), 1)
        self.assertEqual(await self._table_count("profile_request_runtime_guild"), 0)

    async def test_retry_reuses_the_committed_outcome_and_milestone_plan(self) -> None:
        await self.users_repo.add_currency(20, "candies", 30)
        await self._create_request()
        milestones = [{"target": 10, "rewards": {"mute": 1}}]

        first = await self._settle_candy(milestones=milestones)
        retried = await self.service.settle_once(
            guild_id=100,
            requester_id=10,
            target_id=20,
            request_created_at="2026-09-07T12:00:00+00:00",
            outcome="trick",
            amount=999,
        )

        self.assertTrue(first.newly_settled)
        self.assertFalse(retried.newly_settled)
        self.assertEqual(retried.outcome, "candy")
        self.assertEqual(retried.amount, 10)
        self.assertEqual(retried.milestone_items, {"mute": 1})
        target = await self.users_repo.get_user(20)
        self.assertEqual(target["inventory"], {"mute": 1})
        self.assertEqual(target["candies"], 20)

    async def test_failure_after_debit_rolls_back_everything_and_allows_retry(self) -> None:
        await self.users_repo.add_currency(20, "candies", 30)
        await self._create_request()
        failing_repo = FailingProfileRequestsRepo(self.path)
        failing_service = ProfileRequestSettlementService(
            path=self.path,
            requests_repo=failing_repo,
        )

        with self.assertRaisesRegex(RuntimeError, "injected settlement failure"):
            await self._settle_candy(failing_service)

        requester = await self.users_repo.get_user(10)
        target = await self.users_repo.get_user(20)
        metrics = await self.guild_metrics_repo.get_metrics(100)
        self.assertEqual(requester["candies"], 0)
        self.assertEqual(target["candies"], 30)
        self.assertEqual(target["profile_requests_answered_total"], 0)
        self.assertEqual(metrics["profile_requests_resolved_total"], 0)
        self.assertEqual(await self._table_count("profile_request_settlements"), 0)
        self.assertEqual(await self._table_count("profile_request_runtime_guild"), 1)

        settled = await self._settle_candy()
        self.assertTrue(settled.newly_settled)
        self.assertEqual((await self.users_repo.get_user(10))["candies"], 10)

    async def test_insufficient_funds_keeps_the_request_pending(self) -> None:
        await self.users_repo.add_currency(20, "candies", 5)
        await self._create_request()

        with self.assertRaises(ProfileRequestInsufficientFunds):
            await self._settle_candy()

        self.assertEqual((await self.users_repo.get_user(10))["candies"], 0)
        self.assertEqual((await self.users_repo.get_user(20))["candies"], 5)
        self.assertEqual(await self._table_count("profile_request_settlements"), 0)
        self.assertEqual(await self._table_count("profile_request_runtime_guild"), 1)

    async def test_competing_candy_and_trick_clicks_commit_only_one_outcome(self) -> None:
        await self.users_repo.add_currency(20, "candies", 30)
        await self.users_repo.add_currency(20, "trick_coins", 30)
        await self._create_request()
        second_service = ProfileRequestSettlementService(path=self.path)

        candy, trick = await asyncio.gather(
            self._settle_candy(),
            second_service.settle_once(
                guild_id=100,
                requester_id=10,
                target_id=20,
                request_created_at="2026-09-07T12:00:00+00:00",
                outcome="trick",
                amount=5,
            ),
        )

        self.assertEqual(candy.outcome, trick.outcome)
        self.assertEqual(sum(result.newly_settled for result in (candy, trick)), 1)
        requester = await self.users_repo.get_user(10)
        target = await self.users_repo.get_user(20)
        if candy.outcome == "candy":
            self.assertEqual((requester["candies"], requester["trick_coins"]), (10, 0))
            self.assertEqual((target["candies"], target["trick_coins"]), (20, 30))
            self.assertEqual((target["candies_given_total"], target["trick_coins_given_total"]), (10, 0))
        else:
            self.assertEqual((requester["candies"], requester["trick_coins"]), (0, 5))
            self.assertEqual((target["candies"], target["trick_coins"]), (30, 25))
            self.assertEqual((target["candies_given_total"], target["trick_coins_given_total"]), (0, 5))
        self.assertEqual(target["profile_requests_answered_total"], 1)

    async def test_ignore_commits_counter_and_cooldown_without_currency(self) -> None:
        await self._create_request()
        settlement = await self.service.settle_once(
            guild_id=100,
            requester_id=10,
            target_id=20,
            request_created_at="2026-09-07T12:00:00+00:00",
            outcome="ignore",
            amount=999,
            cooldown_until="2099-09-07T13:00:00+00:00",
        )

        self.assertEqual(settlement.outcome, "ignore")
        self.assertEqual(settlement.amount, 0)
        self.assertEqual((await self.users_repo.get_user(20))["profile_requests_answered_total"], 1)
        metrics = await self.guild_metrics_repo.get_metrics(100)
        self.assertEqual(metrics["profile_requests_resolved_total"], 1)
        remaining = await self.requests_repo.get_cooldown_remaining_seconds(
            100,
            10,
            "2099-09-07T12:59:00+00:00",
        )
        self.assertEqual(remaining, 60)

    async def _table_count(self, table_name: str) -> int:
        allowed = {"profile_request_settlements", "profile_request_runtime_guild"}
        if table_name not in allowed:
            raise ValueError("unsupported test table")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table_name}")
            row = await cursor.fetchone()
        return int(row[0])


class ProfileRequestPresentationRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "halloween.sqlite3"
        users_repo = UsersRepo(self.path)
        requests_repo = ProfileRequestsRepo(self.path)
        guild_metrics_repo = GuildMetricsRepo(self.path)
        await users_repo.init_db()
        await requests_repo.init_db()
        await guild_metrics_repo.init_db()
        await users_repo.add_currency(20, "candies", 30)
        await requests_repo.try_create_active_request(
            guild_id=100,
            requester_id=10,
            target_id=20,
            created_at="2026-09-07T12:00:00+00:00",
            expires_at="2099-09-07T12:30:00+00:00",
            now_iso="2026-09-07T12:00:00+00:00",
        )

        cog = object.__new__(ProfileCog)
        cog.i18n = I18nService(users_repo)
        cog.profile_requests_config = {"candy_amount": 10, "trick_coins_amount": 5}
        cog.achievements_config = {}
        cog.profile_request_settlement_service = ProfileRequestSettlementService(
            path=self.path,
            requests_repo=requests_repo,
            users_repo=users_repo,
            guild_metrics_repo=guild_metrics_repo,
        )
        self.users_repo = users_repo
        self.view = TrickOrTreatRequestView(
            cog=cog,
            guild_id=100,
            requester_id=10,
            requester_name="Requester",
            target_id=20,
            locale="pt-BR",
            can_give_candy=True,
            can_give_trick=True,
            timeout_seconds=1800,
            request_created_at="2026-09-07T12:00:00+00:00",
        )

    async def asyncTearDown(self) -> None:
        self.view.stop()
        self.temp_dir.cleanup()

    async def test_discord_edit_failure_does_not_replay_the_transfer(self) -> None:
        response = SimpleNamespace(
            edit_message=AsyncMock(side_effect=[RuntimeError("discord edit failed"), None]),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=20, display_name="Target"),
            response=response,
        )

        with self.assertRaisesRegex(RuntimeError, "discord edit failed"):
            await self.view._finish(interaction, "candy")
        await self.view._finish(interaction, "candy")

        requester = await self.users_repo.get_user(10)
        target = await self.users_repo.get_user(20)
        self.assertEqual(requester["candies"], 10)
        self.assertEqual(target["candies"], 20)
        self.assertEqual(target["profile_requests_answered_total"], 1)
        self.assertEqual(response.edit_message.await_count, 2)


if __name__ == "__main__":
    unittest.main()
