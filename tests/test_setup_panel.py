from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from cogs.leaderboard import LeaderboardCog
from cogs.setup import SetupCog
from repositories.admin_repo import AdminRepo
from views.setup_panel import ROLE_SETTINGS, SetupPanelView


class SetupCommandSurfaceTests(unittest.TestCase):
    def test_setup_is_one_root_command_instead_of_a_command_group(self) -> None:
        cog = SetupCog(SimpleNamespace())

        qualified_names = [command.qualified_name for command in cog.walk_app_commands()]

        self.assertEqual(qualified_names, ["setup"])

    def test_leaderboard_is_no_longer_a_slash_command(self) -> None:
        self.assertEqual(list(LeaderboardCog.__cog_app_commands__), [])


class SetupLeaderboardActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_delegates_publication_to_the_leaderboard_cog(self) -> None:
        publisher = AsyncMock(return_value=(True, "Painel atualizado."))
        leaderboard_cog = SimpleNamespace(publish_or_update_panel=publisher)
        bot = SimpleNamespace(get_cog=MagicMock(return_value=leaderboard_cog))
        cog = SetupCog(bot)
        guild = SimpleNamespace(id=10)
        interaction = SimpleNamespace(guild=guild, user=SimpleNamespace(id=50))

        result = await cog.publish_leaderboard(interaction)

        self.assertEqual(result, (True, "Painel atualizado."))
        bot.get_cog.assert_called_once_with("LeaderboardCog")
        publisher.assert_awaited_once_with(guild, actor_id=50)


class SetupRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = AdminRepo(Path(self.temp_dir.name) / "setup.sqlite3")

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_event_channel_selection_replaces_deduplicates_and_clears_the_list(self) -> None:
        await self.repo.set_event_channels(10, [20, 20, 30])
        self.assertEqual((await self.repo.get_config(10))["event_channel_ids"], [20, 30])

        await self.repo.set_event_channels(10, [])
        self.assertEqual((await self.repo.get_config(10))["event_channel_ids"], [])

    async def test_single_channel_and_role_can_be_cleared(self) -> None:
        await self.repo.set_channel(10, "leaderboard", 20)
        await self.repo.set_role(10, "notificacao", 30)
        await self.repo.set_channel(10, "leaderboard", None)
        await self.repo.set_role(10, "notificacao", None)

        config = await self.repo.get_config(10)
        self.assertIsNone(config["leaderboard_channel_id"])
        self.assertIsNone(config["notification_role_id"])


class SetupPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = AdminRepo(Path(self.temp_dir.name) / "panel.sqlite3")
        self.guild = SimpleNamespace(
            id=10,
            get_channel=lambda _channel_id: None,
            get_role=lambda _role_id: None,
        )
        self.panel = await SetupPanelView.create(
            SimpleNamespace(admin_repo=self.repo),
            guild=self.guild,
            moderator_id=50,
        )

    async def asyncTearDown(self) -> None:
        self.panel.stop()
        self.temp_dir.cleanup()

    def _custom_ids(self) -> set[str]:
        return {
            item.custom_id
            for item in self.panel.walk_children()
            if getattr(item, "custom_id", None)
        }

    async def test_every_page_serializes_as_components_v2_under_discord_limit(self) -> None:
        for page in ("overview", "channels", "roles", "carousel", "system"):
            await self.panel.refresh(page=page)

            self.assertTrue(self.panel.has_components_v2())
            self.assertLessEqual(self.panel.total_children_count, 40)
            self.assertTrue(self.panel.to_components())

    async def test_pages_expose_all_setup_controls(self) -> None:
        await self.panel.refresh(page="channels")
        self.assertTrue(
            {
                "setup:channel:leaderboard",
                "setup:channel:logs",
                "setup:channel:julgamento",
                "setup:channel:eventos",
                "setup:leaderboard:publish",
            }.issubset(self._custom_ids())
        )

        await self.panel.refresh(page="roles")
        expected_roles = {f"setup:role:{setting}" for setting, _key, _label in ROLE_SETTINGS}
        self.assertTrue(expected_roles.issubset(self._custom_ids()))

        await self.panel.refresh(page="carousel")
        self.assertIn("setup:carousel:build", self._custom_ids())

        await self.panel.refresh(page="system")
        self.assertIn("setup:system:reload", self._custom_ids())

    async def test_leaderboard_publication_requires_a_configured_channel(self) -> None:
        await self.panel.refresh(page="channels")
        button = next(
            item
            for item in self.panel.walk_children()
            if getattr(item, "custom_id", None) == "setup:leaderboard:publish"
        )
        self.assertTrue(button.disabled)

        await self.repo.set_channel(self.guild.id, "leaderboard", 20)
        await self.panel.refresh(page="channels")
        button = next(
            item
            for item in self.panel.walk_children()
            if getattr(item, "custom_id", None) == "setup:leaderboard:publish"
        )
        self.assertFalse(button.disabled)

    async def test_only_the_moderator_who_opened_the_panel_can_use_it(self) -> None:
        rejected = SimpleNamespace(
            guild_id=10,
            user=SimpleNamespace(
                id=51,
                guild_permissions=SimpleNamespace(manage_guild=True),
            ),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        self.assertFalse(await self.panel.interaction_check(rejected))
        rejected.response.send_message.assert_awaited_once()

        accepted = SimpleNamespace(
            guild_id=10,
            user=SimpleNamespace(
                id=50,
                guild_permissions=SimpleNamespace(manage_guild=True),
            ),
        )
        self.assertTrue(await self.panel.interaction_check(accepted))


if __name__ == "__main__":
    unittest.main()
