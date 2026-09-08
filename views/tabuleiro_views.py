from __future__ import annotations

from typing import TYPE_CHECKING

import discord


if TYPE_CHECKING:
    from cogs.tabuleiro import TabuleiroCog


class StatusButton(discord.ui.Button):
    def __init__(self, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=True)


class RollButton(discord.ui.Button):
    def __init__(self, cog: "TabuleiroCog", owner_id: int, locale: str) -> None:
        super().__init__(label=cog.i18n.t(locale, "board.roll"), emoji="🎲", style=discord.ButtonStyle.primary)
        self.cog = cog
        self.owner_id = owner_id
        self.locale = locale

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(self.cog.i18n.t(self.locale, "common.not_owner.board"), ephemeral=True)
            return

        await self.cog.handle_roll(interaction)


class ProfileButton(discord.ui.Button):
    def __init__(self, cog: "TabuleiroCog", owner_id: int, locale: str) -> None:
        super().__init__(label=cog.i18n.t(locale, "common.profile"), style=discord.ButtonStyle.success)
        self.cog = cog
        self.owner_id = owner_id
        self.locale = locale

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(self.cog.i18n.t(self.locale, "common.not_owner.board"), ephemeral=True)
            return

        profile_cog = interaction.client.get_cog("ProfileCog")
        if profile_cog is None:
            await interaction.response.send_message(self.cog.i18n.t(self.locale, "common.not_loaded.profile"), ephemeral=True)
            return
        await profile_cog.show_profile_panel(interaction, self.owner_id)


class BoardView(discord.ui.View):
    def __init__(self, cog: "TabuleiroCog", owner_id: int, dice_count: int, laps: int, locale: str) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.owner_id = owner_id
        self.locale = locale
        self.add_item(StatusButton(cog.i18n.t(locale, "board.dice_status", count=dice_count)))
        self.add_item(RollButton(cog, owner_id, locale))
        self.add_item(StatusButton(cog.i18n.t(locale, "board.laps_status", count=laps)))
        self.add_item(ProfileButton(cog, owner_id, locale))
