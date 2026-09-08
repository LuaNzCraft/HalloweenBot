from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import yaml

from services.config_service import CONFIG_PATH, ConfigError, ConfigService


class ConfigServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "halloween.yml"
        self.valid_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        self._write(self.valid_config)
        self.service = ConfigService(self.path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, payload: dict) -> None:
        self.path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def test_loads_the_central_yaml(self) -> None:
        config = self.service.reload()

        self.assertIn("public_events", config)
        self.assertIn("carousel", config)
        self.assertIn("chat_candy", config)

    def test_invalid_hot_edit_keeps_the_last_valid_config(self) -> None:
        original = self.service.reload()
        invalid = deepcopy(self.valid_config)
        invalid["chat_candy"]["spawn_chance_percent"] = 101
        self._write(invalid)

        loaded = self.service.load()

        self.assertEqual(loaded["chat_candy"], original["chat_candy"])

    def test_manual_reload_reports_the_exact_invalid_field(self) -> None:
        self.service.reload()
        invalid = deepcopy(self.valid_config)
        invalid["chat_candy"]["min_options"] = 2
        self._write(invalid)

        with self.assertRaisesRegex(ConfigError, "config.chat_candy.min_options"):
            self.service.reload()

    def test_nan_is_rejected(self) -> None:
        self.path.write_text(
            CONFIG_PATH.read_text(encoding="utf-8").replace(
                "spawn_chance_percent: 60",
                "spawn_chance_percent: .nan",
                1,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "NaN e infinito"):
            self.service.reload()

    def test_admin_mute_toggle_must_be_boolean(self) -> None:
        invalid = deepcopy(self.valid_config)
        invalid["effects"]["admin_mute_delete_messages"] = "true"
        self._write(invalid)

        with self.assertRaisesRegex(ConfigError, "config.effects.admin_mute_delete_messages"):
            self.service.reload()

    def test_shop_rejects_an_unknown_grant_type(self) -> None:
        invalid = deepcopy(self.valid_config)
        invalid["shop"]["dado_extra"]["grant_type"] = "tipo_inexistente"
        self._write(invalid)

        with self.assertRaisesRegex(ConfigError, "config.shop.dado_extra.grant_type"):
            self.service.reload()

    def test_shop_rejects_an_item_that_cannot_be_granted(self) -> None:
        invalid = deepcopy(self.valid_config)
        invalid["shop"]["cesta_abobora"]["grant_id"] = "item_inexistente"
        self._write(invalid)

        with self.assertRaisesRegex(ConfigError, "config.shop.cesta_abobora.grant_id"):
            self.service.reload()

    def test_shop_price_must_be_positive(self) -> None:
        invalid = deepcopy(self.valid_config)
        invalid["shop"]["dado_extra"]["price"] = 0
        self._write(invalid)

        with self.assertRaisesRegex(ConfigError, "config.shop.dado_extra.price"):
            self.service.reload()

    def test_shop_role_quantity_must_be_one(self) -> None:
        invalid = deepcopy(self.valid_config)
        invalid["shop"]["cargo_loja"]["grant_quantity"] = 2
        self._write(invalid)

        with self.assertRaisesRegex(ConfigError, "config.shop.cargo_loja.grant_quantity"):
            self.service.reload()

    def test_observer_archive_message_limit_is_validated(self) -> None:
        invalid = deepcopy(self.valid_config)
        invalid["public_events"]["observer"]["archives"]["max_message_length"] = 1001
        self._write(invalid)

        with self.assertRaisesRegex(
            ConfigError,
            "config.public_events.observer.archives.max_message_length",
        ):
            self.service.reload()

    def test_observer_archive_cache_must_stay_inside_cache(self) -> None:
        invalid = deepcopy(self.valid_config)
        invalid["public_events"]["observer"]["archives"]["cache_directory"] = "data/archives"
        self._write(invalid)

        with self.assertRaisesRegex(
            ConfigError,
            "config.public_events.observer.archives.cache_directory",
        ):
            self.service.reload()

    def test_duplicate_yaml_keys_are_rejected(self) -> None:
        self.path.write_text(
            CONFIG_PATH.read_text(encoding="utf-8") + "\nchat_candy: {}\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "chave duplicada"):
            self.service.reload()

    def test_yaml_aliases_are_rejected(self) -> None:
        text = CONFIG_PATH.read_text(encoding="utf-8").replace(
            "event_channel_ids: []",
            "event_channel_ids: &shared_channels []",
            1,
        )
        self.path.write_text(text + "\nalias_copy: *shared_channels\n", encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "aliases YAML"):
            self.service.reload()


if __name__ == "__main__":
    unittest.main()
