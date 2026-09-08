from __future__ import annotations

import asyncio
import math

import discord
from discord import app_commands
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from services.shop_service import CURRENCY_LABELS, ShopEntry, ShopService


SHOP_COLOR = discord.Color.from_rgb(245, 122, 39)
SHOP_NOTICE_SECONDS = 3
SHOP_PAGE_SIZE = 4
SWEET_COINS_EMOJI = "<:sweetcoins:1496365065773781033>"
TRICK_COINS_EMOJI = "<:trickcoins:1496364971398008932>"
BASKET_NORMAL_EMOJI = "<:cesta_doce:1496691677283483708>"
BASKET_EPIC_EMOJI = "<:cesta_epica:1496688438412574932>"
BASKET_CURSED_EMOJI = "<:cesta_amaldicoada:1496688983672098946>"
SHOP_ENGLISH_LABELS = {
    "dado_extra": "Extra Die",
    "cesta_abobora": "Candy Basket",
    "cesta_doce_epica_shop": "Rare Basket",
    "cesta_doce_amaldicoada_shop": "Cursed Basket",
    "ticket_carrossel": "Carousel Ticket",
    "ticket_carrossel_trick": "Trick Carousel Ticket",
    "cesta_doce_amaldicoada_trick": "Trick Cursed Basket",
    "mute": "Mute",
    "switch": "Switch",
    "interferencia": "Interference",
    "cargo_loja": "Shop Role",
}


class ShopCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.admin_repo = AdminRepo()
        self.shop_service = ShopService()
        self.i18n = self.shop_service.i18n

    async def shop_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        locale = await self.i18n.get_user_locale(interaction.user.id)
        current_lower = current.lower()
        choices: list[app_commands.Choice[str]] = []
        for entry_id, entry in self.shop_service.enabled_entries().items():
            entry_name = await self._entry_name(entry, locale, interaction.guild)
            english_label = SHOP_ENGLISH_LABELS.get(
                entry_id,
                entry_id.replace("_", " ").title(),
            )
            currency_label = self._currency_label(entry.currency, locale)
            label = f"{entry.emoji} {entry_name} ({english_label}) - {entry.price} {currency_label}"
            if (
                current_lower
                and current_lower not in entry_id.lower()
                and current_lower not in entry_name.lower()
                and current_lower not in english_label.lower()
            ):
                continue
            choices.append(app_commands.Choice(name=label[:100], value=entry_id))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(name="loja", description="Mostra a loja Halloween ou compra um item.")
    @app_commands.describe(
        buy="Item da loja para comprar. Deixe vazio para apenas abrir a loja.",
        quantidade="Quantidade desejada. Se faltar saldo, compra o maximo possivel.",
    )
    @app_commands.autocomplete(buy=shop_autocomplete)
    async def loja(self, interaction: discord.Interaction, buy: str | None = None, quantidade: int = 1) -> None:
        await interaction.response.defer(ephemeral=True)

        purchase_message = None
        if buy:
            normalized_buy = buy.strip().lower()

            if normalized_buy == "cargo_loja":
                role = await self._shop_role(interaction.guild)
                if interaction.guild is None or role is None:
                    msg = (
                        "Esse cargo so pode ser comprado dentro de um servidor."
                        if interaction.guild is None
                        else "O cargo da loja ainda nao foi configurado no /setup."
                    )
                    await interaction.followup.send(msg, ephemeral=True)
                    return
                if role in interaction.user.roles:
                    await interaction.followup.send("Voce ja possui o cargo da loja.", ephemeral=True)
                    return

            result = await self.shop_service.purchase(
                interaction.user.id,
                normalized_buy,
                quantity=max(1, quantidade),
            )

            if result.ok and result.entry_id == "cargo_loja":
                # Passa o role já resolvido para evitar segunda busca de config
                role = await self._shop_role(interaction.guild)
                granted = await self._grant_shop_role(interaction, result, role)
                if not granted:
                    return

            purchase_message = result.message

        profile = await self.shop_service.users_repo.get_user(interaction.user.id)
        locale = await self.i18n.get_user_locale(interaction.user.id)
        embed = await self._build_shop_embed(profile, locale, interaction.guild, page_index=0)
        await interaction.followup.send(
            embed=embed,
            view=ShopPanelView(self, interaction.user.id, locale, page_index=0),
            ephemeral=True,
        )
        if purchase_message:
            await self._send_shop_notice(interaction, purchase_message)

    async def _grant_shop_role(
        self,
        interaction: discord.Interaction,
        result,
        role: discord.Role | None,
    ) -> bool:
        if interaction.guild is None:
            await interaction.followup.send("Esse cargo so pode ser comprado dentro de um servidor.", ephemeral=True)
            return False

        if role is None:
            await self.shop_service.users_repo.add_currency(interaction.user.id, "candies", result.spent_amount)
            await interaction.followup.send("O cargo da loja nao esta configurado. O valor foi devolvido.", ephemeral=True)
            return False

        try:
            await interaction.user.add_roles(role, reason="Compra do cargo da loja")
        except (discord.Forbidden, discord.HTTPException):
            await self.shop_service.users_repo.add_currency(interaction.user.id, "candies", result.spent_amount)
            await interaction.followup.send("Nao consegui entregar o cargo da loja. O valor foi devolvido.", ephemeral=True)
            return False

        return True

    async def show_shop_panel(self, interaction: discord.Interaction, owner_id: int, page_index: int = 0) -> None:
        await interaction.response.defer()
        profile = await self.shop_service.users_repo.get_user(owner_id)
        locale = await self.i18n.get_user_locale(owner_id)
        normalized_page = self._normalize_page_index(page_index)
        embed = await self._build_shop_embed(profile, locale, interaction.guild, page_index=normalized_page)
        await interaction.edit_original_response(
            content=None,
            embed=embed,
            attachments=[],
            view=ShopPanelView(self, owner_id, locale, page_index=normalized_page),
        )

    async def _send_shop_notice(self, interaction: discord.Interaction, message: str) -> None:
        locale = await self.i18n.get_user_locale(interaction.user.id)
        embed = discord.Embed(
            title=self.i18n.t(locale, "shop.notice_title"),
            description=message,
            color=SHOP_COLOR,
        )
        try:
            notice = await interaction.followup.send(embed=embed, ephemeral=True, wait=True)
            await asyncio.sleep(SHOP_NOTICE_SECONDS)
            await notice.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    async def _build_shop_embed(
        self,
        profile: dict,
        locale: str,
        guild: discord.Guild | None,
        *,
        page_index: int,
    ) -> discord.Embed:
        normalized_page = self._normalize_page_index(page_index)
        entries = self._entries_for_page(normalized_page)
        balance_text = self._shop_balance_text(profile, locale)
        embed = discord.Embed(
            description=balance_text,
            color=SHOP_COLOR,
        )
        embed.set_author(name=self.i18n.t(locale, "shop.title"), icon_url=self._shop_icon_url(guild))

        for entry in entries:
            entry_name = await self._entry_name(entry, locale, guild)
            embed.add_field(
                name=self._format_entry_title(entry, entry_name, locale),
                value=await self._entry_description(entry, locale, guild),
                inline=False,
            )
        embed.set_footer(text=f"{normalized_page + 1}/{self._total_pages()}")
        return embed

    def _format_entry_title(self, entry: ShopEntry, entry_name: str, locale: str = "pt-BR") -> str:
        currency_label = self._currency_label(entry.currency, locale)
        emoji = self._entry_emoji(entry)
        return f"{emoji} {entry_name} - {entry.price} {currency_label}"

    async def _entry_name(self, entry: ShopEntry, locale: str, guild: discord.Guild | None) -> str:
        if entry.id == "cargo_loja":
            role = await self._shop_role(guild)
            if role is not None:
                return role.name
        return self.i18n.maybe_t(locale, f"shop.{entry.id}.name") or entry.name

    async def _entry_description(self, entry: ShopEntry, locale: str, guild: discord.Guild | None) -> str:
        if entry.id == "cargo_loja":
            role = await self._shop_role(guild)
            if role is not None:
                return f"Desbloqueia o cargo especial de Halloween 2026: {role.mention}."
        return self.i18n.maybe_t(locale, f"shop.{entry.id}.description") or entry.description

    async def _shop_role(self, guild: discord.Guild | None) -> discord.Role | None:
        if guild is None:
            return None
        config = await self.admin_repo.get_config(guild.id)
        role_id = config.get("shop_role_id")
        if not role_id:
            return None
        return guild.get_role(int(role_id))

    def _currency_label(self, currency: str, locale: str) -> str:
        return self.i18n.maybe_t(locale, f"currency.{currency}") or CURRENCY_LABELS[currency]

    def _entry_emoji(self, entry: ShopEntry) -> str:
        if entry.grant_id == "cesta_abobora":
            return BASKET_NORMAL_EMOJI
        if entry.grant_id == "cesta_doce_epica":
            return BASKET_EPIC_EMOJI
        if entry.grant_id == "cesta_doce_amaldicoada":
            return BASKET_CURSED_EMOJI
        return entry.emoji

    def _enabled_entries_list(self) -> list[ShopEntry]:
        currency_priority = {"candies": 0, "trick_coins": 1}
        return sorted(
            self.shop_service.enabled_entries().values(),
            key=lambda entry: (
                currency_priority.get(entry.currency, 99),
                entry.price,
                entry.id,
            ),
        )

    def _shop_balance_text(self, profile: dict, locale: str) -> str:
        candies_label = self.i18n.t(locale, "currency.candies")
        trick_label = self.i18n.t(locale, "currency.trick_coins")
        return (
            f"{SWEET_COINS_EMOJI} **{profile['candies']}** {candies_label}   "
            f"{TRICK_COINS_EMOJI} **{profile['trick_coins']}** {trick_label}"
        )

    def _shop_icon_url(self, guild: discord.Guild | None) -> str | None:
        if guild is not None and guild.icon is not None:
            return guild.icon.url
        if self.bot.user is not None:
            return self.bot.user.display_avatar.url
        return None

    def _total_pages(self) -> int:
        total_entries = len(self._enabled_entries_list())
        return max(1, math.ceil(total_entries / SHOP_PAGE_SIZE))

    def _normalize_page_index(self, page_index: int) -> int:
        if page_index < 0:
            return 0
        last_page = self._total_pages() - 1
        if page_index > last_page:
            return last_page
        return page_index

    def _entries_for_page(self, page_index: int) -> list[ShopEntry]:
        entries = self._enabled_entries_list()
        start = page_index * SHOP_PAGE_SIZE
        end = start + SHOP_PAGE_SIZE
        return entries[start:end]


