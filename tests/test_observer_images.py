from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from cogs.julgamento import JulgamentoCog
from cogs.public_events import PublicEventsCog
from repositories.julgamento_repo import JulgamentoRepo
from repositories.public_events_repo import PublicEventsRepo
from services.observer_question_catalog import ObserverQuestionCatalog
from services.julgamento_texts import JulgamentoTextsService
from services.public_event_runtime import PublicEventRuntimeService
from services.public_event_texts import PublicEventTextsService


class ObserverSchedulerTests(unittest.TestCase):
    def test_uses_observer_as_the_public_event_name(self) -> None:
        cog = object.__new__(PublicEventsCog)
        config = {
            "scheduler": {"fixed_event_name": "Observador"},
            "observer": {"enabled": True},
            "candy_shower": {"enabled": False},
        }

        with patch("cogs.public_events.get_carousel_config", return_value={"enabled": False}):
            self.assertEqual(cog._enabled_event_names(config), ["Observador"])
            self.assertEqual(cog._pick_scheduled_event(config), "Observador")

class ObserverQuestionCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "content" / "halloween").mkdir(parents=True)
        (self.root / "Assets" / "Observador" / "Movies").mkdir(parents=True)
        self.catalog = ObserverQuestionCatalog(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_json(self, relative_path: str, payload: object) -> None:
        path = self.root / relative_path
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_loads_legacy_text_and_movie_image_sources(self) -> None:
        image_path = self.root / "Assets" / "Observador" / "Movies" / "frame_001.webp"
        image_path.write_bytes(b"test-image")
        self._write_json(
            "content/halloween/text.json",
            [{"id": "text_1", "question": "Pergunta?", "answers": ["resposta"]}],
        )
        self._write_json(
            "content/halloween/images.json",
            [
                {
                    "id": "movie_1",
                    "kind": "movie_image",
                    "question": "Que filme e esse?",
                    "image_path": "Assets/Observador/Movies/frame_001.webp",
                    "answers": ["Filme"],
                }
            ],
        )

        questions = self.catalog.load(
            {
                "question_sources": [
                    "content/halloween/text.json",
                    "content/halloween/images.json",
                ]
            }
        )

        self.assertEqual([question["kind"] for question in questions], ["text", "movie_image"])
        self.assertEqual(self.catalog.image_path(questions[1]), image_path.resolve())
        self.assertEqual(self.catalog.attachment_filename(questions[1]), "movie_1.webp")

    def test_rejects_image_outside_the_curated_media_directory(self) -> None:
        outside_path = self.root / "secret.webp"
        outside_path.write_bytes(b"not-allowed")
        self._write_json(
            "content/halloween/images.json",
            [
                {
                    "id": "movie_unsafe",
                    "kind": "movie_image",
                    "question": "Que filme e esse?",
                    "image_path": "secret.webp",
                    "answers": ["Filme"],
                }
            ],
        )

        questions = self.catalog.load({"question_source": "content/halloween/images.json"})

        self.assertEqual(questions, [])

    def test_question_id_disambiguates_equal_prompts(self) -> None:
        questions = [
            {"id": "movie_1", "question": "Que filme e esse?"},
            {"id": "movie_2", "question": "Que filme e esse?"},
        ]

        selected = self.catalog.find_by_id(questions, "movie_2")

        self.assertEqual(selected, questions[1])


class ObserverRuntimePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_repository_persists_question_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = PublicEventsRepo(Path(temp_dir) / "observer.sqlite3")
            await repo.set_observer_state(
                10,
                active=True,
                channel_id=20,
                question_id="movie_001",
                question_text="Que filme e esse?",
                accepted_answers=["panico"],
                answer_match_mode="normalized",
                question_difficulty="easy",
                started_at="2026-09-05T20:00:00+00:00",
                expires_at="2026-09-05T20:01:00+00:00",
                message_id=30,
                winner_user_id=None,
            )

            state = await repo.get_observer_state(10)

            self.assertEqual(state["question_id"], "movie_001")

    async def test_weighted_rotation_selects_only_from_chosen_kind(self) -> None:
        cog = object.__new__(PublicEventsCog)
        cog.observer_catalog = ObserverQuestionCatalog(Path.cwd())
        cog.public_events_repo = SimpleNamespace(
            get_observer_used_question_ids=AsyncMock(return_value=["movie_1"]),
            set_observer_used_question_ids=AsyncMock(),
        )
        questions = [
            {"id": "text_1", "kind": "text"},
            {"id": "movie_1", "kind": "movie_image"},
        ]

        with patch("cogs.public_events.random.choices", return_value=["movie_image"]):
            selected = await cog._pick_observer_question(
                10,
                questions,
                {"round_type_weights": {"text": 70, "movie_image": 30}},
            )

        self.assertEqual(selected["id"], "movie_1")
        cog.public_events_repo.set_observer_used_question_ids.assert_awaited_once_with(10, ["movie_1"])

    async def test_image_round_reuses_the_existing_runtime_with_an_attachment(self) -> None:
        class ClosingLoop:
            @staticmethod
            def create_task(coroutine):
                coroutine.close()
                return MagicMock()

        image_path = Path("Assets/Observador/Movies/movie_1.webp")
        question = {
            "id": "movie_1",
            "kind": "movie_image",
            "question": "Que filme e esse?",
            "answers": ["panico"],
        }
        repo = SimpleNamespace(
            get_observer_state=AsyncMock(return_value={"active": False}),
            set_observer_state=AsyncMock(),
        )
        cog = SimpleNamespace(
            _get_event_channels=AsyncMock(return_value=[SimpleNamespace(id=20)]),
            public_events_repo=repo,
            _load_observer_questions=MagicMock(return_value=[question]),
            _load_observer_archive_questions=AsyncMock(return_value=[]),
            _pick_observer_question=AsyncMock(return_value=question),
            _resolve_observer_match_mode=MagicMock(return_value="normalized"),
            _resolve_observer_difficulty=MagicMock(return_value="easy"),
            _prepare_observer_answer=MagicMock(side_effect=lambda value, _mode: value),
            _expand_observer_answers=MagicMock(return_value=["panico"]),
            _resolve_observer_display_text=MagicMock(return_value=None),
            _resolve_observer_image_path=MagicMock(return_value=image_path),
            _resolve_observer_attachment_filename=MagicMock(return_value="movie_1.webp"),
            _build_observer_question_embed=MagicMock(return_value=discord.Embed()),
            _send_observer_payload=AsyncMock(return_value=SimpleNamespace(id=30)),
            _observer_timeout_tasks={},
            bot=SimpleNamespace(loop=ClosingLoop()),
        )

        ok, reason = await PublicEventRuntimeService().start_observer(
            cog,
            SimpleNamespace(id=10),
            {"observer": {"answer_timeout_seconds": 90}},
        )

        self.assertEqual((ok, reason), (True, "ok"))
        self.assertEqual(cog._send_observer_payload.await_args.kwargs["attachment_path"], image_path)
        self.assertEqual(cog._send_observer_payload.await_args.kwargs["attachment_filename"], "movie_1.webp")
        self.assertEqual(repo.set_observer_state.await_args.kwargs["question_id"], "movie_1")


class ObserverImageEmbedTests(unittest.TestCase):
    def test_question_embed_references_the_attached_image(self) -> None:
        embed = PublicEventTextsService().build_observer_question_embed(
            0x780C0C,
            question_text="Que filme aparece nesta imagem?",
            deadline_at=discord.utils.utcnow(),
            match_mode="normalized",
            display_text=None,
            difficulty="medium",
            attachment_filename="movie_001.webp",
        )

        self.assertEqual(embed.title, "📹 O Observador esta gravando")
        self.assertEqual(embed.image.url, "attachment://movie_001.webp")


class CarrosselImageQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_repository_persists_current_question_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = JulgamentoRepo(Path(temp_dir) / "carrossel.sqlite3")
            await repo.set_runtime(
                10,
                active=True,
                phase="countdown",
                current_question_id="movie_001",
                current_question_text="Que filme e esse?",
            )

            state = await repo.get_runtime(10)

            self.assertEqual(state["current_question_id"], "movie_001")

    async def test_carrossel_sends_the_image_as_an_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_dir = root / "Assets" / "Observador" / "Movies"
            media_dir.mkdir(parents=True)
            image_path = media_dir / "frame.webp"
            image_path.write_bytes(b"test-image")
            question = {
                "id": "movie_001",
                "kind": "movie_image",
                "question": "Que filme e esse?",
                "image_path": "Assets/Observador/Movies/frame.webp",
                "answers": ["filme"],
            }
            cog = object.__new__(JulgamentoCog)
            cog.question_catalog = ObserverQuestionCatalog(root)
            channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=30)))

            sent = await cog._send_round_question_payload(
                channel,
                embed=discord.Embed(),
                view=discord.ui.View(timeout=1),
                question=question,
            )

            self.assertEqual(sent.id, 30)
            attachment = channel.send.await_args.kwargs["file"]
            self.assertEqual(attachment.filename, "movie_001.webp")

    async def test_carrossel_weight_can_select_an_image_round(self) -> None:
        cog = object.__new__(JulgamentoCog)
        cog.question_catalog = ObserverQuestionCatalog(Path.cwd())
        cog._load_questions = MagicMock(
            return_value=[
                {"id": "text_1", "kind": "text", "difficulty": "easy"},
                {"id": "movie_1", "kind": "movie_image", "difficulty": "easy"},
            ]
        )
        cog._load_config = MagicMock(return_value={"round_type_weights": {"text": 65, "movie_image": 35}})

        with patch("cogs.julgamento.random.choices", return_value=["movie_image"]):
            selected = cog._pick_question_by_difficulty("easy")

        self.assertEqual(selected["id"], "movie_1")

    def test_carrossel_embed_references_the_attached_image(self) -> None:
        def translate(_locale: str, key: str, **_kwargs: object) -> str:
            return key

        embed = JulgamentoTextsService().build_round_question_embed(
            translate,
            discord.Color.red(),
            {},
            {"question": "Que filme e esse?"},
            None,
            25,
            attachment_filename="movie_001.webp",
        )

        self.assertEqual(embed.image.url, "attachment://movie_001.webp")


if __name__ == "__main__":
    unittest.main()
