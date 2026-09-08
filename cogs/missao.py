from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.mission_service import MissionService


class MissionPanelView(discord.ui.View):
    def __init__(self, cog: "MissaoCog", owner_id: int, locale: str) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.owner_id = owner_id
        self.locale = locale
        self.profile.label = cog.mission_service.i18n.t(locale, "common.profile")
        self.shop.label = cog.mission_service.i18n.t(locale, "common.shop")
        self.board.label = cog.mission_service.i18n.t(locale, "common.board")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            self.cog.mission_service.i18n.t(self.locale, "common.not_owner.missions"),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Perfil", style=discord.ButtonStyle.success)
    async def profile(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        profile_cog = interaction.client.get_cog("ProfileCog")
        if profile_cog is None:
            await interaction.response.send_message(
                self.cog.mission_service.i18n.t(self.locale, "common.not_loaded.profile"),
                ephemeral=True,
            )
            return
        await profile_cog.show_profile_panel(interaction, self.owner_id)

    @discord.ui.button(label="Loja", style=discord.ButtonStyle.primary)
    async def shop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        shop_cog = interaction.client.get_cog("ShopCog")
        if shop_cog is None:
            await interaction.response.send_message(
                self.cog.mission_service.i18n.t(self.locale, "common.not_loaded.shop"),
                ephemeral=True,
            )
            return
        await shop_cog.show_shop_panel(interaction, self.owner_id)

    @discord.ui.button(label="Tabuleiro", style=discord.ButtonStyle.secondary)
    async def board(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        board_cog = interaction.client.get_cog("TabuleiroCog")
        if board_cog is None:
            await interaction.response.send_message(
                self.cog.mission_service.i18n.t(self.locale, "common.not_loaded.board"),
                ephemeral=True,
            )
            return
        await board_cog.show_board_panel(interaction, self.owner_id)


class MissaoCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.mission_service = MissionService()

    @app_commands.command(name="missao", description="Mostra suas missoes diarias e a missao global.")
    async def missao(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        locale = await self.mission_service.i18n.get_user_locale(interaction.user.id)
        embed = await self.mission_service.build_embed(interaction.guild.id if interaction.guild else None, interaction.user.id)
        await interaction.followup.send(
            embed=embed,
            view=MissionPanelView(self, interaction.user.id, locale),
            ephemeral=True,
        )

    async def show_mission_panel(self, interaction: discord.Interaction, owner_id: int) -> None:
        await interaction.response.defer()
        locale = await self.mission_service.i18n.get_user_locale(owner_id)
        embed = await self.mission_service.build_embed(interaction.guild.id if interaction.guild else None, owner_id)
        await interaction.edit_original_response(
            content=None,
            embed=embed,
            attachments=[],
            view=MissionPanelView(self, owner_id, locale),
        )

    async def send_mission_panel(self, interaction: discord.Interaction, owner_id: int) -> None:
        """Envia as missoes separadas quando o painel de origem usa Components V2."""
        await interaction.response.defer(ephemeral=True)
        locale = await self.mission_service.i18n.get_user_locale(owner_id)
        embed = await self.mission_service.build_embed(
            interaction.guild.id if interaction.guild else None,
            owner_id,
        )
        await interaction.followup.send(
            embed=embed,
            view=MissionPanelView(self, owner_id, locale),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MissaoCog(bot))
