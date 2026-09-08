from __future__ import annotations

from copy import deepcopy
import logging
import math
from pathlib import Path
from threading import RLock
from typing import Any

import yaml
from yaml.constructor import ConstructorError


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "halloween.yml"
LOGGER = logging.getLogger(__name__)


class UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicated = key in mapping
        except TypeError as exc:
            raise ConstructorError("while constructing a mapping", node.start_mark, "chave nao-hashable", key_node.start_mark) from exc
        if duplicated:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"chave duplicada: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class ConfigError(ValueError):
    pass


class ConfigService:
    """Carrega, valida e mantem a ultima configuracao valida em memoria."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._config: dict[str, Any] | None = None
        self._mtime_ns: int | None = None
        self._failed_mtime_ns: int | None = None
        self._version = 0
        self._lock = RLock()

    @property
    def version(self) -> int:
        self.load()
        return self._version

    def load(self) -> dict[str, Any]:
        """Retorna config atual e tenta hot reload quando o arquivo muda.

        Se uma edicao nova for invalida, a ultima versao valida continua ativa.
        A primeira carga, por nao possuir fallback, levanta ``ConfigError``.
        """

        with self._lock:
            try:
                mtime_ns = self.path.stat().st_mtime_ns
            except OSError as exc:
                if self._config is not None:
                    LOGGER.error("Arquivo de configuracao indisponivel; mantendo ultima versao valida: %s", exc)
                    return self._config
                raise ConfigError(f"Arquivo de configuracao nao encontrado: {self.path}") from exc

            if self._config is not None and (mtime_ns == self._mtime_ns or mtime_ns == self._failed_mtime_ns):
                return self._config

            try:
                candidate = self._read_and_validate()
            except ConfigError as exc:
                self._failed_mtime_ns = mtime_ns
                if self._config is None:
                    raise
                LOGGER.error("Configuracao nova invalida; mantendo a ultima versao valida: %s", exc)
                return self._config

            self._activate(candidate, mtime_ns)
            return self._config

    def reload(self) -> dict[str, Any]:
        """Forca validacao e so troca a config ativa quando tudo estiver correto."""

        with self._lock:
            try:
                mtime_ns = self.path.stat().st_mtime_ns
            except OSError as exc:
                raise ConfigError(f"Arquivo de configuracao nao encontrado: {self.path}") from exc
            candidate = self._read_and_validate()
            self._activate(candidate, mtime_ns)
            return deepcopy(self._config)

    def get(self, *path: str, default: Any = None) -> Any:
        current: Any = self.load()
        for part in path:
            if not isinstance(current, dict) or part not in current:
                return deepcopy(default)
            current = current[part]
        return deepcopy(current)

    def _activate(self, candidate: dict[str, Any], mtime_ns: int) -> None:
        self._config = candidate
        self._mtime_ns = mtime_ns
        self._failed_mtime_ns = None
        self._version += 1

    def _read_and_validate(self) -> dict[str, Any]:
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Nao foi possivel ler {self.path}: {exc}") from exc
        if len(raw_text.encode("utf-8")) > 2_000_000:
            raise ConfigError("config: arquivo excede o limite de 2 MB")
        try:
            data = yaml.load(raw_text, Loader=UniqueKeySafeLoader)
        except yaml.YAMLError as exc:
            raise ConfigError(f"YAML invalido: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("config: a raiz precisa ser um objeto YAML")
        self._validate(data)
        return data

    def _validate(self, data: dict[str, Any]) -> None:
        self._validate_yaml_values(data, "config", set())
        required_sections = (
            "guild_defaults",
            "achievements",
            "effects",
            "profile_requests",
            "chat_bonus",
            "chat_candy",
            "board",
            "baskets",
            "items",
            "shop",
            "public_events",
            "carousel",
        )
        for section in required_sections:
            self._mapping(data, section, "config")

        self._integer(data, "config_version", "config", minimum=1)

        achievements = self._mapping(data, "achievements", "config")
        self._integer(achievements, "cinefilo_correct_answers_required", "config.achievements", minimum=1)
        milestones = self._list(achievements, "generosity_candies_milestones", "config.achievements")
        for index, milestone in enumerate(milestones):
            milestone_path = f"config.achievements.generosity_candies_milestones[{index}]"
            if not isinstance(milestone, dict):
                raise ConfigError(f"{milestone_path}: precisa ser um objeto")
            self._integer(milestone, "target", milestone_path, minimum=1)
            rewards = self._mapping(milestone, "rewards", milestone_path)
            self._validate_integer_mapping(rewards, f"{milestone_path}.rewards", minimum=0)

        effects = self._mapping(data, "effects", "config")
        for key in ("mute_duration_seconds", "interference_duration_seconds", "switch_duration_seconds"):
            self._integer(effects, key, "config.effects", minimum=1)
        self._boolean(effects, "admin_mute_delete_messages", "config.effects")
        nickname_pool = self._list(effects, "switch_nickname_pool", "config.effects")
        if not nickname_pool or any(not isinstance(value, str) or not value.strip() for value in nickname_pool):
            raise ConfigError("config.effects.switch_nickname_pool: use uma lista de textos nao vazios")

        profile_requests = self._mapping(data, "profile_requests", "config")
        for key in (
            "candy_amount",
            "trick_coins_amount",
            "daily_requests_per_pair",
            "request_timeout_seconds",
            "active_window_minutes",
            "ignore_cooldown_seconds",
        ):
            self._integer(profile_requests, key, "config.profile_requests", minimum=0)

        chat_bonus = self._mapping(data, "chat_bonus", "config")
        self._boolean(chat_bonus, "enabled", "config.chat_bonus")
        for key in ("threshold_messages", "window_minutes", "button_timeout_seconds"):
            self._integer(chat_bonus, key, "config.chat_bonus", minimum=1)
        self._integer(chat_bonus, "min_message_length", "config.chat_bonus", minimum=0)
        options = self._list(chat_bonus, "options", "config.chat_bonus")
        if not options:
            raise ConfigError("config.chat_bonus.options: precisa conter pelo menos uma opcao")
        for index, option in enumerate(options):
            option_path = f"config.chat_bonus.options[{index}]"
            if not isinstance(option, dict):
                raise ConfigError(f"{option_path}: precisa ser um objeto")
            for key in ("id", "label"):
                self._string(option, key, option_path)
            rewards = self._list(option, "rewards", option_path)
            for reward_index, reward in enumerate(rewards):
                self._validate_reward_entry(reward, f"{option_path}.rewards[{reward_index}]")

        chat_candy = self._mapping(data, "chat_candy", "config")
        self._boolean(chat_candy, "enabled", "config.chat_candy")
        self._probability(chat_candy, "spawn_chance_percent", "config.chat_candy")
        self._number(chat_candy, "spawn_cooldown_seconds", "config.chat_candy", minimum=0)
        self._number(chat_candy, "capture_seconds", "config.chat_candy", minimum=1)
        self._integer(chat_candy, "min_message_length", "config.chat_candy", minimum=0)
        minimum_options = self._integer(chat_candy, "min_options", "config.chat_candy", minimum=3)
        maximum_options = self._integer(chat_candy, "max_options", "config.chat_candy", minimum=3)
        if minimum_options > maximum_options:
            raise ConfigError("config.chat_candy.min_options: nao pode ser maior que max_options")
        emoji_ids = self._list(chat_candy, "emoji_ids", "config.chat_candy")
        if len({str(value) for value in emoji_ids}) < maximum_options:
            raise ConfigError("config.chat_candy.emoji_ids: precisa conter emojis suficientes para max_options")
        for index, emoji_id in enumerate(emoji_ids):
            if isinstance(emoji_id, bool):
                raise ConfigError(f"config.chat_candy.emoji_ids[{index}]: ID invalido")
            try:
                parsed_id = int(emoji_id)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ConfigError(f"config.chat_candy.emoji_ids[{index}]: precisa ser um ID inteiro") from exc
            if parsed_id <= 0:
                raise ConfigError(f"config.chat_candy.emoji_ids[{index}]: precisa ser positivo")
        self._integer(chat_candy, "reward_candies", "config.chat_candy", minimum=0)
        self._integer(chat_candy, "max_spawns_per_day", "config.chat_candy", minimum=0)
        self._boolean(chat_candy, "pause_during_public_events", "config.chat_candy")

        board = self._mapping(data, "board", "config")
        total_tiles = self._integer(board, "total_tiles", "config.board", minimum=1)
        tile_types = self._mapping(board, "tile_types", "config.board")
        coordinates = self._mapping(board, "coordinates", "config.board")
        if len(tile_types) < total_tiles or len(coordinates) < total_tiles:
            raise ConfigError("config.board: tile_types e coordinates precisam cobrir total_tiles")
        for key, coordinate in coordinates.items():
            if not isinstance(coordinate, list) or len(coordinate) != 2:
                raise ConfigError(f"config.board.coordinates.{key}: use [x, y]")
            if any(isinstance(value, bool) or not isinstance(value, int) for value in coordinate):
                raise ConfigError(f"config.board.coordinates.{key}: x e y precisam ser inteiros")

        baskets = self._mapping(data, "baskets", "config")
        open_priority = self._list(baskets, "open_priority", "config.baskets")
        if not open_priority or any(not isinstance(value, str) or not value for value in open_priority):
            raise ConfigError("config.baskets.open_priority: use uma lista de IDs")
        reward_pools = self._mapping(baskets, "reward_pools", "config.baskets")
        for pool_id, pool in reward_pools.items():
            pool_path = f"config.baskets.reward_pools.{pool_id}"
            if not isinstance(pool, dict):
                raise ConfigError(f"{pool_path}: precisa ser um objeto")
            self._integer(pool, "roll_count", pool_path, minimum=1)
            rewards = self._list(pool, "rewards", pool_path)
            if not rewards:
                raise ConfigError(f"{pool_path}.rewards: nao pode ficar vazio")
            total_weight = 0.0
            for index, reward in enumerate(rewards):
                reward_path = f"{pool_path}.rewards[{index}]"
                self._validate_reward_entry(reward, reward_path)
                total_weight += self._number(reward, "weight", reward_path, minimum=0)
            if total_weight <= 0:
                raise ConfigError(f"{pool_path}.rewards: pelo menos um peso precisa ser maior que zero")

        items = self._mapping(data, "items", "config")
        for item_id, item in items.items():
            item_path = f"config.items.{item_id}"
            if not isinstance(item, dict):
                raise ConfigError(f"{item_path}: precisa ser um objeto")
            for key in ("name", "emoji", "description", "kind"):
                self._string(item, key, item_path, allow_empty=key == "emoji")
            for key in ("requires_target", "implemented", "grantable", "usable"):
                self._boolean(item, key, item_path)

        shop = self._mapping(data, "shop", "config")
        for entry_id, entry in shop.items():
            entry_path = f"config.shop.{entry_id}"
            if not isinstance(entry, dict):
                raise ConfigError(f"{entry_path}: precisa ser um objeto")
            for key in ("name", "emoji", "description", "currency", "grant_type", "grant_id"):
                self._string(entry, key, entry_path, allow_empty=key == "emoji")
            self._integer(entry, "price", entry_path, minimum=0)
            self._integer(entry, "grant_quantity", entry_path, minimum=1)
            self._boolean(entry, "enabled", entry_path)

        public_events = self._mapping(data, "public_events", "config")
        scheduler = self._mapping(public_events, "scheduler", "config.public_events")
        self._boolean(scheduler, "enabled", "config.public_events.scheduler")
        self._integer(scheduler, "interval_minutes", "config.public_events.scheduler", minimum=1)
        self._integer(scheduler, "announce_minutes_before", "config.public_events.scheduler", minimum=0)
        self._integer(scheduler, "pause_after_hour", "config.public_events.scheduler", minimum=-1, maximum=23)
        self._integer(scheduler, "resume_hour", "config.public_events.scheduler", minimum=0, maximum=23)
        for key in ("daily_times", "fixed_times", "julgamento_daily_times"):
            if key in scheduler:
                for index, value in enumerate(self._list(scheduler, key, "config.public_events.scheduler")):
                    self._validate_time(value, f"config.public_events.scheduler.{key}[{index}]")

        ghostface = self._mapping(public_events, "ghostface", "config.public_events")
        self._boolean(ghostface, "enabled", "config.public_events.ghostface")
        self._number(ghostface, "answer_timeout_seconds", "config.public_events.ghostface", minimum=1)
        self._validate_weights(ghostface.get("round_type_weights"), "config.public_events.ghostface.round_type_weights")
        question_sources = self._list(ghostface, "question_sources", "config.public_events.ghostface")
        if not question_sources or any(not isinstance(value, str) or not value.strip() for value in question_sources):
            raise ConfigError("config.public_events.ghostface.question_sources: use uma lista de caminhos")

        candy_shower = self._mapping(public_events, "candy_shower", "config.public_events")
        self._boolean(candy_shower, "enabled", "config.public_events.candy_shower")
        self._probability(candy_shower, "spawn_chance_percent", "config.public_events.candy_shower")
        self._number(candy_shower, "capture_seconds", "config.public_events.candy_shower", minimum=1)
        self._number(candy_shower, "duration_minutes", "config.public_events.candy_shower", minimum=1)
        for key in ("min_message_length", "max_spawns_per_event", "max_captures_per_user"):
            self._integer(candy_shower, key, "config.public_events.candy_shower", minimum=0)
        shower_emojis = self._list(candy_shower, "emoji_ids", "config.public_events.candy_shower")
        if not shower_emojis:
            raise ConfigError("config.public_events.candy_shower.emoji_ids: nao pode ficar vazio")

        carousel = self._mapping(data, "carousel", "config")
        self._boolean(carousel, "enabled", "config.carousel")
        self._number(carousel, "answer_timeout_seconds", "config.carousel", minimum=1)
        self._number(carousel, "spectator_minutes_after_end", "config.carousel", minimum=0)
        self._validate_weights(carousel.get("round_type_weights"), "config.carousel.round_type_weights")
        self._integer(carousel, "signup_minutes", "config.carousel", minimum=0)
        signup_seconds = carousel.get("signup_seconds")
        if signup_seconds is not None and (isinstance(signup_seconds, bool) or not isinstance(signup_seconds, int) or signup_seconds < 1):
            raise ConfigError("config.carousel.signup_seconds: use null ou um inteiro positivo")
        winner_rewards = self._mapping(carousel, "winner_rewards", "config.carousel")
        self._validate_integer_mapping(winner_rewards, "config.carousel.winner_rewards", minimum=0)

    def _validate_yaml_values(self, value: Any, path: str, seen_containers: set[int]) -> None:
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen_containers:
                raise ConfigError(f"{path}: aliases YAML nao sao permitidos")
            seen_containers.add(identity)
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ConfigError(f"{path}: todas as chaves precisam ser texto")
                self._validate_yaml_values(child, f"{path}.{key}", seen_containers)
            return
        if isinstance(value, list):
            identity = id(value)
            if identity in seen_containers:
                raise ConfigError(f"{path}: aliases YAML nao sao permitidos")
            seen_containers.add(identity)
            for index, child in enumerate(value):
                self._validate_yaml_values(child, f"{path}[{index}]", seen_containers)
            return
        if value is None or isinstance(value, (str, bool, int)):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ConfigError(f"{path}: NaN e infinito nao sao permitidos")
            return
        raise ConfigError(f"{path}: tipo YAML nao suportado ({type(value).__name__})")

    def _mapping(self, data: dict[str, Any], key: str, path: str) -> dict[str, Any]:
        value = data.get(key)
        if not isinstance(value, dict):
            raise ConfigError(f"{path}.{key}: precisa ser um objeto")
        return value

    def _list(self, data: dict[str, Any], key: str, path: str) -> list[Any]:
        value = data.get(key)
        if not isinstance(value, list):
            raise ConfigError(f"{path}.{key}: precisa ser uma lista")
        return value

    def _boolean(self, data: dict[str, Any], key: str, path: str) -> bool:
        value = data.get(key)
        if not isinstance(value, bool):
            raise ConfigError(f"{path}.{key}: precisa ser true ou false")
        return value

    def _string(self, data: dict[str, Any], key: str, path: str, *, allow_empty: bool = False) -> str:
        value = data.get(key)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            suffix = "texto" if allow_empty else "texto nao vazio"
            raise ConfigError(f"{path}.{key}: precisa ser {suffix}")
        return value

    def _number(
        self,
        data: dict[str, Any],
        key: str,
        path: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ConfigError(f"{path}.{key}: precisa ser um numero finito")
        parsed = float(value)
        if minimum is not None and parsed < minimum:
            raise ConfigError(f"{path}.{key}: precisa ser no minimo {minimum:g}")
        if maximum is not None and parsed > maximum:
            raise ConfigError(f"{path}.{key}: precisa ser no maximo {maximum:g}")
        return parsed

    def _integer(
        self,
        data: dict[str, Any],
        key: str,
        path: str,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path}.{key}: precisa ser um numero inteiro")
        if minimum is not None and value < minimum:
            raise ConfigError(f"{path}.{key}: precisa ser no minimo {minimum}")
        if maximum is not None and value > maximum:
            raise ConfigError(f"{path}.{key}: precisa ser no maximo {maximum}")
        return value

    def _probability(self, data: dict[str, Any], key: str, path: str) -> float:
        return self._number(data, key, path, minimum=0, maximum=100)

    def _validate_weights(self, value: Any, path: str) -> None:
        if not isinstance(value, dict) or not value:
            raise ConfigError(f"{path}: precisa ser um objeto com pelo menos um peso")
        total = 0.0
        for key, weight in value.items():
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(float(weight)):
                raise ConfigError(f"{path}.{key}: precisa ser um numero finito")
            if float(weight) < 0:
                raise ConfigError(f"{path}.{key}: nao pode ser negativo")
            total += float(weight)
        if total <= 0:
            raise ConfigError(f"{path}: pelo menos um peso precisa ser maior que zero")

    def _validate_time(self, value: Any, path: str) -> None:
        parts = str(value).split(":")
        if len(parts) != 2:
            raise ConfigError(f"{path}: use o formato HH:MM")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ConfigError(f"{path}: use o formato HH:MM") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ConfigError(f"{path}: horario invalido")

    def _validate_integer_mapping(self, value: dict[str, Any], path: str, *, minimum: int) -> None:
        for key, amount in value.items():
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < minimum:
                raise ConfigError(f"{path}.{key}: precisa ser um inteiro maior ou igual a {minimum}")

    def _validate_reward_entry(self, reward: Any, path: str) -> None:
        if not isinstance(reward, dict):
            raise ConfigError(f"{path}: precisa ser um objeto")
        self._string(reward, "grant_type", path)
        grant_type = str(reward["grant_type"])
        if grant_type != "currency_bundle":
            self._string(reward, "grant_id", path)
        for key, value in reward.items():
            if key == "weight":
                continue
            if key == "amount" or key.endswith("_amount"):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ConfigError(f"{path}.{key}: precisa ser um inteiro nao negativo")


_CONFIG_SERVICE = ConfigService()


def get_config_service() -> ConfigService:
    return _CONFIG_SERVICE


def get_main_config() -> dict[str, Any]:
    return get_config_service().load()


def get_public_events_config() -> dict[str, Any]:
    return get_config_service().get("public_events", default={})


def get_carousel_config() -> dict[str, Any]:
    return get_config_service().get("carousel", default={})
