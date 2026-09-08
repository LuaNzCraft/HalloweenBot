from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from services.shop_service import CURRENCY_LABELS, ShopEntry, ShopService
from views.shop_panel import ShopCurrency, ShopPanelView


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
        self._role_purchase_locks: dict[int, asyncio.Lock] = {}

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

        active_currency: ShopCurrency = "candies"
        notice: str | None = None
        if buy:
            normalized_buy = buy.strip().lower()
            entry = self.shop_service.get(normalized_buy)
            if entry is not None and entry.currency == "trick_coins":
                active_currency = "trick_coins"
            ok, message = await self.purchase_entry(interaction, normalized_buy, max(1, quantidade))
            notice = f"{'✅' if ok else '❌'} {message}"

        locale = await self.i18n.get_user_locale(interaction.user.id)
        panel = await ShopPanelView.create(
            self,
            owner_id=interaction.user.id,
            locale=locale,
            guild=interaction.guild,
            currency=active_currency,
            notice=notice,
        )
        sent = await interaction.followup.send(
            view=panel,
            ephemeral=True,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        panel.message = sent

    async def purchase_entry(
        self,
        interaction: discord.Interaction,
        entry_id: str,
        quantity: int,
    ) -> tuple[bool, str]:
        entry = self.shop_service.get(entry_id)
        if entry is None or not entry.enabled:
            result = await self.shop_service.purchase(interaction.user.id, entry_id, quantity=max(1, quantity))
            return result.ok, result.message

        if entry.grant_type != "role":
            result = await self.shop_service.purchase(interaction.user.id, entry_id, quantity=max(1, quantity))
            return result.ok, result.message

        lock = self._role_purchase_locks.setdefault(interaction.user.id, asyncio.Lock())
        async with lock:
            return await self._purchase_role(interaction, entry)

    async def _purchase_role(
        self,
        interaction: discord.Interaction,
        entry: ShopEntry,
    ) -> tuple[bool, str]:
        locale = await self.i18n.get_user_locale(interaction.user.id)
        if interaction.guild is None:
            return False, self.i18n.t(locale, "shop.role.guild_required")

        role = await self._shop_role(interaction.guild)
        if role is None:
            return False, self.i18n.t(locale, "shop.role.not_configured")
        if role in getattr(interaction.user, "roles", []):
            return False, self.i18n.t(locale, "shop.role.already_owned")

        result = await self.shop_service.purchase(interaction.user.id, entry.id, quantity=1)
        if not result.ok:
            return False, result.message

        try:
            await interaction.user.add_roles(role, reason="Compra do cargo da loja")
        except (discord.Forbidden, discord.HTTPException):
            await self.shop_service.users_repo.add_currency(
                interaction.user.id,
                entry.currency,
                result.spent_amount,
            )
            return False, self.i18n.t(locale, "shop.role.grant_failed_refunded")

        return True, result.message

    async def show_shop_panel(
        self,
        interaction: discord.Interaction,
        owner_id: int,
        currency: ShopCurrency = "candies",
    ) -> None:
        await interaction.response.defer()
        locale = await self.i18n.get_user_locale(owner_id)
        panel = await ShopPanelView.create(
            self,
            owner_id=owner_id,
            locale=locale,
            guild=interaction.guild,
            currency=currency,
        )
        message = await interaction.edit_original_response(
            content=None,
            embed=None,
            attachments=[],
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        panel.message = message

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
                return self.i18n.t(locale, "shop.role.configured_description", role=role.mention)
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

    def _currency_emoji(self, currency: str) -> str:
        if currency == "trick_coins":
            return TRICK_COINS_EMOJI
        return SWEET_COINS_EMOJI

    def _entry_emoji(self, entry: ShopEntry) -> str:
        if entry.grant_id == "cesta_abobora":
            return BASKET_NORMAL_EMOJI
        if entry.grant_id == "cesta_doce_epica":
            return BASKET_EPIC_EMOJI
        if entry.grant_id == "cesta_doce_amaldicoada":
            return BASKET_CURSED_EMOJI
        return entry.emoji

    def _entries_for_currency(self, currency: ShopCurrency) -> list[ShopEntry]:
        return sorted(
            (
                entry
                for entry in self.shop_service.enabled_entries().values()
                if entry.currency == currency
            ),
            key=lambda entry: (entry.price, entry.id),
        )

    def _shop_balance_text(self, profile: dict, locale: str) -> str:
        candies_label = self.i18n.t(locale, "currency.candies")
        trick_label = self.i18n.t(locale, "currency.trick_coins")
        return (
            f"{SWEET_COINS_EMOJI} **{profile['candies']}** {candies_label}   "
            f"{TRICK_COINS_EMOJI} **{profile['trick_coins']}** {trick_label}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShopCog(bot))
