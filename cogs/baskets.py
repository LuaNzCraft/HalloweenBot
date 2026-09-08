from __future__ import annotations

import asyncio
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from services.inventory_service import InventoryService
from services.profile_service import ProfileService


BASKET_NORMAL_COLOR = discord.Color.from_rgb(245, 122, 39)
BASKET_EPIC_COLOR = discord.Color.from_rgb(214, 158, 46)
BASKET_CURSED_COLOR = discord.Color.from_rgb(46, 125, 50)
BASKET_EMPTY_COLOR = discord.Color.from_rgb(100, 100, 100)

ROOT_DIR = Path(__file__).resolve().parent.parent
NORMAL_BASKET_ICON_PATH = ROOT_DIR / "generated_assets" / "cesta_doce_thumb.png"
EPIC_BASKET_ICON_PATH = ROOT_DIR / "generated_assets" / "cesta_epica_thumb.png"
CURSED_BASKET_ICON_PATH = ROOT_DIR / "generated_assets" / "cesta_amaldicoada_thumb.png"

BASKET_NORMAL_EMOJI = "<:cesta_doce:1496691677283483708>"
BASKET_EPIC_EMOJI = "<:cesta_epica:1496688438412574932>"
BASKET_CURSED_EMOJI = "<:cesta_amaldicoada:1496688983672098946>"
BASKET_EMOJI_BY_KEY = {
    "normal": BASKET_NORMAL_EMOJI,
    "epic": BASKET_EPIC_EMOJI,
    "cursed": BASKET_CURSED_EMOJI,
}


