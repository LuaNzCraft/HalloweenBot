from __future__ import annotations

import asyncio
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import random
import uuid

from PIL import Image, ImageDraw, ImageFont


IMAGE_SIZE = (1200, 630)
MAX_CARD_BYTES = 8 * 1024 * 1024
MAX_DISPLAY_LENGTH = 96
CARD_VERSION = "1"


class ObserverAnagramCardService:
    """Renderiza a apresentacao visual do anagrama sem receber a resposta canonica."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.cache_dir = (self.root_dir / "cache" / "observer_anagrams").resolve()

    async def render(self, question_id: str, display_text: str) -> tuple[Path, str]:
        normalized_id = str(question_id or "").strip()
        normalized_display = " ".join(str(display_text or "").split())
        if not normalized_id:
            raise ValueError("O anagrama precisa de um id.")
        if not normalized_display or len(normalized_display) > MAX_DISPLAY_LENGTH:
            raise ValueError("O texto exibido do anagrama e invalido.")

        digest = sha256(
            f"{CARD_VERSION}\0{normalized_id}\0{normalized_display}".encode("utf-8")
        ).hexdigest()[:20]
        filename = f"observer_signal_{digest}.png"
        path = (self.cache_dir / filename).resolve()
        if not path.is_relative_to(self.cache_dir):
            raise ValueError("Caminho de cache invalido.")

        await asyncio.to_thread(self._ensure_card, path, digest, normalized_display)
        return path, filename

    def _ensure_card(self, path: Path, digest: str, display_text: str) -> None:
        if path.is_file():
            try:
                size = path.stat().st_size
                if 0 < size <= MAX_CARD_BYTES:
                    return
            except OSError:
                pass

        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._render_card_bytes(digest, display_text)
        if len(data) > MAX_CARD_BYTES:
            raise ValueError("O card de anagrama ultrapassou o limite de tamanho.")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _render_card_bytes(self, digest: str, display_text: str) -> bytes:
        image = Image.new("RGB", IMAGE_SIZE, "#050609")
        draw = ImageDraw.Draw(image)
        rng = random.Random(digest)

        for _ in range(520):
            shade = rng.randint(9, 33)
            x = rng.randrange(IMAGE_SIZE[0])
            y = rng.randrange(IMAGE_SIZE[1])
            draw.point((x, y), fill=(shade + rng.randint(0, 8), shade, shade))
        for y in range(0, IMAGE_SIZE[1], 5):
            draw.line((0, y, IMAGE_SIZE[0], y), fill="#111116", width=1)

        regular = self._font(27)
        medium = self._font(34)
        signal_font = self._fitting_font(draw, display_text, max_width=970)

        draw.rounded_rectangle(
            (34, 30, 1166, 600),
            radius=24,
            fill="#090b10",
            outline="#65141f",
            width=3,
        )
        self._draw_viewfinder(draw)
        draw.ellipse((75, 67, 99, 91), fill="#ff2d46")
        draw.text((113, 59), "REC", font=medium, fill="#ff4057")

        archive_code = f"SINAL {digest[:8].upper()}"
        code_width = draw.textbbox((0, 0), archive_code, font=regular)[2]
        draw.text((1125 - code_width, 67), archive_code, font=regular, fill="#8f949f")
        draw.line((75, 122, 1125, 122), fill="#49151c", width=2)

        header = "TRANSMISSÃO CORROMPIDA"
        header_width = draw.textbbox((0, 0), header, font=medium)[2]
        draw.text(((IMAGE_SIZE[0] - header_width) / 2, 165), header, font=medium, fill="#ff4057")

        signal_lines = self._wrap_centered(draw, display_text.upper(), signal_font, 970)
        line_height = signal_font.size + 18 if hasattr(signal_font, "size") else 68
        start_y = 285 - ((len(signal_lines) - 1) * line_height / 2)
        for index, line in enumerate(signal_lines):
            bbox = draw.textbbox((0, 0), line, font=signal_font)
            width = bbox[2] - bbox[0]
            x = (IMAGE_SIZE[0] - width) / 2
            y = start_y + index * line_height
            draw.text((x + 3, y + 3), line, font=signal_font, fill="#3b080f")
            draw.text((x, y), line, font=signal_font, fill="#f1f2f4")

        draw.line((165, 463, 1035, 463), fill="#49151c", width=2)
        instruction = "REORGANIZE O SINAL INTERCEPTADO"
        instruction_width = draw.textbbox((0, 0), instruction, font=regular)[2]
        draw.text(
            ((IMAGE_SIZE[0] - instruction_width) / 2, 500),
            instruction,
            font=regular,
            fill="#a7acb5",
        )
        draw.text((1080, 540), "●", font=medium, fill="#ff2d46")

        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    @staticmethod
    def _draw_viewfinder(draw: ImageDraw.ImageDraw) -> None:
        color = "#8f2431"
        length = 48
        width = 4
        for x, y, sx, sy in (
            (58, 54, 1, 1),
            (1142, 54, -1, 1),
            (58, 576, 1, -1),
            (1142, 576, -1, -1),
        ):
            draw.line((x, y, x + sx * length, y), fill=color, width=width)
            draw.line((x, y, x, y + sy * length), fill=color, width=width)

    def _fitting_font(self, draw: ImageDraw.ImageDraw, text: str, *, max_width: int) -> ImageFont.ImageFont:
        for size in (72, 66, 60, 54, 48, 42):
            font = self._font(size)
            if len(self._wrap_centered(draw, text.upper(), font, max_width)) <= 2:
                return font
        return self._font(38)

    @staticmethod
    def _wrap_centered(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
        max_width: int,
    ) -> list[str]:
        words = text.split()
        if not words:
            return []
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current}   {word}".strip()
            if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = word
        if current:
            lines.append(current)
        return lines

    @staticmethod
    def _font(size: int) -> ImageFont.ImageFont:
        for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "DejaVuSans.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(name, size=size)
            except OSError:
                continue
        return ImageFont.load_default()
