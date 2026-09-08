from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from cogs.julgamento import JulgamentoCog
from cogs.profile import ProfileCog, TrickOrTreatRequestView
from repositories.profile_requests_repo import ProfileRequestsRepo
from repositories.public_events_repo import PublicEventsRepo
from repositories.users_repo import UsersRepo
from services.i18n_service import I18nService
from services.public_event_runtime import PublicEventRuntimeService


class CapturingLoop:
    def __init__(self) -> None:
        self.coroutines: list = []

    def create_task(self, coroutine):
        self.coroutines.append(coroutine)
        return SimpleNamespace(cancel=Mock())

    def close_coroutines(self) -> None:
        for coroutine in self.coroutines:
            coroutine.close()


class RestartReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_observer_timeout_is_recreated_after_ready(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "halloween.sqlite3"
            repo = PublicEventsRepo(path)
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            await repo.set_observer_state(
                100,
                active=True,
                channel_id=200,
                question_id="observer-1",
                question_text="Pergunta",
                accepted_answers=["resposta"],
                answer_match_mode="normalized",
                question_difficulty="easy",
                started_at=datetime.now(timezone.utc).isoformat(),
                expires_at=expires_at.isoformat(),
                message_id=300,
                winner_user_id=None,
            )
            loop = CapturingLoop()
            cog = SimpleNamespace(
                public_events_repo=repo,
                bot=SimpleNamespace(get_guild=lambda guild_id: object(), loop=loop),
                _observer_timeout_tasks={},
                _parse_dt=lambda value: datetime.fromisoformat(str(value)) if value else None,
            )

            await PublicEventRuntimeService().restore_active_observer_timeouts(cog)

            self.assertIn(100, cog._observer_timeout_tasks)
            self.assertEqual(len(loop.coroutines), 1)
            loop.close_coroutines()

    async def test_carrossel_active_at_restart_is_cancelled_after_ready(self) -> None:
        cog = object.__new__(JulgamentoCog)
        guild = SimpleNamespace(id=100)
        cog.bot = SimpleNamespace(guilds=[guild])
        cog.repo = SimpleNamespace(get_runtime=AsyncMock(return_value={"active": True}))
        cog.admin_repo = SimpleNamespace(
            get_runtime=AsyncMock(return_value={"event_active": True}),
            get_config=AsyncMock(return_value={}),
        )
        cog._startup_reconciled = False
        cog._startup_reconcile_lock = asyncio.Lock()
        cog._edit_signup_message_cancelled = AsyncMock()
        cog.force_reset = AsyncMock()
        cog._get_event_channels = AsyncMock(return_value=[])

        await cog._reconcile_after_ready()
        await cog._reconcile_after_ready()

        cog._edit_signup_message_cancelled.assert_awaited_once_with(guild, {"active": True})
        cog.force_reset.assert_awaited_once_with(guild)
        self.assertTrue(cog._startup_reconciled)

    async def test_active_profile_request_view_is_registered_by_message_id(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "halloween.sqlite3"
            users_repo = UsersRepo(path)
            requests_repo = ProfileRequestsRepo(path)
            await users_repo.init_db()
            await requests_repo.init_db()
            await users_repo.add_currency(20, "candies", 20)
            created_at = datetime.now(timezone.utc).isoformat()
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            await requests_repo.try_create_active_request(
                guild_id=100,
                requester_id=10,
                target_id=20,
                channel_id=200,
                created_at=created_at,
                expires_at=expires_at,
                now_iso=created_at,
            )
            await requests_repo.mark_active_request_published(100, 10, created_at, 300)

            registered: list[tuple[TrickOrTreatRequestView, int]] = []
            guild = SimpleNamespace(
                id=100,
                get_channel=lambda channel_id: None,
                get_thread=lambda channel_id: None,
                get_member=lambda user_id: SimpleNamespace(display_name="Requester") if user_id == 10 else None,
            )
            bot = SimpleNamespace(
                get_guild=lambda guild_id: guild if guild_id == 100 else None,
                add_view=lambda view, *, message_id: registered.append((view, message_id)),
            )
            cog = object.__new__(ProfileCog)
            cog.bot = bot
            cog.profile_requests_repo = requests_repo
            cog.i18n = I18nService(users_repo)
            cog.profile_service = SimpleNamespace(
                get_profile=AsyncMock(return_value={"candies": 20, "trick_coins": 0})
            )
            cog.profile_requests_config = {"candy_amount": 10, "trick_coins_amount": 5}
            cog._restored_request_expiry_tasks = {}

            await cog._restore_active_request_views()

            self.assertEqual(len(registered), 1)
            view, message_id = registered[0]
            self.assertEqual(message_id, 300)
            self.assertIsNone(view.timeout)
            self.assertEqual(view.request_created_at, created_at)
            for task in tuple(cog._restored_request_expiry_tasks.values()):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            view.stop()


if __name__ == "__main__":
    unittest.main()
