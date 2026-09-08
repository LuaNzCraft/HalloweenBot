from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.items_runtime import ItemsRuntimeService


class FakeMember:
    def __init__(self, member_id: int, *, administrator: bool) -> None:
        self.id = member_id
        self.bot = False
        self.display_name = f"member-{member_id}"
        self.guild_permissions = SimpleNamespace(administrator=administrator)
        self.top_role = 1
        self.timeout = AsyncMock()


def make_runtime() -> ItemsRuntimeService:
    runtime = object.__new__(ItemsRuntimeService)
    runtime.effects_config = {
        "mute_duration_seconds": 60,
        "admin_mute_delete_messages": True,
    }
    runtime.effects_repo = SimpleNamespace(
        create_effect=AsyncMock(return_value=10),
        list_active_effects=AsyncMock(return_value=[]),
        deactivate_effect=AsyncMock(),
    )
    runtime.audit_service = SimpleNamespace(log=AsyncMock())
    return runtime


class AdminMuteMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_new_admin_message_during_active_effect(self) -> None:
        runtime = make_runtime()
        member = FakeMember(20, administrator=True)
        runtime.effects_repo.list_active_effects.return_value = [
            {
                "id": 5,
                "target_id": member.id,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
                "payload": {"mode": "delete_messages"},
            }
        ]
        message = SimpleNamespace(
            author=member,
            guild=SimpleNamespace(id=1),
            delete=AsyncMock(),
        )

        with patch("services.items_runtime.discord.Member", FakeMember):
            deleted = await runtime.handle_admin_mute_message(message)

        self.assertTrue(deleted)
        message.delete.assert_awaited_once_with()

    async def test_expired_effect_is_deactivated_without_deleting_message(self) -> None:
        runtime = make_runtime()
        member = FakeMember(20, administrator=True)
        runtime.effects_repo.list_active_effects.return_value = [
            {
                "id": 5,
                "target_id": member.id,
                "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                "payload": {"mode": "delete_messages"},
            }
        ]
        message = SimpleNamespace(
            author=member,
            guild=SimpleNamespace(id=1),
            delete=AsyncMock(),
        )

        with patch("services.items_runtime.discord.Member", FakeMember):
            deleted = await runtime.handle_admin_mute_message(message)

        self.assertFalse(deleted)
        message.delete.assert_not_awaited()
        runtime.effects_repo.deactivate_effect.assert_awaited_once_with(5)

    async def test_never_deletes_non_admin_message(self) -> None:
        runtime = make_runtime()
        member = FakeMember(20, administrator=False)
        message = SimpleNamespace(
            author=member,
            guild=SimpleNamespace(id=1),
            delete=AsyncMock(),
        )

        with patch("services.items_runtime.discord.Member", FakeMember):
            deleted = await runtime.handle_admin_mute_message(message)

        self.assertFalse(deleted)
        message.delete.assert_not_awaited()
        runtime.effects_repo.list_active_effects.assert_not_awaited()


class AdminMuteApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_board_curse_uses_message_deletion_for_admin(self) -> None:
        runtime = make_runtime()
        target = FakeMember(20, administrator=True)
        guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_messages=True, moderate_members=False),
                top_role=0,
            ),
        )

        applied = await runtime.apply_board_mute_curse(
            guild=guild,
            target=target,
            duration_seconds=60,
        )

        self.assertTrue(applied)
        target.timeout.assert_not_awaited()
        payload = runtime.effects_repo.create_effect.await_args.kwargs["payload"]
        self.assertEqual(payload["mode"], "delete_messages")

    async def test_item_uses_message_deletion_for_admin(self) -> None:
        runtime = make_runtime()
        target = FakeMember(20, administrator=True)
        actor = SimpleNamespace(id=30, display_name="actor")
        users_repo = SimpleNamespace(
            get_user=AsyncMock(return_value={"inventory": {"mute": 1}}),
            remove_inventory_item=AsyncMock(),
            add_inventory_item=AsyncMock(),
            increment_event_counter=AsyncMock(),
            increment_tricks_applied_total=AsyncMock(),
        )
        runtime.inventory_service = SimpleNamespace(users_repo=users_repo)
        runtime.mission_service = SimpleNamespace(
            increment_guild_metric=AsyncMock(),
            increment_user_guild_metric=AsyncMock(),
            evaluate_progress=AsyncMock(return_value=[]),
        )
        runtime.texts = SimpleNamespace(mute_applied_embed=MagicMock(return_value="embed"))
        runtime.i18n = SimpleNamespace(t=MagicMock(side_effect=lambda _locale, key, **_kwargs: key))
        interaction = SimpleNamespace(
            user=actor,
            guild=SimpleNamespace(
                id=1,
                me=SimpleNamespace(
                    guild_permissions=SimpleNamespace(manage_messages=True, moderate_members=False),
                    top_role=0,
                ),
            ),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch("services.items_runtime.discord.Member", FakeMember):
            await runtime.handle_mute(interaction, target, "pt-BR")

        target.timeout.assert_not_awaited()
        users_repo.remove_inventory_item.assert_awaited_once_with(actor.id, "mute", 1)
        payload = runtime.effects_repo.create_effect.await_args.kwargs["payload"]
        self.assertEqual(payload["mode"], "delete_messages")
        runtime.texts.mute_applied_embed.assert_called_once()
        self.assertTrue(runtime.texts.mute_applied_embed.call_args.kwargs["delete_messages"])


class MuteObserverGuardrailTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_mute_before_observer(self) -> None:
        runtime = make_runtime()
        runtime.julgamento_repo = SimpleNamespace(get_runtime=AsyncMock(return_value={"active": False}))
        runtime.public_events_repo = SimpleNamespace(
            get_observer_state=AsyncMock(return_value={"active": False}),
            get_state=AsyncMock(),
        )
        runtime.i18n = SimpleNamespace(t=MagicMock(side_effect=lambda _locale, key, **_kwargs: key))
        runtime.load_public_events_config = MagicMock(
            return_value={"scheduler": {"announce_minutes_before": 5}}
        )

        runtime.public_events_repo.get_state.return_value = {
            "next_event_name": "Observador",
            "next_run_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        }

        reason = await runtime.get_mute_guardrail_reason(1, "pt-BR")

        self.assertEqual(reason, "mute.blocked.observer_soon")

    async def test_cleanup_deactivates_expired_regular_discord_mutes(self) -> None:
        runtime = make_runtime()
        runtime.effects_repo.list_active_effects.return_value = [
            {
                "id": 42,
                "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                "payload": {"mode": "discord_timeout"},
            }
        ]

        await runtime.cleanup_expired_admin_mutes()

        runtime.effects_repo.deactivate_effect.assert_awaited_once_with(42)


if __name__ == "__main__":
    unittest.main()
