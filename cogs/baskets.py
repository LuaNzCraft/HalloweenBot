from __future__ import annotations

from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from repositories.guild_metrics_repo import GuildMetricsRepo
from repositories.user_guild_metrics_repo import UserGuildMetricsRepo
from services.inventory_service import InventoryService
from services.profile_service import ProfileService
from views.basket_panel import BasketPanelData, BasketPanelView


BASKET_NORMAL_COLOR = discord.Color.from_rgb(245, 122, 39)
BASKET_EPIC_COLOR = discord.Color.from_rgb(214, 158, 46)
BASKET_CURSED_COLOR = discord.Color.from_rgb(46, 125, 50)
BASKET_EMPTY_COLOR = discord.Color.from_rgb(100, 100, 100)

ROOT_DIR = Path(__file__).resolve().parent.parent
NORMAL_BASKET_ICON_PATH = ROOT_DIR / "generated_assets" / "cesta_doce_thumb.png"
EPIC_BASKET_ICON_PATH = ROOT_DIR / "generated_assets" / "cesta_epica_thumb.png"
CURSED_BASKET_ICON_PATH = ROOT_DIR / "generated_assets" / "cesta_amaldicoada_thumb.png"


class BasketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        bootstrap_inventory = InventoryService()
        self.inventory_service = InventoryService(
            users_repo=bootstrap_inventory.users_repo,
            item_catalog=bootstrap_inventory.item_catalog,
            guild_metrics_repo=GuildMetricsRepo(),
            user_guild_metrics_repo=UserGuildMetricsRepo(),
            i18n_service=bootstrap_inventory.i18n,
        )
        self.i18n = self.inventory_service.i18n
        self.profile_service = ProfileService(
            users_repo=self.inventory_service.users_repo,
            item_catalog=self.inventory_service.item_catalog,
            i18n_service=self.i18n,
        )

    @app_commands.command(name="cestas", description="Abre o painel das suas Cestas de Doce.")
    async def cestas(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        locale = await self.i18n.get_user_locale(interaction.user.id)
        panel, file = await self._build_panel(interaction.user.id, locale)
        sent = await interaction.followup.send(
            file=file,
            view=panel,
            ephemeral=True,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        panel.message = sent

    async def show_basket_panel(self, interaction: discord.Interaction, owner_id: int) -> None:
        await interaction.response.defer()
        locale = await self.i18n.get_user_locale(owner_id)
        panel, file = await self._build_panel(owner_id, locale)
        message = await interaction.edit_original_response(
            content=None,
            embed=None,
            attachments=[file],
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        panel.message = message

    async def open_baskets_from_panel(
        self,
        interaction: discord.Interaction,
        user_id: int,
        quantity: int,
        old_panel: BasketPanelView,
    ) -> None:
        await interaction.response.defer()
        locale = await self.i18n.get_user_locale(user_id)
        result = await self.inventory_service.open_pumpkin_baskets(
            user_id,
            quantity,
            guild_id=interaction.guild.id if interaction.guild else None,
        )
        result_title = (
            self.i18n.t(locale, "baskets.opened_title", count=result.opened)
            if result.ok
            else None
        )
        panel, file = await self._build_panel(
            user_id,
            locale,
            result_text=self._format_reward_block(result.message),
            result_title=result_title,
            existing_panel=old_panel,
        )
        message = await interaction.edit_original_response(
            attachments=[file],
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        panel.message = message

    async def _build_panel(
        self,
        user_id: int,
        locale: str,
        *,
        result_text: str | None = None,
        result_title: str | None = None,
        existing_panel: BasketPanelView | None = None,
    ) -> tuple[BasketPanelView, discord.File]:
        profile = await self.profile_service.get_profile(user_id)
        inventory = profile.get("inventory", {})
        basket_count = self._basket_total(inventory)
        color, icon_path, active_key = self._resolve_panel_visuals(inventory, basket_count)
        data = BasketPanelData(
            inventory={key: int(value) for key, value in inventory.items()},
            basket_count=basket_count,
            active_key=active_key,
            accent_color=color,
            icon_filename=icon_path.name,
            result_text=result_text,
            result_title=result_title,
        )
        if existing_panel is not None:
            existing_panel.refresh(data)
            panel = existing_panel
        else:
            panel = BasketPanelView(
                self,
                owner_id=user_id,
                locale=locale,
                data=data,
            )
        return (
            panel,
            discord.File(str(icon_path), filename=icon_path.name),
        )

    def _resolve_panel_visuals(
        self,
        inventory: dict,
        basket_count: int,
    ) -> tuple[discord.Color, Path, str]:
        if basket_count <= 0:
            return BASKET_EMPTY_COLOR, NORMAL_BASKET_ICON_PATH, "empty"
        if int(inventory.get("cesta_doce_amaldicoada", 0)) > 0:
            return BASKET_CURSED_COLOR, CURSED_BASKET_ICON_PATH, "cursed"
        if int(inventory.get("cesta_doce_epica", 0)) > 0:
            return BASKET_EPIC_COLOR, EPIC_BASKET_ICON_PATH, "epic"
        return BASKET_NORMAL_COLOR, NORMAL_BASKET_ICON_PATH, "normal"

    def _format_reward_block(self, result_text: str) -> str:
        lines = []
        for raw_line in result_text.strip().splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                lines.append("")
                continue
            lines.append(line.replace("**", ""))
        return "```yaml\n" + "\n".join(lines) + "\n```"

    def _basket_total(self, inventory: dict) -> int:
        return (
            int(inventory.get("cesta_abobora", 0))
            + int(inventory.get("cesta_doce_epica", 0))
            + int(inventory.get("cesta_doce_amaldicoada", 0))
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BasketsCog(bot))
