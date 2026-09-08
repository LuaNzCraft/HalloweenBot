from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import aiosqlite

from repositories.chat_candy_repo import ChatCandyRepo
from repositories.guild_metrics_repo import GuildMetricsRepo
from repositories.public_events_repo import PublicEventsRepo
from repositories.reward_ledger_repo import RewardLedgerRepo
from repositories.user_guild_metrics_repo import UserGuildMetricsRepo
from repositories.users_repo import UsersRepo
from services.chat_candy_runtime import ChatCandyRuntimeService
from services.i18n_service import I18nService
from services.inventory_service import InventoryService
from services.public_event_texts import PublicEventTextsService
from services.reward_settlement_service import RewardSettlementService
from cogs.julgamento import JulgamentoCog
from cogs.public_events import PublicEventsCog


class FailingRewardLedgerRepo(RewardLedgerRepo):
    async def _apply_settlement(
        self,
        db: aiosqlite.Connection,
        user_id: int,
        guild_id: int | None,
        currencies: dict[str, int],
        items: dict[str, int],
        counters: dict[str, int],
    ) -> None:
        await super()._apply_settlement(db, user_id, guild_id, currencies, items, counters)
        raise RuntimeError("injected_settlement_failure")


class RewardSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "reward-settlement.sqlite3"

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_two_instances_settle_all_reward_components_once(self) -> None:
        first_service = RewardSettlementService(path=self.db_path)
        second_service = RewardSettlementService(path=self.db_path)
        kwargs = {
            "currencies": {"candies": 5, "trick_coins": 2, "dice_count": 1, "xp_bank": 3},
            "items": {"mute": 2},
            "counters": {"horror_questions_correct_total": 1},
        }

        first, second = await asyncio.gather(
            first_service.settle_once("event:1", "test_event", 10, 20, **kwargs),
            second_service.settle_once("event:1", "test_event", 10, 20, **kwargs),
        )

        self.assertEqual(sum(result.newly_settled for result in (first, second)), 1)
        user = await UsersRepo(self.db_path).get_user(10)
        self.assertEqual(user["candies"], 5)
        self.assertEqual(user["trick_coins"], 2)
        self.assertEqual(user["dice_count"], 1)
        self.assertEqual(user["xp_bank"], 3)
        self.assertEqual(user["horror_questions_correct_total"], 1)
        self.assertEqual(user["inventory"]["mute"], 2)
        guild = await GuildMetricsRepo(self.db_path).get_metrics(20)
        self.assertEqual(guild["candies_total"], 5)
        self.assertEqual(guild["trick_coins_total"], 2)
        self.assertEqual(guild["xp_bank"], 3)
        self.assertEqual(guild["horror_questions_correct_total"], 1)
        user_guild = await UserGuildMetricsRepo(self.db_path).get_top_rows(20, "candies_total", 10)
        self.assertEqual(user_guild, [{"user_id": 10, "value": 5}])

    async def test_retry_uses_the_first_committed_plan(self) -> None:
        service = RewardSettlementService(path=self.db_path)
        original = await service.settle_once(
            "random-event:1",
            "random_event",
            10,
            20,
            currencies={"candies": 5},
            items={"mute": 1},
        )
        retried = await service.settle_once(
            "random-event:1",
            "random_event",
            10,
            20,
            currencies={"candies": 999},
            items={"switch": 999},
        )

        self.assertTrue(original.newly_settled)
        self.assertFalse(retried.newly_settled)
        self.assertEqual(retried.currencies, {"candies": 5})
        self.assertEqual(retried.items, {"mute": 1})
        user = await UsersRepo(self.db_path).get_user(10)
        self.assertEqual(user["candies"], 5)
        self.assertEqual(user["inventory"], {"mute": 1})

    async def test_failure_rolls_back_ledger_and_economy_for_retry(self) -> None:
        failing_service = RewardSettlementService(
            path=self.db_path,
            ledger_repo=FailingRewardLedgerRepo(self.db_path),
        )
        with self.assertRaisesRegex(RuntimeError, "injected_settlement_failure"):
            await failing_service.settle_once(
                "failed-event:1",
                "failed_event",
                10,
                20,
                currencies={"candies": 5},
                items={"mute": 1},
            )

        user = await UsersRepo(self.db_path).get_user(10)
        self.assertEqual(user["candies"], 0)
        self.assertEqual(user["inventory"], {})
        retried = await RewardSettlementService(path=self.db_path).settle_once(
            "failed-event:1",
            "failed_event",
            10,
            20,
            currencies={"candies": 5},
            items={"mute": 1},
        )
        self.assertTrue(retried.newly_settled)
        self.assertEqual((await UsersRepo(self.db_path).get_user(10))["candies"], 5)


class PendingEventRewardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pending-event-rewards.sqlite3"

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_chat_candy_winner_remains_pending_until_marked_settled(self) -> None:
        repo = ChatCandyRepo(self.db_path)
        await repo.init_db()
        now = datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc)
        spawn_id = await repo.create_spawn(
            10,
            20,
            30,
            102,
            [101, 102, 103],
            5,
            created_at=now,
            expires_at=now + timedelta(seconds=15),
            spawn_day="2026-09-06",
            cooldown_seconds=0,
            daily_cap=10,
        )
        status, _ = await repo.submit_attempt(10, 30, 40, 102, attempted_at=now + timedelta(seconds=1))

        self.assertEqual(status, "won")
        self.assertEqual([row["spawn_id"] for row in await repo.list_pending_reward_spawns()], [spawn_id])
        self.assertTrue(await repo.mark_reward_settled(int(spawn_id), 40, now + timedelta(seconds=2)))
        self.assertEqual(await repo.list_pending_reward_spawns(), [])

    async def test_ghostface_winner_remains_pending_until_marked_settled(self) -> None:
        repo = PublicEventsRepo(self.db_path)
        await repo.set_ghostface_state(
            10,
            active=False,
            channel_id=20,
            question_id="question-1",
            question_text="Question?",
            accepted_answers=["answer"],
            answer_match_mode="normalized",
            question_difficulty="easy",
            started_at="2026-09-06T20:00:00+00:00",
            expires_at="2026-09-06T20:01:00+00:00",
            message_id=30,
            winner_user_id=40,
        )

        pending = await repo.list_pending_ghostface_rewards()
        self.assertEqual([(row["guild_id"], row["winner_user_id"]) for row in pending], [(10, 40)])
        self.assertTrue(await repo.mark_ghostface_reward_settled(10, 40, "2026-09-06T20:00:02+00:00"))
        self.assertEqual(await repo.list_pending_ghostface_rewards(), [])

    async def test_only_one_concurrent_ghostface_winner_is_claimed(self) -> None:
        repo = PublicEventsRepo(self.db_path)
        await repo.set_ghostface_state(
            10,
            active=True,
            channel_id=20,
            question_id="question-1",
            question_text="Question?",
            accepted_answers=["answer"],
            answer_match_mode="normalized",
            question_difficulty="easy",
            started_at="2026-09-06T20:00:00+00:00",
            expires_at="2026-09-06T20:01:00+00:00",
            message_id=30,
            winner_user_id=None,
        )

        first, second = await asyncio.gather(
            repo.claim_ghostface_winner(10, 40),
            PublicEventsRepo(self.db_path).claim_ghostface_winner(10, 41),
        )

        self.assertEqual(sum((first, second)), 1)
        state = await repo.get_ghostface_state(10)
        self.assertFalse(state["active"])
        self.assertIn(state["winner_user_id"], {40, 41})
        self.assertIsNone(state["reward_settled_at"])


class EventRewardIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "event-reward-integration.sqlite3"
        self.users_repo = UsersRepo(self.db_path)
        self.i18n = I18nService(self.users_repo)
        self.settlement_service = RewardSettlementService(path=self.db_path, users_repo=self.users_repo)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_candy_shower_retry_does_not_reroll_or_repay(self) -> None:
        cog = object.__new__(PublicEventsCog)
        cog.users_repo = self.users_repo
        cog.i18n = self.i18n
        cog.inventory_service = InventoryService(users_repo=self.users_repo, i18n_service=self.i18n)
        cog.reward_settlement_service = self.settlement_service
        cog.mission_service = SimpleNamespace(evaluate_progress=AsyncMock(return_value=[]))
        original_config = {
            "reward_per_capture": {"candies": 2},
            "item_rewards": [{"item_id": "mute", "chance_percent_per_capture": 100}],
        }

        first = await cog._grant_spider_rewards(10, 20, 3, original_config, "20:event-1")
        retried = await cog._grant_spider_rewards(
            10,
            20,
            3,
            {"reward_per_capture": {"candies": 999}, "item_rewards": []},
            "20:event-1",
        )

        self.assertEqual(first, retried)
        user = await self.users_repo.get_user(10)
        self.assertEqual(user["candies"], 6)
        self.assertEqual(user["inventory"]["mute"], 3)

    async def test_chat_candy_reconciliation_pays_pending_winner_once(self) -> None:
        repo = ChatCandyRepo(self.db_path)
        await repo.init_db()
        now = datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc)
        await repo.create_spawn(
            20,
            30,
            40,
            102,
            [101, 102, 103],
            5,
            created_at=now,
            expires_at=now + timedelta(seconds=15),
            spawn_day="2026-09-06",
            cooldown_seconds=0,
            daily_cap=10,
        )
        status, _ = await repo.submit_attempt(20, 40, 10, 102, attempted_at=now + timedelta(seconds=1))
        self.assertEqual(status, "won")
        cog = SimpleNamespace(
            chat_candy_repo=repo,
            reward_settlement_service=self.settlement_service,
            mission_service=SimpleNamespace(evaluate_progress=AsyncMock(return_value=[])),
            texts=PublicEventTextsService(),
            bot=SimpleNamespace(get_channel=lambda _channel_id: object()),
        )
        runtime = ChatCandyRuntimeService()

        await runtime.reconcile_pending_rewards(cog)
        await runtime.reconcile_pending_rewards(cog)

        self.assertEqual((await self.users_repo.get_user(10))["candies"], 5)
        self.assertEqual(await repo.list_pending_reward_spawns(), [])

    async def test_ghostface_retry_preserves_reward_and_correct_answer_counter(self) -> None:
        cog = object.__new__(PublicEventsCog)
        cog.users_repo = self.users_repo
        cog.i18n = self.i18n
        cog.inventory_service = InventoryService(users_repo=self.users_repo, i18n_service=self.i18n)
        cog.reward_settlement_service = self.settlement_service
        cog.mission_service = SimpleNamespace(evaluate_progress=AsyncMock(return_value=[]))
        config = {
            "reward_pool": {
                "currency": {"grant_type": "currency", "grant_id": "candies", "amount": 5},
                "item": {"grant_type": "item", "grant_id": "mute", "amount": 1},
            },
            "difficulty_defaults": {"easy": {"reward_count": 2, "allow_baskets": True}},
        }

        first = await cog._grant_ghostface_rewards(10, config, 20, "easy", "ghostface:20:event-1")
        retried = await cog._grant_ghostface_rewards(10, {}, 20, "easy", "ghostface:20:event-1")

        self.assertEqual(first, retried)
        user = await self.users_repo.get_user(10)
        self.assertEqual(user["candies"], 5)
        self.assertEqual(user["inventory"]["mute"], 1)
        self.assertEqual(user["horror_questions_correct_total"], 1)
        guild = await GuildMetricsRepo(self.db_path).get_metrics(20)
        self.assertEqual(guild["horror_questions_correct_total"], 1)

    async def test_carrossel_retry_does_not_pay_the_winner_twice(self) -> None:
        cog = object.__new__(JulgamentoCog)
        cog.users_repo = self.users_repo
        cog.i18n = self.i18n
        cog.reward_settlement_service = self.settlement_service
        cog.mission_service = SimpleNamespace(evaluate_progress=AsyncMock(return_value=[]))
        config = {"winner_rewards": {"candies": 5, "mute": 1}}
        cog._load_config = lambda: config

        first, _ = await cog._grant_carrossel_winner_rewards(20, 10, "carrossel:20:event-1")
        config["winner_rewards"] = {"candies": 999, "switch": 999}
        retried, _ = await cog._grant_carrossel_winner_rewards(20, 10, "carrossel:20:event-1")

        self.assertEqual(first, retried)
        user = await self.users_repo.get_user(10)
        self.assertEqual(user["candies"], 5)
        self.assertEqual(user["inventory"], {"mute": 1})


if __name__ == "__main__":
    unittest.main()
