from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

import discord


if TYPE_CHECKING:
    from cogs.help import HelpCog


LOGGER = logging.getLogger(__name__)
HELP_COLOR = discord.Color.from_rgb(245, 122, 39)


@dataclass(frozen=True, slots=True)
class HelpPage:
    title_key: str
    description_key: str
    emoji: str


HELP_PAGES: tuple[HelpPage, ...] = (
    HelpPage("help.page.overview.title", "help.page.overview.description", "📋"),
    HelpPage("help.page.economy.title", "help.page.economy.description", "🍬"),
    HelpPage("help.page.missions.title", "help.page.missions.description", "🕯️"),
    HelpPage("help.page.board.title", "help.page.board.description", "🎲"),
    HelpPage("help.page.events.title", "help.page.events.description", "📹"),
    HelpPage("help.page.candy_hunt.title", "help.page.candy_hunt.description", "🔎"),
    HelpPage("help.page.items.title", "help.page.items.description", "🎒"),
    HelpPage("help.page.commands.title", "help.page.commands.description", "⌨️"),
)


class HelpPageSelect(discord.ui.Select):
    def __init__(self, panel: HelpPanelView) -> None:
        options = [
            discord.SelectOption(
                label=panel.cog.i18n.t(panel.locale, page.title_key),
                value=str(index),
                emoji=page.emoji,
                default=index == panel.page_index,
            )
            for index, page in enumerate(HELP_PAGES)
        ]
        super().__init__(
            placeholder=panel.cog.i18n.t(panel.locale, "help.select.placeholder"),
            min_values=1,
            max_values=1,
            options=options,
            custom_id="help:page:select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, HelpPanelView):
            return
        await panel.show_page(interaction, int(self.values[0]))


class HelpNavigationButton(discord.ui.Button):
    def __init__(
        self,
        direction: int,
        label: str,
        *,
        disabled: bool,
    ) -> None:
        custom_id = "previous" if direction < 0 else "next"
        super().__init__(
            label=label,
            emoji="◀️" if direction < 0 else "▶️",
            style=discord.ButtonStyle.danger,
            disabled=disabled,
            custom_id=f"help:page:{custom_id}",
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, HelpPanelView):
            return
        await panel.show_page(interaction, panel.page_index + self.direction)


class HelpPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: HelpCog,
        *,
        owner_id: int,
        locale: str,
        page_index: int = 0,
        timeout: float = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.locale = locale
        self.page_index = self._clamp_page(page_index)
        self.message: discord.Message | None = None
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            self.cog.i18n.t(self.locale, "common.not_owner.help"),
            ephemeral=True,
        )
        return False

    async def show_page(self, interaction: discord.Interaction, page_index: int) -> None:
        await interaction.response.defer()
        self.page_index = self._clamp_page(page_index)
        self._rebuild()
        await interaction.edit_original_response(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(self._build_container())
        self.add_item(self._build_navigation_row())

    def _build_container(self) -> discord.ui.Container:
        page = HELP_PAGES[self.page_index]
        i18n = self.cog.i18n
        container = discord.ui.Container(accent_color=HELP_COLOR)
        container.add_item(
            discord.ui.TextDisplay(
                f"# 🎃 {i18n.t(self.locale, 'help.title')}\n"
                f"{i18n.t(self.locale, 'help.description')}"
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                HelpPageSelect(self),
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        container.add_item(
            discord.ui.TextDisplay(
                f"## {page.emoji} {i18n.t(self.locale, page.title_key)}\n"
                f"{i18n.t(self.locale, page.description_key)}"
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                f"-# {i18n.t(self.locale, 'help.footer', current=self.page_index + 1, total=len(HELP_PAGES))}"
            )
        )
        return container

    def _build_navigation_row(self) -> discord.ui.ActionRow:
        i18n = self.cog.i18n
        return discord.ui.ActionRow(
            HelpNavigationButton(
                -1,
                i18n.t(self.locale, "help.button.previous"),
                disabled=self.page_index <= 0,
            ),
            discord.ui.Button(
                label=i18n.t(
                    self.locale,
                    "help.button.page",
                    current=self.page_index + 1,
                    total=len(HELP_PAGES),
                ),
                style=discord.ButtonStyle.secondary,
                disabled=True,
                custom_id="help:page:indicator",
            ),
            HelpNavigationButton(
                1,
                i18n.t(self.locale, "help.button.next"),
                disabled=self.page_index >= len(HELP_PAGES) - 1,
            ),
        )

    @staticmethod
    def _clamp_page(page_index: int) -> int:
        return min(len(HELP_PAGES) - 1, max(0, int(page_index)))

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
        /,
    ) -> None:
        LOGGER.exception("Erro no painel de help item=%s", item, exc_info=error)
        message = self.cog.i18n.t(self.locale, "help.panel_failed")
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        for item in self.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass
