from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import random
from typing import TYPE_CHECKING

import discord

from views.public_event_views import ObserverTranslateView, PublicEventTranslateView


if TYPE_CHECKING:
    from cogs.public_events import PublicEventsCog


LOGGER = logging.getLogger(__name__)


class PublicEventRuntimeService:
    async def start_observer(
        self,
        cog: "PublicEventsCog",
        guild: discord.Guild,
        config: dict,
    ) -> tuple[bool, str]:
        observer_cfg = config.get("observer", {})
        channels = await cog._get_event_channels(guild)
        if not channels:
            return False, "Nenhum canal de evento esta configurado."

        active_state = await cog.public_events_repo.get_observer_state(guild.id)
        if active_state.get("winner_user_id") and not active_state.get("reward_settled_at"):
            try:
                await self.settle_observer_reward(cog, guild, active_state)
            except Exception:
                LOGGER.exception("Observador anterior ainda possui recompensa pendente na guild %s", guild.id)
                return False, "O premio da rodada anterior ainda esta sendo reconciliado."
            active_state = await cog.public_events_repo.get_observer_state(guild.id)
        if active_state.get("active"):
            return False, "O Observador ja esta ativo."

        questions = cog._load_observer_questions(observer_cfg)
        questions.extend(await cog._load_observer_archive_questions(guild.id, observer_cfg))
        if not questions:
            return False, "Nao ha perguntas do Observador configuradas."

        question = await cog._pick_observer_question(guild.id, questions, observer_cfg)
        if question is None:
            return False, "Nao foi possivel escolher uma pergunta do Observador."
        question_id = str(question.get("id") or "")
        match_mode = cog._resolve_observer_match_mode(question)
        difficulty = cog._resolve_observer_difficulty(question)
        timeout_seconds = int(observer_cfg.get("answer_timeout_seconds", 90))
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=timeout_seconds)
        accepted_answers = [
            cog._prepare_observer_answer(str(answer), match_mode)
            for answer in cog._expand_observer_answers(question)
        ]
        display_text = cog._resolve_observer_display_text(question, match_mode)
        attachment_path = cog._resolve_observer_image_path(question)
        attachment_filename = cog._resolve_observer_attachment_filename(question)
        channel = channels[0]
        embed = cog._build_observer_question_embed(
            str(question["question"]),
            expires_at,
            match_mode,
            display_text,
            difficulty,
            attachment_filename,
        )
        message = await cog._send_observer_payload(
            channel,
            observer_cfg,
            embed=embed,
            view=ObserverTranslateView(cog, question, expires_at),
            attachment_path=attachment_path,
            attachment_filename=attachment_filename,
            wait=True,
        )
        if message is None:
            return False, "Nao foi possivel publicar o Observador no canal configurado."
        await cog.public_events_repo.set_observer_state(
            guild.id,
            active=True,
            channel_id=channel.id,
            question_id=question_id,
            question_text=str(question["question"]),
            accepted_answers=accepted_answers,
            answer_match_mode=match_mode,
            question_difficulty=difficulty,
            started_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            message_id=message.id,
            winner_user_id=None,
        )
        existing_timeout = cog._observer_timeout_tasks.pop(guild.id, None)
        if existing_timeout is not None:
            existing_timeout.cancel()
        cog._observer_timeout_tasks[guild.id] = cog.bot.loop.create_task(
            self.finish_observer_if_unanswered(cog, guild.id, expires_at)
        )
        return True, "ok"

    async def finish_observer_if_unanswered(
        self,
        cog: "PublicEventsCog",
        guild_id: int,
        expires_at: datetime,
    ) -> None:
        current_task = asyncio.current_task()
        try:
            delay = max(0.0, (expires_at - datetime.now(timezone.utc)).total_seconds())
            await discord.utils.sleep_until(expires_at) if delay > 0 else None
            if guild_id in cog._resolving_observer_guilds:
                return
            cog._resolving_observer_guilds.add(guild_id)
            state = await cog.public_events_repo.get_observer_state(guild_id)
            if not state.get("active"):
                return
            guild = cog.bot.get_guild(guild_id)
            if guild is None:
                await cog.public_events_repo.set_observer_state(
                    guild_id,
                    active=False,
                    channel_id=state.get("channel_id"),
                    question_id=state.get("question_id"),
                    question_text=state.get("question_text"),
                    accepted_answers=state.get("accepted_answers", []),
                    answer_match_mode=state.get("answer_match_mode", "normalized"),
                    question_difficulty=state.get("question_difficulty", "easy"),
                    started_at=state.get("started_at"),
                    expires_at=state.get("expires_at"),
                    message_id=state.get("message_id"),
                    winner_user_id=None,
                )
                return
            channel = guild.get_channel(int(state["channel_id"])) if state.get("channel_id") else None
            observer_cfg = cog._load_config().get("observer", {})
            try:
                question = await cog._find_observer_question_for_state(
                    guild_id,
                    observer_cfg,
                    question_id=state.get("question_id"),
                    question_text=state.get("question_text"),
                )
            except Exception:
                LOGGER.exception("Falha ao recuperar pergunta do Observador na guild %s", guild_id)
                question = None
            answer_label, answer_value = cog._resolve_observer_timeout_answer(question)
            revealed_path = cog._resolve_observer_revealed_image_path(question)
            revealed_filename = cog._resolve_observer_revealed_attachment_filename(question)
            timeout_embed = cog._build_observer_timeout_embed(
                answer_label=answer_label,
                answer_value=answer_value,
            )
            if revealed_path is not None and revealed_filename:
                timeout_embed.set_image(url=f"attachment://{revealed_filename}")
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    edited = False
                    message_id = int(state.get("message_id") or 0)
                    if message_id > 0 and revealed_path is None:
                        try:
                            original_message = await channel.fetch_message(message_id)
                            await original_message.edit(
                                embed=timeout_embed,
                                view=None,
                            )
                            edited = True
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            edited = False
                    if not edited:
                        await cog._send_observer_payload(
                            channel,
                            observer_cfg,
                            embed=timeout_embed,
                            attachment_path=revealed_path,
                            attachment_filename=revealed_filename,
                        )
                except discord.HTTPException:
                    pass
            await cog.public_events_repo.set_observer_state(
                guild_id,
                active=False,
                channel_id=state.get("channel_id"),
                question_id=state.get("question_id"),
                question_text=state.get("question_text"),
                accepted_answers=state.get("accepted_answers", []),
                answer_match_mode=state.get("answer_match_mode", "normalized"),
                question_difficulty=state.get("question_difficulty", "easy"),
                started_at=state.get("started_at"),
                expires_at=state.get("expires_at"),
                message_id=state.get("message_id"),
                winner_user_id=None,
            )
        except asyncio.CancelledError:
            raise
        finally:
            if cog._observer_timeout_tasks.get(guild_id) is current_task:
                cog._observer_timeout_tasks.pop(guild_id, None)
            cog._resolving_observer_guilds.discard(guild_id)

    async def handle_observer_message(self, cog: "PublicEventsCog", message: discord.Message) -> None:
        if message.guild.id in cog._resolving_observer_guilds:
            return
        cog._resolving_observer_guilds.add(message.guild.id)
        try:
            state = await cog.public_events_repo.get_observer_state(message.guild.id)
            if not state.get("active"):
                return
            if state.get("channel_id") != message.channel.id:
                return
            expires_at = cog._parse_dt(state.get("expires_at"))
            if expires_at is None or expires_at <= datetime.now(timezone.utc):
                return

            match_mode = str(state.get("answer_match_mode") or "normalized")
            prepared_answer = cog._prepare_observer_answer(message.content, match_mode)
            answers = {str(answer) for answer in state.get("accepted_answers", [])}
            if prepared_answer not in answers:
                return

            config = cog._load_config().get("observer", {})
            claimed = await cog.public_events_repo.claim_observer_winner(message.guild.id, message.author.id)
            if not claimed:
                return
            timeout_task = cog._observer_timeout_tasks.pop(message.guild.id, None)
            if timeout_task is not None:
                timeout_task.cancel()
            claimed_state = dict(state)
            claimed_state["winner_user_id"] = message.author.id
            try:
                reward_lines = await self.settle_observer_reward(cog, message.guild, claimed_state, config)
                summary = "\n".join(reward_lines) if reward_lines else "Sem recompensa configurada."
            except Exception:
                LOGGER.exception(
                    "Falha ao liquidar Observador (guild=%s, user=%s)",
                    message.guild.id,
                    message.author.id,
                )
                summary = "Recompensa pendente de verificacao."
            try:
                question = await cog._find_observer_question_for_state(
                    message.guild.id,
                    config,
                    question_id=state.get("question_id"),
                    question_text=state.get("question_text"),
                )
                winner_embed = cog._build_observer_winner_embed(message.author.mention, summary)
                if question is not None and cog.observer_catalog.kind(question) == "chat_quote":
                    answer_label, answer_value = cog._resolve_observer_timeout_answer(question)
                    winner_embed.add_field(name=answer_label, value=answer_value, inline=False)
                revealed_path = cog._resolve_observer_revealed_image_path(question)
                revealed_filename = cog._resolve_observer_revealed_attachment_filename(question)
                if revealed_path is not None and revealed_filename:
                    winner_embed.set_image(url=f"attachment://{revealed_filename}")
                await cog._send_observer_payload(
                    message.channel,
                    config,
                    embed=winner_embed,
                    attachment_path=revealed_path,
                    attachment_filename=revealed_filename,
                )
            except Exception:
                LOGGER.exception(
                    "Falha ao publicar resultado do Observador (guild=%s, user=%s)",
                    message.guild.id,
                    message.author.id,
                )
        finally:
            cog._resolving_observer_guilds.discard(message.guild.id)

    async def reconcile_pending_observer_rewards(self, cog: "PublicEventsCog") -> None:
        for state in await cog.public_events_repo.list_pending_observer_rewards():
            guild_id = int(state["guild_id"])
            guild = cog.bot.get_guild(guild_id)
            if guild is None:
                continue
            try:
                await self.settle_observer_reward(cog, guild, state)
            except Exception:
                LOGGER.exception(
                    "Falha ao reconciliar Observador (guild=%s, user=%s)",
                    guild_id,
                    state.get("winner_user_id"),
                )

    async def restore_active_observer_timeouts(self, cog: "PublicEventsCog") -> None:
        for state in await cog.public_events_repo.list_active_observer_states():
            guild_id = int(state["guild_id"])
            if cog.bot.get_guild(guild_id) is None or guild_id in cog._observer_timeout_tasks:
                continue
            expires_at = cog._parse_dt(state.get("expires_at")) or datetime.now(timezone.utc)
            cog._observer_timeout_tasks[guild_id] = cog.bot.loop.create_task(
                self.finish_observer_if_unanswered(cog, guild_id, expires_at)
            )

    async def settle_observer_reward(
        self,
        cog: "PublicEventsCog",
        guild: discord.Guild,
        state: dict,
        config: dict | None = None,
    ) -> list[str]:
        winner_user_id = int(state["winner_user_id"])
        event_id = str(state.get("message_id") or state.get("started_at") or state.get("question_id") or "unknown")
        reward_lines = await cog._grant_observer_rewards(
            winner_user_id,
            config or cog._load_config().get("observer", {}),
            guild.id,
            str(state.get("question_difficulty") or "easy"),
            f"observer:{guild.id}:{event_id}",
        )
        await cog.public_events_repo.mark_observer_reward_settled(
            guild.id,
            winner_user_id,
            datetime.now(timezone.utc).isoformat(),
        )
        total = int((await cog.users_repo.get_user(winner_user_id)).get("horror_questions_correct_total", 0))
        await cog._grant_cinefilo_role_if_eligible(guild, winner_user_id, total)
        return reward_lines

    async def restore_spider_finish_tasks(self, cog: "PublicEventsCog") -> None:
        for guild in cog.bot.guilds:
            state = await cog.public_events_repo.get_spider_state(guild.id)
            if not state.get("active"):
                continue
            ends_at = cog._parse_dt(state.get("ends_at"))
            if ends_at is None or ends_at <= datetime.now(timezone.utc):
                await self.finish_spider_infestation(cog, guild)
                continue
            self.schedule_spider_finish(cog, guild.id, ends_at)

    async def start_spider_infestation(
        self,
        cog: "PublicEventsCog",
        guild: discord.Guild,
        config: dict | None = None,
    ) -> tuple[bool, str]:
        spider_cfg = cog._spider_config(config)
        channels = await cog._get_event_channels(guild)
        if not channels:
            return False, "Nenhum canal de evento configurado para a infestacao."

        state = await cog.public_events_repo.get_spider_state(guild.id)
        if state.get("active"):
            return False, "A infestacao ja esta acontecendo."

        duration_minutes = max(1, int(spider_cfg.get("duration_minutes", 15)))
        now = datetime.now(timezone.utc)
        ends_at = now + timedelta(minutes=duration_minutes)
        await cog.public_events_repo.set_spider_state(
            guild.id,
            active=True,
            started_at=now.isoformat(),
            ends_at=ends_at.isoformat(),
            total_spawns=0,
        )
        self.schedule_spider_finish(cog, guild.id, ends_at)
        await cog._send_event_payload(
            guild,
            embed=cog._build_spider_start_embed(ends_at),
            view=PublicEventTranslateView(cog._build_spider_start_embed_en(ends_at)),
        )
        return True, "ok"

    def schedule_spider_finish(self, cog: "PublicEventsCog", guild_id: int, ends_at: datetime) -> None:
        existing = cog._spider_finish_tasks.pop(guild_id, None)
        if existing is not None:
            existing.cancel()
        cog._spider_finish_tasks[guild_id] = asyncio.create_task(
            self.finish_spider_after_delay(cog, guild_id, ends_at)
        )

    async def finish_spider_after_delay(self, cog: "PublicEventsCog", guild_id: int, ends_at: datetime) -> None:
        try:
            await discord.utils.sleep_until(ends_at)
            guild = cog.bot.get_guild(guild_id)
            if guild is not None:
                await self.finish_spider_infestation(cog, guild)
        except asyncio.CancelledError:
            raise
        finally:
            current_task = asyncio.current_task()
            if cog._spider_finish_tasks.get(guild_id) is current_task:
                cog._spider_finish_tasks.pop(guild_id, None)

    async def finish_spider_if_expired(self, cog: "PublicEventsCog", guild: discord.Guild) -> None:
        state = await cog.public_events_repo.get_spider_state(guild.id)
        if not state.get("active"):
            return
        ends_at = cog._parse_dt(state.get("ends_at"))
        if ends_at is not None and ends_at <= datetime.now(timezone.utc):
            await self.finish_spider_infestation(cog, guild)

    async def finish_spider_infestation(
        self,
        cog: "PublicEventsCog",
        guild: discord.Guild,
        *,
        cancelled: bool = False,
    ) -> None:
        if guild.id in cog._finishing_spider_guilds:
            return
        cog._finishing_spider_guilds.add(guild.id)
        try:
            state = await cog.public_events_repo.get_spider_state(guild.id)
            if not state.get("active"):
                return

            finish_task = cog._spider_finish_tasks.pop(guild.id, None)
            if finish_task is not None:
                finish_task.cancel()

            counts = await cog.public_events_repo.list_spider_capture_counts(guild.id)
            spider_cfg = cog._spider_config()
            if cancelled:
                await cog.public_events_repo.reset_spider_state(guild.id)
                await cog._send_event_message(guild, "🍬 Chuva de Doces cancelada.")
                return

            reward_lines: list[str] = []
            reward_lines_en: list[str] = []
            for row in counts:
                user_id = int(row["user_id"])
                captures = int(row["captures"])
                if captures <= 0:
                    continue
                event_key = f"{guild.id}:{state.get('started_at') or state.get('ends_at') or 'unknown'}"
                reward_summary = await cog._grant_spider_rewards(
                    user_id,
                    guild.id,
                    captures,
                    spider_cfg,
                    event_key,
                )
                member = guild.get_member(user_id)
                mention = member.mention if member else f"<@{user_id}>"
                reward_lines.append(f"{mention}: **{captures}** doce(s) - {reward_summary}")
                reward_lines_en.append(f"{mention}: **{captures}** candy drop(s) - {reward_summary}")

            total_spawns = int(state.get("total_spawns") or 0)
            if reward_lines:
                body = "\n".join(reward_lines[:15])
                if len(reward_lines) > 15:
                    body += f"\n...e mais {len(reward_lines) - 15} participante(s)."
                body_en = "\n".join(reward_lines_en[:15])
                if len(reward_lines_en) > 15:
                    body_en += f"\n...and {len(reward_lines_en) - 15} more participant(s)."
            else:
                body = "Ninguem capturou doces desta vez."
                body_en = "Nobody captured candy drops this time."
            await cog._send_event_payload(
                guild,
                embed=cog._build_spider_finish_embed(body=body, total_spawns=total_spawns),
                view=PublicEventTranslateView(
                    cog._build_spider_finish_embed_en(body=body_en, total_spawns=total_spawns)
                ),
            )
            await cog.public_events_repo.reset_spider_state(guild.id)
        finally:
            cog._finishing_spider_guilds.discard(guild.id)

    async def maybe_spawn_spider(self, cog: "PublicEventsCog", message: discord.Message) -> None:
        state = await cog.public_events_repo.get_spider_state(message.guild.id)
        if not state.get("active"):
            return
        ends_at = cog._parse_dt(state.get("ends_at"))
        if ends_at is None or ends_at <= datetime.now(timezone.utc):
            await self.finish_spider_infestation(cog, message.guild)
            return

        spider_cfg = cog._spider_config()
        emoji_id = cog._spider_emoji_id(spider_cfg)
        channels = await cog._get_event_channels(message.guild)
        if message.channel.id not in {channel.id for channel in channels}:
            return
        min_length = int(spider_cfg.get("min_message_length", 3))
        if len(message.content.strip()) < min_length:
            return

        max_spawns = int(spider_cfg.get("max_spawns_per_event", 0) or 0)
        if max_spawns > 0 and int(state.get("total_spawns") or 0) >= max_spawns:
            return
        spawn_chance = float(spider_cfg.get("spawn_chance_percent", 8))
        if random.uniform(0, 100) > spawn_chance:
            return

        capture_seconds = max(1, float(spider_cfg.get("capture_seconds", 3)))
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=capture_seconds)
        spawn_id = await cog.public_events_repo.create_spider_spawn(
            message.guild.id,
            message.channel.id,
            message.id,
            emoji_id,
            now.isoformat(),
            expires_at.isoformat(),
        )
        try:
            await message.add_reaction(cog._candy_drop_emoji(message.guild, emoji_id))
        except discord.HTTPException:
            await cog.public_events_repo.close_spider_spawn(message.guild.id, spawn_id)
            return
        await cog.public_events_repo.increment_spider_spawns(message.guild.id)
        self.schedule_spider_expire(cog, message.guild.id, message.channel.id, message.id, spawn_id, emoji_id, expires_at)

    def schedule_spider_expire(
        self,
        cog: "PublicEventsCog",
        guild_id: int,
        channel_id: int,
        message_id: int,
        spawn_id: int,
        emoji_id: int,
        expires_at: datetime,
    ) -> None:
        key = (guild_id, spawn_id)
        existing = cog._spider_expire_tasks.pop(key, None)
        if existing is not None:
            existing.cancel()
        cog._spider_expire_tasks[key] = asyncio.create_task(
            self.expire_spider_spawn(cog, guild_id, channel_id, message_id, spawn_id, emoji_id, expires_at)
        )

    async def expire_spider_spawn(
        self,
        cog: "PublicEventsCog",
        guild_id: int,
        channel_id: int,
        message_id: int,
        spawn_id: int,
        emoji_id: int,
        expires_at: datetime,
    ) -> None:
        try:
            await discord.utils.sleep_until(expires_at)
            guild = cog.bot.get_guild(guild_id)
            channel = cog.bot.get_channel(channel_id)
            if guild is not None and isinstance(channel, (discord.TextChannel, discord.Thread)):
                message: discord.Message | None = None
                try:
                    message = await channel.fetch_message(message_id)
                    await message.clear_reaction(cog._candy_drop_emoji(guild, emoji_id))
                except discord.Forbidden:
                    if message is not None and cog.bot.user is not None:
                        try:
                            await message.remove_reaction(cog._candy_drop_emoji(guild, emoji_id), cog.bot.user)
                        except discord.HTTPException:
                            pass
                except discord.HTTPException:
                    pass
            await cog.public_events_repo.close_spider_spawn(guild_id, spawn_id)
        except asyncio.CancelledError:
            raise
        finally:
            current_task = asyncio.current_task()
            key = (guild_id, spawn_id)
            if cog._spider_expire_tasks.get(key) is current_task:
                cog._spider_expire_tasks.pop(key, None)

    async def handle_spider_reaction(
        self,
        cog: "PublicEventsCog",
        payload: discord.RawReactionActionEvent,
    ) -> None:
        if payload.guild_id is None:
            return
        if cog.bot.user is not None and payload.user_id == cog.bot.user.id:
            return
        spider_cfg = cog._spider_config()
        emoji_id = cog._spider_emoji_id(spider_cfg)
        if emoji_id > 0 and payload.emoji.id != emoji_id:
            return
        if emoji_id <= 0 and str(payload.emoji.name) != "ðŸ•·ï¸":
            return

        state = await cog.public_events_repo.get_spider_state(payload.guild_id)
        if not state.get("active"):
            return
        ends_at = cog._parse_dt(state.get("ends_at"))
        if ends_at is None or ends_at <= datetime.now(timezone.utc):
            guild = cog.bot.get_guild(payload.guild_id)
            if guild is not None:
                await self.finish_spider_infestation(cog, guild)
            return

        spawn = await cog.public_events_repo.get_active_spider_spawn(payload.guild_id, payload.message_id, emoji_id)
        if spawn is None:
            return
        expires_at = cog._parse_dt(spawn.get("expires_at"))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            await cog.public_events_repo.close_spider_spawn(payload.guild_id, int(spawn["spawn_id"]))
            return

        max_captures = int(spider_cfg.get("max_captures_per_user", 0) or 0)
        if max_captures > 0:
            current = await cog.public_events_repo.count_spider_captures_for_user(payload.guild_id, payload.user_id)
            if current >= max_captures:
                return

        await cog.public_events_repo.add_spider_capture(
            payload.guild_id,
            int(spawn["spawn_id"]),
            payload.user_id,
            datetime.now(timezone.utc).isoformat(),
        )

