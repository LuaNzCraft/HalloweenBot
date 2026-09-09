from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from cogs.public_events import PublicEventsCog
from services.observer_question_catalog import ObserverQuestionCatalog
from services.public_event_texts import PublicEventTextsService


def _cog() -> PublicEventsCog:
    cog = object.__new__(PublicEventsCog)
    cog.texts = PublicEventTextsService()
    cog.observer_catalog = ObserverQuestionCatalog(Path.cwd())
    return cog


class ObserverPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cog = _cog()
        self.deadline = discord.utils.utcnow() + timedelta(minutes=1)

    def _components(self, panel: discord.ui.LayoutView) -> str:
        self.addCleanup(panel.stop)
        self.assertTrue(panel.has_components_v2())
        self.assertLessEqual(panel.total_children_count, 40)
        return str(panel.to_components())

    def test_archive_question_uses_hidden_card_and_translate_button(self) -> None:
        question = {
            "id": "archive_a1b2c3",
            "kind": "chat_quote",
            "question": "Quem disse isso?",
            "question_en": "Who said this?",
        }

        panel = self.cog._build_observer_question_panel(
            question,
            self.deadline,
            "normalized",
            None,
            "medium",
            "observer_archive_hidden.png",
        )
        components = self._components(panel)

        self.assertIn("ARQUIVO DO OBSERVADOR", components)
        self.assertIn("attachment://observer_archive_hidden.png", components)
        self.assertIn("observer:translate", components)

    def test_archive_winner_uses_revealed_card_and_identity(self) -> None:
        question = {"id": "archive_a1b2c3", "kind": "chat_quote"}

        panel = self.cog._build_observer_winner_panel(
            question,
            winner_mention="<@40>",
            summary="10 doces",
            answer_label="Identidade confirmada",
            answer_value="Pessoa Teste",
            attachment_filename="observer_archive_revealed.png",
        )
        components = self._components(panel)

        self.assertIn("IDENTIDADE CONFIRMADA", components)
        self.assertIn("Pessoa Teste", components)
        self.assertIn("10 doces", components)
        self.assertIn("attachment://observer_archive_revealed.png", components)
        self.assertNotIn("observer:translate", components)

    def test_translation_is_an_ephemeral_ready_v2_panel(self) -> None:
        question = {
            "id": "observer_001",
            "kind": "text",
            "question_en": "Which horror franchise is this?",
            "match_mode": "normalized",
            "difficulty": "easy",
        }

        panel = self.cog.build_observer_translation_panel(question, self.deadline)

        self.assertIsNotNone(panel)
        components = self._components(panel)
        self.assertIn("OBSERVER TRANSMISSION", components)
        self.assertIn("Which horror franchise is this?", components)

    def test_exact_display_guardrails_survive_the_v2_migration(self) -> None:
        question = {
            "id": "observer_exact",
            "kind": "text",
            "question": "Repita o sinal.",
            "display_text": "nao olhe",
        }

        spaced_text = self.cog._resolve_observer_display_text(question, "spaces")
        spaced_panel = self.cog._build_observer_question_panel(
            question,
            self.deadline,
            "spaces",
            spaced_text,
            "hard",
        )
        spoiler_text = self.cog._resolve_observer_display_text(question, "spoiler")
        spoiler_panel = self.cog._build_observer_question_panel(
            question,
            self.deadline,
            "spoiler",
            spoiler_text,
            "hard",
        )

        spaced_components = self._components(spaced_panel)
        spoiler_components = self._components(spoiler_panel)
        spaced_copy = "\n".join(
            item.content
            for item in spaced_panel.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )

        self.assertTrue(spaced_components)
        self.assertTrue(spoiler_components)
        self.assertIn("\u2060", spaced_copy)
        self.assertIn("||nao||", spoiler_components)
        self.assertIn("||olhe||", spoiler_components)


class ObserverPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_payload_does_not_send_content_or_embed(self) -> None:
        cog = _cog()
        panel = cog._build_observer_timeout_panel(
            None,
            answer_label="Resposta correta",
            answer_value="`panico`",
        )
        self.addCleanup(panel.stop)
        webhook = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=30)))
        cog._get_or_create_observer_webhook = AsyncMock(return_value=webhook)

        result = await cog._send_observer_payload(
            SimpleNamespace(),
            {"webhook_name": "Observador"},
            view=panel,
            wait=True,
        )

        self.assertEqual(result.id, 30)
        kwargs = webhook.send.await_args.kwargs
        self.assertIs(kwargs["view"], panel)
        self.assertNotIn("content", kwargs)
        self.assertNotIn("embed", kwargs)
        self.assertEqual(
            kwargs["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )


if __name__ == "__main__":
    unittest.main()
