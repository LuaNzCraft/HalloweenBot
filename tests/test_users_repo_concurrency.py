from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from repositories.users_repo import UsersRepo


class UsersRepoConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "users-concurrency.sqlite3"
        self.repo = UsersRepo(self.db_path)
        await self.repo.init_db()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_update_user_serializes_concurrent_snapshot_mutations(self) -> None:
        other_repo = UsersRepo(self.db_path)

        def increment(payload: dict) -> None:
            payload["baskets_opened_total"] = int(payload.get("baskets_opened_total", 0)) + 1

        repos = (self.repo, other_repo)
        await asyncio.gather(*(repos[index % 2].update_user(10, increment) for index in range(20)))

        user = await self.repo.get_user(10)
        self.assertEqual(user["baskets_opened_total"], 20)

    async def test_snapshot_mutation_preserves_concurrent_economy_operations(self) -> None:
        await self.repo.add_inventory_item(10, "mute", 1)
        await self.repo.add_currency(10, "candies", 10)
        other_repo = UsersRepo(self.db_path)

        def increment_profile_and_inventory(payload: dict) -> None:
            payload["profile_requests_answered_total"] = (
                int(payload.get("profile_requests_answered_total", 0)) + 1
            )
            inventory = payload.setdefault("inventory", {})
            inventory["switch"] = int(inventory.get("switch", 0)) + 1

        await asyncio.gather(
            self.repo.update_user(10, increment_profile_and_inventory),
            other_repo.add_currency(10, "candies", 5),
            other_repo.spend_currency(10, "candies", 4),
            other_repo.add_inventory_item(10, "interferencia", 1),
            other_repo.remove_inventory_item(10, "mute", 1),
        )

        user = await self.repo.get_user(10)
        self.assertEqual(user["profile_requests_answered_total"], 1)
        self.assertEqual(user["candies"], 11)
        self.assertEqual(user["inventory"]["switch"], 1)
        self.assertEqual(user["inventory"]["interferencia"], 1)
        self.assertNotIn("mute", user["inventory"])

    async def test_only_one_concurrent_removal_can_consume_the_last_item(self) -> None:
        await self.repo.add_inventory_item(10, "mute", 1)
        other_repo = UsersRepo(self.db_path)

        results = await asyncio.gather(
            self.repo.remove_inventory_item(10, "mute", 1),
            other_repo.remove_inventory_item(10, "mute", 1),
            return_exceptions=True,
        )

        successes = [result for result in results if not isinstance(result, Exception)]
        failures = [result for result in results if isinstance(result, ValueError)]
        self.assertEqual(successes, [0])
        self.assertEqual(len(failures), 1)
        self.assertEqual(str(failures[0]), "not_enough_items")
        self.assertNotIn("mute", (await self.repo.get_user(10))["inventory"])


if __name__ == "__main__":
    unittest.main()
