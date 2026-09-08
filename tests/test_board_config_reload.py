from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.tabuleiro import TabuleiroCog


class BoardConfigReloadTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self) -> TabuleiroCog:
        cog = object.__new__(TabuleiroCog)
        cog.items_runtime = SimpleNamespace(effects_config={"old": True})
        cog.board_config = {"total_tiles": 16}
        cog._board_reload_lock = asyncio.Lock()
        cog._build_board_state = MagicMock(return_value={"built": True})
        cog._apply_board_state = MagicMock()
        return cog

    async def test_does_not_rebuild_frames_when_board_section_is_unchanged(self) -> None:
        cog = self._cog()
        with (
            patch("cogs.tabuleiro.get_effects_config", return_value={"mute_duration_seconds": 90}),
            patch("cogs.tabuleiro.get_board_config", return_value={"total_tiles": 16}),
            patch("cogs.tabuleiro.asyncio.to_thread", new_callable=AsyncMock) as to_thread,
        ):
            await cog.reload_config()

        to_thread.assert_not_awaited()
        cog._apply_board_state.assert_not_called()
        self.assertEqual(cog.items_runtime.effects_config, {"mute_duration_seconds": 90})

    async def test_changed_board_is_built_off_thread_before_atomic_swap(self) -> None:
        cog = self._cog()
        changed = {"total_tiles": 20}
        built_state = {"built": True}
        with (
            patch("cogs.tabuleiro.get_effects_config", return_value={}),
            patch("cogs.tabuleiro.get_board_config", return_value=changed),
            patch("cogs.tabuleiro.asyncio.to_thread", new=AsyncMock(return_value=built_state)) as to_thread,
        ):
            await cog.reload_config()

        to_thread.assert_awaited_once_with(cog._build_board_state, changed)
        cog._apply_board_state.assert_called_once_with(built_state)

    async def test_swap_waits_for_an_active_roll_to_finish(self) -> None:
        cog = self._cog()
        cog._rolling_users = {99}
        changed = {"total_tiles": 20}
        with (
            patch("cogs.tabuleiro.get_effects_config", return_value={}),
            patch("cogs.tabuleiro.get_board_config", return_value=changed),
            patch("cogs.tabuleiro.asyncio.to_thread", new=AsyncMock(return_value={"built": True})),
        ):
            reload_task = asyncio.create_task(cog.reload_config())
            await asyncio.sleep(0.02)
            cog._apply_board_state.assert_not_called()
            cog._rolling_users.clear()
            await reload_task

        cog._apply_board_state.assert_called_once_with({"built": True})


if __name__ == "__main__":
    unittest.main()
