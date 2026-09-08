from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord

from cogs.baskets import (
    BASKET_CURSED_COLOR,
    BASKET_EMPTY_COLOR,
    BasketsCog,
)
from repositories.users_repo import UsersRepo
from services.i18n_service import I18nService
from views.basket_panel import BasketPanelData, BasketPanelView


class BasketPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        users_repo = UsersRepo(Path(self.temp_dir.name) / "baskets.sqlite3")
        i18n = I18nService(users_repo=users_repo)
        self.cog = SimpleNamespace(
            i18n=i18n,
            open_baskets_from_panel=AsyncMock(),
        )
        self.panel = BasketPanelView(
            self.cog,
            owner_id=10,
            locale="pt-BR",
            data=BasketPanelData(
                inventory={
                    "cesta_abobora": 3,
                    "cesta_doce_epica": 2,
                    "cesta_doce_amaldicoada": 1,
                },
                basket_count=6,
                active_key="cursed",
                accent_color=BASKET_CURSED_COLOR,
                icon_filename="cesta_amaldicoada_thumb.png",
                user_name="Jogador",
            ),
        )

    async def asyncTearDown(self) -> None:
        self.panel.stop()
        self.temp_dir.cleanup()

    def _custom_ids(self, panel: BasketPanelView | None = None) -> set[str]:
        active_panel = panel or self.panel
        return {
            item.custom_id
            for item in active_panel.walk_children()
            if getattr(item, "custom_id", None)
        }

    async def test_panel_uses_v2_and_keeps_actions_inside_component_limit(self) -> None:
        self.assertTrue(self.panel.has_components_v2())
        self.assertLessEqual(self.panel.total_children_count, 40)
        self.assertEqual(
            self._custom_ids(),
            {
                "baskets:open:1",
                "baskets:open:5",
                "baskets:navigation:profile",
                "baskets:navigation:board",
                "baskets:navigation:missions",
            },
        )
        components = str(self.panel.to_components())
        self.assertIn("Seu estoque", components)
        self.assertIn("Proxima abertura", components)
        self.assertIn("Cesta Amaldiçoada", components)
        self.assertNotIn("🍬 Doces", components)

    async def test_reward_result_preserves_the_blackbox(self) -> None:
        result = "```yaml\n+ 20 Moedas Doce\n```"
        panel = BasketPanelView(
            self.cog,
            owner_id=10,
            locale="pt-BR",
            data=BasketPanelData(
                inventory={"cesta_abobora": 1},
                basket_count=1,
                active_key="normal",
                accent_color=discord.Color.orange(),
                icon_filename="cesta_doce_thumb.png",
                result_text=result,
                result_title="Cestas abertas: 1",
            ),
        )
        self.addAsyncCleanup(self._stop_panel, panel)

        components = str(panel.to_components())
        self.assertIn("```yaml", components)
        self.assertIn("+ 20 Moedas Doce", components)
        self.assertIn("Cestas abertas: 1", components)

    async def test_empty_stock_disables_both_open_buttons(self) -> None:
        panel = BasketPanelView(
            self.cog,
            owner_id=10,
            locale="pt-BR",
            data=BasketPanelData(
                inventory={},
                basket_count=0,
                active_key="empty",
                accent_color=BASKET_EMPTY_COLOR,
                icon_filename="cesta_doce_thumb.png",
            ),
        )
        self.addAsyncCleanup(self._stop_panel, panel)

        buttons = {
            item.custom_id: item
            for item in panel.walk_children()
            if getattr(item, "custom_id", "").startswith("baskets:open:")
        }
        self.assertTrue(buttons["baskets:open:1"].disabled)
        self.assertTrue(buttons["baskets:open:5"].disabled)

    async def test_only_owner_can_use_panel(self) -> None:
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        self.assertFalse(await self.panel.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()

    async def test_profile_button_opens_a_separate_legacy_profile_response(self) -> None:
        profile_cog = SimpleNamespace(send_profile_panel=AsyncMock())
        interaction = SimpleNamespace(
            client=SimpleNamespace(get_cog=lambda name: profile_cog if name == "ProfileCog" else None),
        )
        profile_button = next(
            item
            for item in self.panel.walk_children()
            if getattr(item, "custom_id", None) == "baskets:navigation:profile"
        )

        await profile_button.callback(interaction)

        profile_cog.send_profile_panel.assert_awaited_once_with(interaction, 10)

    async def test_consecutive_open_actions_keep_the_same_live_panel(self) -> None:
        cog = object.__new__(BasketsCog)
        cog.i18n = self.cog.i18n
        cog.inventory_service = SimpleNamespace(
            open_pumpkin_baskets=AsyncMock(
                side_effect=lambda _user_id, quantity, **_kwargs: SimpleNamespace(
                    ok=True,
                    opened=quantity,
                    message=f"**+ abertura {quantity}x**",
                )
            )
        )

        async def rebuild(*_args: object, existing_panel: BasketPanelView, **_kwargs: object):
            existing_panel.refresh(existing_panel.data)
            return existing_panel, SimpleNamespace()

        cog._build_panel = AsyncMock(side_effect=rebuild)
        self.panel.cog = cog

        def make_interaction() -> SimpleNamespace:
            return SimpleNamespace(
                guild=None,
                response=SimpleNamespace(defer=AsyncMock()),
                edit_original_response=AsyncMock(return_value=SimpleNamespace()),
            )

        first = make_interaction()
        second = make_interaction()
        await cog.open_baskets_from_panel(first, 10, 1, self.panel)
        await cog.open_baskets_from_panel(second, 10, 5, self.panel)

        self.assertIs(first.edit_original_response.await_args.kwargs["view"], self.panel)
        self.assertIs(second.edit_original_response.await_args.kwargs["view"], self.panel)
        self.assertFalse(self.panel.is_finished())
        quantities = [call.args[1] for call in cog.inventory_service.open_pumpkin_baskets.await_args_list]
        self.assertEqual(quantities, [1, 5])

    async def test_visual_priority_prefers_cursed_then_epic_then_normal(self) -> None:
        cog = object.__new__(BasketsCog)

        color, _path, key = cog._resolve_panel_visuals(
            {
                "cesta_abobora": 2,
                "cesta_doce_epica": 1,
                "cesta_doce_amaldicoada": 1,
            },
            4,
        )
        self.assertEqual(key, "cursed")
        self.assertEqual(color, BASKET_CURSED_COLOR)

        _color, _path, key = cog._resolve_panel_visuals(
            {"cesta_abobora": 2, "cesta_doce_epica": 1},
            3,
        )
        self.assertEqual(key, "epic")

        _color, _path, key = cog._resolve_panel_visuals(
            {"cesta_abobora": 2},
            2,
        )
        self.assertEqual(key, "normal")

    async def test_reward_formatter_removes_embed_markdown_only(self) -> None:
        cog = object.__new__(BasketsCog)

        result = cog._format_reward_block("**+ 20 Doces**\n**+ 1 dado**")

        self.assertEqual(result, "```yaml\n+ 20 Doces\n+ 1 dado\n```")

    async def _stop_panel(self, panel: BasketPanelView) -> None:
        panel.stop()


if __name__ == "__main__":
    unittest.main()
