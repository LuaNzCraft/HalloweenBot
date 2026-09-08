from __future__ import annotations

import discord

from services.i18n_service import I18nService


class TabuleiroTextService:
    def __init__(self, i18n: I18nService) -> None:
        self.i18n = i18n

    def build_notice_embed(self, title: str, description: str) -> discord.Embed:
        return discord.Embed(
            title=title,
            description=description,
            color=discord.Color.from_rgb(245, 122, 39),
        )

    def build_special_lap_embed(self, locale: str, laps: int, description: str) -> discord.Embed:
        return discord.Embed(
            title=self.i18n.t(locale, "board.notice.special_lap_title", laps=laps),
            description=description,
            color=discord.Color.from_rgb(255, 170, 40),
        )

    def build_century_lap_embed(
        self,
        locale: str,
        member_name: str,
        member_mention: str,
        laps: int,
        rewards_description: str,
        *,
        role_mention: str | None = None,
        avatar_url: str | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=self.i18n.t(locale, "board.notice.century_title", laps=laps),
            description=self.i18n.t(locale, "board.notice.century_description", player=member_mention, laps=laps),
            color=discord.Color.from_rgb(212, 84, 24),
        )
        embed.set_author(name=member_name, icon_url=avatar_url or discord.Embed.Empty)
        if role_mention:
            embed.add_field(
                name=self.i18n.t(locale, "board.notice.century_role_name"),
                value=self.i18n.t(locale, "board.notice.century_role_value", role=role_mention),
                inline=False,
            )
        embed.add_field(
            name=self.i18n.t(locale, "board.notice.century_rewards_name", laps=laps),
            value=rewards_description[:1024],
            inline=False,
        )
        return embed

    def panel_content(self, locale: str, position: int, laps: int, dice: int) -> str:
        return self.i18n.t(locale, "board.panel", position=position, laps=laps, dice=dice)

    def rolling_notice_title(self, locale: str) -> str:
        return self.i18n.t(locale, "board.notice.rolling_title")

    def rolling_notice(self, locale: str, relative_time: str) -> str:
        return self.i18n.t(locale, "board.notice.rolling", time=relative_time)

    def no_dice_notice_title(self, locale: str) -> str:
        return self.i18n.t(locale, "board.notice.no_dice_title")

    def no_dice_notice(self, locale: str) -> str:
        return self.i18n.t(locale, "board.notice.no_dice")

    def result_notice_title(self, locale: str) -> str:
        return self.i18n.t(locale, "board.notice.result_title")