class BasketPanelView(discord.ui.View):
    def __init__(self, cog: "BasketsCog", owner_id: int, locale: str, basket_count: int) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.owner_id = owner_id
        self.locale = locale
        self._lock = asyncio.Lock()

        self.open_one.label = cog.i18n.t(locale, "baskets.open_one")
        self.open_one.disabled = basket_count < 1

        self.open_five.label = cog.i18n.t(locale, "baskets.open_five")
        self.open_five.disabled = basket_count < 5

        self.profile.label = cog.i18n.t(locale, "common.profile")
        self.clear_items()
        self.add_item(self.open_one)
        self.add_item(self.profile)
        self.add_item(self.open_five)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            self.cog.i18n.t(self.locale, "common.not_owner.baskets"),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Abrir 1", style=discord.ButtonStyle.primary, emoji="🧺")
    async def open_one(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            await self.cog.open_baskets_from_panel(interaction, self.owner_id, 1, self)

    @discord.ui.button(label="Abrir 5", style=discord.ButtonStyle.primary, emoji="🎃")
    async def open_five(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            await self.cog.open_baskets_from_panel(interaction, self.owner_id, 5, self)

    @discord.ui.button(label="Perfil", style=discord.ButtonStyle.success)
    async def profile(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        profile_cog = interaction.client.get_cog("ProfileCog")
        if profile_cog is None:
            await interaction.response.send_message(
                self.cog.i18n.t(self.locale, "common.not_loaded.profile"),
                ephemeral=True,
            )
            return
        await profile_cog.show_profile_panel(interaction, self.owner_id)

class BasketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        bootstrap_inventory = InventoryService()
        self.inventory_service = InventoryService(
            users_repo=bootstrap_inventory.users_repo,
            item_catalog=bootstrap_inventory.item_catalog,
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
        basket_count, embed, file = await self._build_panel(interaction.user.id, locale, user=interaction.user)
        await interaction.followup.send(
            embed=embed,
            file=file,
            view=BasketPanelView(self, interaction.user.id, locale, basket_count),
            ephemeral=True,
        )

    async def show_basket_panel(self, interaction: discord.Interaction, owner_id: int) -> None:
        await interaction.response.defer()
        locale = await self.i18n.get_user_locale(owner_id)
        member = interaction.guild.get_member(owner_id) if interaction.guild else None
        basket_count, embed, file = await self._build_panel(owner_id, locale, user=member)
        await interaction.edit_original_response(
            content=None,
            embed=embed,
            attachments=[file],
            view=BasketPanelView(self, owner_id, locale, basket_count),
        )

    async def build_basket_panel_embed(self, user_id: int, locale: str | None = None) -> discord.Embed:
        locale = locale or await self.i18n.get_user_locale(user_id)
        return await self._build_embed_only(user_id, locale)

    async def open_baskets_from_panel(
        self,
        interaction: discord.Interaction,
        user_id: int,
        quantity: int,
        view: BasketPanelView,
    ) -> None:
        await interaction.response.defer()
        locale = await self.i18n.get_user_locale(user_id)
        member = interaction.guild.get_member(user_id) if interaction.guild else None
        result = await self.inventory_service.open_pumpkin_baskets(
            user_id,
            quantity,
            guild_id=interaction.guild.id if interaction.guild else None,
        )
        result_text = result.message
        basket_count, embed, file = await self._build_panel(
            user_id,
            locale,
            user=member,
            result_text=result_text,
            title=self.i18n.t(locale, "baskets.opened_title", count=result.opened) if result.ok else None,
        )
        await interaction.edit_original_response(
            embed=embed,
            attachments=[file],
            view=_refreshed_view(self, view, basket_count),
        )

    async def _build_panel(
        self,
        user_id: int,
        locale: str,
        user: discord.User | discord.Member | None = None,
        result_text: str | None = None,
        title: str | None = None,
    ) -> tuple[int, discord.Embed, discord.File]:
        profile = await self.profile_service.get_profile(user_id)
        inventory = profile.get("inventory", {})
        basket_count = self._basket_total(inventory)
        candies = int(profile.get("candies", 0))
        embed, file = self._build_embed(inventory, candies, result_text, title=title, locale=locale, user=user)
        return basket_count, embed, file

    async def _build_embed_only(
        self,
        user_id: int,
        locale: str,
        user: discord.User | discord.Member | None = None,
    ) -> discord.Embed:
        profile = await self.profile_service.get_profile(user_id)
        inventory = profile.get("inventory", {})
        basket_count = self._basket_total(inventory)
        candies = int(profile.get("candies", 0))
        return self._compose_embed(
            inventory,
            basket_count,
            candies,
            locale=locale,
            user=user,
        )

    def _build_embed(
        self,
        inventory: dict,
        candies: int,
        result_text: str | None = None,
        *,
        title: str | None = None,
        locale: str = "pt-BR",
        user: discord.User | discord.Member | None = None,
    ) -> tuple[discord.Embed, discord.File]:
        basket_count = self._basket_total(inventory)
        color, icon_path, active_key = self._resolve_embed_visuals(inventory, basket_count)
        embed = self._compose_embed(
            inventory,
            basket_count,
            candies,
            locale=locale,
            user=user,
            title=title,
            result_text=result_text,
            color=color,
            active_key=active_key,
        )
        file = discord.File(str(icon_path), filename=icon_path.name)
        embed.set_thumbnail(url=f"attachment://{icon_path.name}")
        return embed, file

    def _compose_embed(
        self,
        inventory: dict,
        basket_count: int,
        candies: int,
        *,
        locale: str,
        user: discord.User | discord.Member | None = None,
        title: str | None = None,
        result_text: str | None = None,
        color: discord.Color | None = None,
        active_key: str | None = None,
    ) -> discord.Embed:
        resolved_color, _, resolved_active_key = self._resolve_embed_visuals(
            inventory,
            basket_count,
            color=color,
            active_key=active_key,
        )
        embed = discord.Embed(
            title=title or self.i18n.t(locale, "baskets.title"),
            description=(
                f"{self.i18n.t(locale, 'baskets.available', count=basket_count)}\n"
                f"{self.i18n.t(locale, f'baskets.mode.{resolved_active_key}.description')}"
            ),
            color=resolved_color,
        )
        if user:
            embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        normal_name = self.i18n.maybe_t(locale, "item.cesta_abobora.name") or "Cesta"
        epic_name = self.i18n.maybe_t(locale, "item.cesta_doce_epica.name") or "Rara"
        cursed_name = self.i18n.maybe_t(locale, "item.cesta_doce_amaldicoada.name") or "Amaldicoada"
        cursed_panel_name = "Amaldicoada" if locale == "pt-BR" else cursed_name
        embed.add_field(name=self.i18n.t(locale, "baskets.field.candies"), value=str(candies), inline=True)
        embed.add_field(name=self.i18n.t(locale, "baskets.field.total"), value=str(basket_count), inline=True)
        embed.add_field(
            name=self.i18n.t(locale, "baskets.field.active"),
            value=(
                f"{BASKET_EMOJI_BY_KEY.get(resolved_active_key, '')} "
                f"{self.i18n.t(locale, f'baskets.mode.{resolved_active_key}.name')}"
            ).strip(),
            inline=True,
        )
        embed.add_field(
            name=f"{BASKET_NORMAL_EMOJI} {normal_name}",
            value=str(int(inventory.get("cesta_abobora", 0))),
            inline=True,
        )
        embed.add_field(
            name=f"{BASKET_EPIC_EMOJI} {epic_name}",
            value=str(int(inventory.get("cesta_doce_epica", 0))),
            inline=True,
        )
        embed.add_field(
            name=f"{BASKET_CURSED_EMOJI} {cursed_panel_name}",
            value=str(int(inventory.get("cesta_doce_amaldicoada", 0))),
            inline=True,
        )
        if basket_count <= 0:
            embed.add_field(name="\u200b", value=self.i18n.t(locale, "baskets.empty"), inline=False)
        if result_text:
            formatted_result = self._format_reward_block(result_text)
            embed.add_field(
                name=self.i18n.t(locale, f"baskets.mode.{resolved_active_key}.rewards"),
                value=formatted_result[:1024],
                inline=False,
            )
        embed.set_footer(text=self.i18n.t(locale, f"baskets.mode.{resolved_active_key}.footer"))
        return embed

    def _resolve_embed_visuals(
        self,
        inventory: dict,
        basket_count: int,
        *,
        color: discord.Color | None = None,
        active_key: str | None = None,
    ) -> tuple[discord.Color, Path, str]:
        if basket_count <= 0:
            return color or BASKET_EMPTY_COLOR, NORMAL_BASKET_ICON_PATH, active_key or "empty"
        resolved_color, icon_path, resolved_active_key = self._active_basket_visuals(inventory)
        return color or resolved_color, icon_path, active_key or resolved_active_key

    def _active_basket_visuals(self, inventory: dict) -> tuple[discord.Color, Path, str]:
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


def _refreshed_view(cog: BasketsCog, old_view: BasketPanelView, basket_count: int) -> BasketPanelView:
    return BasketPanelView(cog, old_view.owner_id, old_view.locale, basket_count)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BasketsCog(bot))
