from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord

from cogs.shop import ShopCog
from repositories.admin_repo import AdminRepo
from repositories.users_repo import UsersRepo
from services.shop_service import ShopService
from views.shop_panel import ShopPanelView


class ShopPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "shop.sqlite3"
        self.users_repo = UsersRepo(self.db_path)
        self.cog = ShopCog(SimpleNamespace())
        self.cog.admin_repo = AdminRepo(self.db_path)
        self.cog.shop_service = ShopService(users_repo=self.users_repo)
        self.cog.i18n = self.cog.shop_service.i18n
        self.panel = await ShopPanelView.create(
            self.cog,
            owner_id=10,
            locale="pt-BR",
            guild=None,
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

    async def test_currency_tabs_split_items_and_offer_quick_quantities(self) -> None:
        self.assertTrue(self.panel.has_components_v2())
        self.assertLessEqual(self.panel.total_children_count, 40)
        candy_ids = self._custom_ids()
        self.assertIn("shop:currency:candies", candy_ids)
        self.assertIn("shop:currency:trick_coins", candy_ids)
        self.assertIn("shop:buy:cesta_abobora:1", candy_ids)
        self.assertIn("shop:buy:cesta_abobora:5", candy_ids)
        self.assertIn("shop:buy:cesta_abobora:10", candy_ids)
        self.assertNotIn("shop:buy:mute:1", candy_ids)

        self.assertIn("shop:buy:cargo_loja:1", candy_ids)
        self.assertNotIn("shop:buy:cargo_loja:5", candy_ids)
        self.assertNotIn("shop:buy:cargo_loja:10", candy_ids)

        await self.panel.refresh(currency="trick_coins")

        self.assertLessEqual(self.panel.total_children_count, 40)
        trick_ids = self._custom_ids()
        self.assertIn("shop:buy:mute:1", trick_ids)
        self.assertIn("shop:buy:mute:5", trick_ids)
        self.assertIn("shop:buy:mute:10", trick_ids)
        self.assertNotIn("shop:buy:cesta_abobora:1", trick_ids)
        self.panel.to_components()

    async def test_purchase_notice_keeps_the_panel_inside_the_component_limit(self) -> None:
        await self.panel.refresh(notice="✅ Compra concluida.")

        self.assertLessEqual(self.panel.total_children_count, 40)
        self.panel.to_components()

    async def test_large_currency_catalog_is_paginated_below_the_component_limit(self) -> None:
        original = self.cog.shop_service.get("mute")
        self.assertIsNotNone(original)
        self.cog.shop_service._entries = {
            f"extra_{index}": replace(original, id=f"extra_{index}", price=index + 1)
            for index in range(8)
        }

        await self.panel.refresh(currency="trick_coins")

        self.assertLessEqual(self.panel.total_children_count, 40)
        self.assertIn("shop:page:1", self._custom_ids())
        self.panel.to_components()

    async def test_only_the_owner_can_use_the_shop_panel(self) -> None:
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        self.assertFalse(await self.panel.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()

    async def test_quick_quantity_purchase_uses_the_existing_shop_service(self) -> None:
        await self.users_repo.add_currency(10, "trick_coins", 1000)
        interaction = SimpleNamespace(user=SimpleNamespace(id=10), guild=None)

        ok, _message = await self.cog.purchase_entry(interaction, "mute", 5)

        profile = await self.users_repo.get_user(10)
        self.assertTrue(ok)
        self.assertEqual(profile["inventory"]["mute"], 5)
        self.assertEqual(profile["trick_coins"], 825)

    async def test_insufficient_balance_uses_the_players_currency_language(self) -> None:
        await self.users_repo.set_locale(10, "en-US")

        result = await self.cog.shop_service.purchase(10, "mute", 1)

        self.assertFalse(result.ok)
        self.assertIn("Trick Coins", result.message)
        self.assertNotIn("Moedas Travessas", result.message)

    async def test_invalid_grant_is_rejected_before_charging_the_player(self) -> None:
        await self.users_repo.add_currency(10, "trick_coins", 100)
        original = self.cog.shop_service.get("mute")
        self.assertIsNotNone(original)
        broken = replace(original, id="broken", grant_type="unknown")
        self.cog.shop_service._entries = {"broken": broken}

        result = await self.cog.shop_service.purchase(10, "broken", 1)

        profile = await self.users_repo.get_user(10)
        self.assertFalse(result.ok)
        self.assertEqual(profile["trick_coins"], 100)

    async def test_concurrent_role_clicks_charge_only_once(self) -> None:
        role = SimpleNamespace(id=70, mention="<@&70>")
        member_roles: list[object] = []

        async def grant_role(granted_role: object, **_kwargs: object) -> None:
            member_roles.append(granted_role)

        member = SimpleNamespace(
            id=10,
            roles=member_roles,
            add_roles=AsyncMock(side_effect=grant_role),
        )
        guild = SimpleNamespace(id=20, get_role=lambda _role_id: role)
        interaction = SimpleNamespace(user=member, guild=guild)
        await self.cog.admin_repo.set_role(20, "loja", role.id)
        await self.users_repo.add_currency(10, "candies", 2000)

        results = await asyncio.gather(
            self.cog.purchase_entry(interaction, "cargo_loja", 1),
            self.cog.purchase_entry(interaction, "cargo_loja", 1),
        )

        profile = await self.users_repo.get_user(10)
        self.assertEqual(sum(ok for ok, _message in results), 1)
        self.assertEqual(profile["candies"], 1000)
        self.assertEqual(member.add_roles.await_count, 1)

    async def test_role_delivery_failure_refunds_the_configured_currency(self) -> None:
        role = SimpleNamespace(id=70, mention="<@&70>")
        forbidden = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"message": "Missing Permissions", "code": 50013},
        )
        member = SimpleNamespace(
            id=10,
            roles=[],
            add_roles=AsyncMock(side_effect=forbidden),
        )
        guild = SimpleNamespace(id=20, get_role=lambda _role_id: role)
        interaction = SimpleNamespace(user=member, guild=guild)
        await self.cog.admin_repo.set_role(20, "loja", role.id)
        await self.users_repo.add_currency(10, "candies", 1000)

        ok, _message = await self.cog.purchase_entry(interaction, "cargo_loja", 1)

        profile = await self.users_repo.get_user(10)
        self.assertFalse(ok)
        self.assertEqual(profile["candies"], 1000)


if __name__ == "__main__":
    unittest.main()
