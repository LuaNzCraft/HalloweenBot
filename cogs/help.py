from __future__ import annotations

from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from services.i18n_service import I18nService


HELP_COLOR = discord.Color.from_rgb(245, 122, 39)


@dataclass(frozen=True)
class HelpPage:
    title_key: str
    description_key: str


HELP_PAGES = [
    HelpPage("help.page.overview.title", "help.page.overview.description"),
    HelpPage("help.page.economy.title", "help.page.economy.description"),
    HelpPage("help.page.missions.title", "help.page.missions.description"),
    HelpPage("help.page.board.title", "help.page.board.description"),
    HelpPage("help.page.events.title", "help.page.events.description"),
    HelpPage("help.page.items.title", "help.page.items.description"),
    HelpPage("help.page.commands.title", "help.page.commands.description"),
]


class HelpView(discord.ui.View):
    def __init__(self, cog: "HelpCog", owner_id: int, locale: str, page_index: int) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.owner_id = owner_id
        self.locale = locale
        self.page_index = page_index
        self._sync_state()

    def _sync_state(self) -> None:
        self.page_button.label = f"{self.page_index + 1}/{len(HELP_PAGES)}"
        self.first_button.disabled = self.page_index <= 0
        self.previous_button.disabled = self.page_index <= 0
        self.next_button.disabled = self.page_index >= len(HELP_PAGES) - 1
        self.last_button.disabled = self.page_index >= len(HELP_PAGES) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            self.cog.i18n.t(self.locale, "common.not_owner.help"),
            ephemeral=True,
        )
        return False

    async def _go_to_page(self, interaction: discord.Interaction, next_index: int) -> None:
        self.page_index = next_index
        self._sync_state()
        embed = self.cog.build_help_embed(self.locale, self.page_index)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="<<", style=discord.ButtonStyle.secondary)
    async def first_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go_to_page(interaction, 0)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go_to_page(interaction, max(0, self.page_index - 1))

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go_to_page(interaction, min(len(HELP_PAGES) - 1, self.page_index + 1))

    @discord.ui.button(label=">>", style=discord.ButtonStyle.secondary)
    async def last_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go_to_page(interaction, len(HELP_PAGES) - 1)


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.i18n = I18nService()

    @app_commands.command(name="help", description="Explica como o evento Halloween funciona.")
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        locale = await self.i18n.get_user_locale(interaction.user.id)
        embed = self.build_help_embed(locale, 0)
        await interaction.followup.send(
            embed=embed,
            view=HelpView(self, interaction.user.id, locale, 0),
            ephemeral=True,
        )

    def build_help_embed(self, locale: str, page_index: int) -> discord.Embed:
        page = HELP_PAGES[page_index]
        embed = discord.Embed(
            title=self.i18n.t(locale, "help.title"),
            description=self.i18n.t(locale, "help.description"),
            color=HELP_COLOR,
        )
        embed.add_field(
            name=self.i18n.t(locale, page.title_key),
            value=self.i18n.t(locale, page.description_key),
            inline=False,
        )
        embed.set_footer(
            text=self.i18n.t(
                locale,
                "help.footer",
                current=page_index + 1,
                total=len(HELP_PAGES),
            )
        )
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
