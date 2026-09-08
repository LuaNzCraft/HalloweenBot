from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakSet

from utils.event_config import get_items_config

ROOT_DIR = Path(__file__).resolve().parent.parent
ITEMS_PATH = ROOT_DIR / "content" / "halloween" / "items.json"


@dataclass(frozen=True)
class ItemDefinition:
    id: str
    name: str
    emoji: str
    description: str
    kind: str
    requires_target: bool
    implemented: bool
    grantable: bool = True
    usable: bool = True


class ItemCatalog:
    _instances: WeakSet["ItemCatalog"] = WeakSet()

    def __init__(self, path: Path = ITEMS_PATH) -> None:
        self.path = path
        self._items: dict[str, ItemDefinition] | None = None
        self._instances.add(self)

    def invalidate(self) -> None:
        self._items = None

    @classmethod
    def invalidate_all(cls) -> None:
        for catalog in list(cls._instances):
            catalog.invalidate()

    def get(self, item_id: str) -> ItemDefinition | None:
        return self.items.get(item_id)

    def grantable_items(self) -> dict[str, ItemDefinition]:
        return {
            item_id: item
            for item_id, item in self.items.items()
            if item.grantable
        }

    def usable_items(self) -> dict[str, ItemDefinition]:
        return {
            item_id: item
            for item_id, item in self.items.items()
            if item.grantable and item.usable
        }

    @property
    def items(self) -> dict[str, ItemDefinition]:
        if self._items is None:
            raw_items = get_items_config()
            if not raw_items:
                with self.path.open("r", encoding="utf-8") as file:
                    raw_items = json.load(file)
            self._items = {
                item_id: ItemDefinition(id=item_id, **payload)
                for item_id, payload in raw_items.items()
            }
        return self._items
