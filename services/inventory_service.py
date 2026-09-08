from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

from repositories.guild_metrics_repo import GuildMetricsRepo
from repositories.user_guild_metrics_repo import UserGuildMetricsRepo
from repositories.users_repo import UsersRepo
from services.i18n_service import I18nService
from services.item_catalog import ItemCatalog, ItemDefinition
from utils.event_config import get_basket_open_priority, get_basket_rewards_config

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class UseItemResult:
    ok: bool
    public: bool
    message: str


@dataclass(frozen=True)
class OpenBasketsResult:
    ok: bool
    opened: int
    remaining: int
    message: str


class InventoryService:
    def __init__(
        self,
        users_repo: UsersRepo | None = None,
        item_catalog: ItemCatalog | None = None,
        guild_metrics_repo: GuildMetricsRepo | None = None,
        user_guild_metrics_repo: UserGuildMetricsRepo | None = None,
        i18n_service: I18nService | None = None,
    ) -> None:
        self.users_repo = users_repo or UsersRepo()
        self.item_catalog = item_catalog or ItemCatalog()
        self.guild_metrics_repo = guild_metrics_repo
        self.user_guild_metrics_repo = user_guild_metrics_repo
        self.i18n = i18n_service or I18nService(self.users_repo)
        self._basket_user_locks: dict[int, asyncio.Lock] = {}

    async def grant_item(self, user_id: int, item_id: str, quantity: int = 1) -> int:
        item = self.item_catalog.get(item_id)
        if item is None or not item.grantable:
            raise ValueError("unknown_item")
        return await self.users_repo.add_inventory_item(user_id, item_id, quantity)

    async def use_item(self, user_id: int, item_id: str, target_id: int | None = None) -> UseItemResult:
        locale = await self.i18n.get_user_locale(user_id)
        item = self.item_catalog.get(item_id)
        if item is None:
            return UseItemResult(False, False, self.i18n.t(locale, "items.unknown"))

        if item.requires_target and target_id is None:
            return UseItemResult(False, False, self.i18n.t(locale, "items.needs_target"))

        if not item.implemented:
            return UseItemResult(False, False, self.i18n.t(locale, "items.not_implemented"))

        # Social/runtime items are handled by ItemsCog/ItemsRuntimeService, not by the generic inventory path.
        if item.id in {"mute", "switch", "interferencia", "observer_hint"}:
            return UseItemResult(False, False, self.i18n.t(locale, "items.not_implemented"))

        user = await self.users_repo.get_user(user_id)
        inventory = user.get("inventory", {})
        if int(inventory.get(item_id, 0)) <= 0:
            return UseItemResult(False, False, self.i18n.t(locale, "items.not_owned"))

        if item.kind == "consumable":
            await self.users_repo.remove_inventory_item(user_id, item_id, 1)

        return await self._apply_item(user_id, item, locale)

    async def _apply_item(self, user_id: int, item: ItemDefinition, locale: str) -> UseItemResult:
        if item.id in {"cesta_abobora", "cesta_doce_epica", "cesta_doce_amaldicoada"}:
            result = await self.open_pumpkin_baskets(user_id, 1)
            if not result.ok:
                return UseItemResult(False, False, result.message)
            reward_message = result.message.removeprefix("- ").strip()
            return UseItemResult(True, False, self.i18n.t(locale, "items.cesta_abobora", reward=reward_message))

        return UseItemResult(False, False, self.i18n.t(locale, "items.no_effect"))

    async def open_pumpkin_baskets(self, user_id: int, quantity: int, guild_id: int | None = None) -> OpenBasketsResult:
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        lock = self._basket_user_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            return await self._open_pumpkin_baskets_locked(user_id, quantity, guild_id)

    async def _open_pumpkin_baskets_locked(
        self,
        user_id: int,
        quantity: int,
        guild_id: int | None = None,
    ) -> OpenBasketsResult:
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        user = await self.users_repo.get_user(user_id)
        locale = str(user.get("locale") or "pt-BR")
        inventory = user.get("inventory", {})
        basket_priority = get_basket_open_priority() or ["cesta_doce_amaldicoada", "cesta_doce_epica", "cesta_abobora"]
        basket_counts = {basket_id: int(inventory.get(basket_id, 0)) for basket_id in basket_priority}
        current = sum(basket_counts.values())
        if current < quantity:
            return OpenBasketsResult(
                ok=False,
                opened=0,
                remaining=current,
                message=self.i18n.t(locale, "baskets.not_enough", current=current, quantity=quantity),
            )

        remaining_to_open = quantity
        opened_by_type: list[str] = []
        for basket_id in basket_priority:
            available = basket_counts.get(basket_id, 0)
            if available <= 0 or remaining_to_open <= 0:
                continue
            to_open = min(available, remaining_to_open)
            opened_by_type.extend([basket_id] * to_open)
            remaining_to_open -= to_open

        planned_user = dict(user)
        planned_inventory = dict(inventory)
        planned_user["inventory"] = planned_inventory
        for basket_id in opened_by_type:
            planned_inventory[basket_id] = int(planned_inventory.get(basket_id, 0)) - 1
            if planned_inventory[basket_id] <= 0:
                planned_inventory.pop(basket_id, None)

        reward_groups: dict[str, list[str]] = {}
        reward_group_order: list[str] = []
        reward_group_counts: dict[str, int] = {}
        metric_totals = {"candies": 0, "trick_coins": 0, "xp_bank": 0}
        try:
            for basket_id in opened_by_type:
                reward_messages, reward_metric_totals = self._roll_basket_reward_plan(planned_user, locale, basket_id)
                for metric_name, amount in reward_metric_totals.items():
                    metric_totals[metric_name] += amount
                if basket_id not in reward_groups:
                    reward_group_order.append(basket_id)
                    reward_groups[basket_id] = []
                    reward_group_counts[basket_id] = 0
                reward_group_counts[basket_id] += 1
                reward_groups[basket_id].extend(f"- {message}" for message in reward_messages)
        except RuntimeError:
            LOGGER.exception("Failed to resolve basket rewards for user_id=%s", user_id)
            return OpenBasketsResult(
                ok=False,
                opened=0,
                remaining=current,
                message=self.i18n.t(locale, "baskets.open_failed"),
            )

        planned_user["baskets_opened_total"] = int(planned_user.get("baskets_opened_total", 0)) + quantity
        await self.users_repo.update_user(
            user_id,
            lambda current_user: current_user.update(
                {
                    "candies": planned_user.get("candies", 0),
                    "candies_total": planned_user.get("candies_total", 0),
                    "trick_coins": planned_user.get("trick_coins", 0),
                    "trick_coins_total": planned_user.get("trick_coins_total", 0),
                    "dice_count": planned_user.get("dice_count", 0),
                    "xp_bank": planned_user.get("xp_bank", 0),
                    "baskets_opened_total": planned_user.get("baskets_opened_total", 0),
                    "inventory": dict(planned_inventory),
                }
            ),
        )
        if self.guild_metrics_repo is not None and guild_id is not None:
            if metric_totals["candies"] > 0:
                await self.guild_metrics_repo.increment_metric(guild_id, "candies_total", metric_totals["candies"])
            if metric_totals["trick_coins"] > 0:
                await self.guild_metrics_repo.increment_metric(guild_id, "trick_coins_total", metric_totals["trick_coins"])
            if metric_totals["xp_bank"] > 0:
                await self.guild_metrics_repo.increment_metric(guild_id, "xp_bank", metric_totals["xp_bank"])
        if self.user_guild_metrics_repo is not None and guild_id is not None:
            await self.user_guild_metrics_repo.increment_metric(guild_id, user_id, "baskets_opened_total", quantity)
            if metric_totals["candies"] > 0:
                await self.user_guild_metrics_repo.increment_metric(guild_id, user_id, "candies_total", metric_totals["candies"])
            if metric_totals["trick_coins"] > 0:
                await self.user_guild_metrics_repo.increment_metric(guild_id, user_id, "trick_coins_total", metric_totals["trick_coins"])
            if metric_totals["xp_bank"] > 0:
                await self.user_guild_metrics_repo.increment_metric(guild_id, user_id, "xp_bank", metric_totals["xp_bank"])

        reward_lines: list[str] = []
        for basket_id in reward_group_order:
            basket_name = self.i18n.maybe_t(locale, f"item.{basket_id}.name") or basket_id
            count = reward_group_counts[basket_id]
            header = f"━━ {basket_name} x{count} ━━"
            reward_block = "\n".join(reward_groups[basket_id])
            reward_lines.append(f"{header}\n{reward_block}")

        remaining = current - quantity
        return OpenBasketsResult(
            ok=True,
            opened=quantity,
            remaining=remaining,
            message="\n\n".join(reward_lines),
        )

    def _roll_basket_reward_plan(
        self,
        user: dict,
        locale: str,
        basket_item_id: str,
    ) -> tuple[list[str], dict[str, int]]:
        basket_config = get_basket_rewards_config(basket_item_id)
        rewards = basket_config.get("rewards", [])
        if not rewards:
            raise RuntimeError("basket rewards config is empty")
        roll_count = max(1, int(basket_config.get("roll_count", 1)))
        reward_messages: list[str] = []
        metric_totals = {"candies": 0, "trick_coins": 0, "xp_bank": 0}
        inventory = user.setdefault("inventory", {})

        def add_currency(field: str, amount: int) -> None:
            user[field] = int(user.get(field, 0)) + amount
            if field == "candies":
                user["candies_total"] = int(user.get("candies_total", 0)) + amount
                metric_totals["candies"] += amount
            elif field == "trick_coins":
                user["trick_coins_total"] = int(user.get("trick_coins_total", 0)) + amount
                metric_totals["trick_coins"] += amount
            elif field == "xp_bank":
                metric_totals["xp_bank"] += amount

        for _ in range(roll_count):
            reward = random.choices(
                rewards,
                weights=[int(entry.get("weight", 1)) for entry in rewards],
                k=1,
            )[0]
            grant_type = str(reward.get("grant_type") or "")
            grant_id = str(reward.get("grant_id") or "")

            if grant_type == "currency":
                configured_amount = reward.get("amount")
                amount = int(configured_amount) if configured_amount is not None else random.randint(
                    int(reward["min_amount"]),
                    int(reward["max_amount"]),
                )
                add_currency(grant_id, amount)
                if grant_id == "candies":
                    reward_messages.append(self.i18n.t(locale, "baskets.reward.candies", amount=amount))
                    continue
                if grant_id == "trick_coins":
                    reward_messages.append(self.i18n.t(locale, "baskets.reward.trick_coins", amount=amount))
                    continue
                if grant_id == "dice_count":
                    reward_messages.append(self.i18n.t(locale, "baskets.reward.dice", amount=amount))
                    continue
                if grant_id == "xp_bank":
                    reward_messages.append(self.i18n.t(locale, "baskets.reward.xp", amount=amount))
                    continue

            if grant_type == "currency_bundle":
                candies_amount = int(
                    reward.get("candies_amount")
                    or random.randint(int(reward["candies_min_amount"]), int(reward["candies_max_amount"]))
                )
                trick_amount = int(
                    reward.get("trick_coins_amount")
                    or random.randint(int(reward["trick_coins_min_amount"]), int(reward["trick_coins_max_amount"]))
                )
                add_currency("candies", candies_amount)
                add_currency("trick_coins", trick_amount)
                candies_text = self.i18n.t(locale, "baskets.reward.candies", amount=candies_amount)
                trick_text = self.i18n.t(locale, "baskets.reward.trick_coins", amount=trick_amount)
                reward_messages.append(f"{candies_text} + {trick_text}")
                continue

            if grant_type == "item":
                amount = int(reward.get("amount", 1))
                item = self.item_catalog.get(grant_id)
                if item is None or not item.grantable:
                    raise RuntimeError(f"unsupported basket item reward: {grant_id}")
                total = int(inventory.get(grant_id, 0)) + amount
                inventory[grant_id] = total
                item_name = self.i18n.maybe_t(locale, f"item.{grant_id}.name") or grant_id
                reward_messages.append(self.i18n.t(locale, "baskets.reward.item", item_name=item_name, amount=amount, total=total))
                continue

            raise RuntimeError(f"unsupported basket reward config: {grant_type}:{grant_id}")

        return reward_messages, metric_totals
