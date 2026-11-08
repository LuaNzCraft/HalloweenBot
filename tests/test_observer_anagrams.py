from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord
from PIL import Image

from cogs.public_events import PublicEventsCog
from services.observer_anagram_card_service import ObserverAnagramCardService
from services.observer_question_catalog import ObserverQuestionCatalog
from services.items_runtime import ItemsRuntimeService
from services.public_event_runtime import PublicEventRuntimeService
from services.public_event_texts import PublicEventTextsService
from views.public_event_views import ObserverPanelView


class ObserverAnagramCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "content" / "halloween").mkdir(parents=True)
        self.catalog = ObserverQuestionCatalog(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_questions(self, questions: list[dict]) -> None:
        path = self.root / "content" / "halloween" / "anagrams.json"
        path.write_text(json.dumps(questions), encoding="utf-8")

    def test_builds_a_stable_display_separate_from_the_answer(self) -> None:
        self._write_questions(
            [
                {
                    "id": "signal_001",
                    "kind": "anagram",
                    "question": "Reconstrua o sinal.",
                    "answers": ["Pennywise"],
                }
            ]
        )

        first = self.catalog.load({"question_source": "content/halloween/anagrams.json"})
        second = self.catalog.load({"question_source": "content/halloween/anagrams.json"})

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["kind"], "anagram")
        self.assertEqual(first[0]["match_mode"], "anagram")
        self.assertEqual(first[0]["display_text"], second[0]["display_text"])
        self.assertNotEqual(first[0]["display_text"].replace(" ", "").casefold(), "pennywise")
        self.assertEqual(
            sorted(first[0]["display_text"].replace(" ", "").casefold()),
            sorted("pennywise"),
        )

    def test_rejects_a_curated_display_with_the_wrong_letters(self) -> None:
        self._write_questions(
            [
                {
                    "id": "signal_unsafe",
                    "kind": "anagram",
                    "question": "Reconstrua o sinal.",
                    "display_text": "LETRAS ERRADAS",
                    "answers": ["Pennywise"],
                }
            ]
        )

        questions = self.catalog.load({"question_source": "content/halloween/anagrams.json"})

        self.assertEqual(questions, [])

    def test_project_catalog_contains_at_least_thirty_valid_anagrams(self) -> None:
        root = Path(__file__).resolve().parent.parent
        catalog = ObserverQuestionCatalog(root)

        questions = catalog.load(
            {"question_source": "content/halloween/observer_anagram_questions.json"}
        )

        self.assertGreaterEqual(len(questions), 30)
        self.assertTrue(all(question["kind"] == "anagram" for question in questions))


class ObserverAnagramCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_renders_an_opaque_cached_png(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ObserverAnagramCardService(Path(temp_dir))

            first_path, first_filename = await service.render("secret_answer_id", "WYSE ENIP")
            second_path, second_filename = await service.render("secret_answer_id", "WYSE ENIP")

            self.assertEqual((first_path, first_filename), (second_path, second_filename))
            self.assertTrue(first_path.is_file())
            self.assertNotIn("secret_answer_id", first_filename)
            self.assertNotIn("pennywise", first_filename)
            self.assertTrue(first_filename.startswith("observer_signal_"))
            with Image.open(first_path) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (1200, 630))

    async def test_cog_returns_no_attachment_when_the_renderer_fails(self) -> None:
        cog = object.__new__(PublicEventsCog)
        cog.observer_anagram_card_service = SimpleNamespace(
            render=AsyncMock(side_effect=ValueError("invalid"))
        )

        path, filename = await cog._render_observer_anagram_attachment(
            {"id": "signal_001"},
            "ABCD",
        )

        self.assertIsNone(path)
        self.assertIsNone(filename)


class ObserverAnagramRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_renders_anagram_instead_of_resolving_a_movie_image(self) -> None:
        class ClosingLoop:
            @staticmethod
            def create_task(coroutine):
                coroutine.close()
                return MagicMock()

        question = {
            "id": "signal_001",
            "kind": "anagram",
            "question": "Reconstrua.",
            "answers": ["pennywise"],
            "display_text": "WYSE ENIP",
        }
        image_path = Path("cache/observer_anagrams/observer_signal_hash.png")
        repo = SimpleNamespace(
            get_observer_state=AsyncMock(return_value={"active": False}),
            set_observer_state=AsyncMock(),
        )
        question_panel = SimpleNamespace(message=None)
        cog = SimpleNamespace(
            _get_event_channels=AsyncMock(return_value=[SimpleNamespace(id=20)]),
            public_events_repo=repo,
            _load_observer_questions=MagicMock(return_value=[question]),
            _load_observer_archive_questions=AsyncMock(return_value=[]),
            _pick_observer_question=AsyncMock(return_value=question),
            _resolve_observer_match_mode=MagicMock(return_value="anagram"),
            _resolve_observer_difficulty=MagicMock(return_value="easy"),
            _prepare_observer_answer=MagicMock(return_value="pennywise"),
            _expand_observer_answers=MagicMock(return_value=["pennywise"]),
            _resolve_observer_display_text=MagicMock(return_value="WYSE ENIP"),
            _render_observer_anagram_attachment=AsyncMock(
                return_value=(image_path, "observer_signal_hash.png")
            ),
            _resolve_observer_image_path=MagicMock(),
            _resolve_observer_attachment_filename=MagicMock(),
            _build_observer_question_panel=MagicMock(return_value=question_panel),
            _send_observer_payload=AsyncMock(return_value=SimpleNamespace(id=30)),
            _observer_timeout_tasks={},
            bot=SimpleNamespace(loop=ClosingLoop()),
        )

        result = await PublicEventRuntimeService().start_observer(
            cog,
            SimpleNamespace(id=10),
            {"observer": {"answer_timeout_seconds": 90}},
        )

        self.assertEqual(result, (True, "ok"))
        cog._render_observer_anagram_attachment.assert_awaited_once_with(question, "WYSE ENIP")
        self.assertEqual(
            cog._send_observer_payload.await_args.kwargs["attachment_path"],
            image_path,
        )
        self.assertIs(cog._send_observer_payload.await_args.kwargs["view"], question_panel)
        self.assertEqual(question_panel.message.id, 30)
        cog._resolve_observer_image_path.assert_not_called()

    def test_anagram_uses_normalized_answer_validation(self) -> None:
        cog = object.__new__(PublicEventsCog)

        mode = cog._resolve_observer_match_mode({"match_mode": "anagram"})

        self.assertEqual(mode, "anagram")
        self.assertEqual(cog._prepare_observer_answer("  PÂNICO!  ", mode), "panico")

    def test_timeout_and_item_hint_use_the_canonical_answer(self) -> None:
        catalog = ObserverQuestionCatalog(Path.cwd())
        question = {
            "id": "signal_001",
            "kind": "anagram",
            "match_mode": "anagram",
            "display_text": "SREY MEIC HAML",
            "answers": ["michael myers", "myers"],
        }
        cog = object.__new__(PublicEventsCog)
        cog.observer_catalog = catalog
        items_runtime = object.__new__(ItemsRuntimeService)
        items_runtime.observer_catalog = catalog

        _, timeout_answer = cog._resolve_observer_timeout_answer(question)

        self.assertEqual(timeout_answer, "`michael myers`")
        self.assertEqual(items_runtime.select_hint_answer(question, "pt-BR"), "michael myers")


class ObserverAnagramPanelTests(unittest.TestCase):
    def test_panel_uses_the_card_without_repeating_copyable_letters(self) -> None:
        content = PublicEventTextsService().build_observer_question_panel_content(
            question_text="Reconstrua o sinal.",
            deadline_at=discord.utils.utcnow(),
            match_mode="anagram",
            display_text="WYSE ENIP",
            difficulty="medium",
            question_kind="anagram",
            has_attachment=True,
        )
        panel = ObserverPanelView(
            content,
            color=discord.Color.red(),
            attachment_filename="observer_signal_hash.png",
        )
        self.addCleanup(panel.stop)
        components = str(panel.to_components())

        self.assertTrue(panel.has_components_v2())
        self.assertIn("attachment://observer_signal_hash.png", components)
        self.assertIn("Reorganize o sinal", components)
        self.assertNotIn("WYSE ENIP", components)


if __name__ == "__main__":
    unittest.main()
