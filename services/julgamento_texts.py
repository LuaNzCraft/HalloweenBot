from __future__ import annotations

from datetime import datetime
from typing import Callable

import discord


Translator = Callable[..., str]


class JulgamentoTextsService:
    def build_event_base_embed(self, jt: Translator, color: discord.Color, locale: str = "pt-BR") -> discord.Embed:
        embed = discord.Embed(
            title=jt(locale, "base.title"),
            description=jt(locale, "base.description"),
            color=color,
        )
        embed.add_field(name=jt(locale, "base.rules_name"), value=jt(locale, "base.rules_value"), inline=False)
        embed.add_field(name=jt(locale, "base.audience_name"), value=jt(locale, "base.audience_value"), inline=False)
        return embed

    def build_interrupted_embed(self, jt: Translator, color: discord.Color, locale: str = "pt-BR") -> discord.Embed:
        return discord.Embed(
            title=jt(locale, "interrupted.title"),
            description=jt(locale, "interrupted.description"),
            color=color,
        )

    def build_announcement_embed(
        self,
        jt: Translator,
        color: discord.Color,
        locale: str,
        starts_at: datetime,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=jt(locale, "announcement.title"),
            description=jt(
                locale,
                "announcement.description",
                starts_at=discord.utils.format_dt(starts_at, style="R"),
            ),
            color=color,
        )
        embed.add_field(
            name=jt(locale, "announcement.prep_name"),
            value=jt(locale, "announcement.prep_value"),
            inline=False,
        )
        return embed

    def build_round_announcement_embed(
        self,
        jt: Translator,
        color: discord.Color,
        locale: str,
        next_round: int,
        alive_count: int,
        mode_name: str,
        countdown_ends_at: datetime,
        responder_id: int | None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=jt(locale, "round_announcement.title"),
            description=(
                jt(
                    locale,
                    "round_announcement.description_header",
                    round=next_round,
                    alive=alive_count,
                    mode=mode_name,
                )
                + "\n\n"
                + (
                    jt(locale, "round_announcement.collective_intro")
                    if responder_id is None
                    else jt(locale, "round_announcement.individual_intro", responder=f"<@{responder_id}>")
                )
                + "\n\n"
                + jt(locale, "round_announcement.outro")
            ),
            color=color,
        )
        embed.add_field(
            name=jt(locale, "round_announcement.question_in_name"),
            value=discord.utils.format_dt(countdown_ends_at, style="R"),
            inline=False,
        )
        embed.add_field(
            name=jt(locale, "round_announcement.rule_name"),
            value=(
                jt(locale, "round_announcement.rule_collective")
                if responder_id is None
                else jt(locale, "round_announcement.rule_individual")
            ),
            inline=False,
        )
        return embed

    def build_individual_correct_embed(
        self,
        jt: Translator,
        color: discord.Color,
        locale: str,
        member_mention: str,
    ) -> discord.Embed:
        return discord.Embed(
            title=jt(locale, "correct.title"),
            description=jt(locale, "correct.description", member=member_mention),
            color=color,
        )

    def build_timeout_embed(
        self,
        jt: Translator,
        color: discord.Color,
        locale: str,
        member_mention: str,
    ) -> discord.Embed:
        return discord.Embed(
            title=jt(locale, "timeout.title"),
            description=jt(locale, "timeout.description", member=member_mention),
            color=color,
        )

    def build_elimination_summary_embed(
        self,
        jt: Translator,
        color: discord.Color,
        locale: str,
        question_text: str,
        eliminated_mentions: str,
        fastest_mentions: str,
        alive_count: int,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=jt(locale, "elimination.title"),
            description=jt(locale, "elimination.description", eliminated=eliminated_mentions),
            color=color,
        )
        embed.add_field(name=jt(locale, "elimination.question_name"), value=question_text, inline=False)
        embed.add_field(name=jt(locale, "elimination.fastest_name"), value=fastest_mentions, inline=False)
        embed.add_field(name=jt(locale, "elimination.alive_name"), value=str(alive_count), inline=True)
        return embed

    def build_finish_embed(
        self,
        jt: Translator,
        color: discord.Color,
        locale: str,
        winner_mention: str | None,
    ) -> discord.Embed:
        description = (
            jt(locale, "finish.winner_description", winner=winner_mention)
            if winner_mention
            else jt(locale, "finish.no_winner_description")
        )
        return discord.Embed(
            title=jt(locale, "finish.title"),
            description=description,
            color=color,
        )

    def build_ticket_required_embed(
        self,
        jt: Translator,
        color: discord.Color,
        locale: str,
        *,
        inventory_missing: bool = False,
    ) -> discord.Embed:
        description_key = "join.ticket_missing_inventory_description" if inventory_missing else "join.ticket_required_description"
        return discord.Embed(
            title=jt(locale, "join.ticket_required_title"),
            description=jt(locale, description_key),
            color=color,
        )

    def build_join_confirmed_embed(
        self,
        jt: Translator,
        color: discord.Color,
        locale: str,
        *,
        remaining_tickets: int,
        channel_link: str,
        thread_link: str,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=jt(locale, "join.confirmed_title"),
            description=jt(locale, "join.confirmed_description"),
            color=color,
        )
        embed.add_field(name=jt(locale, "join.remaining_tickets_name"), value=str(remaining_tickets), inline=False)
        embed.add_field(name=jt(locale, "join.rounds_channel_name"), value=channel_link, inline=False)
        embed.add_field(name=jt(locale, "join.audience_channel_name"), value=thread_link, inline=False)
        return embed

    def build_round_question_embed(
        self,
        jt: Translator,
        color: discord.Color,
        runtime: dict,
        question: dict,
        responder_id: int | None,
        timeout_seconds: int,
        locale: str = "pt-BR",
        *,
        attachment_filename: str | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=jt(locale, "question.title"),
            description=f"**{str(question.get('question') or runtime.get('current_question_text') or '-')}**",
            color=color,
        )
        if responder_id is None:
            embed.add_field(
                name=jt(locale, "question.rule_name"),
                value=jt(locale, "question.rule_collective"),
                inline=False,
            )
        else:
            embed.add_field(
                name=jt(locale, "question.rule_name"),
                value=jt(locale, "question.rule_individual", responder=f"<@{responder_id}>"),
                inline=False,
            )
        embed.add_field(
            name=jt(locale, "question.time_name"),
            value=jt(locale, "question.time_value", seconds=timeout_seconds),
            inline=False,
        )
        if attachment_filename:
            embed.set_image(url=f"attachment://{attachment_filename}")
        return embed

    def build_question_translation_embed(
        self,
        jt: Translator,
        color: discord.Color,
        question: dict,
        timeout_seconds: int,
    ) -> discord.Embed | None:
        question_en = str(question.get("question_en") or "").strip()
        if not question_en:
            return None
        embed = discord.Embed(
            title=jt("en-US", "translation.title"),
            description=jt("en-US", "translation.description", question=question_en),
            color=color,
        )
        embed.add_field(
            name=jt("en-US", "translation.time_name"),
            value=jt("en-US", "translation.time_value", seconds=timeout_seconds),
            inline=False,
        )
        embed.set_footer(text=jt("en-US", "translation.footer"))
        return embed
