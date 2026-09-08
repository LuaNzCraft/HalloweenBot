from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

import discord


if TYPE_CHECKING:
    from cogs.shop import ShopCog
    from services.shop_service import ShopEntry


LOGGER = logging.getLogger(__name__)
SHOP_COLOR = discord.Color.from_rgb(245, 122, 39)
ShopCurrency = Literal["candies", "trick_coins"]


class ShopCurrencyButton(discord.ui.Button):
    def __init__(self, currency: ShopCurrency, label: str, emoji: str, *, active: bool) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            disabled=active,
            custom_id=f"shop:currency:{currency}",
        )
        self.currency = currency

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, ShopPanelView):
            return
        await interaction.response.defer()
        await panel.refresh(currency=self.currency)
        await interaction.edit_original_response(
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ShopBuyButton(discord.ui.Button):
    def __init__(self, entry_id: str, quantity: int) -> None:
        super().__init__(
            label=f"{quantity}x",
            emoji="🛒",
            style=discord.ButtonStyle.success,
            custom_id=f"shop:buy:{entry_id}:{quantity}",
        )
        self.entry_id = entry_id
        self.quantity = quantity

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, ShopPanelView):
            return
        await interaction.response.defer()
        try:
            ok, message = await panel.cog.purchase_entry(
                interaction,
                self.entry_id,
                self.quantity,
            )
            notice = f"{'✅' if ok else '❌'} {message}"
        except Exception:
            LOGGER.exception(
                "Falha na compra rapida da loja user=%s item=%s quantity=%s",
                interaction.user.id,
                self.entry_id,
                self.quantity,
            )
            notice = f"❌ {panel.cog.i18n.t(panel.locale, 'shop.purchase_failed')}"
        await panel.refresh(notice=notice)
        await interaction.edit_original_response(
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ShopPageButton(discord.ui.Button):
    def __init__(self, *, label: str, target_page: int, disabled: bool = False) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            custom_id=f"shop:page:{target_page}",
        )
        self.target_page = target_page

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, ShopPanelView):
            return
        await interaction.response.defer()
        await panel.refresh(page_index=self.target_page)
        await interaction.edit_original_response(
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ShopPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: ShopCog,
        *,
        owner_id: int,
        locale: str,
        guild: discord.Guild | None,
        currency: ShopCurrency = "candies",
        timeout: float = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.locale = locale
        self.guild = guild
        self.currency = currency
        self.page_index = 0
        self.notice: str | None = None
        self.message: discord.Message | None = None

    @classmethod
    async def create(
        cls,
        cog: ShopCog,
        *,
        owner_id: int,
        locale: str,
        guild: discord.Guild | None,
        currency: ShopCurrency = "candies",
        notice: str | None = None,
    ) -> ShopPanelView:
        panel = cls(
            cog,
            owner_id=owner_id,
            locale=locale,
            guild=guild,
            currency=currency,
        )
        await panel.refresh(notice=notice)
        return panel

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            self.cog.i18n.t(self.locale, "common.not_owner.shop"),
            ephemeral=True,
        )
        return False

    async def refresh(
        self,
        *,
        currency: ShopCurrency | None = None,
        page_index: int | None = None,
        notice: str | None = None,
    ) -> None:
        if currency is not None:
            if currency != self.currency:
                self.page_index = 0
            self.currency = currency
        if page_index is not None:
            self.page_index = max(0, page_index)
        self.notice = notice
        profile = await self.cog.shop_service.users_repo.get_user(self.owner_id)
        self.clear_items()
        self.add_item(await self._build_container(profile))

    async def _build_container(self, profile: dict) -> discord.ui.Container:
        container = discord.ui.Container(accent_color=SHOP_COLOR)
        header = (
            f"# 🛒 {self.cog.i18n.t(self.locale, 'shop.title')}\n"
            f"{self.cog._shop_balance_text(profile, self.locale)}"
        )
        if self.notice:
            header += f"\n\n{self.notice[:900]}"
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(
            discord.ui.ActionRow(
                ShopCurrencyButton(
                    "candies",
                    self.cog.i18n.t(self.locale, "shop.tab.candies"),
                    "🍬",
                    active=self.currency == "candies",
                ),
                ShopCurrencyButton(
                    "trick_coins",
                    self.cog.i18n.t(self.locale, "shop.tab.trick_coins"),
                    "😈",
                    active=self.currency == "trick_coins",
                ),
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        container.add_item(
            discord.ui.TextDisplay(
                f"## {self.cog.i18n.t(self.locale, f'shop.section.{self.currency}')}\n"
                f"{self.cog.i18n.t(self.locale, 'shop.quick_buy_hint')}"
            )
        )

        entries = self.cog._entries_for_currency(self.currency)
        if not entries:
            container.add_item(discord.ui.TextDisplay(self.cog.i18n.t(self.locale, "shop.empty")))
            return container

        page_size = 6 if len(entries) <= 6 else 5
        total_pages = max(1, (len(entries) + page_size - 1) // page_size)
        self.page_index = min(self.page_index, total_pages - 1)
        start = self.page_index * page_size
        for entry in entries[start : start + page_size]:
            await self._add_entry(container, entry)
        if total_pages > 1:
            container.add_item(
                discord.ui.ActionRow(
                    ShopPageButton(
                        label="‹",
                        target_page=self.page_index - 1,
                        disabled=self.page_index == 0,
                    ),
                    discord.ui.Button(
                        label=f"{self.page_index + 1}/{total_pages}",
                        style=discord.ButtonStyle.primary,
                        disabled=True,
                        custom_id="shop:page:current",
                    ),
                    ShopPageButton(
                        label="›",
                        target_page=self.page_index + 1,
                        disabled=self.page_index >= total_pages - 1,
                    ),
                )
            )
        return container

    async def _add_entry(self, container: discord.ui.Container, entry: ShopEntry) -> None:
        name = await self.cog._entry_name(entry, self.locale, self.guild)
        description = await self.cog._entry_description(entry, self.locale, self.guild)
        price = (
            f"{self.cog._currency_emoji(entry.currency)} **{entry.price}** "
            f"{self.cog._currency_label(entry.currency, self.locale)}"
        )
        container.add_item(
            discord.ui.TextDisplay(
                f"### {self.cog._entry_emoji(entry)} {name}\n{description}\n-# {price}"
            )
        )
        quantities = (1,) if entry.grant_type == "role" else (1, 5, 10)
        container.add_item(
            discord.ui.ActionRow(
                *(ShopBuyButton(entry.id, quantity) for quantity in quantities)
            )
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
        /,
    ) -> None:
        LOGGER.exception("Erro no painel da loja item=%s", item, exc_info=error)
        message = self.cog.i18n.t(self.locale, "shop.purchase_failed")
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
