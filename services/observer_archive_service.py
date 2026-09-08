from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import random
import re
import secrets
import uuid

from PIL import Image, ImageDraw, ImageFont, ImageOps


MESSAGE_LINK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/(?P<guild_id>\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)/?$",
    re.IGNORECASE,
)
IMAGE_SIZE = (1200, 630)
MAX_AVATAR_BYTES = 8 * 1024 * 1024
MAX_CARD_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ObserverArchiveDraft:
    public_id: str
    guild_id: int
    source_channel_id: int
    source_message_id: int
    author_id: int
    author_display_name: str
    author_username: str
    message_text: str
    aliases: tuple[str, ...]
    avatar_blob: bytes | None
    created_by: int
    created_at: str

    def edited(self, *, message_text: str, aliases: tuple[str, ...]) -> "ObserverArchiveDraft":
        return replace(self, message_text=message_text, aliases=aliases)


class ObserverArchiveService:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()

    @staticmethod
    def parse_message_link(value: str) -> tuple[int, int, int] | None:
        match = MESSAGE_LINK_RE.fullmatch(str(value or "").strip())
        if match is None:
            return None
        return tuple(int(match.group(name)) for name in ("guild_id", "channel_id", "message_id"))

    @staticmethod
    def new_public_id() -> str:
        return secrets.token_hex(6)

    @staticmethod
    def normalize_aliases(*values: str, extra: str | None = None) -> tuple[str, ...]:
        candidates = list(values)
        if extra:
            candidates.extend(extra.split(","))
        aliases: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            alias = " ".join(str(candidate or "").strip().lstrip("@").split())
            normalized = alias.casefold()
            if not alias or normalized in seen:
                continue
            seen.add(normalized)
            aliases.append(alias[:80])
        return tuple(aliases[:12])

    def build_draft(
        self,
        *,
        guild_id: int,
        source_channel_id: int,
        source_message_id: int,
        author_id: int,
        author_display_name: str,
        author_username: str,
        message_text: str,
        created_by: int,
        avatar_blob: bytes | None,
        extra_aliases: str | None = None,
        max_message_length: int = 500,
    ) -> ObserverArchiveDraft:
        cleaned_text = " ".join(str(message_text or "").replace("\x00", "").split())
        safe_limit = min(max(40, int(max_message_length)), 1000)
        if not cleaned_text:
            raise ValueError("A mensagem precisa ter conteudo textual.")
        if len(cleaned_text) > safe_limit:
            raise ValueError(f"A mensagem ultrapassa o limite de {safe_limit} caracteres.")
        display_name = " ".join(str(author_display_name or "").split())[:80]
        username = " ".join(str(author_username or "").split())[:80]
        aliases = self.normalize_aliases(display_name, username, extra=extra_aliases)
        if not display_name or not username or not aliases:
            raise ValueError("Nao foi possivel capturar uma identidade valida para o autor.")
        if avatar_blob is not None and len(avatar_blob) > MAX_AVATAR_BYTES:
            avatar_blob = None
        return ObserverArchiveDraft(
            public_id=self.new_public_id(),
            guild_id=int(guild_id),
            source_channel_id=int(source_channel_id),
            source_message_id=int(source_message_id),
            author_id=int(author_id),
            author_display_name=display_name,
            author_username=username,
            message_text=cleaned_text,
            aliases=aliases,
            avatar_blob=avatar_blob,
            created_by=int(created_by),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    async def render_preview(self, draft: ObserverArchiveDraft) -> bytes:
        return await asyncio.to_thread(self._render_card_bytes, draft, False)

    async def render_archive_pair(
        self,
        draft: ObserverArchiveDraft,
        cache_directory: str,
    ) -> tuple[str, str]:
        cache_dir = self._resolve_cache_directory(cache_directory)
        return await asyncio.to_thread(self._render_pair_sync, draft, cache_dir)

    def resolve_cached_image(self, relative_path: str, cache_directory: str) -> Path | None:
        cache_dir = self._resolve_cache_directory(cache_directory)
        candidate = (self.root_dir / str(relative_path or "")).resolve()
        if not candidate.is_relative_to(cache_dir) or candidate.suffix.lower() != ".png":
            return None
        if not candidate.is_file():
            return None
        try:
            if candidate.stat().st_size > MAX_CARD_BYTES:
                return None
        except OSError:
            return None
        return candidate

    async def remove_cached_images(self, paths: tuple[str, ...], cache_directory: str) -> None:
        cache_dir = self._resolve_cache_directory(cache_directory)

        def remove() -> None:
            for relative_path in paths:
                candidate = (self.root_dir / relative_path).resolve()
                if candidate.is_relative_to(cache_dir):
                    candidate.unlink(missing_ok=True)

        await asyncio.to_thread(remove)

    @staticmethod
    def archive_to_question(archive: dict) -> dict:
        public_id = str(archive["public_id"])
        return {
            "id": f"archive_{public_id}",
            "kind": "chat_quote",
            "question": "Quem disse isso?",
            "question_en": "Who said this?",
            "answers": list(archive.get("aliases") or []),
            "match_mode": "normalized",
            "difficulty": "medium",
            "image_path": str(archive["hidden_image_path"]),
            "revealed_image_path": str(archive["revealed_image_path"]),
            "archive_public_id": public_id,
            "author_display_name": str(archive["author_display_name"]),
            "author_username": str(archive["author_username"]),
        }

    @staticmethod
    def draft_from_archive(archive: dict) -> ObserverArchiveDraft:
        return ObserverArchiveDraft(
            public_id=str(archive["public_id"]),
            guild_id=int(archive["guild_id"]),
            source_channel_id=int(archive["source_channel_id"]),
            source_message_id=int(archive["source_message_id"]),
            author_id=int(archive["author_id"]),
            author_display_name=str(archive["author_display_name"]),
            author_username=str(archive["author_username"]),
            message_text=str(archive["message_text"]),
            aliases=tuple(str(value) for value in archive.get("aliases") or []),
            avatar_blob=archive.get("avatar_blob"),
            created_by=int(archive["created_by"]),
            created_at=str(archive["created_at"]),
        )

    def _resolve_cache_directory(self, relative_path: str) -> Path:
        value = str(relative_path or "cache/observer_archives").strip()
        candidate = (self.root_dir / value).resolve()
        cache_root = (self.root_dir / "cache").resolve()
        if not candidate.is_relative_to(cache_root):
            raise ValueError("O cache dos arquivos precisa ficar dentro de cache/.")
        return candidate

    def _render_pair_sync(self, draft: ObserverArchiveDraft, cache_dir: Path) -> tuple[str, str]:
        guild_dir = (cache_dir / str(draft.guild_id)).resolve()
        if not guild_dir.is_relative_to(cache_dir):
            raise ValueError("Diretorio de cache invalido.")
        guild_dir.mkdir(parents=True, exist_ok=True)
        hidden_path = guild_dir / f"observer_{draft.public_id}_hidden.png"
        revealed_path = guild_dir / f"observer_{draft.public_id}_revealed.png"
        hidden_bytes = self._render_card_bytes(draft, False)
        revealed_bytes = self._render_card_bytes(draft, True)
        if len(hidden_bytes) > MAX_CARD_BYTES or len(revealed_bytes) > MAX_CARD_BYTES:
            raise ValueError("O card gerado ultrapassou o limite de tamanho.")
        self._atomic_write(hidden_path, hidden_bytes)
        try:
            self._atomic_write(revealed_path, revealed_bytes)
        except Exception:
            hidden_path.unlink(missing_ok=True)
            raise
        return (
            hidden_path.relative_to(self.root_dir).as_posix(),
            revealed_path.relative_to(self.root_dir).as_posix(),
        )

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _render_card_bytes(self, draft: ObserverArchiveDraft, revealed: bool) -> bytes:
        image = Image.new("RGB", IMAGE_SIZE, "#090b10")
        draw = ImageDraw.Draw(image)
        rng = random.Random(draft.public_id)
        for _ in range(360):
            shade = rng.randint(13, 35)
            x = rng.randrange(IMAGE_SIZE[0])
            y = rng.randrange(IMAGE_SIZE[1])
            draw.point((x, y), fill=(shade, shade, shade + rng.randint(0, 7)))
        for y in range(0, IMAGE_SIZE[1], 5):
            draw.line((0, y, IMAGE_SIZE[0], y), fill=(13, 14, 18), width=1)

        regular = self._font(31)
        quote_font = self._font(43)
        small = self._font(24)
        identity_font = self._font(36)
        draw.rounded_rectangle((35, 32, 1165, 598), radius=24, outline="#55202a", width=3, fill="#0d1017")
        draw.ellipse((75, 68, 99, 92), fill="#ff334f")
        draw.text((113, 61), "REC", font=identity_font, fill="#ff5368")
        archive_label = f"ARQUIVO {draft.public_id.upper()}"
        archive_label_width = draw.textbbox((0, 0), archive_label, font=small)[2]
        draw.text((1125 - archive_label_width, 70), archive_label, font=small, fill="#aeb4c0")
        draw.line((75, 120, 1125, 120), fill="#51232d", width=2)

        avatar_box = (78, 158, 298, 378)
        if revealed:
            avatar = self._avatar_image(draft.avatar_blob, (220, 220))
            mask = Image.new("L", (220, 220), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 219, 219), fill=255)
            image.paste(avatar, (78, 158), mask)
            draw.ellipse(avatar_box, outline="#ff5368", width=5)
            author_text = draft.author_display_name
            status_text = "IDENTIDADE CONFIRMADA"
            status_color = "#ff5368"
        else:
            draw.ellipse(avatar_box, fill="#020305", outline="#ff334f", width=4)
            draw.ellipse((136, 218, 240, 322), outline="#3f1019", width=7)
            draw.line((150, 270, 226, 270), fill="#ff334f", width=6)
            author_text = "████████████"
            status_text = "IDENTIDADE OCULTA"
            status_color = "#707784"

        draw.text((78, 404), status_text, font=small, fill=status_color)
        draw.text((78, 443), author_text, font=identity_font, fill="#f2f3f5")

        quote_lines = self._wrap_text(draw, f'“{draft.message_text}”', quote_font, 745)
        if len(quote_lines) > 6:
            quote_lines = quote_lines[:6]
            quote_lines[-1] = quote_lines[-1].rstrip(" .") + "…"
        y = 171
        for line in quote_lines:
            draw.text((350, y), line, font=quote_font, fill="#edf0f5")
            y += 60
        draw.text((350, 530), "QUEM DISSE ISSO?", font=regular, fill="#ff5368" if not revealed else "#aeb4c0")
        draw.text((1060, 558), "●", font=regular, fill="#ff334f")

        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    @staticmethod
    def _avatar_image(avatar_blob: bytes | None, size: tuple[int, int]) -> Image.Image:
        if avatar_blob:
            try:
                with Image.open(BytesIO(avatar_blob)) as source:
                    return ImageOps.fit(source.convert("RGB"), size, method=Image.Resampling.LANCZOS)
            except (OSError, ValueError):
                pass
        placeholder = Image.new("RGB", size, "#161a22")
        draw = ImageDraw.Draw(placeholder)
        draw.ellipse((50, 28, 170, 148), fill="#343a46")
        draw.rounded_rectangle((34, 138, 186, 230), radius=50, fill="#343a46")
        return placeholder

    @staticmethod
    def _font(size: int) -> ImageFont.ImageFont:
        for font_name in ("arial.ttf", "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(font_name, size=size)
            except OSError:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _wrap_text(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
        max_width: int,
    ) -> list[str]:
        words = text.split()
        if not words:
            return [""]
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            chunk = ""
            for character in word:
                candidate = f"{chunk}{character}"
                if chunk and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                    lines.append(chunk)
                    chunk = character
                else:
                    chunk = candidate
            current = chunk
        if current:
            lines.append(current)
        return lines
