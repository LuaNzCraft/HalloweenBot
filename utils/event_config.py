from __future__ import annotations

from typing import Any

from services.config_service import get_config_service


def load_event_config() -> dict[str, Any]:
    return get_config_service().get(default={})


def reload_event_config() -> dict[str, Any]:
    return get_config_service().reload()


def get_items_config() -> dict[str, Any]:
    return get_config_service().get("items", default={})


def get_shop_config() -> dict[str, Any]:
    return get_config_service().get("shop", default={})


def get_basket_rewards_config(basket_item_id: str) -> dict[str, Any]:
    reward_pools = get_config_service().get("baskets", "reward_pools", default={})
    pool = reward_pools.get(basket_item_id, {})
    return dict(pool) if isinstance(pool, dict) else {}


def get_basket_open_priority() -> list[str]:
    priority = get_config_service().get("baskets", "open_priority", default=[])
    return [str(item_id) for item_id in priority]


def get_board_config() -> dict[str, Any]:
    return get_config_service().get("board", default={})


def get_effects_config() -> dict[str, Any]:
    return get_config_service().get("effects", default={})


def get_profile_requests_config() -> dict[str, Any]:
    return get_config_service().get("profile_requests", default={})


def get_achievements_config() -> dict[str, Any]:
    return get_config_service().get("achievements", default={})


def get_guild_defaults() -> dict[str, Any]:
    defaults = get_config_service().get("guild_defaults", default={})
    return {
        "leaderboard_channel_id": defaults.get("leaderboard_channel_id"),
        "log_channel_id": defaults.get("log_channel_id"),
        "event_channel_ids": list(defaults.get("event_channel_ids", [])),
        "julgamento_channel_id": defaults.get("julgamento_channel_id"),
        "julgamento_thread_id": defaults.get("julgamento_thread_id"),
        "julgamento_base_message_id": defaults.get("julgamento_base_message_id"),
        "shop_role_id": defaults.get("shop_role_id"),
        "board_100_role_id": defaults.get("board_100_role_id"),
        "notification_role_id": defaults.get("notification_role_id"),
        "cinefilo_role_id": defaults.get("cinefilo_role_id"),
        "julgamento_active_role_id": defaults.get("julgamento_active_role_id"),
        "julgamento_muted_role_id": defaults.get("julgamento_muted_role_id"),
        "julgamento_spectator_role_id": defaults.get("julgamento_spectator_role_id"),
    }
