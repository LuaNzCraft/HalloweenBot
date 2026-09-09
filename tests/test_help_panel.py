from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from services.i18n_service import I18nService
from views.help_panel import HELP_PAGES, HelpPageSelect, HelpPanelView


class HelpPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cog = SimpleNamespace(i18n=I18nService())
        self.panel = HelpPanelView(
            self.cog,
            owner_id=50,
            locale="pt-BR",
        )

    def tearDown(self) -> None:
        self.panel.stop()

    def _custom_ids(self) -> set[str]:
        return {
            item.custom_id
            for item in self.panel.walk_children()
            if getattr(item, "custom_id", None)
        }

    def _page_select(self) -> HelpPageSelect:
        return next(
            item
            for item in self.panel.walk_children()
            if isinstance(item, HelpPageSelect)
        )

    def test_panel_uses_components_v2_with_category_navigation(self) -> None:
        self.assertTrue(self.panel.has_components_v2())
        self.assertLessEqual(self.panel.total_children_count, 40)
        self.assertTrue(self.panel.to_components())
        self.assertTrue(
            {
                "help:page:select",
                "help:page:previous",
                "help:page:indicator",
                "help:page:next",
            }.issubset(self._custom_ids())
        )

    def test_every_page_serializes_and_select_marks_current_page(self) -> None:
        for page_index in range(len(HELP_PAGES)):
            self.panel.page_index = page_index
            self.panel._rebuild()

            select = self._page_select()
            self.assertTrue(self.panel.to_components())
            self.assertLessEqual(self.panel.total_children_count, 40)
            self.assertEqual(len(select.options), len(HELP_PAGES))
            self.assertEqual(
                [index for index, option in enumerate(select.options) if option.default],
                [page_index],
            )

    def test_page_index_is_clamped_to_available_pages(self) -> None:
        first = HelpPanelView(self.cog, owner_id=50, locale="pt-BR", page_index=-1)
        last = HelpPanelView(self.cog, owner_id=50, locale="pt-BR", page_index=999)
        self.addCleanup(first.stop)
        self.addCleanup(last.stop)

        self.assertEqual(first.page_index, 0)
        self.assertEqual(last.page_index, len(HELP_PAGES) - 1)

    async def test_only_the_panel_owner_can_interact(self) -> None:
        rejected = SimpleNamespace(
            user=SimpleNamespace(id=51),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        accepted = SimpleNamespace(user=SimpleNamespace(id=50))

        self.assertFalse(await self.panel.interaction_check(rejected))
        rejected.response.send_message.assert_awaited_once()
        self.assertTrue(await self.panel.interaction_check(accepted))


if __name__ == "__main__":
    unittest.main()
