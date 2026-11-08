from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import math
import random
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from cogs.public_events import PublicEventsCog


LOGGER = logging.getLogger(__name__)
SAO_PAULO_TIMEZONE = timezone(timedelta(hours=-3), "America/Sao_Paulo")


class ChatCandyRuntimeService:
    async def maybe_spawn(self, cog: "PublicEventsCog", message: discord.Message) -> None:
        config = cog._chat_candy_config()
        if not config["enabled"] or len(config["emoji_ids"]) < 3:
            return
        if not await cog.event_runtime_service.can_run_public_event(message.guild.id):
            return
        if not await cog._message_is_in_event_channel(message):
            return
        if len(message.content.strip()) < config["min_message_length"]:
            return
        if config["pause_during_public_events"] and await cog._has_active_public_runtime(message.guild.id):
            return

        lock = cog._chat_candy_guild_locks.setdefault(message.guild.id, asyncio.Lock())
        async with lock:
            now = datetime.now(timezone.utc)
            spawn_day = now.astimezone(SAO_PAULO_TIMEZONE).date().isoformat()
            if not await cog.chat_candy_repo.can_spawn(
                message.guild.id,
                now=now,
                spawn_day=spawn_day,
                cooldown_seconds=config["spawn_cooldown_seconds"],
                daily_cap=config["max_spawns_per_day"],
            ):
                return
            if config["spawn_chance_percent"] <= 0:
                return
            if random.random() * 100 >= config["spawn_chance_percent"]:
                return

            option_count = random.randint(config["min_options"], config["max_options"])
            option_ids = random.sample(config["emoji_ids"], k=option_count)
            correct_emoji_id = random.choice(option_ids)
            expires_at = now + timedelta(seconds=config["capture_seconds"])
            correct_emoji = cog._candy_drop_emoji(message.guild, correct_emoji_id)
            embed = cog.texts.build_chat_candy_spawn_embed(
                discord.Color.from_rgb(115, 82, 168),
                target_emoji=str(correct_emoji),
                reward_candies=config["reward_candies"],
                expires_at=expires_at,
                option_count=option_count,
                **self._locale_kwargs(cog, message.channel),
            )

            try:
                spawn_message = await message.channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                return

            spawn_id = await cog.chat_candy_repo.create_spawn(
                message.guild.id,
                message.channel.id,
                spawn_message.id,
                correct_emoji_id,
                option_ids,
                config["reward_candies"],
                created_at=now,
                expires_at=expires_at,
                spawn_day=spawn_day,
                cooldown_seconds=config["spawn_cooldown_seconds"],
                daily_cap=config["max_spawns_per_day"],
            )
            if spawn_id is None:
                await self._safe_delete(spawn_message)
                return

            try:
                for emoji_id in option_ids:
                    await spawn_message.add_reaction(cog._candy_drop_emoji(message.guild, emoji_id))
            except (discord.Forbidden, discord.HTTPException):
                await cog.chat_candy_repo.close_spawn(spawn_id, closed_at=datetime.now(timezone.utc))
                await self._safe_delete(spawn_message)
                return

            self.schedule_expiry(
                cog,
                spawn_id=spawn_id,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                message_id=spawn_message.id,
                expires_at=expires_at,
            )

    async def handle_reaction(
        self,
        cog: "PublicEventsCog",
        payload: discord.RawReactionActionEvent,
    ) -> None:
        if payload.guild_id is None or payload.emoji.id is None:
            return
        if cog.bot.user is not None and payload.user_id == cog.bot.user.id:
            return

        status, spawn = await cog.chat_candy_repo.submit_attempt(
            payload.guild_id,
            payload.message_id,
            payload.user_id,
            int(payload.emoji.id),
            attempted_at=datetime.now(timezone.utc),
        )
        if status != "won" or spawn is None:
            return

        spawn_id = int(spawn["spawn_id"])
        expiry_task = cog._chat_candy_expire_tasks.pop(spawn_id, None)
        if expiry_task is not None and expiry_task is not asyncio.current_task():
            expiry_task.cancel()

        reward_candies = max(0, int(spawn.get("reward_candies") or 0))
        reward_summary = f"{reward_candies} doce" if reward_candies == 1 else f"{reward_candies} doces"
        try:
            reward_summary = await self._settle_winner_reward(cog, spawn)
        except Exception:
            LOGGER.exception(
                "Falha ao conceder doce do chat (guild=%s, spawn=%s, user=%s)",
                payload.guild_id,
                spawn_id,
                payload.user_id,
            )
            reward_summary = "recompensa pendente de verificacao"

        await self._publish_winner_result(cog, spawn, reward_summary)

    async def reconcile_pending_rewards(self, cog: "PublicEventsCog") -> None:
        for spawn in await cog.chat_candy_repo.list_pending_reward_spawns():
            try:
                reward_summary = await self._settle_winner_reward(cog, spawn)
                await self._publish_winner_result(cog, spawn, reward_summary)
            except Exception:
                LOGGER.exception(
                    "Falha ao reconciliar doce do chat (guild=%s, spawn=%s, user=%s)",
                    spawn.get("guild_id"),
                    spawn.get("spawn_id"),
                    spawn.get("winner_user_id"),
                )

    async def _settle_winner_reward(self, cog: "PublicEventsCog", spawn: dict[str, Any]) -> str:
        spawn_id = int(spawn["spawn_id"])
        guild_id = int(spawn["guild_id"])
        winner_user_id = int(spawn["winner_user_id"])
        reward_candies = max(0, int(spawn.get("reward_candies") or 0))
        settlement = await cog.reward_settlement_service.settle_once(
            f"chat_candy:{spawn_id}",
            "chat_candy",
            winner_user_id,
            guild_id,
            currencies={"candies": reward_candies},
        )
        await cog.chat_candy_repo.mark_reward_settled(
            spawn_id,
            winner_user_id,
            datetime.now(timezone.utc),
        )
        await cog.mission_service.evaluate_progress(guild_id, winner_user_id)
        paid = int(settlement.currencies.get("candies", 0))
        return f"{paid} doce" if paid == 1 else f"{paid} doces"

    async def _publish_winner_result(
        self,
        cog: "PublicEventsCog",
        spawn: dict[str, Any],
        reward_summary: str,
    ) -> None:
        winner_user_id = int(spawn["winner_user_id"])
        winner = f"<@{winner_user_id}>"
        message = await self._fetch_message(cog, int(spawn["channel_id"]), int(spawn["message_id"]))
        if message is None:
            return
        embed = cog.texts.build_chat_candy_winner_embed(
            discord.Color.from_rgb(115, 82, 168),
            winner_mention=winner,
            reward_summary=reward_summary,
            **self._locale_kwargs(cog, message.channel),
        )
        edited = True
        try:
            await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            edited = False
        await self._safe_clear_reactions(message)
        delay = float(cog._chat_candy_config().get("delete_winner_after_seconds", 0) or 0)
        if edited and delay > 0:
            cog._schedule_message_delete(message, delay)

    async def restore_expiry_tasks(self, cog: "PublicEventsCog") -> None:
        await self.reconcile_pending_rewards(cog)
        for spawn in await cog.chat_candy_repo.list_active_spawns():
            try:
                expires_at = datetime.fromisoformat(str(spawn["expires_at"]))
            except (TypeError, ValueError):
                await cog.chat_candy_repo.close_spawn(
                    int(spawn["spawn_id"]),
                    closed_at=datetime.now(timezone.utc),
                )
                continue
            self.schedule_expiry(
                cog,
                spawn_id=int(spawn["spawn_id"]),
                guild_id=int(spawn["guild_id"]),
                channel_id=int(spawn["channel_id"]),
                message_id=int(spawn["message_id"]),
                expires_at=expires_at,
            )

    async def cancel_guild_spawn(self, cog: "PublicEventsCog", guild_id: int) -> bool:
        spawn = await cog.chat_candy_repo.get_active_spawn_for_guild(guild_id)
        if spawn is None:
            return False
        spawn_id = int(spawn["spawn_id"])
        closed = await cog.chat_candy_repo.close_spawn(spawn_id, closed_at=datetime.now(timezone.utc))
        if not closed:
            return False
        expiry_task = cog._chat_candy_expire_tasks.pop(spawn_id, None)
        if expiry_task is not None:
            expiry_task.cancel()
        message = await self._fetch_message(cog, int(spawn["channel_id"]), int(spawn["message_id"]))
        if message is not None:
            try:
                await message.edit(
                    embed=cog.texts.build_chat_candy_cancelled_embed(
                        discord.Color.from_rgb(70, 70, 80),
                        **self._locale_kwargs(cog, message.channel),
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            await self._safe_clear_reactions(message)
        return True

    def schedule_expiry(
        self,
        cog: "PublicEventsCog",
        *,
        spawn_id: int,
        guild_id: int,
        channel_id: int,
        message_id: int,
        expires_at: datetime,
    ) -> None:
        previous = cog._chat_candy_expire_tasks.pop(spawn_id, None)
        if previous is not None:
            previous.cancel()
        cog._chat_candy_expire_tasks[spawn_id] = asyncio.create_task(
            self.expire_spawn(
                cog,
                spawn_id=spawn_id,
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                expires_at=expires_at,
            )
        )

    async def expire_spawn(
        self,
        cog: "PublicEventsCog",
        *,
        spawn_id: int,
        guild_id: int,
        channel_id: int,
        message_id: int,
        expires_at: datetime,
    ) -> None:
        try:
            await discord.utils.sleep_until(expires_at)
            closed = await cog.chat_candy_repo.close_spawn(spawn_id, closed_at=datetime.now(timezone.utc))
            if not closed:
                return
            message = await self._fetch_message(cog, channel_id, message_id)
            if message is None:
                return
            embed = cog.texts.build_chat_candy_timeout_embed(
                discord.Color.from_rgb(70, 70, 80),
                **self._locale_kwargs(cog, message.channel),
            )
            edited = True
            try:
                await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                edited = False
            await self._safe_clear_reactions(message)
            delay = float(cog._chat_candy_config().get("delete_timeout_after_seconds", 0) or 0)
            if edited and delay > 0:
                cog._schedule_message_delete(message, delay)
        except asyncio.CancelledError:
            raise
        finally:
            if cog._chat_candy_expire_tasks.get(spawn_id) is asyncio.current_task():
                cog._chat_candy_expire_tasks.pop(spawn_id, None)

    async def _fetch_message(
        self,
        cog: "PublicEventsCog",
        channel_id: int,
        message_id: int,
    ) -> discord.Message | None:
        channel = cog.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await cog.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if not hasattr(channel, "fetch_message"):
            return None
        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    @staticmethod
    def _locale_kwargs(cog: "PublicEventsCog", channel: Any) -> dict[str, str]:
        # So passa locale quando nao e pt-BR, para o caminho em portugues ficar identico ao de antes.
        resolver = getattr(cog, "_chat_candy_locale", None)
        locale = resolver(channel) if callable(resolver) else "pt-BR"
        return {"locale": locale} if isinstance(locale, str) and locale != "pt-BR" else {}

    async def _safe_clear_reactions(self, message: discord.Message) -> None:
        try:
            await message.clear_reactions()
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _safe_delete(self, message: discord.Message) -> None:
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            return

    @staticmethod
    def normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
        emoji_ids: list[int] = []
        for value in raw.get("emoji_ids") or []:
            try:
                emoji_id = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if emoji_id > 0 and emoji_id not in emoji_ids:
                emoji_ids.append(emoji_id)

        min_options = ChatCandyRuntimeService._safe_int(raw.get("min_options"), 3, minimum=3, maximum=20)
        max_options = ChatCandyRuntimeService._safe_int(raw.get("max_options"), 4, minimum=3, maximum=20)
        max_available = len(emoji_ids)
        if max_available >= 3:
            min_options = min(min_options, max_available)
            max_options = min(max(max_options, min_options), max_available)

        return {
            "enabled": bool(raw.get("enabled", False)),
            "emoji_ids": emoji_ids,
            "spawn_chance_percent": ChatCandyRuntimeService._safe_float(
                raw.get("spawn_chance_percent"), 60.0, minimum=0.0, maximum=100.0
            ),
            "spawn_cooldown_seconds": ChatCandyRuntimeService._safe_float(
                raw.get("spawn_cooldown_seconds"), 30.0, minimum=0.0, maximum=86400.0
            ),
            "capture_seconds": ChatCandyRuntimeService._safe_float(
                raw.get("capture_seconds"), 15.0, minimum=1.0, maximum=3600.0
            ),
            "min_message_length": ChatCandyRuntimeService._safe_int(
                raw.get("min_message_length"), 3, minimum=0, maximum=4000
            ),
            "min_options": min_options,
            "max_options": max_options,
            "reward_candies": ChatCandyRuntimeService._safe_int(
                raw.get("reward_candies"), 1, minimum=0, maximum=1_000_000
            ),
            "max_spawns_per_day": ChatCandyRuntimeService._safe_int(
                raw.get("max_spawns_per_day"), 150, minimum=0, maximum=1_000_000
            ),
            "pause_during_public_events": bool(raw.get("pause_during_public_events", True)),
            "delete_winner_after_seconds": ChatCandyRuntimeService._safe_float(
                raw.get("delete_winner_after_seconds"), 8.0, minimum=0.0, maximum=3600.0
            ),
            "delete_timeout_after_seconds": ChatCandyRuntimeService._safe_float(
                raw.get("delete_timeout_after_seconds"), 0.0, minimum=0.0, maximum=3600.0
            ),
        }

    @staticmethod
    def _safe_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(parsed):
            return default
        return min(max(parsed, minimum), maximum)

    @staticmethod
    def _safe_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return min(max(parsed, minimum), maximum)
