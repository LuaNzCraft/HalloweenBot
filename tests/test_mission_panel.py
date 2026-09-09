from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from cogs.missao import MissaoCog
from services.i18n_service import I18nService
from services.mission_service import MissionDefinition, MissionPanelData, MissionProgress
from views.mission_panel import MissionPanelView


def _progress(
    mission_type: str,
    *,
    progress: int,
    target: int = 10,
    completed: bool = False,
    rewarded: bool = False,
) -> MissionProgress:
    mission = MissionDefinition(
        id=f"{mission_type}_001",
        metric="messages_sent",
        target=target,
        rewards={"candies": 10},
        title_pt=f"Missao {mission_type}",
        title_en=f"{mission_type} mission",
        description_pt="Complete o objetivo.",
        description_en="Complete the objective.",
    )
    return MissionProgress(
        mission_type=mission_type,
        period_key="2026-09-08",
        mission=mission,
        progress=progress,
        target=target,
        completed=completed,
        reward_claimed=rewarded,
        reward_just_granted=False,
        reward_text="10 Moedas Doce",
    )


class MissionPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.data = MissionPanelData(
            locale="pt-BR",
            personal=_progress("daily_personal", progress=3),
            global_daily=_progress("daily_global", progress=10, completed=True, rewarded=True),
            event_mission=None,
        )
        self.cog = SimpleNamespace(
            mission_service=SimpleNamespace(
                i18n=I18nService(),
                get_panel_data=AsyncMock(return_value=self.data),
            )
        )
        self.panel = MissionPanelView(
            self.cog,
            owner_id=50,
            data=self.data,
        )

    def tearDown(self) -> None:
        self.panel.stop()

    def _custom_ids(self) -> set[str]:
        return {
            item.custom_id
            for item in self.panel.walk_children()
            if getattr(item, "custom_id", None)
        }

    def test_panel_uses_components_v2_with_tabs_and_navigation(self) -> None:
        self.assertTrue(self.panel.has_components_v2())
        self.assertLessEqual(self.panel.total_children_count, 40)
        self.assertTrue(self.panel.to_components())
        self.assertTrue(
            {
                "missions:tab:overview",
                "missions:tab:personal",
                "missions:tab:global",
                "missions:tab:event",
                "missions:navigation:profile",
                "missions:navigation:shop",
                "missions:navigation:board",
                "missions:refresh",
            }.issubset(self._custom_ids())
        )

    def test_every_tab_serializes_and_event_can_be_inactive(self) -> None:
        for page in ("overview", "personal", "global", "event"):
            self.panel.refresh(page=page)
            self.assertEqual(self.panel.page, page)
            self.assertTrue(self.panel.to_components())
            self.assertLessEqual(self.panel.total_children_count, 40)

    def test_progress_bar_is_compact_and_bounded(self) -> None:
        self.assertEqual(
            MissionPanelView._progress_bar(_progress("daily", progress=3)),
            "███░░░░░░░",
        )
        self.assertEqual(
            MissionPanelView._progress_bar(_progress("daily", progress=50)),
            "██████████",
        )

    async def test_only_the_panel_owner_can_interact(self) -> None:
        rejected = SimpleNamespace(
            user=SimpleNamespace(id=51),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        accepted = SimpleNamespace(user=SimpleNamespace(id=50))

        self.assertFalse(await self.panel.interaction_check(rejected))
        rejected.response.send_message.assert_awaited_once()
        self.assertTrue(await self.panel.interaction_check(accepted))


class MissionCogPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cog_builds_the_v2_panel_from_service_data(self) -> None:
        data = MissionPanelData(
            locale="en-US",
            personal=_progress("daily_personal", progress=2),
            global_daily=_progress("daily_global", progress=4),
            event_mission=_progress("event_global", progress=6),
        )
        cog = object.__new__(MissaoCog)
        cog.mission_service = SimpleNamespace(
            i18n=I18nService(),
            get_panel_data=AsyncMock(return_value=data),
        )

        panel = await cog._build_mission_panel(10, 50)

        self.assertIsInstance(panel, MissionPanelView)
        self.assertTrue(panel.has_components_v2())
        cog.mission_service.get_panel_data.assert_awaited_once_with(10, 50)
        panel.stop()


if __name__ == "__main__":
    unittest.main()
