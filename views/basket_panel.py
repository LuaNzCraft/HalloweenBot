from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

import discord


if TYPE_CHECKING:
    from cogs.baskets import BasketsCog


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BasketPanelData:
    inventory: dict[str, int]
    basket_count: int
    active_key: str
    accent_color: discord.Color
    icon_filename: str
    user_name: str | None = None
    result_text: str | None = None
    result_title: str | None = None


class BasketOpenButton(discord.ui.Button):
    def __init__(self, quantity: int, label: str, *, disabled: bool) -> None:
        super().__init__(
            label=label,
            emoji="🧺" if quantity == 1 else "🎃",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            custom_id=f"baskets:open:{quantity}",
        )
        self.quantity = quantity

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, BasketPanelView):
            return
        async with panel.open_lock:
            await panel.cog.open_baskets_from_panel(
                interaction,
                panel.owner_id,
                self.quantity,
                panel,
            )


class BasketNavigationButton(discord.ui.Button):
    TARGETS = {
        "profile": ("ProfileCog", "send_profile_panel", "common.not_loaded.profile"),
        "board": ("TabuleiroCog", "send_board_panel", "common.not_loaded.board"),
        "missions": ("MissaoCog", "send_mission_panel", "common.not_loaded.missions"),
    }

    def __init__(self, target: str, label: str, emoji: str) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"baskets:navigation:{target}",
        )
        self.target = target

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, BasketPanelView):
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


class BasketPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: BasketsCog,
        *,
        owner_id: int,
        locale: str,
        data: BasketPanelData,
        timeout: float = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.locale = locale
        self.data = data
        self.open_lock = asyncio.Lock()
        self.message: discord.Message | None = None
        self.add_item(self._build_container())

    def refresh(self, data: BasketPanelData) -> None:
        self.data = data
        self.clear_items()
        self.add_item(self._build_container())

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            self.cog.i18n.t(self.locale, "common.not_owner.baskets"),
            ephemeral=True,
        )
        return False

    def _build_container(self) -> discord.ui.Container:
        data = self.data
        container = discord.ui.Container(accent_color=data.accent_color)
        heading = f"# {self.cog.i18n.t(self.locale, 'baskets.title')}"
        if data.user_name:
            heading += f"\n-# {discord.utils.escape_markdown(data.user_name)}"
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    f"{heading}\n\n"
                    f"{self.cog.i18n.t(self.locale, 'baskets.available', count=data.basket_count)}"
                ),
                accessory=discord.ui.Thumbnail(f"attachment://{data.icon_filename}"),
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        container.add_item(discord.ui.TextDisplay(self._stock_text()))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self._active_basket_text()))
        container.add_item(
            discord.ui.ActionRow(
                BasketOpenButton(
                    1,
                    self.cog.i18n.t(self.locale, "baskets.open_one"),
                    disabled=data.basket_count < 1,
                ),
                BasketOpenButton(
                    5,
                    self.cog.i18n.t(self.locale, "baskets.open_five"),
                    disabled=data.basket_count < 5,
                ),
            )
        )
        if data.result_text:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
            result_title = data.result_title or self.cog.i18n.t(
                self.locale,
                f"baskets.mode.{data.active_key}.rewards",
            )
            container.add_item(
                discord.ui.TextDisplay(
                    f"## {result_title}\n{data.result_text}"
                )
            )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                f"-# {self.cog.i18n.t(self.locale, f'baskets.mode.{data.active_key}.footer')}"
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        container.add_item(
            discord.ui.ActionRow(
                BasketNavigationButton(
                    "profile",
                    self.cog.i18n.t(self.locale, "common.profile"),
                    "👤",
                ),
                BasketNavigationButton(
                    "board",
                    self.cog.i18n.t(self.locale, "common.board"),
                    "🎲",
                ),
                BasketNavigationButton(
                    "missions",
                    self.cog.i18n.t(self.locale, "common.missions"),
                    "🕯️",
                ),
            )
        )
        return container

    def _stock_text(self) -> str:
        inventory = self.data.inventory
        normal_name = self.cog.i18n.maybe_t(self.locale, "item.cesta_abobora.name") or "Cesta"
        epic_name = self.cog.i18n.maybe_t(self.locale, "item.cesta_doce_epica.name") or "Rara"
        cursed_name = (
            self.cog.i18n.maybe_t(self.locale, "item.cesta_doce_amaldicoada.name")
            or "Amaldiçoada"
        )
        return (
            f"## {self.cog.i18n.t(self.locale, 'baskets.stock_title')}\n"
            f"<:cesta_doce:1496691677283483708> **{normal_name}:** "
            f"{int(inventory.get('cesta_abobora', 0))}\n"
            f"<:cesta_epica:1496688438412574932> **{epic_name}:** "
            f"{int(inventory.get('cesta_doce_epica', 0))}\n"
            f"<:cesta_amaldicoada:1496688983672098946> **{cursed_name}:** "
            f"{int(inventory.get('cesta_doce_amaldicoada', 0))}"
        )

    def _active_basket_text(self) -> str:
        active_key = self.data.active_key
        emoji = {
            "normal": "<:cesta_doce:1496691677283483708>",
            "epic": "<:cesta_epica:1496688438412574932>",
            "cursed": "<:cesta_amaldicoada:1496688983672098946>",
            "empty": "🕯️",
        }.get(active_key, "🎃")
        return (
            f"## {self.cog.i18n.t(self.locale, 'baskets.next_title')}\n"
            f"{emoji} **{self.cog.i18n.t(self.locale, f'baskets.mode.{active_key}.name')}**\n"
            f"{self.cog.i18n.t(self.locale, f'baskets.mode.{active_key}.description')}"
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
        /,
    ) -> None:
        LOGGER.exception("Erro no painel de cestas item=%s", item, exc_info=error)
        message = self.cog.i18n.t(self.locale, "baskets.open_failed")
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
