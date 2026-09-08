from __future__ import annotations

from repositories.board_read_repo import BoardReadRepo
from repositories.users_repo import UsersRepo
from services.i18n_service import I18nService
from services.item_catalog import ItemCatalog


class ProfileService:
    def __init__(
        self,
        users_repo: UsersRepo | None = None,
        board_repo: BoardReadRepo | None = None,
        item_catalog: ItemCatalog | None = None,
        i18n_service: I18nService | None = None,
    ) -> None:
        self.users_repo = users_repo or UsersRepo()
        self.board_repo = board_repo or BoardReadRepo()
        self.item_catalog = item_catalog or ItemCatalog()
        self.i18n = i18n_service

    async def get_profile(self, user_id: int) -> dict:
        user = await self.users_repo.get_user(user_id)
        board_position, board_laps = await self.board_repo.get_board_state(user_id)
        inventory_summary = self.build_inventory_summary(
            user.get("inventory", {}),
            locale=user.get("locale", "pt-BR"),
            limit=6,
        )

        return {
            **user,
            "board_position": board_position,
            "board_laps": board_laps,
            "inventory_summary": inventory_summary,
        }

    def build_inventory_summary(self, inventory: dict[str, int], locale: str = "pt-BR", limit: int = 10) -> str:
        visible_items = [
            (item_id, quantity)
            for item_id, quantity in inventory.items()
            if int(quantity) > 0
        ]
        if not visible_items:
            return self.i18n.t(locale, "inventory.empty") if self.i18n else "Inventario vazio."

        lines: list[str] = []
        for item_id, quantity in sorted(visible_items)[:limit]:
            item = self.item_catalog.get(item_id)
            translated_name = self.i18n.maybe_t(locale, f"item.{item_id}.name") if self.i18n else None
            name = translated_name or (item.name if item else item_id)
            emoji = f"{item.emoji} " if item and item.emoji else ""
            lines.append(f"{emoji}{name} x{quantity}")

        hidden_count = len(visible_items) - limit
        if hidden_count > 0:
            hidden_line = (
                self.i18n.maybe_t(locale, "inventory.hidden", count=hidden_count)
                if self.i18n
                else None
            )
            lines.append(hidden_line or f"+{hidden_count} outro(s) item(ns)")

        return "\n".join(lines)