class ShopPanelView(discord.ui.View):
    def __init__(self, cog: ShopCog, owner_id: int, locale: str, page_index: int) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.owner_id = owner_id
        self.locale = locale
        self.page_index = cog._normalize_page_index(page_index)
        total_pages = cog._total_pages()

        self.page_button.label = f"{self.page_index + 1}/{total_pages}"
        self.first_button.disabled = self.page_index <= 0
        self.previous_button.disabled = self.page_index <= 0
        self.next_button.disabled = self.page_index >= total_pages - 1
        self.last_button.disabled = self.page_index >= total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(self.cog.i18n.t(self.locale, "common.not_owner.shop"), ephemeral=True)
        return False

    async def _show_page(self, interaction: discord.Interaction, page_index: int) -> None:
        normalized_page = self.cog._normalize_page_index(page_index)
        profile = await self.cog.shop_service.users_repo.get_user(self.owner_id)
        embed = await self.cog._build_shop_embed(profile, self.locale, interaction.guild, page_index=normalized_page)
        await interaction.response.edit_message(
            embed=embed,
            view=ShopPanelView(self.cog, self.owner_id, self.locale, normalized_page),
        )

    @discord.ui.button(label="<<", style=discord.ButtonStyle.secondary)
    async def first_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show_page(interaction, 0)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show_page(interaction, self.page_index - 1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show_page(interaction, self.page_index + 1)

    @discord.ui.button(label=">>", style=discord.ButtonStyle.secondary)
    async def last_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show_page(interaction, self.cog._total_pages() - 1)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShopCog(bot))
