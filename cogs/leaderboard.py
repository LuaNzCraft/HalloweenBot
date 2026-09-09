from __future__ import annotations

import discord
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from services.audit_service import AuditService
from services.leaderboard_service import LEADERBOARD_ORDER, LeaderboardService

DEFAULT_PAGE = LEADERBOARD_ORDER[0]


class LeaderboardPanelView(discord.ui.View):
    def __init__(self, service: LeaderboardService, active: str = DEFAULT_PAGE) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.active = service.normalize_leaderboard_type(active)
        self.page_index = service.get_page_index(self.active)
        total_pages = service.get_total_pages()

        first_button = discord.ui.Button(
            label="<<",
            style=discord.ButtonStyle.secondary,
            custom_id="halloween_lb_first",
            disabled=self.page_index <= 0,
        )
        first_button.callback = self._go_first
        self.add_item(first_button)

        previous_button = discord.ui.Button(
            label="<",
            style=discord.ButtonStyle.secondary,
            custom_id="halloween_lb_prev",
            disabled=self.page_index <= 0,
        )
        previous_button.callback = self._go_previous
        self.add_item(previous_button)

        page_button = discord.ui.Button(
            label=f"{self.page_index + 1}/{total_pages}",
            style=discord.ButtonStyle.primary,
            custom_id="halloween_lb_page",
            disabled=True,
        )
        self.add_item(page_button)

        next_button = discord.ui.Button(
            label=">",
            style=discord.ButtonStyle.secondary,
            custom_id="halloween_lb_next",
            disabled=self.page_index >= total_pages - 1,
        )
        next_button.callback = self._go_next
        self.add_item(next_button)

        last_button = discord.ui.Button(
            label=">>",
            style=discord.ButtonStyle.secondary,
            custom_id="halloween_lb_last",
            disabled=self.page_index >= total_pages - 1,
        )
        last_button.callback = self._go_last
        self.add_item(last_button)

    async def _go_first(self, interaction: discord.Interaction) -> None:
        await self._show(interaction, self.service.get_page_by_index(0))

    async def _go_previous(self, interaction: discord.Interaction) -> None:
        previous_page = self.service.get_page_by_index(self.page_index - 1)
        await self._show(interaction, previous_page)

    async def _go_next(self, interaction: discord.Interaction) -> None:
        next_page = self.service.get_page_by_index(self.page_index + 1)
        await self._show(interaction, next_page)

    async def _go_last(self, interaction: discord.Interaction) -> None:
        await self._show(interaction, self.service.get_page_by_index(self.service.get_total_pages() - 1))

    async def _show(self, interaction: discord.Interaction, page: str) -> None:
        await interaction.response.defer()
        normalized_page = self.service.normalize_leaderboard_type(page)
        embed = await self.service.build_embed(normalized_page, interaction.guild)
        new_view = LeaderboardPanelView(self.service, active=normalized_page)
        await interaction.edit_original_response(embed=embed, view=new_view)
        if interaction.guild:
            await self.service.set_panel_page(interaction.guild.id, normalized_page)


class LeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.admin_repo = AdminRepo()
        self.audit_service = AuditService(bot, self.admin_repo)
        self.leaderboard_service = LeaderboardService(bot, self.admin_repo)
        bot.add_view(LeaderboardPanelView(self.leaderboard_service, active=DEFAULT_PAGE))

    async def cog_load(self) -> None:
        await self._restore_all_panels()

    async def _restore_all_panels(self) -> None:
        try:
            panels = await self.leaderboard_service.get_all_panels()
        except Exception:
            return

        for guild_id, channel_id, message_id, active_page in panels:
            try:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue
                channel = guild.get_channel(channel_id)
                if channel is None:
                    continue
                message = await channel.fetch_message(message_id)
                view = LeaderboardPanelView(self.leaderboard_service, active=active_page or DEFAULT_PAGE)
                await message.edit(view=view)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                continue

    async def publish_or_update_panel(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
    ) -> tuple[bool, str]:
        try:
            active_page = await self.leaderboard_service.get_panel_page(guild.id) or DEFAULT_PAGE
        except Exception:
            active_page = DEFAULT_PAGE

        try:
            await self.leaderboard_service.publish_or_update_panel(
                guild,
                LeaderboardPanelView(self.leaderboard_service, active=active_page),
            )
        except ValueError as exc:
            if str(exc) == "leaderboard_channel_not_configured":
                return False, "Selecione primeiro o canal de leaderboards nesta pagina."
            return False, "Nao consegui atualizar o painel de rankings configurado."
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return (
                False,
                "Nao consegui publicar no canal configurado. Verifique minhas permissoes.",
            )

        await self.audit_service.log(
            guild_id=guild.id,
            actor_id=actor_id,
            action="leaderboard.update",
            payload={"type": "painel"},
        )
        return True, "Painel de rankings publicado ou atualizado."


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LeaderboardCog(bot))
