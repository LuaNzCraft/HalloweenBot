from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.i18n_service import I18nService
from views.help_panel import HelpPanelView


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.i18n = I18nService()

    @app_commands.command(name="help", description="Explica como o evento Halloween funciona.")
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        locale = await self.i18n.get_user_locale(interaction.user.id)
        panel = HelpPanelView(
            self,
            owner_id=interaction.user.id,
            locale=locale,
        )
        sent = await interaction.followup.send(
            view=panel,
            ephemeral=True,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        panel.message = sent


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
