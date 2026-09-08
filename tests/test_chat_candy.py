from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

import discord

from repositories.chat_candy_repo import ChatCandyRepo
from services.chat_candy_runtime import ChatCandyRuntimeService
from services.public_event_texts import PublicEventTextsService


class ChatCandyRepoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = ChatCandyRepo(Path(self.temp_dir.name) / "chat-candy.sqlite3")
        await self.repo.init_db()
        self.now = datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _create_spawn(self, *, message_id: int = 30) -> int:
        spawn_id = await self.repo.create_spawn(
            10,
            20,
            message_id,
            102,
            [101, 102, 103],
            1,
            created_at=self.now,
            expires_at=self.now + timedelta(seconds=15),
            spawn_day="2026-09-05",
            cooldown_seconds=0,
            daily_cap=150,
        )
        self.assertIsNotNone(spawn_id)
        return int(spawn_id)

    async def test_wrong_choice_consumes_the_users_only_attempt(self) -> None:
        await self._create_spawn()

        first_status, _ = await self.repo.submit_attempt(
            10, 30, 40, 101, attempted_at=self.now + timedelta(seconds=1)
        )
        second_status, _ = await self.repo.submit_attempt(
            10, 30, 40, 102, attempted_at=self.now + timedelta(seconds=2)
        )

        self.assertEqual(first_status, "incorrect")
        self.assertEqual(second_status, "already_attempted")

    async def test_only_one_concurrent_correct_reaction_can_win(self) -> None:
        await self._create_spawn()

        results = await asyncio.gather(
            self.repo.submit_attempt(10, 30, 41, 102, attempted_at=self.now + timedelta(seconds=1)),
            self.repo.submit_attempt(10, 30, 42, 102, attempted_at=self.now + timedelta(seconds=1)),
        )

        self.assertEqual(sum(status == "won" for status, _ in results), 1)

    async def test_spawn_respects_global_cooldown_and_daily_cap(self) -> None:
        await self._create_spawn()
        active = await self.repo.list_active_spawns()
        await self.repo.close_spawn(int(active[0]["spawn_id"]), closed_at=self.now + timedelta(seconds=1))

        during_cooldown = await self.repo.can_spawn(
            10,
            now=self.now + timedelta(seconds=10),
            spawn_day="2026-09-05",
            cooldown_seconds=30,
            daily_cap=1,
        )
        after_cooldown_but_capped = await self.repo.can_spawn(
            10,
            now=self.now + timedelta(seconds=40),
            spawn_day="2026-09-05",
            cooldown_seconds=30,
            daily_cap=1,
        )
        next_day = await self.repo.can_spawn(
            10,
            now=self.now + timedelta(days=1),
            spawn_day="2026-09-06",
            cooldown_seconds=30,
            daily_cap=1,
        )

        self.assertFalse(during_cooldown)
        self.assertFalse(after_cooldown_but_capped)
        self.assertTrue(next_day)


class ChatCandyConfigTests(unittest.TestCase):
    def test_config_rejects_non_finite_numbers_and_bounds_options(self) -> None:
        config = ChatCandyRuntimeService.normalize_config(
            {
                "enabled": True,
                "emoji_ids": [1, 2, 3, 4],
                "spawn_chance_percent": float("nan"),
                "capture_seconds": float("inf"),
                "min_options": 50,
                "max_options": 2,
            }
        )

        self.assertEqual(config["spawn_chance_percent"], 60.0)
        self.assertEqual(config["capture_seconds"], 15.0)
        self.assertEqual(config["min_options"], 4)
        self.assertEqual(config["max_options"], 4)

    def test_spawn_embed_explains_the_single_attempt_rule(self) -> None:
        embed = PublicEventTextsService().build_chat_candy_spawn_embed(
            discord.Color.purple(),
            target_emoji="<:doce:123>",
            reward_candies=1,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=15),
            option_count=3,
        )

        self.assertIn("uma tentativa", embed.description.lower())
        self.assertIn("<:doce:123>", embed.description)


if __name__ == "__main__":
    unittest.main()
