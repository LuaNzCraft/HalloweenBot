from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Literal

import discord

from utils.profile_embed import build_inventory_block


if TYPE_CHECKING:
    from cogs.profile import ProfileCog


LOGGER = logging.getLogger(__name__)
PROFILE_COLOR = discord.Color.from_rgb(245, 122, 39)
SWEET_COINS_EMOJI = "<:sweetcoins:1496365065773781033>"
TRICK_COINS_EMOJI = "<:trickcoins:1496364971398008932>"
ProfilePage = Literal["summary", "inventory"]


@dataclass(frozen=True)
class ProfilePanelData:
    display_name: str
    avatar_url: str
    xp_bank: int
    candies: int
    trick_coins: int
    dice_count: int
    board_position: int
    board_laps: int
    candies_total: int
    tricks_applied_total: int
    inventory_summary: str


def _resources_text(cog: ProfileCog, locale: str, data: ProfilePanelData) -> str:
    return (
        f"## {cog.i18n.t(locale, 'profile.resources')}\n"
        f"{SWEET_COINS_EMOJI} {cog.i18n.t(locale, 'profile.candies')} "
        f"**{data.candies}**  •  "
        f"{TRICK_COINS_EMOJI} {cog.i18n.t(locale, 'profile.trick_coins')} "
        f"**{data.trick_coins}**\n"
        f"✨ {cog.i18n.t(locale, 'profile.xp_bank')} **{data.xp_bank}**  •  "
        f"🎲 {cog.i18n.t(locale, 'profile.dice')} **{data.dice_count}**"
    )


def _journey_text(cog: ProfileCog, locale: str, data: ProfilePanelData) -> str:
    return (
        f"## {cog.i18n.t(locale, 'profile.journey')}\n"
        f"🏠 {cog.i18n.t(locale, 'profile.tile')} **{data.board_position}**  •  "
        f"🔁 {cog.i18n.t(locale, 'profile.laps')} **{data.board_laps}**\n"
        f"🍬 {cog.i18n.t(locale, 'profile.candies_total')} "
        f"**{data.candies_total}**  •  "
        f"😈 {cog.i18n.t(locale, 'profile.tricks_total')} "
        f"**{data.tricks_applied_total}**"
    )


def build_public_profile_container(
    cog: ProfileCog,
    locale: str,
    data: ProfilePanelData,
) -> discord.ui.Container:
    display_name = discord.utils.escape_markdown(data.display_name)
    container = discord.ui.Container(accent_color=PROFILE_COLOR)
    container.add_item(
        discord.ui.Section(
            discord.ui.TextDisplay(
                f"# {cog.i18n.t(locale, 'profile.public_title', name=display_name)}\n"
                f"{cog.i18n.t(locale, 'profile.public_description')}"
            ),
            accessory=discord.ui.Thumbnail(data.avatar_url),
        )
    )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    container.add_item(discord.ui.TextDisplay(_resources_text(cog, locale, data)))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(_journey_text(cog, locale, data)))
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(cog.i18n.t(locale, "profile.public_footer"))
    )
    return container


class ProfileTabButton(discord.ui.Button):
    def __init__(self, page: ProfilePage, label: str, *, active: bool) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.danger if active else discord.ButtonStyle.secondary,
            disabled=active,
            custom_id=f"profile:tab:{page}",
        )
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, ProfilePanelView):
            return
        await interaction.response.defer()
        panel.refresh(self.page)
        await interaction.edit_original_response(
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ProfileNavigationButton(discord.ui.Button):
    TARGETS = {
        "shop": ("ShopCog", "show_shop_panel", "common.not_loaded.shop"),
        "board": ("TabuleiroCog", "send_board_panel", "common.not_loaded.board"),
        "missions": ("MissaoCog", "send_mission_panel", "common.not_loaded.missions"),
        "baskets": ("BasketsCog", "show_basket_panel", "common.not_loaded.baskets"),
    }

    def __init__(self, target: str, label: str, emoji: str) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.danger,
            custom_id=f"profile:navigation:{target}",
        )
        self.target = target

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, ProfilePanelView):
            return
        cog_name, method_name, error_key = self.TARGETS[self.target]
        target_cog = interaction.client.get_cog(cog_name)
        if target_cog is None:
            await interaction.response.send_message(
                panel.cog.i18n.t(panel.locale, error_key),
                ephemeral=True,
            )
            return
        await getattr(target_cog, method_name)(interaction, panel.owner_id)


class ProfilePanelView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: ProfileCog,
        *,
        owner_id: int,
        locale: str,
        data: ProfilePanelData,
        timeout: float = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.locale = locale
        self.data = data
        self.page: ProfilePage = "summary"
        self.message: discord.Message | None = None
        self._rebuild()

    def refresh(self, page: ProfilePage) -> None:
        self.page = page
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(self._build_container())
        self.add_item(self._build_navigation_row())

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            self.cog.i18n.t(self.locale, "common.not_owner.profile"),
            ephemeral=True,
        )
        return False

    def _build_container(self) -> discord.ui.Container:
        data = self.data
        container = discord.ui.Container(accent_color=PROFILE_COLOR)
        display_name = discord.utils.escape_markdown(data.display_name)
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    f"# {self.cog.i18n.t(self.locale, 'profile.title', name=display_name)}\n"
                    f"{self.cog.i18n.t(self.locale, 'profile.description')}"
                ),
                accessory=discord.ui.Thumbnail(data.avatar_url),
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                ProfileTabButton(
                    "summary",
                    self.cog.i18n.t(self.locale, "profile.tab.summary"),
                    active=self.page == "summary",
                ),
                ProfileTabButton(
                    "inventory",
                    self.cog.i18n.t(self.locale, "profile.tab.inventory"),
                    active=self.page == "inventory",
                ),
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        if self.page == "inventory":
            container.add_item(discord.ui.TextDisplay(self._inventory_text()))
        else:
            container.add_item(discord.ui.TextDisplay(self._resources_text()))
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(self._journey_text()))
        return container

    def _resources_text(self) -> str:
        return _resources_text(self.cog, self.locale, self.data)

    def _journey_text(self) -> str:
        return _journey_text(self.cog, self.locale, self.data)

    def _inventory_text(self) -> str:
        return (
            f"## {self.cog.i18n.t(self.locale, 'profile.inventory')}\n"
            f"{build_inventory_block(self.data.inventory_summary, self.cog.i18n, self.locale)}"
        )

    def _build_navigation_row(self) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            ProfileNavigationButton(
                "shop",
                self.cog.i18n.t(self.locale, "common.shop"),
                "🛒",
            ),
            ProfileNavigationButton(
                "board",
                self.cog.i18n.t(self.locale, "common.board"),
                "🎲",
            ),
            ProfileNavigationButton(
                "missions",
                self.cog.i18n.t(self.locale, "common.missions"),
                "🕯️",
            ),
            ProfileNavigationButton(
                "baskets",
                self.cog.i18n.t(self.locale, "common.open_baskets"),
                "🧺",
            ),
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
        /,
    ) -> None:
        LOGGER.exception("Erro no painel de perfil item=%s", item, exc_info=error)
        message = self.cog.i18n.t(self.locale, "profile.panel_failed")
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
