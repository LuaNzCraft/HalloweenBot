from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord

from cogs.profile import PublicProfileView
from repositories.users_repo import UsersRepo
from services.i18n_service import I18nService
from views.profile_panel import ProfilePanelData, ProfilePanelView


class ProfilePanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        users_repo = UsersRepo(Path(self.temp_dir.name) / "profile.sqlite3")
        self.i18n = I18nService(users_repo=users_repo)
        self.cog = SimpleNamespace(i18n=self.i18n)
        self.panel = ProfilePanelView(
            self.cog,
            owner_id=10,
            locale="pt-BR",
            data=ProfilePanelData(
                display_name="Jogador",
                avatar_url="https://example.com/avatar.png",
                xp_bank=1250,
                candies=320,
                trick_coins=45,
                dice_count=3,
                board_position=8,
                board_laps=2,
                candies_total=900,
                tricks_applied_total=7,
                inventory_summary="Cesta Amaldiçoada x1\nMute x2",
            ),
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

    async def test_summary_is_compact_and_uses_components_v2(self) -> None:
        self.assertTrue(self.panel.has_components_v2())
        self.assertLessEqual(self.panel.total_children_count, 40)
        self.assertEqual(len(self.panel.children), 2)
        self.assertIsInstance(self.panel.children[0], discord.ui.Container)
        self.assertIsInstance(self.panel.children[1], discord.ui.ActionRow)

        components = str(self.panel.to_components())
        self.assertIn("Recursos", components)
        self.assertIn("Jornada", components)
        self.assertNotIn("```md", components)

    async def test_inventory_tab_shows_only_the_inventory_codebox(self) -> None:
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        button = next(
            item
            for item in self.panel.walk_children()
            if getattr(item, "custom_id", None) == "profile:tab:inventory"
        )

        await button.callback(interaction)

        components = str(self.panel.to_components())
        self.assertEqual(self.panel.page, "inventory")
        self.assertIn("```md", components)
        self.assertIn("Cesta Amaldiçoada x1", components)
        self.assertNotIn("🎒", components)
        self.assertLessEqual(self.panel.total_children_count, 40)
        interaction.edit_original_response.assert_awaited_once()

    async def test_navigation_stays_outside_the_profile_card(self) -> None:
        self.assertEqual(
            self._custom_ids(),
            {
                "profile:navigation:shop",
                "profile:navigation:board",
                "profile:navigation:missions",
                "profile:navigation:baskets",
                "profile:tab:summary",
                "profile:tab:inventory",
            },
        )
        navigation = self.panel.children[1]
        self.assertTrue(
            all(item.style is discord.ButtonStyle.danger for item in navigation.children)
        )

    async def test_only_owner_can_use_profile_navigation(self) -> None:
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        self.assertFalse(await self.panel.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()

    async def test_basket_navigation_uses_the_v2_compatible_panel_transition(self) -> None:
        baskets_cog = SimpleNamespace(show_basket_panel=AsyncMock())
        interaction = SimpleNamespace(
            client=SimpleNamespace(
                get_cog=lambda name: baskets_cog if name == "BasketsCog" else None
            )
        )
        button = next(
            item
            for item in self.panel.walk_children()
            if getattr(item, "custom_id", None) == "profile:navigation:baskets"
        )

        await button.callback(interaction)

        baskets_cog.show_basket_panel.assert_awaited_once_with(interaction, 10)

    async def test_public_profile_reuses_compact_summary_without_inventory(self) -> None:
        public_panel = PublicProfileView(
            self.cog,
            guild_id=1,
            viewer_id=10,
            target_id=20,
            locale="pt-BR",
            data=self.panel.data,
            can_request=True,
        )
        self.addCleanup(public_panel.stop)

        self.assertTrue(public_panel.has_components_v2())
        self.assertEqual(len(public_panel.children), 2)
        self.assertIsInstance(public_panel.children[0], discord.ui.Container)
        self.assertIsInstance(public_panel.children[1], discord.ui.ActionRow)
        components = str(public_panel.to_components())
        self.assertIn("Perfil Publico - Jogador", components)
        self.assertIn("Recursos", components)
        self.assertIn("Jornada", components)
        self.assertNotIn("Inventario Halloween", components)
        self.assertNotIn("profile:tab:inventory", components)
        self.assertEqual(public_panel.request_button.label, "Pedir Doce")
        self.assertIs(public_panel.request_button.style, discord.ButtonStyle.danger)

    async def test_public_request_defers_before_database_work(self) -> None:
        response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())

        async def get_cooldown(*_args) -> int:
            response.defer.assert_awaited_once()
            return 12

        request_repo = SimpleNamespace(
            get_cooldown_remaining_seconds=AsyncMock(side_effect=get_cooldown)
        )
        request_cog = SimpleNamespace(
            i18n=self.i18n,
            profile_requests_repo=request_repo,
        )
        public_panel = PublicProfileView(
            request_cog,
            guild_id=1,
            viewer_id=10,
            target_id=20,
            locale="pt-BR",
            data=self.panel.data,
            can_request=True,
        )
        self.addCleanup(public_panel.stop)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            user=SimpleNamespace(id=10),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await public_panel.submit_request(interaction, public_panel.request_button)

        response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()

    async def test_failed_publication_does_not_count_as_sent_request(self) -> None:
        http_error = discord.HTTPException(
            SimpleNamespace(status=500, reason="error"),
            {"message": "failed", "code": 0},
        )
        request_repo = SimpleNamespace(
            get_cooldown_remaining_seconds=AsyncMock(return_value=0),
            try_create_active_request=AsyncMock(return_value=True),
            register_daily_request=AsyncMock(return_value=True),
            delete_active_request=AsyncMock(),
            delete_daily_request=AsyncMock(),
        )
        users_repo = SimpleNamespace(increment_event_counter=AsyncMock())
        request_cog = SimpleNamespace(
            i18n=self.i18n,
            profile_requests_repo=request_repo,
            profile_requests_config={
                "request_timeout_seconds": 1800,
                "daily_requests_per_pair": 1,
                "candy_amount": 10,
                "trick_coins_amount": 5,
            },
            profile_service=SimpleNamespace(
                get_profile=AsyncMock(return_value={"candies": 10, "trick_coins": 5}),
                users_repo=users_repo,
            ),
            _is_recently_active_in_event=AsyncMock(return_value=True),
            build_trick_or_treat_request_embed=lambda **_kwargs: discord.Embed(),
        )
        public_panel = PublicProfileView(
            request_cog,
            guild_id=1,
            viewer_id=10,
            target_id=20,
            locale="pt-BR",
            data=self.panel.data,
            can_request=True,
        )
        self.addCleanup(public_panel.stop)
        target = SimpleNamespace(id=20, display_name="Alvo", mention="<@20>")
        channel = SimpleNamespace(id=2, send=AsyncMock(side_effect=http_error))
        interaction = SimpleNamespace(
            guild=SimpleNamespace(
                id=1,
                get_member=lambda user_id: target if user_id == 20 else None,
            ),
            channel=channel,
            user=SimpleNamespace(id=10, display_name="Visitante"),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await public_panel.submit_request(interaction, public_panel.request_button)

        users_repo.increment_event_counter.assert_not_awaited()
        request_repo.delete_active_request.assert_awaited_once()
        request_repo.delete_daily_request.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
