from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakSet

from repositories.users_repo import UsersRepo
from services.i18n_service import I18nService
from services.item_catalog import ItemCatalog
from utils.event_config import get_shop_config


ROOT_DIR = Path(__file__).resolve().parent.parent
SHOP_PATH = ROOT_DIR / "content" / "halloween" / "shop.json"

CURRENCY_LABELS = {
    "candies": "Moedas Doce",
    "trick_coins": "Moedas Travessas",
}
GRANT_CURRENCY_FIELDS = {"candies", "trick_coins", "dice_count", "xp_bank"}


@dataclass(frozen=True)
class ShopEntry:
    id: str
    name: str
    emoji: str
    description: str
    price: int
    currency: str
    grant_type: str
    grant_id: str
    grant_quantity: int
    enabled: bool = True


@dataclass(frozen=True)
class PurchaseResult:
    ok: bool
    message: str
    entry_id: str | None = None
    quantity: int = 0
    spent_amount: int = 0


class ShopService:
    _instances: WeakSet["ShopService"] = WeakSet()

    def __init__(
        self,
        users_repo: UsersRepo | None = None,
        item_catalog: ItemCatalog | None = None,
        i18n_service: I18nService | None = None,
        path: Path = SHOP_PATH,
    ) -> None:
        self.users_repo = users_repo or UsersRepo()
        self.item_catalog = item_catalog or ItemCatalog()
        self.i18n = i18n_service or I18nService(self.users_repo)
        self.path = path
        self._entries: dict[str, ShopEntry] | None = None
        self._purchase_locks: dict[int, asyncio.Lock] = {}
        self._instances.add(self)

    @classmethod
    def reload_all(cls) -> None:
        for service in list(cls._instances):
            service.reload_entries()

    def _get_purchase_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._purchase_locks:
            self._purchase_locks[user_id] = asyncio.Lock()
        return self._purchase_locks[user_id]

    @property
    def entries(self) -> dict[str, ShopEntry]:
        if self._entries is None:
            self.reload_entries()
        return self._entries  # type: ignore[return-value]

    def reload_entries(self) -> None:
        """Recarrega as entradas da loja do config ou do arquivo JSON."""
        raw_entries = get_shop_config()
        if not raw_entries:
            with self.path.open("r", encoding="utf-8") as file:
                raw_entries = json.load(file)
        self._entries = {
            entry_id: ShopEntry(id=entry_id, **payload)
            for entry_id, payload in raw_entries.items()
        }

    def enabled_entries(self) -> dict[str, ShopEntry]:
        return {
            entry_id: entry
            for entry_id, entry in self.entries.items()
            if entry.enabled
        }

    def get(self, entry_id: str) -> ShopEntry | None:
        return self.entries.get(entry_id)

    async def purchase(self, user_id: int, entry_id: str, quantity: int = 1) -> PurchaseResult:
        async with self._get_purchase_lock(user_id):
            return await self._do_purchase(user_id, entry_id, quantity)

    async def _do_purchase(self, user_id: int, entry_id: str, quantity: int = 1) -> PurchaseResult:
        locale = await self.i18n.get_user_locale(user_id)
        if quantity <= 0:
            quantity = 1

        entry = self.get(entry_id)
        if entry is None or not entry.enabled:
            return PurchaseResult(False, self.i18n.t(locale, "shop.unavailable"), entry_id=entry_id)

        if entry.currency not in CURRENCY_LABELS:
            return PurchaseResult(False, self.i18n.t(locale, "shop.currency_not_configured"), entry_id=entry.id)

        grant_item = None
        if entry.grant_type == "item":
            grant_item = self.item_catalog.get(entry.grant_id)
            if grant_item is None or not grant_item.grantable:
                return PurchaseResult(False, self.i18n.t(locale, "shop.grant_item_not_configured"))
        elif entry.grant_type == "currency":
            if entry.grant_id not in GRANT_CURRENCY_FIELDS:
                return PurchaseResult(False, self.i18n.t(locale, "shop.grant_currency_not_configured"))
        elif entry.grant_type == "role":
            if entry.grant_id != "shop_role":
                return PurchaseResult(False, self.i18n.t(locale, "shop.grant_role_not_configured"))
        else:
            return PurchaseResult(
                False,
                self.i18n.t(locale, "shop.grant_type_not_implemented"),
                entry_id=entry.id,
            )

        # Cargos só podem ser comprados uma vez; força quantity antes de qualquer cálculo
        if entry.grant_type == "role":
            quantity = 1

        user = await self.users_repo.get_user(user_id)
        current = int(user.get(entry.currency, 0))
        affordable_quantity = current // entry.price if entry.price > 0 else quantity
        if affordable_quantity <= 0:
            label = self.i18n.maybe_t(locale, f"currency.{entry.currency}") or CURRENCY_LABELS[entry.currency]
            return PurchaseResult(
                False,
                self.i18n.t(locale, "shop.insufficient", current=current, currency=label, price=entry.price),
                entry_id=entry.id,
            )

        purchased_quantity = min(quantity, affordable_quantity)
        spent_amount = entry.price * purchased_quantity
        try:
            await self.users_repo.spend_currency(user_id, entry.currency, spent_amount)
        except ValueError:
            label = self.i18n.maybe_t(locale, f"currency.{entry.currency}") or CURRENCY_LABELS[entry.currency]
            return PurchaseResult(
                False,
                self.i18n.t(locale, "shop.insufficient", current=current, currency=label, price=entry.price),
                entry_id=entry.id,
            )

        entry_name = self.i18n.maybe_t(locale, f"shop.{entry.id}.name") or entry.name
        partial = purchased_quantity < quantity

        if entry.grant_type == "item":
            total = await self.users_repo.add_inventory_item(
                user_id,
                entry.grant_id,
                entry.grant_quantity * purchased_quantity,
            )
            i18n_key = "shop.purchase_partial" if partial else "shop.purchase_done"
            return PurchaseResult(
                True,
                self.i18n.t(
                    locale,
                    i18n_key,
                    name=entry_name,
                    quantity=entry.grant_quantity * purchased_quantity,
                    total=total,
                    requested=quantity,
                    purchased=purchased_quantity,
                ),
                entry_id=entry.id,
                quantity=purchased_quantity,
                spent_amount=spent_amount,
            )

        if entry.grant_type == "currency":
            total = await self.users_repo.add_currency(
                user_id,
                entry.grant_id,
                entry.grant_quantity * purchased_quantity,
            )
            i18n_key = "shop.purchase_partial_currency" if partial else "shop.purchase_done_currency"
            return PurchaseResult(
                True,
                self.i18n.t(
                    locale,
                    i18n_key,
                    name=entry_name,
                    quantity=entry.grant_quantity * purchased_quantity,
                    total=total,
                    requested=quantity,
                    purchased=purchased_quantity,
                ),
                entry_id=entry.id,
                quantity=purchased_quantity,
                spent_amount=spent_amount,
            )

        if entry.grant_type == "role":
            return PurchaseResult(
                True,
                self.i18n.t(locale, "shop.purchase_done_role", name=entry_name),
                entry_id=entry.id,
                quantity=purchased_quantity,
                spent_amount=spent_amount,
            )

        return PurchaseResult(False, self.i18n.t(locale, "shop.grant_type_not_implemented"), entry_id=entry.id)
