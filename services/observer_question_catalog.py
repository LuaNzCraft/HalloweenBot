from __future__ import annotations

import json
from hashlib import sha256
import logging
import random
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path


logger = logging.getLogger(__name__)

DEFAULT_QUESTION_SOURCE = "content/halloween/observer_questions.json"
DEFAULT_MEDIA_DIRECTORY = "Assets/Observador/Movies"
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ObserverQuestionCatalog:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.media_dir = (self.root_dir / DEFAULT_MEDIA_DIRECTORY).resolve()

    def load(self, observer_cfg: dict) -> list[dict]:
        questions: list[dict] = []
        seen_ids: set[str] = set()

        for source in self._question_sources(observer_cfg):
            path = self._resolve_source_path(source)
            if path is None or not path.is_file():
                logger.warning("Fonte de perguntas do Observador nao encontrada: %s", source)
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.exception("Falha ao carregar fonte de perguntas do Observador: %s", path)
                continue
            if not isinstance(payload, list):
                logger.warning("Fonte do Observador precisa conter uma lista: %s", path)
                continue

            for raw_question in payload:
                question = self._validate_question(raw_question, source=path)
                if question is None:
                    continue
                question_id = str(question["id"])
                if question_id in seen_ids:
                    logger.warning("ID duplicado ignorado no catalogo do Observador: %s", question_id)
                    continue
                seen_ids.add(question_id)
                questions.append(question)

        return questions

    def find_by_id(self, questions: list[dict], question_id: str | None) -> dict | None:
        normalized_id = str(question_id or "").strip()
        if not normalized_id:
            return None
        return next(
            (question for question in questions if str(question.get("id") or "").strip() == normalized_id),
            None,
        )

    def find_by_text(
        self,
        questions: list[dict],
        question_text: str | None,
        normalizer: Callable[[str], str],
    ) -> dict | None:
        normalized_question = normalizer(str(question_text or ""))
        if not normalized_question:
            return None
        for question in questions:
            for candidate in (str(question.get("question") or ""), str(question.get("question_en") or "")):
                if candidate and normalizer(candidate) == normalized_question:
                    return question
        return None

    def kind(self, question: dict) -> str:
        value = str(question.get("kind") or "text").strip().lower()
        if value in {"image", "movie_image"}:
            return "movie_image"
        if value == "chat_quote":
            return "chat_quote"
        if value == "anagram":
            return "anagram"
        return "text"

    def image_path(self, question: dict) -> Path | None:
        if self.kind(question) != "movie_image":
            return None
        relative_path = str(question.get("image_path") or "").strip()
        return self._resolve_image_path(relative_path)

    def attachment_filename(self, question: dict) -> str | None:
        image_path = self.image_path(question)
        if image_path is None:
            return None
        question_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(question.get("id") or "movie"))
        return f"{question_id}{image_path.suffix.lower()}"

    def _question_sources(self, observer_cfg: dict) -> list[str]:
        configured_sources = observer_cfg.get("question_sources")
        if isinstance(configured_sources, list):
            sources = [str(value).strip() for value in configured_sources if str(value).strip()]
            if sources:
                return sources
        fallback = str(observer_cfg.get("question_source") or DEFAULT_QUESTION_SOURCE).strip()
        return [fallback]

    def _resolve_source_path(self, relative_path: str) -> Path | None:
        candidate = (self.root_dir / relative_path).resolve()
        if not candidate.is_relative_to(self.root_dir):
            logger.warning("Fonte fora da raiz do projeto ignorada: %s", relative_path)
            return None
        return candidate

    def _resolve_image_path(self, relative_path: str) -> Path | None:
        if not relative_path:
            return None
        candidate = (self.root_dir / relative_path).resolve()
        if not candidate.is_relative_to(self.media_dir):
            return None
        if candidate.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            return None
        if not candidate.is_file():
            return None
        try:
            if candidate.stat().st_size > MAX_IMAGE_BYTES:
                return None
        except OSError:
            return None
        return candidate

    def _validate_question(self, raw_question: object, *, source: Path) -> dict | None:
        if not isinstance(raw_question, dict):
            logger.warning("Entrada invalida ignorada no catalogo do Observador: %s", source)
            return None
        if raw_question.get("enabled") is False:
            return None

        question = dict(raw_question)
        question_id = str(question.get("id") or "").strip()
        question_text = str(question.get("question") or "").strip()
        if not question_id or not question_text:
            logger.warning("Pergunta sem id ou enunciado ignorada em %s", source)
            return None

        question["id"] = question_id
        question["kind"] = self.kind(question)
        if question["kind"] == "movie_image" and self.image_path(question) is None:
            logger.warning("Imagem invalida ou ausente para a pergunta %s", question_id)
            return None

        answers = question.get("answers")
        has_answers = isinstance(answers, list) and any(str(answer).strip() for answer in answers)
        if not has_answers and not str(question.get("answer_pool") or "").strip():
            logger.warning("Pergunta sem respostas ignorada: %s", question_id)
            return None

        if question["kind"] == "anagram":
            canonical_answer = next(
                (str(answer).strip() for answer in answers if str(answer).strip()),
                "",
            )
            if not canonical_answer:
                logger.warning("Anagrama sem resposta canonica ignorado: %s", question_id)
                return None
            display_text = str(question.get("display_text") or "").strip()
            if not display_text:
                display_text = self._build_anagram_display(question_id, canonical_answer)
            if (
                not display_text
                or len(display_text) > 96
                or self._anagram_signature(display_text) != self._anagram_signature(canonical_answer)
            ):
                logger.warning("Letras invalidas no anagrama %s", question_id)
                return None
            question["match_mode"] = "anagram"
            question["display_text"] = display_text
            question["display_text_en"] = str(question.get("display_text_en") or display_text).strip()
        return question

    @classmethod
    def _build_anagram_display(cls, question_id: str, canonical_answer: str) -> str:
        letters = [char for char in canonical_answer if char.isalnum()]
        if len(letters) < 2:
            return ""
        original = "".join(letters)
        rng = random.Random(sha256(question_id.encode("utf-8")).digest())
        for _ in range(8):
            rng.shuffle(letters)
            if "".join(letters).casefold() != original.casefold():
                break
        else:
            letters = letters[1:] + letters[:1]
        return " ".join("".join(letters[index:index + 4]) for index in range(0, len(letters), 4))

    @staticmethod
    def _anagram_signature(value: str) -> str:
        normalized = unicodedata.normalize("NFD", str(value).casefold())
        return "".join(
            sorted(
                char
                for char in normalized
                if char.isalnum() and unicodedata.category(char) != "Mn"
            )
        )
