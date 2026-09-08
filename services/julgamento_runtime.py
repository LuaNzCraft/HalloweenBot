from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import TYPE_CHECKING

import discord

from views.julgamento_views import CarrosselTranslateView


if TYPE_CHECKING:
    from cogs.julgamento import JulgamentoCog


LOGGER = logging.getLogger(__name__)


class JulgamentoRuntimeService:
    async def start_round(
        self,
        cog: "JulgamentoCog",
        guild: discord.Guild,
        runtime: dict,
        alive: list[dict],
    ) -> None:
        if guild.id in cog._starting_round_guilds:
            return
        cog._starting_round_guilds.add(guild.id)
        try:
            channel = await cog._resolve_text_channel(guild, runtime.get("channel_id"))
            thread = await cog._resolve_thread(guild, runtime.get("thread_id"))
            if not isinstance(channel, discord.TextChannel) or not isinstance(thread, discord.Thread):
                return

            next_round = int(runtime.get("round_number") or 0) + 1
            alive_count = len(alive)
            difficulty = cog._difficulty_for_round(next_round, alive_count)
            question = cog._pick_question_by_difficulty(difficulty)
            if question is None:
                await cog._send_silas_message(channel, content=cog._jt("pt-BR", "public.no_question_content"))
                await self.finish_event(cog, guild, None)
                return

            countdown_seconds = 30 if next_round == 1 else 10
            countdown_ends_at = datetime.now(timezone.utc) + timedelta(seconds=countdown_seconds)
            responder_id = cog._pick_individual_responder(runtime, alive, next_round) if alive_count <= 2 else None
            mode_name = "Resposta individual" if responder_id else "Corrida de digitacao"

            cog._pending_questions[guild.id] = dict(question)
            await cog.repo.set_runtime(
                guild.id,
                phase="countdown",
                round_number=next_round,
                current_player_id=responder_id,
                current_question_id=str(question.get("id") or "") or None,
                current_question_text=str(question["question"]),
                current_answers=cog._expand_answers(question),
                current_correct_order=[],
                current_match_mode=str(question.get("match_mode") or "normalized"),
                answer_deadline_at=countdown_ends_at.isoformat(),
            )
            await cog._sync_round_roles(guild, round_open=True, active_user_id=responder_id)
            embed_en = cog._build_round_announcement_embed_en(
                next_round,
                alive_count,
                mode_name,
                countdown_ends_at,
                responder_id,
            )
            embed_pt = cog._build_round_announcement_embed(
                "pt-BR",
                next_round,
                alive_count,
                mode_name,
                countdown_ends_at,
                responder_id,
            )
            round_message = await cog._send_silas_message(
                channel,
                embed=embed_pt,
                view=cog._build_public_translate_view(embed_en),
                wait=True,
            )
            if round_message is None:
                await cog._clear_round_state(guild.id)
                return
            self.schedule_countdown_launch(cog, guild.id, next_round, countdown_seconds)
            await cog._refresh_base_message(guild)
        finally:
            cog._starting_round_guilds.discard(guild.id)

    def schedule_countdown_launch(
        self,
        cog: "JulgamentoCog",
        guild_id: int,
        round_number: int,
        delay_seconds: int,
    ) -> None:
        existing_task = cog._countdown_tasks.pop(guild_id, None)
        if existing_task is not None:
            existing_task.cancel()
        cog._countdown_tasks[guild_id] = asyncio.create_task(
            self.launch_countdown_after_delay(cog, guild_id, round_number, delay_seconds)
        )

    async def launch_countdown_after_delay(
        self,
        cog: "JulgamentoCog",
        guild_id: int,
        round_number: int,
        delay_seconds: int,
    ) -> None:
        try:
            await asyncio.sleep(max(0, delay_seconds) + 0.2)
            guild = cog.bot.get_guild(guild_id)
            if guild is None:
                return
            runtime = await cog.repo.get_runtime(guild_id)
            if not runtime.get("active") or runtime.get("phase") != "countdown":
                return
            if int(runtime.get("round_number") or 0) != round_number:
                return
            await self.launch_countdown_question(cog, guild, runtime)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Falha no timer direto da pergunta do Carrossel para guild %s", guild_id)
        finally:
            current_task = asyncio.current_task()
            if cog._countdown_tasks.get(guild_id) is current_task:
                cog._countdown_tasks.pop(guild_id, None)

    async def launch_countdown_question(self, cog: "JulgamentoCog", guild: discord.Guild, runtime: dict) -> None:
        if guild.id in cog._launching_guilds:
            return
        cog._launching_guilds.add(guild.id)
        try:
            runtime = await cog.repo.get_runtime(guild.id)
            if runtime.get("phase") != "countdown":
                return
            channel = await cog._resolve_text_channel(guild, runtime.get("channel_id"))
            if not isinstance(channel, discord.TextChannel):
                return
            question = (
                cog._pending_questions.get(guild.id)
                or cog._find_question_by_id(runtime.get("current_question_id"))
                or {"question": runtime.get("current_question_text")}
            )
            timeout_seconds = int(cog._load_config().get("answer_timeout_seconds", 25))
            deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
            responder_id = int(runtime.get("current_player_id") or 0) or None
            embed = cog._build_round_question_embed(runtime, question, responder_id, timeout_seconds, locale="pt-BR")
            sent = await cog._send_round_question_payload(
                channel,
                embed=embed,
                view=CarrosselTranslateView(cog, question, timeout_seconds),
                question=question,
            )
            if sent is None:
                LOGGER.error("Falha ao enviar pergunta do Carrossel para guild %s", guild.id)
                await cog._clear_round_state(guild.id)
                return
            await cog.repo.set_runtime(
                guild.id,
                phase="running",
                answer_deadline_at=deadline.isoformat(),
            )
            await cog._refresh_base_message(guild)
        finally:
            cog._launching_guilds.discard(guild.id)

    async def resolve_round(self, cog: "JulgamentoCog", guild: discord.Guild, runtime: dict) -> None:
        alive = await cog._alive_participants(guild.id)
        alive_ids = [int(item["user_id"]) for item in alive]
        if len(alive_ids) <= 1:
            await self.finish_event(cog, guild, alive_ids[0] if alive_ids else None)
            return

        current_player_id = int(runtime.get("current_player_id") or 0) or None
        if current_player_id is not None:
            await self.resolve_individual_round_timeout(cog, guild, current_player_id)
            return

        correct_order = [
            int(user_id)
            for user_id in runtime.get("current_correct_order", [])
            if int(user_id) in alive_ids
        ]
        await self.resolve_collective_round(cog, guild, runtime, alive_ids, correct_order)

    async def handle_individual_correct_answer(self, cog: "JulgamentoCog", message: discord.Message) -> None:
        if message.guild.id in cog._resolving_guilds:
            return
        cog._resolving_guilds.add(message.guild.id)
        try:
            # Fecha a rodada no estado antes de awaits lentos para evitar timeout concorrente.
            await cog._clear_round_state(message.guild.id)
            try:
                await message.add_reaction("\u2705")
            except discord.HTTPException:
                pass
            await cog._increment_horror_correct_answers(message.guild, message.author.id)
            await cog._send_silas_message(
                message.channel,
                embed=cog._build_individual_correct_embed("pt-BR", message.author.mention),
                view=cog._build_public_translate_view(cog._build_individual_correct_embed_en(message.author.mention)),
            )
            await cog._sync_round_roles(message.guild, round_open=False)
            await cog._refresh_base_message(message.guild)
        finally:
            cog._resolving_guilds.discard(message.guild.id)

    async def handle_collective_correct_answer(
        self,
        cog: "JulgamentoCog",
        message: discord.Message,
        correct_order: list[int],
    ) -> None:
        alive = await cog._alive_participants(message.guild.id)
        if len(alive) <= 2:
            if message.guild.id in cog._finishing_guilds:
                return
            cog._finishing_guilds.add(message.guild.id)
            try:
                try:
                    await message.add_reaction("\u2705")
                except discord.HTTPException:
                    pass
                await self.finish_event(cog, message.guild, message.author.id)
            finally:
                cog._finishing_guilds.discard(message.guild.id)
            return

        correct_order.append(message.author.id)
        await cog.repo.set_runtime(message.guild.id, current_correct_order=correct_order)
        await cog._increment_horror_correct_answers(message.guild, message.author.id)
        try:
            await message.add_reaction("\u2705")
        except discord.HTTPException:
            pass
        if len(correct_order) >= len(alive):
            updated_runtime = await cog.repo.get_runtime(message.guild.id)
            await self.resolve_round(cog, message.guild, updated_runtime)

    async def resolve_individual_round_timeout(
        self,
        cog: "JulgamentoCog",
        guild: discord.Guild,
        current_player_id: int,
    ) -> None:
        await cog.repo.set_participant_status(guild.id, current_player_id, "spectator")
        member = guild.get_member(current_player_id)
        if member is not None:
            await cog._move_member_to_spectator(guild, member)
        runtime = await cog.repo.get_runtime(guild.id)
        channel = guild.get_channel(int(runtime["channel_id"])) if runtime.get("channel_id") else None
        if isinstance(channel, discord.TextChannel):
            member_mention = member.mention if member else f"<@{current_player_id}>"
            await cog._send_silas_message(
                channel,
                embed=cog._build_timeout_embed("pt-BR", member_mention),
                view=cog._build_public_translate_view(cog._build_timeout_embed_en(member_mention)),
            )
        await cog._sync_round_roles(guild, round_open=False)
        await cog._clear_round_state(guild.id)
        await cog._refresh_base_message(guild)

    async def resolve_collective_round(
        self,
        cog: "JulgamentoCog",
        guild: discord.Guild,
        runtime: dict,
        alive_ids: list[int],
        correct_order: list[int],
    ) -> None:
        answered_ids = set(correct_order)
        eliminated_ids = [user_id for user_id in alive_ids if user_id not in answered_ids]

        if not eliminated_ids and not correct_order:
            channel = guild.get_channel(int(runtime["channel_id"])) if runtime.get("channel_id") else None
            if isinstance(channel, discord.TextChannel):
                await cog._send_silas_message(channel, content=cog._jt("pt-BR", "public.no_answers_content"))
            await cog._sync_round_roles(guild, round_open=False)
            await cog._clear_round_state(guild.id)
            await cog._refresh_base_message(guild)
            return

        if not eliminated_ids and len(alive_ids) > 2 and correct_order:
            eliminated_ids.append(correct_order[-1])

        channel = guild.get_channel(int(runtime["channel_id"])) if runtime.get("channel_id") else None
        if eliminated_ids:
            for user_id in eliminated_ids:
                await cog.repo.set_participant_status(guild.id, user_id, "spectator")
                member = guild.get_member(user_id)
                if member is not None:
                    await cog._move_member_to_spectator(guild, member)
            await self.announce_elimination_summary(cog, guild, runtime, eliminated_ids, correct_order)
        elif isinstance(channel, discord.TextChannel):
            await cog._send_silas_message(channel, content=cog._jt("pt-BR", "public.no_elimination_content"))

        await cog._sync_round_roles(guild, round_open=False)
        await cog._clear_round_state(guild.id)
        await cog._refresh_base_message(guild)

    async def announce_elimination_summary(
        self,
        cog: "JulgamentoCog",
        guild: discord.Guild,
        runtime: dict,
        eliminated_ids: list[int],
        correct_order: list[int],
    ) -> None:
        alive_count = await cog._alive_count(guild.id)
        event_channels = await cog._get_event_channels(guild, await cog.admin_repo.get_config(guild.id))
        if not event_channels:
            return
        eliminated_mentions = ", ".join(
            guild.get_member(user_id).mention if guild.get_member(user_id) else f"<@{user_id}>"
            for user_id in eliminated_ids
        )
        fastest_mentions = ", ".join(
            guild.get_member(user_id).mention if guild.get_member(user_id) else f"<@{user_id}>"
            for user_id in correct_order[:3]
        ) or cog._jt("pt-BR", "elimination.nobody_answered")
        embed = discord.Embed(
            title=cog._jt("pt-BR", "elimination.title"),
            description=cog._jt("pt-BR", "elimination.description", eliminated=eliminated_mentions),
            color=cog.JULGAMENTO_COLOR if hasattr(cog, "JULGAMENTO_COLOR") else discord.Color.from_rgb(171, 52, 40),
        )
        embed.add_field(name=cog._jt("pt-BR", "elimination.question_name"), value=str(runtime.get("current_question_text") or "-"), inline=False)
        embed.add_field(name=cog._jt("pt-BR", "elimination.fastest_name"), value=fastest_mentions, inline=False)
        embed.add_field(name=cog._jt("pt-BR", "elimination.alive_name"), value=str(alive_count), inline=True)
        await cog._send_silas_message(
            event_channels[0],
            embed=embed,
            view=cog._build_public_translate_view(
                cog._build_elimination_summary_embed_en(
                    str(runtime.get("current_question_text") or "-"),
                    eliminated_mentions,
                    cog._jt("en-US", "elimination.nobody_answered")
                    if fastest_mentions == cog._jt("pt-BR", "elimination.nobody_answered")
                    else fastest_mentions,
                    alive_count,
                )
            ),
        )

    async def finish_event(self, cog: "JulgamentoCog", guild: discord.Guild, winner_user_id: int | None) -> None:
        runtime = await cog.repo.get_runtime(guild.id)
        event_channels = await cog._get_event_channels(guild, await cog.admin_repo.get_config(guild.id))
        winner = guild.get_member(int(winner_user_id)) if winner_user_id else None
        reward_lines: list[str] = []
        mission_notices: list[str] = []
        if winner_user_id:
            event_id = str(runtime.get("started_at") or runtime.get("signup_message_id") or "unknown")
            reward_lines, mission_notices = await cog._grant_carrossel_winner_rewards(
                guild.id,
                int(winner_user_id),
                f"carrossel:{guild.id}:{event_id}",
            )
        if event_channels:
            await cog._send_silas_message(
                event_channels[0],
                embed=cog._build_finish_embed("pt-BR", winner.mention if winner else None),
                view=cog._build_public_translate_view(cog._build_finish_embed_en(winner.mention if winner else None)),
            )
            if winner and reward_lines:
                embed_pt = cog._build_reward_summary_embed("pt-BR", winner.mention, reward_lines + mission_notices)
                embed_en = cog._build_reward_summary_embed("en-US", winner.mention, reward_lines + mission_notices)
                await cog._send_silas_message(
                    event_channels[0],
                    embed=embed_pt,
                    view=cog._build_public_translate_view(embed_en),
                )
        spectator_end = datetime.now(timezone.utc) + timedelta(minutes=int(cog._load_config().get("spectator_minutes_after_end", 10)))
        await cog.repo.set_runtime(guild.id, spectator_ends_at=spectator_end.isoformat(), winner_user_id=winner_user_id, phase="finished")
        thread = guild.get_channel(int(runtime["thread_id"])) if runtime.get("thread_id") else None
        if isinstance(thread, discord.Thread):
            try:
                await thread.edit(archived=False, locked=False)
            except discord.HTTPException:
                pass
        await cog._refresh_base_message(guild, winner_user_id=winner_user_id, finished=True)
