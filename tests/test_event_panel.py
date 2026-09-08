from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.evento import EventoCog
from cogs.public_events import PublicEventsCog
from repositories.admin_repo import AdminRepo
from repositories.julgamento_repo import JulgamentoRepo
from repositories.public_events_repo import PublicEventsRepo
from views.event_panel import EventPanelView


class EventCommandSurfaceTests(unittest.TestCase):
    def test_evento_replaces_the_old_manual_start_command(self) -> None:
        event_commands = [command.name for command in EventoCog.__cog_app_commands__]
        public_commands = [command.name for command in PublicEventsCog.__cog_app_commands__]

        self.assertEqual(event_commands, ["evento"])
        self.assertNotIn("iniciar", public_commands)
        self.assertIn("tempo", public_commands)


class EventPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        path = Path(self.temp_dir.name) / "event-panel.sqlite3"
        self.cog = object.__new__(EventoCog)
        self.cog.admin_repo = AdminRepo(path)
        self.cog.public_events_repo = PublicEventsRepo(path)
        self.cog.julgamento_repo = JulgamentoRepo(path)
        self.cog.bot = SimpleNamespace(get_cog=MagicMock(return_value=None))
        self.cog.audit_service = SimpleNamespace(log=AsyncMock())
        self.guild = SimpleNamespace(id=10)
        self.panel = await EventPanelView.create(
            self.cog,
            guild=self.guild,
            moderator_id=20,
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

    async def test_pages_are_compact_components_v2_and_expose_controls(self) -> None:
        expected_by_page = {
            "overview": set(),
            "control": {
                "event:control:start",
                "event:control:pause",
                "event:control:resume",
                "event:control:end",
            },
            "public": {
                "event:public:start:observer",
                "event:public:start:aranhas",
                "event:public:start:julgamento",
                "event:public:cancel",
            },
        }
        for page, expected in expected_by_page.items():
            await self.panel.refresh(page=page)
            self.assertTrue(self.panel.has_components_v2())
            self.assertLessEqual(self.panel.total_children_count, 40)
            self.assertTrue(expected.issubset(self._custom_ids()))
            self.assertIn("event:refresh", self._custom_ids())

    async def test_only_the_admin_who_opened_the_panel_can_use_it(self) -> None:
        denied = SimpleNamespace(
            user=SimpleNamespace(
                id=99,
                guild_permissions=SimpleNamespace(manage_guild=True),
            ),
            guild_id=10,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        allowed = SimpleNamespace(
            user=SimpleNamespace(
                id=20,
                guild_permissions=SimpleNamespace(manage_guild=True),
            ),
            guild_id=10,
        )

        self.assertFalse(await self.panel.interaction_check(denied))
        self.assertTrue(await self.panel.interaction_check(allowed))
        denied.response.send_message.assert_awaited_once()

    async def test_observer_button_starts_event_and_refreshes_same_panel(self) -> None:
        self.cog.start_public_event = AsyncMock(
            return_value=(True, "Observador iniciado para teste.")
        )
        await self.panel.refresh(page="public")
        button = next(
            item
            for item in self.panel.walk_children()
            if getattr(item, "custom_id", None) == "event:public:start:observer"
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await button.callback(interaction)

        self.cog.start_public_event.assert_awaited_once_with(interaction, "observer")
        interaction.response.defer.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once()
        self.assertIn("Observador iniciado", self.panel.notice)

    async def test_ending_the_global_event_requires_confirmation(self) -> None:
        await self.cog.admin_repo.set_runtime(10, event_active=True)
        self.cog._encerrar = AsyncMock(return_value=(True, "Evento encerrado."))
        await self.panel.refresh(page="control")
        end_button = next(
            item
            for item in self.panel.walk_children()
            if getattr(item, "custom_id", None) == "event:control:end"
        )
        first_interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await end_button.callback(first_interaction)

        self.cog._encerrar.assert_not_awaited()
        self.assertIn("event:confirm:end:yes", self._custom_ids())
        confirm_button = next(
            item
            for item in self.panel.walk_children()
            if getattr(item, "custom_id", None) == "event:confirm:end:yes"
        )
        confirm_interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        await confirm_button.callback(confirm_interaction)

        self.cog._encerrar.assert_awaited_once_with(confirm_interaction)
        self.assertIsNone(self.panel.confirmation)


class ManualPublicEventStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_simultaneous_clicks_start_only_one_observer(self) -> None:
        cog = object.__new__(PublicEventsCog)
        cog._manual_start_locks = {}
        active = False

        async def has_active(_guild_id: int) -> bool:
            return active

        async def start_observer(_guild, _config) -> tuple[bool, str]:
            nonlocal active
            await asyncio.sleep(0)
            active = True
            return True, "ok"

        cog._has_active_public_runtime = AsyncMock(side_effect=has_active)
        cog._start_observer = AsyncMock(side_effect=start_observer)
        cog._load_config = MagicMock(return_value={})
        cog._audit_manual_start = AsyncMock()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            user=SimpleNamespace(id=20),
        )

        first, second = await asyncio.gather(
            cog.start_manual_event(interaction, "observer"),
            cog.start_manual_event(interaction, "observer"),
        )

        self.assertEqual(sum(1 for ok, _message in (first, second) if ok), 1)
        cog._start_observer.assert_awaited_once()
        cog._audit_manual_start.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
