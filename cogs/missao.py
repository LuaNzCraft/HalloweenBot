from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.mission_service import MissionService
from views.mission_panel import MissionPanelView


class MissaoCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.mission_service = MissionService()

    @app_commands.command(name="missao", description="Mostra suas missoes diarias e a missao global.")
    async def missao(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await self._build_mission_panel(interaction.guild_id, interaction.user.id)
        sent = await interaction.followup.send(
            view=panel,
            ephemeral=True,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        panel.message = sent

    async def show_mission_panel(self, interaction: discord.Interaction, owner_id: int) -> None:
        await interaction.response.defer()
        panel = await self._build_mission_panel(interaction.guild_id, owner_id)
        message = await interaction.edit_original_response(
            content=None,
            embed=None,
            attachments=[],
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        panel.message = message

    async def send_mission_panel(self, interaction: discord.Interaction, owner_id: int) -> None:
        """Envia as missoes separadas quando o painel de origem usa Components V2."""
        await interaction.response.defer(ephemeral=True)
        panel = await self._build_mission_panel(interaction.guild_id, owner_id)
        sent = await interaction.followup.send(
            view=panel,
            ephemeral=True,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        panel.message = sent

    async def _build_mission_panel(
        self,
        guild_id: int | None,
        owner_id: int,
    ) -> MissionPanelView:
        data = await self.mission_service.get_panel_data(guild_id, owner_id)
        return MissionPanelView(
            self,
            owner_id=owner_id,
            data=data,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MissaoCog(bot))
