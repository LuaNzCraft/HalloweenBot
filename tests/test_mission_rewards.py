from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import aiosqlite

from repositories.guild_metrics_repo import GuildMetricsRepo
from repositories.missions_repo import MissionsRepo
from repositories.user_guild_metrics_repo import UserGuildMetricsRepo
from repositories.users_repo import UsersRepo
from services.mission_service import MissionService


class FailingMissionRewardRepo(MissionsRepo):
    async def _apply_reward_rows(
        self,
        db: aiosqlite.Connection,
        user_id: int,
        guild_id: int | None,
        rewards: dict[str, int],
    ) -> None:
        raise RuntimeError("injected_reward_failure")


class MissionRewardConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_path = root / "mission-rewards.sqlite3"
        self.missions_path = root / "missions.json"
        self.missions_path.write_text(
            json.dumps(
                {
                    "daily_personal": [self._mission("personal_test")],
                    "daily_global": [self._mission("global_test")],
                    "event_global": [self._mission("event_test")],
                }
            ),
            encoding="utf-8",
        )
        self.users_repo = UsersRepo(self.db_path)
        await self.users_repo.init_db()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def _mission(self, mission_id: str) -> dict[str, object]:
        return {
            "id": mission_id,
            "metric": "candies_total",
            "target": 1,
            "rewards": {"candies": 5},
            "title_pt": "Teste",
            "title_en": "Test",
            "description_pt": "Teste",
            "description_en": "Test",
        }

    def _service(self, missions_repo: MissionsRepo | None = None) -> MissionService:
        return MissionService(
            path=self.db_path,
            missions_path=self.missions_path,
            missions_repo=missions_repo,
        )

    async def test_two_service_instances_grant_personal_reward_once(self) -> None:
        first_service = self._service()
        second_service = self._service()
        await first_service.get_personal_daily(10, "pt-BR")
        await self.users_repo.add_currency(10, "candies", 1)

        first, second = await asyncio.gather(
            first_service.get_personal_daily(10, "pt-BR"),
            second_service.get_personal_daily(10, "pt-BR"),
        )

        self.assertEqual(sum(result.reward_just_granted for result in (first, second)), 1)
        self.assertTrue(first.reward_claimed)
        self.assertTrue(second.reward_claimed)
        self.assertEqual((await self.users_repo.get_user(10))["candies"], 6)

    async def test_two_service_instances_grant_global_reward_once(self) -> None:
        first_service = self._service()
        second_service = self._service()
        await first_service.get_global_daily(20, 10, "pt-BR")
        await GuildMetricsRepo(self.db_path).increment_metric(20, "candies_total", 1)

        first, second = await asyncio.gather(
            first_service.get_global_daily(20, 10, "pt-BR"),
            second_service.get_global_daily(20, 10, "pt-BR"),
        )

        self.assertEqual(sum(result.reward_just_granted for result in (first, second)), 1)
        self.assertEqual((await self.users_repo.get_user(10))["candies"], 5)
        self.assertEqual((await GuildMetricsRepo(self.db_path).get_metrics(20))["candies_total"], 6)
        self.assertEqual(
            await UserGuildMetricsRepo(self.db_path).get_top_rows(20, "candies_total", 10),
            [{"user_id": 10, "value": 5}],
        )

    async def test_failed_reward_rolls_back_claim_and_can_be_retried(self) -> None:
        normal_service = self._service()
        await normal_service.get_personal_daily(10, "pt-BR")
        await self.users_repo.add_currency(10, "candies", 1)
        failing_service = self._service(FailingMissionRewardRepo(self.db_path))

        with self.assertRaisesRegex(RuntimeError, "injected_reward_failure"):
            await failing_service.get_personal_daily(10, "pt-BR")

        state = await MissionsRepo(self.db_path).get_user_state(
            10,
            "daily_personal",
            normal_service._today_key(),
        )
        self.assertIsNone(state["reward_granted_at"])
        self.assertEqual((await self.users_repo.get_user(10))["candies"], 1)

        retried = await normal_service.get_personal_daily(10, "pt-BR")
        self.assertTrue(retried.reward_just_granted)
        self.assertEqual((await self.users_repo.get_user(10))["candies"], 6)


if __name__ == "__main__":
    unittest.main()
