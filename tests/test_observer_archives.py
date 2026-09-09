from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import discord
from PIL import Image, ImageDraw

from cogs.observer_archives import ObserverArchivesCog
from cogs.public_events import PublicEventsCog
from repositories.observer_archives_repo import ObserverArchivesRepo
from repositories.public_events_repo import PublicEventsRepo
from services.observer_archive_service import ObserverArchiveService
from services.observer_question_catalog import ObserverQuestionCatalog
from services.public_event_runtime import PublicEventRuntimeService


def archive_payload(*, source_message_id: int = 30) -> dict:
    return {
        "public_id": "a1b2c3d4e5f6",
        "guild_id": 10,
        "source_channel_id": 20,
        "source_message_id": source_message_id,
        "author_id": 40,
        "author_display_name": "Pessoa Teste",
        "author_username": "pessoa.teste",
        "message_text": "essa cesta certamente nao e amaldicoada",
        "aliases": ["Pessoa Teste", "pessoa.teste"],
        "avatar_blob": None,
        "hidden_image_path": "cache/observer_archives/10/observer_a1b2c3d4e5f6_hidden.png",
        "revealed_image_path": "cache/observer_archives/10/observer_a1b2c3d4e5f6_revealed.png",
        "created_by": 50,
        "created_at": "2026-09-08T12:00:00+00:00",
    }


class ObserverArchiveCommandTests(unittest.TestCase):
    def test_archive_commands_are_grouped_under_observer_file(self) -> None:
        cog = ObserverArchivesCog(SimpleNamespace())

        qualified_names = [command.qualified_name for command in cog.walk_app_commands()]

        self.assertEqual(
            qualified_names,
            [
                "observador arquivo",
                "observador arquivo adicionar",
                "observador arquivo listar",
                "observador arquivo status",
            ],
        )


class ObserverArchivesRepoTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_snapshot_and_controls_active_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = ObserverArchivesRepo(Path(temp_dir) / "archives.sqlite3")
            created = await repo.create_archive(archive_payload())

            self.assertEqual(created["aliases"], ["Pessoa Teste", "pessoa.teste"])
            self.assertEqual(
                (await repo.get_by_question_id(10, "archive_a1b2c3d4e5f6"))["source_message_id"],
                30,
            )
            self.assertEqual(len(await repo.list_active(10)), 1)

            self.assertTrue(await repo.set_active(10, "a1b2c3d4e5f6", False))
            self.assertEqual(await repo.list_active(10), [])

    async def test_rejects_archiving_the_same_source_message_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = ObserverArchivesRepo(Path(temp_dir) / "archives.sqlite3")
            await repo.create_archive(archive_payload())

            with self.assertRaises(aiosqlite.IntegrityError):
                await repo.create_archive({**archive_payload(), "public_id": "ffffffffffff"})


class ObserverArchiveServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = ObserverArchiveService(self.root)
        self.draft = self.service.build_draft(
            guild_id=10,
            source_channel_id=20,
            source_message_id=30,
            author_id=40,
            author_display_name="Pessoa Teste",
            author_username="pessoa.teste",
            message_text="essa cesta certamente nao e amaldicoada",
            created_by=50,
            avatar_blob=None,
            extra_aliases="Pessoa, pessoa teste",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parses_only_discord_message_links(self) -> None:
        self.assertEqual(
            self.service.parse_message_link("https://discord.com/channels/10/20/30"),
            (10, 20, 30),
        )
        self.assertIsNone(self.service.parse_message_link("https://example.com/channels/10/20/30"))

    async def test_renders_hidden_and_revealed_cards_with_opaque_names(self) -> None:
        hidden_preview = await self.service.render_preview(self.draft)
        hidden_path, revealed_path = await self.service.render_archive_pair(
            self.draft,
            "cache/observer_archives",
        )

        with Image.open(BytesIO(hidden_preview)) as image:
            self.assertEqual(image.size, (1200, 630))
        self.assertNotIn(b"Pessoa Teste", hidden_preview)
        self.assertTrue((self.root / hidden_path).is_file())
        self.assertTrue((self.root / revealed_path).is_file())
        self.assertNotEqual((self.root / hidden_path).read_bytes(), (self.root / revealed_path).read_bytes())
        self.assertNotIn("pessoa", Path(hidden_path).name.lower())

    def test_wraps_a_single_long_word_inside_the_card_width(self) -> None:
        image = Image.new("RGB", (1200, 630))
        draw = ImageDraw.Draw(image)
        font = self.service._font(43)

        lines = self.service._wrap_text(draw, "a" * 500, font, 745)

        self.assertGreater(len(lines), 1)
        self.assertTrue(
            all(draw.textbbox((0, 0), line, font=font)[2] <= 745 for line in lines)
        )

    async def test_dynamic_question_enters_the_chat_quote_rotation(self) -> None:
        hidden_path, revealed_path = await self.service.render_archive_pair(
            self.draft,
            "cache/observer_archives",
        )
        repo = ObserverArchivesRepo(self.root / "archives.sqlite3")
        await repo.create_archive(
            {
                **archive_payload(),
                "public_id": self.draft.public_id,
                "hidden_image_path": hidden_path,
                "revealed_image_path": revealed_path,
            }
        )
        (self.root / hidden_path).unlink()
        (self.root / revealed_path).unlink()
        cog = object.__new__(PublicEventsCog)
        cog.observer_archives_repo = repo
        cog.observer_archive_service = self.service
        cog.observer_catalog = ObserverQuestionCatalog(self.root)
        cog.public_events_repo = PublicEventsRepo(self.root / "events.sqlite3")
        cog._load_config = lambda: {
            "observer": {
                "archives": {
                    "enabled": True,
                    "cache_directory": "cache/observer_archives",
                }
            }
        }
        config = {
            "archives": {"enabled": True, "cache_directory": "cache/observer_archives"},
            "round_type_weights": {"chat_quote": 100},
        }
        questions = await cog._load_observer_archive_questions(10, config)
        with patch("cogs.public_events.random.choices", return_value=["chat_quote"]):
            selected = await cog._pick_observer_question(10, questions, config)

        self.assertEqual(selected["kind"], "chat_quote")
        self.assertEqual(selected["answers"], ["Pessoa Teste", "pessoa.teste"])
        self.assertIsNotNone(cog._resolve_observer_image_path(selected))
        self.assertIsNotNone(cog._resolve_observer_revealed_image_path(selected))


class ObserverArchiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.question = {
            "id": "archive_a1b2c3d4e5f6",
            "kind": "chat_quote",
            "author_display_name": "Pessoa Teste",
        }
        self.revealed_path = Path("cache/observer_archives/10/revealed.png")

    async def test_winner_response_publishes_the_revealed_card(self) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        state = {
            "active": True,
            "channel_id": 20,
            "question_id": self.question["id"],
            "question_text": "Quem disse isso?",
            "accepted_answers": ["pessoa teste"],
            "answer_match_mode": "normalized",
            "question_difficulty": "medium",
            "expires_at": expires_at.isoformat(),
            "message_id": 30,
        }
        repo = SimpleNamespace(
            get_observer_state=AsyncMock(return_value=state),
            claim_observer_winner=AsyncMock(return_value=True),
        )
        channel = SimpleNamespace(id=20)
        guild = SimpleNamespace(id=10)
        message = SimpleNamespace(
            guild=guild,
            channel=channel,
            author=SimpleNamespace(id=40, mention="<@40>"),
            content="Pessoa Teste",
        )
        winner_panel = SimpleNamespace()
        cog = SimpleNamespace(
            _resolving_observer_guilds=set(),
            _observer_timeout_tasks={},
            public_events_repo=repo,
            _parse_dt=lambda value: datetime.fromisoformat(value),
            _prepare_observer_answer=lambda value, _mode: value.strip().casefold(),
            _load_config=lambda: {"observer": {}},
            _find_observer_question_for_state=AsyncMock(return_value=self.question),
            _build_observer_winner_panel=MagicMock(return_value=winner_panel),
            observer_catalog=ObserverQuestionCatalog(Path.cwd()),
            _resolve_observer_timeout_answer=lambda _question: (
                "Identidade confirmada",
                "Pessoa Teste",
            ),
            _resolve_observer_revealed_image_path=lambda _question: self.revealed_path,
            _resolve_observer_revealed_attachment_filename=lambda _question: "revealed.png",
            _send_observer_payload=AsyncMock(),
        )
        runtime = PublicEventRuntimeService()
        runtime.settle_observer_reward = AsyncMock(return_value=["10 doces"])

        await runtime.handle_observer_message(cog, message)

        sent = cog._send_observer_payload.await_args
        self.assertEqual(sent.kwargs["attachment_path"], self.revealed_path)
        self.assertEqual(sent.kwargs["attachment_filename"], "revealed.png")
        self.assertIs(sent.kwargs["view"], winner_panel)
        self.assertEqual(
            cog._build_observer_winner_panel.call_args.kwargs["answer_value"],
            "Pessoa Teste",
        )

    async def test_timeout_after_restart_publishes_the_revealed_card(self) -> None:
        expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        state = {
            "active": True,
            "channel_id": 20,
            "question_id": self.question["id"],
            "question_text": "Quem disse isso?",
            "accepted_answers": ["pessoa teste"],
            "answer_match_mode": "normalized",
            "question_difficulty": "medium",
            "started_at": (expires_at - timedelta(minutes=1)).isoformat(),
            "expires_at": expires_at.isoformat(),
            "message_id": 30,
        }
        repo = SimpleNamespace(
            get_observer_state=AsyncMock(return_value=state),
            set_observer_state=AsyncMock(),
        )
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 20
        guild = SimpleNamespace(id=10, get_channel=MagicMock(return_value=channel))
        timeout_panel = SimpleNamespace()
        cog = SimpleNamespace(
            _resolving_observer_guilds=set(),
            _observer_timeout_tasks={},
            public_events_repo=repo,
            bot=SimpleNamespace(get_guild=lambda _guild_id: guild),
            _load_config=lambda: {"observer": {}},
            _find_observer_question_for_state=AsyncMock(return_value=self.question),
            _resolve_observer_timeout_answer=lambda _question: (
                "Identidade confirmada",
                "Pessoa Teste",
            ),
            _resolve_observer_revealed_image_path=lambda _question: self.revealed_path,
            _resolve_observer_revealed_attachment_filename=lambda _question: "revealed.png",
            _build_observer_timeout_panel=MagicMock(return_value=timeout_panel),
            _send_observer_payload=AsyncMock(),
        )

        await PublicEventRuntimeService().finish_observer_if_unanswered(cog, 10, expires_at)

        sent = cog._send_observer_payload.await_args
        self.assertEqual(sent.kwargs["attachment_path"], self.revealed_path)
        self.assertEqual(sent.kwargs["attachment_filename"], "revealed.png")
        self.assertIs(sent.kwargs["view"], timeout_panel)
        self.assertEqual(
            cog._build_observer_timeout_panel.call_args.kwargs["answer_value"],
            "Pessoa Teste",
        )
        self.assertFalse(repo.set_observer_state.await_args.kwargs["active"])


if __name__ == "__main__":
    unittest.main()
