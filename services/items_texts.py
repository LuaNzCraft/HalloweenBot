from __future__ import annotations

from datetime import datetime, timezone

import discord

from services.i18n_service import I18nService


ITEMS_COLOR = discord.Color.from_rgb(245, 122, 39)


class ItemsTextService:
    def __init__(self, i18n: I18nService) -> None:
        self.i18n = i18n

    def format_switch_end_time(self, expires_at: datetime) -> str:
        unix = int(expires_at.astimezone(timezone.utc).timestamp())
        return f"<t:{unix}:R> (<t:{unix}:f>)"

    def mute_no_item_embed(self, locale: str, actor_name: str, target_name: str) -> discord.Embed:
        return discord.Embed(
            title=self.i18n.t(locale, "mute.no_item_title"),
            description=self.i18n.t(locale, "mute.no_item", actor=actor_name, target=target_name),
            color=ITEMS_COLOR,
        )

    def mute_applied_embed(
        self,
        locale: str,
        *,
        actor_name: str,
        target_name: str,
        reflected: bool,
        delete_messages: bool,
        duration_seconds: int,
        expires_at: datetime,
        mission_notices: list[str],
    ) -> discord.Embed:
        if reflected:
            title_key = "mute.reflected_title"
            description_key = "mute.admin_reflected" if delete_messages else "mute.reflected"
        else:
            title_key = "mute.admin_applied_title" if delete_messages else "mute.applied_title"
            description_key = "mute.admin_applied" if delete_messages else "mute.applied"
        description_kwargs = {
            "actor": actor_name,
            "target": target_name,
            "minutes": max(1, duration_seconds // 60),
            "ends_at": self.format_switch_end_time(expires_at),
        }
        embed = discord.Embed(
            title=self.i18n.t(locale, title_key),
            description=self.i18n.t(locale, description_key, **description_kwargs),
            color=ITEMS_COLOR,
        )
        if mission_notices:
            embed.add_field(
                name=self.i18n.t(locale, "missions.title"),
                value="\n".join(mission_notices)[:1024],
                inline=False,
            )
        return embed

    def switch_applied_embed(
        self,
        locale: str,
        *,
        actor_name: str,
        target_name: str,
        duration_seconds: int,
        expires_at: datetime,
    ) -> discord.Embed:
        return discord.Embed(
            title=self.i18n.t(locale, "switch.applied_title"),
            description=self.i18n.t(
                locale,
                "switch.applied",
                actor=actor_name,
                target=target_name,
                minutes=max(1, duration_seconds // 60),
                ends_at=self.format_switch_end_time(expires_at),
            ),
            color=ITEMS_COLOR,
        )

    def interference_applied_embed(
        self,
        locale: str,
        *,
        actor_name: str,
        target_name: str,
        duration_seconds: int,
        expires_at: datetime,
    ) -> discord.Embed:
        return discord.Embed(
            title=self.i18n.t(locale, "interference.applied_title"),
            description=self.i18n.t(
                locale,
                "interference.applied",
                actor=actor_name,
                target=target_name,
                minutes=max(1, duration_seconds // 60),
                ends_at=self.format_switch_end_time(expires_at),
            ),
            color=ITEMS_COLOR,
        )

    def ghostface_hint_embed(
        self,
        locale: str,
        *,
        word_count: int,
        initials: str,
        pattern: str,
        remaining_at: datetime | None,
        extra_hint: str | None,
        ) -> discord.Embed:
        embed = discord.Embed(
            title=self.i18n.t(locale, "ghostface_hint.title"),
            description=self.i18n.t(locale, "ghostface_hint.description"),
            color=ITEMS_COLOR,
        )
        embed.add_field(
            name=self.i18n.t(locale, "ghostface_hint.words_name"),
            value=self.i18n.t(locale, "ghostface_hint.words_value", count=word_count),
            inline=False,
        )
        embed.add_field(
            name=self.i18n.t(locale, "ghostface_hint.initials_name"),
            value=f"`{initials}`",
            inline=False,
        )
        embed.add_field(
            name=self.i18n.t(locale, "ghostface_hint.pattern_name"),
            value=f"`{pattern}`",
            inline=False,
        )
        if extra_hint:
            embed.add_field(
                name=self.i18n.t(locale, "ghostface_hint.extra_name"),
                value=extra_hint,
                inline=False,
            )
        if remaining_at is not None:
            embed.add_field(
                name=self.i18n.t(locale, "ghostface_hint.remaining_name"),
                value=discord.utils.format_dt(remaining_at, style="R"),
                inline=False,
            )
        return embed
