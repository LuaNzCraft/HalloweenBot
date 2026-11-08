from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import io
import logging
from pathlib import Path
from typing import Dict, List, Tuple
import random

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from repositories.admin_repo import AdminRepo
from repositories.board_read_repo import BoardReadRepo
from repositories.effects_repo import EffectsRepo
from repositories.julgamento_repo import JulgamentoRepo
from repositories.public_events_repo import PublicEventsRepo
from services.i18n_service import I18nService
from services.audit_service import AuditService
from services.inventory_service import InventoryService
from services.items_runtime import ItemsRuntimeService
from services.items_texts import ItemsTextService
from services.mission_service import MissionService
from services.tabuleiro_texts import TabuleiroTextService
from repositories.users_repo import UsersRepo
from utils.event_config import get_board_config, get_effects_config
from views.tabuleiro_views import BoardView


ROOT_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT_DIR / "Assets" / "Tabuleiros"
OPTIMIZED_ASSETS_DIR = ASSETS_DIR / "optimized"
LEGACY_OPTIMIZED_ASSETS_DIR = ASSETS_DIR / "novoreze"
DB_DIR = ROOT_DIR / "data"
DB_PATH = DB_DIR / "halloween.sqlite3"
CACHE_DIR = ROOT_DIR / "cache" / "board_rolls"
LOGGER = logging.getLogger(__name__)

BOARD_IMAGE_CANDIDATES = (
    ASSETS_DIR / "board.png",
    ASSETS_DIR / "tabuleiro_base.png",
    ASSETS_DIR / "tabuleiro.png",
)
TOKEN_IMAGE_CANDIDATES = (
    OPTIMIZED_ASSETS_DIR / "personagem.png",
    LEGACY_OPTIMIZED_ASSETS_DIR / "personagem.png",
    ASSETS_DIR / "token.png",
    ASSETS_DIR / "personagem.png",
)
RESULT_WINDOW_PATH = ASSETS_DIR / "WindowFinal.png"


def asset_path(filename: str) -> Path:
    optimized_path = OPTIMIZED_ASSETS_DIR / filename
    if optimized_path.exists():
        return optimized_path
    return LEGACY_OPTIMIZED_ASSETS_DIR / filename


ROLL_DICE_PATHS: Dict[int, Path] = {
    1: asset_path("dado1.png"),
    2: asset_path("dado2.png"),
    3: asset_path("dado3.png"),
    4: asset_path("dado4.png"),
    5: asset_path("dado5.png"),
    6: asset_path("dado6.png"),
}
CUSTOM_FONT_CANDIDATES = (
    ASSETS_DIR / "Fonte" / "MidnightMinutes_DEMO.otf",
)

RESULT_ICON_PATHS: Dict[str, Path] = {
    "Candy": asset_path("Doce.png"),
    "Trap": asset_path("Maldição.png"),
    "Trick": asset_path("Travessura.png"),
    "Dice": asset_path("Dado.png"),
    "Basket": asset_path("Cesta.png"),
    "Start": asset_path("Dado.png"),
}
NOTICE_THUMBNAIL_CANDIDATES: Dict[str, Tuple[Path, ...]] = {
    "Candy": (
        ROOT_DIR / "Assets" / "Moeda Doce.png",
        asset_path("Doce.png"),
    ),
    "Trick": (
        ROOT_DIR / "Assets" / "MoedaTravessa.png",
        asset_path("Travessura.png"),
    ),
    "Dice": (
        ROOT_DIR / "Assets" / "Tabuleiros" / "novoreze" / "Dado.png",
        asset_path("Dado.png"),
    ),
    "Basket": (
        ROOT_DIR / "Assets" / "Cesta de doce.png",
        asset_path("Cesta.png"),
    ),
    "Trap": (
        asset_path("MaldiÃ§Ã£o.png"),
    ),
    "Start": (
        ROOT_DIR / "Assets" / "Tabuleiros" / "novoreze" / "Dado.png",
        asset_path("Dado.png"),
    ),
}

TOTAL_CASAS = 16

# Ordem corrigida conforme seu percurso:
# base da direita para a esquerda, sobe pela esquerda, cruza o topo, desce pela direita.
BOARD_TILES: Dict[int, str] = {
    0: "Start",
    1: "Dice",
    2: "Candy",
    3: "Candy",
    4: "Basket",
    5: "Candy",
    6: "Trap",
    7: "Trick",
    8: "Dice",
    9: "Candy",
    10: "Candy",
    11: "Trap",
    12: "Basket",
    13: "Trick",
    14: "Trap",
    15: "Basket",
}

# Coordenadas na mesma ordem do percurso acima.
BOARD_COORDINATES: Dict[int, Tuple[int, int]] = {
    0: (930, 914),
    1: (715, 913),
    2: (510, 922),
    3: (310, 922),
    4: (99, 919),
    5: (96, 726),
    6: (104, 506),
    7: (96, 300),
    8: (98, 110),
    9: (316, 110),
    10: (518, 108),
    11: (715, 104),
    12: (919, 108),
    13: (921, 285),
    14: (921, 512),
    15: (926, 715),
}

TOKEN_SIZE = (150, 220)
INITIAL_FRAME_DURATION_MS = 1000
ROLL_SPIN_FRAME_DURATION_MS = 170
ROLL_FINAL_FACE_DURATION_MS = 650
MOVE_FRAME_DURATION_MS = 430
LANDING_FRAME_DURATION_MS = 700
RESULT_FRAME_DURATION_MS = 100
ROLL_LOCK_BUFFER_MS = 4000
ROLL_LOCK_ADJUSTMENTS_MS = {
    5: -500,
    6: -1000,
}
BOARD_NOTICE_SECONDS = 5
CENTURY_LAP_CELEBRATION_TIMEOUT_SECONDS = 180
CENTURY_LAP_CELEBRATION_MAX_CLAIMS = 10
CENTURY_LAP_CELEBRATION_BASKET_ID = "cesta_abobora"
CENTURY_LAP_CELEBRATION_DICE_AMOUNT = 1
RESULT_ICON_SIZE = (370, 410)
RESULT_ICON_CENTER = (512, 460)
RESULT_TEXT_CENTER = (512, 765)
RESULT_TEXT_COLOR = "#FDE7A6"
RESULT_TEXT_STROKE = "#24101F"
DICE_REVEAL_SIZE = (285, 310)
DICE_REVEAL_CENTER = (512, 390)
ANIMATION_FORMAT = "WEBP"  # Troque para "GIF" se o Discord/client ficar pior.
WEBP_QUALITY = 65
WEBP_METHOD = 0
ANIMATION_CACHE_VERSION = "v1"
PREGENERATE_ANIMATION_CACHE = True

RESULT_MESSAGES: Dict[str, str] = {
    "Candy": "Voce recebeu Doce",
    "Trap": "Voce recebeu Maldicao",
    "Trick": "Voce recebeu Travessura",
    "Dice": "Voce recebeu Dado",
    "Basket": "Voce recebeu Cesta",
    "Start": "Voce voltou ao inicio",
}

TRAP_ITEM_REWARDS = ("mute", "switch")
BASKET_ITEM_IDS = {"cesta_abobora", "cesta_doce_epica", "cesta_doce_amaldicoada"}


def resolve_board_asset(relative_path: str) -> Path:
    return ASSETS_DIR / relative_path
def resolve_existing_path(candidates: Tuple[Path, ...]) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Nenhum asset encontrado entre: {', '.join(str(path) for path in candidates)}")


def normalize_position(position: int, total_tiles: int = TOTAL_CASAS) -> int:
    return position % total_tiles


def build_path_positions(start_position: int, roll_value: int, total_tiles: int = TOTAL_CASAS) -> List[int]:
    positions = []
    current = start_position
    for _ in range(roll_value):
        current = normalize_position(current + 1, total_tiles)
        positions.append(current)
    return positions


def build_dice_spin_sequence(final_value: int) -> List[int]:
    sequence = [1, 4, 2, 6, 3, 5]
    if sequence[-1] == final_value:
        sequence.append(2 if final_value != 2 else 4)
    sequence.append(final_value)
    return sequence


def get_roll_animation_duration_seconds(roll_value: int) -> float:
    spin_sequence = build_dice_spin_sequence(roll_value)
    total_ms = (
        INITIAL_FRAME_DURATION_MS
        + (len(spin_sequence) - 1) * ROLL_SPIN_FRAME_DURATION_MS
        + ROLL_FINAL_FACE_DURATION_MS
        + roll_value * MOVE_FRAME_DURATION_MS
        + LANDING_FRAME_DURATION_MS
        + RESULT_FRAME_DURATION_MS
        + ROLL_LOCK_BUFFER_MS
        + ROLL_LOCK_ADJUSTMENTS_MS.get(roll_value, 0)
    )
    return total_ms / 1000


class CenturyLapCelebrateView(discord.ui.View):
    def __init__(self, cog: "TabuleiroCog", locale: str, laps: int) -> None:
        super().__init__(timeout=CENTURY_LAP_CELEBRATION_TIMEOUT_SECONDS)
        self.cog = cog
        self.locale = locale
        self.laps = laps
        self.claimed_user_ids: set[int] = set()
        self._lock = asyncio.Lock()
        self.message: discord.Message | None = None
        self._refresh_button()

    def _refresh_button(self) -> None:
        remaining = max(0, CENTURY_LAP_CELEBRATION_MAX_CLAIMS - len(self.claimed_user_ids))
        self.celebrate.label = self.cog.i18n.t(self.locale, "board.notice.celebrate_button", remaining=remaining)
        self.celebrate.disabled = remaining <= 0

    async def on_timeout(self) -> None:
        self.celebrate.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except (discord.NotFound, discord.HTTPException):
            return

    @discord.ui.button(label="🎉 Parabenizar", style=discord.ButtonStyle.success, emoji="🎉")
    async def celebrate(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if interaction.user.id in self.claimed_user_ids:
                await interaction.response.send_message(
                    self.cog.i18n.t(self.locale, "board.notice.celebrate_already_claimed"),
                    ephemeral=True,
                )
                return
            if len(self.claimed_user_ids) >= CENTURY_LAP_CELEBRATION_MAX_CLAIMS:
                self._refresh_button()
                await interaction.response.edit_message(view=self)
                await interaction.followup.send(
                    self.cog.i18n.t(self.locale, "board.notice.celebrate_exhausted"),
                    ephemeral=True,
                )
                return

            self.claimed_user_ids.add(interaction.user.id)
            basket_total = await self.cog.users_repo.add_inventory_item(
                interaction.user.id,
                CENTURY_LAP_CELEBRATION_BASKET_ID,
                1,
            )
            dice_total = await self.cog.users_repo.add_currency(
                interaction.user.id,
                "dice_count",
                CENTURY_LAP_CELEBRATION_DICE_AMOUNT,
            )
            self._refresh_button()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                self.cog.i18n.t(
                    self.locale,
                    "board.notice.celebrate_claimed",
                    basket_total=basket_total,
                    dice_total=dice_total,
                ),
                ephemeral=True,
            )


class TabuleiroCog(commands.Cog):
    _BOARD_STATE_ATTRIBUTES = (
        "board_config",
        "total_tiles",
        "board_tiles",
        "board_coordinates",
        "result_messages",
        "trap_item_rewards",
        "roll_dice_paths",
        "result_icon_paths",
        "notice_thumbnail_paths",
        "board_path",
        "token_path",
        "result_window_path",
        "_board_base",
        "_token_base",
        "_result_window_base",
        "_roll_dice_bases",
        "_result_icon_bases",
        "_position_frame_cache",
        "_dice_reveal_frame_cache",
        "_result_frame_cache",
        "_position_frame_gif_cache",
        "_dice_reveal_frame_gif_cache",
        "_result_frame_gif_cache",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_path = DB_PATH
        self.admin_repo = AdminRepo(DB_PATH)
        self.users_repo = UsersRepo(DB_PATH)
        self.effects_repo = EffectsRepo(DB_PATH)
        self.public_events_repo = PublicEventsRepo(DB_PATH)
        self.julgamento_repo = JulgamentoRepo(DB_PATH)
        self.i18n = I18nService(self.users_repo)
        self.texts = TabuleiroTextService(self.i18n)
        self.mission_service = MissionService(users_repo=self.users_repo, board_repo=BoardReadRepo(DB_PATH))
        self.items_runtime = ItemsRuntimeService(
            bot=bot,
            i18n=self.i18n,
            admin_repo=self.admin_repo,
            effects_repo=self.effects_repo,
            public_events_repo=self.public_events_repo,
            julgamento_repo=self.julgamento_repo,
            audit_service=AuditService(bot),
            inventory_service=InventoryService(users_repo=self.users_repo, i18n_service=self.i18n),
            mission_service=self.mission_service,
            effects_config=get_effects_config(),
            texts=ItemsTextService(self.i18n),
        )
        self._board_reload_lock = asyncio.Lock()
        self._apply_board_state(self._build_board_state(get_board_config()))
        self._player_locks: Dict[int, asyncio.Lock] = {}
        self._rolling_users: set[int] = set()
        self._rolling_until: Dict[int, datetime] = {}
        self._notice_users: set[int] = set()
        self._animation_file_locks: Dict[Tuple[int, int], asyncio.Lock] = {}

    async def reload_config(self) -> None:
        self.items_runtime.effects_config = get_effects_config()
        new_config = get_board_config()
        if new_config == self.board_config:
            return
        async with self._board_reload_lock:
            if new_config == self.board_config:
                return
            state = await asyncio.to_thread(self._build_board_state, new_config)
            while getattr(self, "_rolling_users", set()):
                await asyncio.sleep(0.1)
            self._apply_board_state(state)

    @classmethod
    def _build_board_state(cls, board_config: dict) -> dict:
        candidate = object.__new__(cls)
        candidate.board_config = board_config
        candidate.total_tiles = int(board_config.get("total_tiles", TOTAL_CASAS))
        candidate.board_tiles = {
            int(key): str(value)
            for key, value in board_config.get("tile_types", BOARD_TILES).items()
        } or dict(BOARD_TILES)
        candidate.board_coordinates = {
            int(key): (int(value[0]), int(value[1]))
            for key, value in board_config.get("coordinates", {}).items()
        } or dict(BOARD_COORDINATES)
        candidate.result_messages = {
            str(key): str(value)
            for key, value in board_config.get("result_messages", RESULT_MESSAGES).items()
        } or dict(RESULT_MESSAGES)
        candidate.trap_item_rewards = tuple(board_config.get("trap_item_rewards", TRAP_ITEM_REWARDS)) or TRAP_ITEM_REWARDS
        board_assets = board_config.get("assets", {})
        board_image_candidates = tuple(
            resolve_board_asset(path)
            for path in board_assets.get("board_image_candidates", ["board.png", "tabuleiro_base.png", "tabuleiro.png"])
        )
        token_image_candidates = tuple(
            resolve_board_asset(path)
            for path in board_assets.get(
                "token_image_candidates",
                ["optimized/personagem.png", "novoreze/personagem.png", "token.png", "personagem.png"],
            )
        )
        candidate.roll_dice_paths = {
            int(key): resolve_board_asset(value)
            for key, value in board_assets.get("dice_faces", {}).items()
        } or dict(ROLL_DICE_PATHS)
        candidate.result_icon_paths = {
            str(key): resolve_board_asset(value)
            for key, value in board_assets.get("result_icons", {}).items()
        } or dict(RESULT_ICON_PATHS)
        candidate.notice_thumbnail_paths = {
            tile_type: next((path for path in candidates if path.exists()), None)
            for tile_type, candidates in NOTICE_THUMBNAIL_CANDIDATES.items()
        }
        candidate.board_path = resolve_existing_path(board_image_candidates)
        candidate.token_path = resolve_existing_path(token_image_candidates)
        candidate.result_window_path = resolve_board_asset(str(board_assets.get("result_window", "WindowFinal.png")))
        candidate._board_base = Image.open(candidate.board_path).convert("RGBA")
        candidate._token_base = Image.open(candidate.token_path).convert("RGBA")
        candidate._result_window_base = Image.open(candidate.result_window_path).convert("RGBA")
        candidate._roll_dice_bases = {
            value: Image.open(path).convert("RGBA")
            for value, path in candidate.roll_dice_paths.items()
            if path.exists()
        }
        candidate._result_icon_bases = {
            tile_type: Image.open(path).convert("RGBA")
            for tile_type, path in candidate.result_icon_paths.items()
            if path.exists()
        }
        candidate._position_frame_cache = {}
        candidate._dice_reveal_frame_cache = {}
        candidate._result_frame_cache = {}
        candidate._position_frame_gif_cache = {}
        candidate._dice_reveal_frame_gif_cache = {}
        candidate._result_frame_gif_cache = {}
        candidate._warm_render_cache()
        return {name: getattr(candidate, name) for name in cls._BOARD_STATE_ATTRIBUTES}

    def _apply_board_state(self, state: dict) -> None:
        for name in self._BOARD_STATE_ATTRIBUTES:
            setattr(self, name, state[name])

    async def cog_load(self) -> None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._animation_cache_dir.mkdir(parents=True, exist_ok=True)
        await self.users_repo.init_db()
        await self._init_db()
        if PREGENERATE_ANIMATION_CACHE:
            self.bot.loop.create_task(self._warm_animation_file_cache())

    @property
    def _animation_cache_dir(self) -> Path:
        format_name = ANIMATION_FORMAT.lower()
        return CACHE_DIR / f"{format_name}_{ANIMATION_CACHE_VERSION}"

    async def _init_db(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    user_id INTEGER PRIMARY KEY,
                    posicao_atual INTEGER NOT NULL DEFAULT 0,
                    voltas INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.commit()

    def _get_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._player_locks:
            self._player_locks[user_id] = asyncio.Lock()
        return self._player_locks[user_id]

    async def _get_or_create_player(self, user_id: int) -> Tuple[int, int]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT posicao_atual, voltas FROM players WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is not None:
                return int(row[0]), int(row[1])

            await db.execute(
                "INSERT INTO players (user_id, posicao_atual, voltas) VALUES (?, 0, 0)",
                (user_id,),
            )
            await db.commit()
            return 0, 0

    async def _save_player(self, user_id: int, position: int, laps: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO players (user_id, posicao_atual, voltas)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    posicao_atual = excluded.posicao_atual,
                    voltas = excluded.voltas
                """,
                (user_id, position, laps),
            )
            await db.commit()

    def _load_font(self, size: int) -> ImageFont.ImageFont:
        truetype_candidates = (
            *CUSTOM_FONT_CANDIDATES,
            Path("C:/Windows/Fonts/arialbd.ttf"),
            Path("C:/Windows/Fonts/segoeuib.ttf"),
            Path("C:/Windows/Fonts/segoeui.ttf"),
            Path("C:/Windows/Fonts/arial.ttf"),
        )
        for candidate in truetype_candidates:
            if candidate.exists():
                return ImageFont.truetype(str(candidate), size=size)
        return ImageFont.load_default()

    def _warm_render_cache(self) -> None:
        for position in self.board_coordinates:
            self._position_frame_cache[position] = self._render_position_frame(position)
            self._position_frame_gif_cache[position] = self._to_gif_frame(self._position_frame_cache[position])

        for position in self.board_coordinates:
            for value in self._roll_dice_bases:
                self._dice_reveal_frame_cache[(position, value)] = self._render_dice_reveal_frame(position, value)
                self._dice_reveal_frame_gif_cache[(position, value)] = self._to_gif_frame(
                    self._dice_reveal_frame_cache[(position, value)]
                )

        for position in self.board_coordinates:
            for tile_type in self._result_icon_bases:
                self._result_frame_cache[(position, tile_type)] = self._render_result_frame(position, tile_type)
                self._result_frame_gif_cache[(position, tile_type)] = self._to_gif_frame(
                    self._result_frame_cache[(position, tile_type)]
                )

    def _to_gif_frame(self, image: Image.Image) -> Image.Image:
        return image.convert("P", palette=Image.Palette.ADAPTIVE)

    def _render_position_frame(self, position: int) -> Image.Image:
        board = self._board_base.copy()
        token = self._token_base.copy()
        token.thumbnail(TOKEN_SIZE, Image.Resampling.LANCZOS)

        center_x, center_y = self.board_coordinates[position]
        paste_x = center_x - token.width // 2
        paste_y = center_y - token.height // 2

        board.alpha_composite(token, (paste_x, paste_y))
        return board

    def _compose_frame(self, position: int) -> Image.Image:
        return self._position_frame_cache[position].copy()

    def _build_static_image_bytes(self, position: int) -> io.BytesIO:
        image = self._compose_frame(position)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer

    def _render_result_frame(self, position: int, tile_type: str) -> Image.Image:
        image = self._position_frame_cache[position].copy()
        image.alpha_composite(self._result_window_base)

        icon = self._result_icon_bases[tile_type].copy()
        icon.thumbnail(RESULT_ICON_SIZE, Image.Resampling.LANCZOS)
        image_center_x = image.width // 2
        icon_x = image_center_x - icon.width // 2
        icon_y = RESULT_ICON_CENTER[1] - icon.height // 2
        image.alpha_composite(icon, (icon_x, icon_y))

        draw = ImageDraw.Draw(image)
        font = self._load_font(46)
        draw.text(
            (image_center_x, RESULT_TEXT_CENTER[1]),
            self.result_messages[tile_type],
            fill=RESULT_TEXT_COLOR,
            font=font,
            anchor="mm",
            stroke_width=3,
            stroke_fill=RESULT_TEXT_STROKE,
        )
        return image

    def _compose_result_frame(self, position: int, tile_type: str) -> Image.Image:
        return self._result_frame_cache[(position, tile_type)].copy()

    def _render_dice_reveal_frame(self, position: int, roll_value: int) -> Image.Image:
        image = self._position_frame_cache[position].copy()
        dice_image = self._roll_dice_bases[roll_value].copy()
        dice_image.thumbnail(DICE_REVEAL_SIZE, Image.Resampling.LANCZOS)

        image_center_x = image.width // 2
        paste_x = image_center_x - dice_image.width // 2
        paste_y = DICE_REVEAL_CENTER[1] - dice_image.height // 2
        image.alpha_composite(dice_image, (paste_x, paste_y))
        return image

    def _compose_dice_reveal_frame(self, position: int, roll_value: int) -> Image.Image:
        return self._dice_reveal_frame_cache[(position, roll_value)].copy()

    def _build_animation_bytes(self, start_position: int, positions: List[int], roll_value: int, final_tile_type: str) -> io.BytesIO:
        frames: List[Image.Image] = []
        durations: List[int] = []
        use_webp = ANIMATION_FORMAT.upper() == "WEBP"

        frames.append(
            self._position_frame_cache[start_position].copy()
            if use_webp
            else self._position_frame_gif_cache[start_position].copy()
        )
        durations.append(INITIAL_FRAME_DURATION_MS)

        spin_sequence = build_dice_spin_sequence(roll_value)
        for index, face_value in enumerate(spin_sequence):
            frames.append(
                self._dice_reveal_frame_cache[(start_position, face_value)].copy()
                if use_webp
                else self._dice_reveal_frame_gif_cache[(start_position, face_value)].copy()
            )
            if index == len(spin_sequence) - 1:
                durations.append(ROLL_FINAL_FACE_DURATION_MS)
            else:
                durations.append(ROLL_SPIN_FRAME_DURATION_MS)

        for position in positions:
            frames.append(
                self._position_frame_cache[position].copy()
                if use_webp
                else self._position_frame_gif_cache[position].copy()
            )
            durations.append(MOVE_FRAME_DURATION_MS)

        final_position = positions[-1] if positions else start_position
        frames.append(
            self._position_frame_cache[final_position].copy()
            if use_webp
            else self._position_frame_gif_cache[final_position].copy()
        )
        durations.append(LANDING_FRAME_DURATION_MS)

        frames.append(
            self._result_frame_cache[(final_position, final_tile_type)].copy()
            if use_webp
            else self._result_frame_gif_cache[(final_position, final_tile_type)].copy()
        )
        durations.append(RESULT_FRAME_DURATION_MS)

        buffer = io.BytesIO()
        if use_webp:
            frames[0].save(
                buffer,
                format="WEBP",
                save_all=True,
                append_images=frames[1:],
                duration=durations,
                loop=1,
                quality=WEBP_QUALITY,
                method=WEBP_METHOD,
            )
        else:
            frames[0].save(
                buffer,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=durations,
                disposal=2,
            )
        buffer.seek(0)
        return buffer

    def _animation_cache_path(self, start_position: int, roll_value: int) -> Path:
        extension = "webp" if ANIMATION_FORMAT.upper() == "WEBP" else "gif"
        return self._animation_cache_dir / f"start_{start_position}_roll_{roll_value}.{extension}"

    def _get_animation_file_lock(self, start_position: int, roll_value: int) -> asyncio.Lock:
        key = (start_position, roll_value)
        if key not in self._animation_file_locks:
            self._animation_file_locks[key] = asyncio.Lock()
        return self._animation_file_locks[key]

    def _generate_animation_cache_file_sync(self, start_position: int, roll_value: int) -> Path:
        final_position = normalize_position(start_position + roll_value, self.total_tiles)
        final_tile_type = self.board_tiles[final_position]
        positions = build_path_positions(start_position, roll_value, self.total_tiles)
        animation_bytes = self._build_animation_bytes(start_position, positions, roll_value, final_tile_type)

        cache_path = self._animation_cache_path(start_position, roll_value)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cache_path.with_suffix(f"{cache_path.suffix}.tmp")
        temp_path.write_bytes(animation_bytes.getvalue())
        temp_path.replace(cache_path)
        return cache_path

    async def _get_or_create_animation_cache_file(self, start_position: int, roll_value: int) -> Path:
        cache_path = self._animation_cache_path(start_position, roll_value)
        if cache_path.exists():
            return cache_path

        async with self._get_animation_file_lock(start_position, roll_value):
            if cache_path.exists():
                return cache_path
            return await asyncio.to_thread(self._generate_animation_cache_file_sync, start_position, roll_value)

    async def _warm_animation_file_cache(self) -> None:
        total = len(self.board_coordinates) * 6
        created = 0
        LOGGER.info("Aquecendo cache de animações do tabuleiro (%s arquivos possíveis)", total)
        for start_position in sorted(self.board_coordinates):
            for roll_value in range(1, 7):
                cache_path = self._animation_cache_path(start_position, roll_value)
                if cache_path.exists():
                    continue
                try:
                    await self._get_or_create_animation_cache_file(start_position, roll_value)
                    created += 1
                except Exception:
                    LOGGER.exception(
                        "Falha ao gerar cache de animação do tabuleiro: start=%s roll=%s",
                        start_position,
                        roll_value,
                    )
        LOGGER.info("Cache de animações do tabuleiro pronto (%s novos arquivos)", created)

    async def _build_board_file(self, position: int) -> discord.File:
        image_bytes = self._build_static_image_bytes(position)
        return discord.File(fp=image_bytes, filename="tabuleiro.png")

    async def _build_roll_file(self, start_position: int, roll_value: int, final_tile_type: str) -> discord.File:
        cache_path = await self._get_or_create_animation_cache_file(start_position, roll_value)
        return discord.File(str(cache_path), filename=cache_path.name)

    async def _send_board_notice(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        *,
        auto_delete: bool = True,
        embed: discord.Embed | None = None,
        thumbnail_path: Path | None = None,
    ) -> None:
        user_id = interaction.user.id
        if user_id in self._notice_users:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return

        self._notice_users.add(user_id)
        embed = embed or self.texts.build_notice_embed(title, description)
        file: discord.File | None = None
        if thumbnail_path is not None and thumbnail_path.exists():
            file = discord.File(str(thumbnail_path), filename=thumbnail_path.name)
            embed.set_thumbnail(url=f"attachment://{thumbnail_path.name}")

        try:
            if interaction.response.is_done():
                send_kwargs = {
                    "embed": embed,
                    "ephemeral": True,
                    "wait": True,
                }
                if file is not None:
                    send_kwargs["file"] = file
                message = await interaction.followup.send(**send_kwargs)
            else:
                send_kwargs = {
                    "embed": embed,
                    "ephemeral": True,
                }
                if file is not None:
                    send_kwargs["file"] = file
                await interaction.response.send_message(**send_kwargs)
                message = await interaction.original_response()
        except discord.HTTPException:
            self._notice_users.discard(user_id)
            return

        if auto_delete:
            self.bot.loop.create_task(self._delete_board_notice_later(user_id, message))
        else:
            self._notice_users.discard(user_id)

    async def _delete_board_notice_later(self, user_id: int, message: discord.WebhookMessage) -> None:
        try:
            await asyncio.sleep(BOARD_NOTICE_SECONDS)
            await message.delete()
        except (discord.NotFound, discord.HTTPException):
            pass
        finally:
            self._notice_users.discard(user_id)

    def _highest_special_lap(self, previous_laps: int, new_laps: int, *, divisor: int) -> int | None:
        if new_laps <= previous_laps or divisor <= 0:
            return None
        reached = [lap for lap in range(previous_laps + 1, new_laps + 1) if lap % divisor == 0]
        return max(reached) if reached else None

    def _notice_thumbnail_path(self, tile_type: str) -> Path | None:
        return self.notice_thumbnail_paths.get(tile_type)

    async def _send_public_century_lap_announcement(
        self,
        guild: discord.Guild | None,
        locale: str,
        member: discord.Member | discord.User,
        laps: int,
        rewards_description: str,
        *,
        role_mention: str | None = None,
    ) -> None:
        if guild is None:
            return
        config = await self.admin_repo.get_config(guild.id)
        channel_ids = [int(channel_id) for channel_id in (config.get("event_channel_ids") or []) if channel_id]
        if not channel_ids:
            return
        channel = guild.get_channel(channel_ids[0])
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            return
        content_parts: list[str] = []
        notification_role_id = config.get("notification_role_id")
        if notification_role_id:
            notification_role = guild.get_role(int(notification_role_id))
            if notification_role is not None:
                content_parts.append(notification_role.mention)
        content_parts.append(member.mention)
        embed = self.texts.build_century_lap_embed(
            locale,
            member.display_name,
            member.mention,
            laps,
            rewards_description,
            role_mention=role_mention,
            avatar_url=member.display_avatar.url,
        )
        view = CenturyLapCelebrateView(self, locale, laps)
        try:
            message = await channel.send(content=" ".join(content_parts), embed=embed, view=view)
            view.message = message
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _apply_tile_reward(
        self,
        user_id: int,
        tile_type: str,
        laps: int,
        *,
        guild_id: int | None = None,
        guild: discord.Guild | None = None,
    ) -> str | None:
        locale = await self.i18n.get_user_locale(user_id)
        reward_config = self.board_config.get("rewards", {}).get(tile_type, {})
        grant_type = str(reward_config.get("grant_type") or "")

        if grant_type == "currency":
            grant_id = str(reward_config.get("grant_id") or "")
            if "base_min" in reward_config or "base_max" in reward_config:
                base_min = int(reward_config.get("base_min", 0))
                base_max = int(reward_config.get("base_max", base_min))
                per_lap_bonus = int(reward_config.get("per_lap_bonus", 0))
                max_bonus_laps = int(reward_config.get("max_bonus_laps", laps))
                scaled_laps = min(laps, max_bonus_laps) if max_bonus_laps >= 0 else laps
                amount = random.randint(base_min, base_max) + (scaled_laps * per_lap_bonus)
            else:
                amount = int(reward_config.get("amount", 0))

            total = await self.users_repo.add_currency(user_id, grant_id, amount)
            await self.mission_service.increment_guild_currency(guild_id, grant_id, amount)
            metric_mapping = {
                "candies": "candies_total",
                "trick_coins": "trick_coins_total",
                "xp_bank": "xp_bank",
            }
            metric = metric_mapping.get(grant_id)
            if metric is not None:
                await self.mission_service.increment_user_guild_metric(guild_id, user_id, metric, amount)
            if grant_id == "candies":
                return self.i18n.t(locale, "board.reward.candy", amount=amount)
            if grant_id == "trick_coins":
                return self.i18n.t(locale, "board.reward.trick", amount=amount)
            if grant_id == "dice_count":
                return self.i18n.t(locale, "board.reward.dice", total=total)

        if grant_type == "item":
            item_id = str(reward_config.get("grant_id") or "")
            amount = int(reward_config.get("amount", 1))
            total = await self.users_repo.add_inventory_item(user_id, item_id, amount)
            item_name = self.i18n.maybe_t(locale, f"item.{item_id}.name") or item_id
            if item_id in BASKET_ITEM_IDS:
                return self.i18n.t(locale, "board.reward.basket", item_name=item_name, total=total)
            return self.i18n.t(locale, "board.reward.trap", item_name=item_name, total=total)

        if grant_type == "trap_item":
            item_id = random.choice(self.trap_item_rewards)
            amount = int(reward_config.get("amount", 1))
            total = await self.users_repo.add_inventory_item(user_id, item_id, amount)
            item_name = self.i18n.maybe_t(locale, f"item.{item_id}.name") or item_id
            return self.i18n.t(locale, "board.reward.trap", item_name=item_name, total=total)

        if grant_type == "curse_pool":
            return await self._apply_curse_pool_reward(
                user_id,
                locale,
                reward_config,
                guild=guild,
            )

        if grant_type == "none" or tile_type == "Start":
            return self.i18n.t(locale, "board.reward.start")

        return None

    async def _apply_curse_pool_reward(
        self,
        user_id: int,
        locale: str,
        reward_config: dict,
        *,
        guild: discord.Guild | None,
    ) -> str:
        pool = [entry for entry in (reward_config.get("pool") or []) if isinstance(entry, dict)]
        weighted_pool: list[dict] = []
        for entry in pool:
            weight = int(entry.get("weight", 0) or 0)
            if weight > 0:
                weighted_pool.extend([entry] * weight)
        if not weighted_pool:
            return self.i18n.t(locale, "board.reward.curse_failed")

        selected = random.choice(weighted_pool)
        kind = str(selected.get("kind") or "").strip().lower()

        if kind == "lose_currency":
            currency = str(selected.get("currency") or "").strip()
            amount = max(0, int(selected.get("amount", 0) or 0))
            if amount <= 0:
                return self.i18n.t(locale, "board.reward.curse_failed")
            user = await self.users_repo.get_user(user_id)
            before = int(user.get(currency, 0) or 0)
            after = await self.users_repo.add_currency(user_id, currency, -amount)
            lost = max(0, before - after)
            if currency == "candies":
                return self.i18n.t(locale, "board.reward.curse_candies", amount=lost)
            if currency == "dice_count":
                return self.i18n.t(locale, "board.reward.curse_dice", amount=lost)
            return self.i18n.t(locale, "board.reward.curse_failed")

        if guild is None:
            return self.i18n.t(locale, "board.reward.curse_failed")
        member = guild.get_member(user_id)
        if member is None:
            return self.i18n.t(locale, "board.reward.curse_failed")

        if kind == "self_mute":
            duration_seconds = max(60, int(selected.get("duration_seconds", 60) or 60))
            applied = await self.items_runtime.apply_board_mute_curse(
                guild=guild,
                target=member,
                duration_seconds=duration_seconds,
            )
            if not applied:
                return self.i18n.t(locale, "board.reward.curse_failed")
            return self.i18n.t(locale, "board.reward.curse_mute", minutes=max(1, duration_seconds // 60))

        if kind == "self_switch":
            duration_seconds = max(60, int(selected.get("duration_seconds", 180) or 180))
            applied = await self.items_runtime.apply_board_switch_curse(
                guild=guild,
                target=member,
                duration_seconds=duration_seconds,
            )
            if not applied:
                return self.i18n.t(locale, "board.reward.curse_failed")
            return self.i18n.t(locale, "board.reward.curse_switch", minutes=max(1, duration_seconds // 60))

        if kind == "self_interference":
            duration_seconds = max(60, int(selected.get("duration_seconds", 180) or 180))
            applied = await self.items_runtime.apply_board_interference_curse(
                guild=guild,
                target=member,
                duration_seconds=duration_seconds,
            )
            if not applied:
                return self.i18n.t(locale, "board.reward.curse_failed")
            return self.i18n.t(locale, "board.reward.curse_interference", minutes=max(1, duration_seconds // 60))

        if kind == "blessing_item":
            item_pool = [
                str(item_id).strip()
                for item_id in selected.get("item_pool", self.trap_item_rewards)
                if str(item_id).strip()
            ]
            if not item_pool:
                item_pool = list(self.trap_item_rewards)
            item_id = random.choice(item_pool)
            amount = max(1, int(selected.get("amount", 1) or 1))
            total = await self.users_repo.add_inventory_item(user_id, item_id, amount)
            item_name = self.i18n.maybe_t(locale, f"item.{item_id}.name") or item_id
            return self.i18n.t(locale, "board.reward.trap", item_name=item_name, total=total)

        return self.i18n.t(locale, "board.reward.curse_failed")

    async def _grant_lap_milestone_rewards(
        self,
        user_id: int,
        locale: str,
        milestone_lap: int,
        rewards: dict,
        *,
        guild_id: int | None = None,
    ) -> list[str]:
        lines = [f"🏁 Recompensa da volta **{milestone_lap}**:"]
        metric_mapping = {
            "candies": "candies_total",
            "trick_coins": "trick_coins_total",
            "xp_bank": "xp_bank",
        }
        reward_keys = {
            "candies": "missions.reward.candies",
            "trick_coins": "missions.reward.trick_coins",
            "dice_count": "missions.reward.dice",
            "xp_bank": "missions.reward.xp",
        }

        for reward_type, amount_raw in rewards.items():
            amount = int(amount_raw or 0)
            if amount <= 0:
                continue
            if reward_type in {"candies", "trick_coins", "dice_count", "xp_bank"}:
                await self.users_repo.add_currency(user_id, reward_type, amount)
                if reward_type in metric_mapping:
                    await self.mission_service.increment_guild_currency(guild_id, reward_type, amount)
                    await self.mission_service.increment_user_guild_metric(
                        guild_id,
                        user_id,
                        metric_mapping[reward_type],
                        amount,
                    )
                reward_key = reward_keys.get(reward_type)
                if reward_key:
                    lines.append(f"- {self.i18n.t(locale, reward_key, amount=amount)}")
                continue

            total = await self.users_repo.add_inventory_item(user_id, reward_type, amount)
            item_name = self.i18n.maybe_t(locale, f"item.{reward_type}.name") or reward_type
            lines.append(
                f"- {self.i18n.t(locale, 'baskets.reward.item', item_name=item_name, amount=amount, total=total)}"
            )

        return lines

    async def _collect_lap_milestone_lines(
        self,
        user_id: int,
        locale: str,
        previous_laps: int,
        new_laps: int,
        *,
        guild_id: int | None = None,
    ) -> list[str]:
        if new_laps <= previous_laps:
            return []

        milestone_cfg = self.board_config.get("lap_milestones", {})
        if not isinstance(milestone_cfg, dict):
            return []

        lines: list[str] = []
        recurring_basket_rewards = self.board_config.get("recurring_basket_rewards", [])
        for lap_value in range(previous_laps + 1, new_laps + 1):
            for rule in recurring_basket_rewards:
                if not isinstance(rule, dict):
                    continue
                every_laps = int(rule.get("every_laps", 0) or 0)
                item_id = str(rule.get("item_id") or "")
                amount = int(rule.get("amount", 1) or 1)
                exclude_multiples_of = {
                    int(value)
                    for value in rule.get("exclude_multiples_of", [])
                    if str(value).isdigit() and int(value) > 0
                }
                if every_laps <= 0 or not item_id or lap_value % every_laps != 0:
                    continue
                if any(lap_value % divisor == 0 for divisor in exclude_multiples_of):
                    continue
                lines.extend(
                    await self._grant_lap_milestone_rewards(
                        user_id,
                        locale,
                        lap_value,
                        {item_id: amount},
                        guild_id=guild_id,
                    )
                )
        reached = [
            (int(lap_key), dict(rewards))
            for lap_key, rewards in milestone_cfg.items()
            if str(lap_key).isdigit()
            and previous_laps < int(lap_key) <= new_laps
            and isinstance(rewards, dict)
        ]
        for lap_value, rewards in sorted(reached, key=lambda item: item[0]):
            lines.extend(
                await self._grant_lap_milestone_rewards(
                    user_id,
                    locale,
                    lap_value,
                    rewards,
                    guild_id=guild_id,
                )
            )
        return lines

    async def _grant_board_100_role_if_needed(self, guild: discord.Guild | None, user_id: int, laps: int) -> discord.Role | None:
        if guild is None or laps < 100:
            return None
        config = await self.admin_repo.get_config(guild.id)
        role_id = config.get("board_100_role_id")
        if not role_id:
            return None
        role = guild.get_role(int(role_id))
        member = guild.get_member(user_id)
        if role is None or member is None or role in member.roles:
            return None
        try:
            await member.add_roles(role, reason=f"Tabuleiro: {laps} voltas")
        except (discord.Forbidden, discord.HTTPException):
            return None
        return role

    @app_commands.command(name="tabuleiro", description="Mostra seu tabuleiro atual.")
    async def tabuleiro(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        position, laps, dice_count, board_file = await self._build_board_panel_payload(interaction.user.id)
        locale = await self.i18n.get_user_locale(interaction.user.id)
        await interaction.followup.send(
            content=self.texts.panel_content(locale, position, laps, dice_count),
            file=board_file,
            view=BoardView(self, interaction.user.id, dice_count, laps, locale),
            ephemeral=True,
        )

    async def show_board_panel(self, interaction: discord.Interaction, owner_id: int) -> None:
        await interaction.response.defer()
        position, laps, dice_count, board_file = await self._build_board_panel_payload(owner_id)
        locale = await self.i18n.get_user_locale(owner_id)
        await interaction.edit_original_response(
            content=self.texts.panel_content(locale, position, laps, dice_count),
            embed=None,
            attachments=[board_file],
            view=BoardView(self, owner_id, dice_count, laps, locale),
        )

    async def send_board_panel(self, interaction: discord.Interaction, owner_id: int) -> None:
        """Envia o tabuleiro separado quando o painel de origem usa Components V2."""
        await interaction.response.defer(ephemeral=True)
        position, laps, dice_count, board_file = await self._build_board_panel_payload(owner_id)
        locale = await self.i18n.get_user_locale(owner_id)
        await interaction.followup.send(
            content=self.texts.panel_content(locale, position, laps, dice_count),
            file=board_file,
            view=BoardView(self, owner_id, dice_count, laps, locale),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _build_board_panel_payload(self, user_id: int) -> tuple[int, int, int, discord.File]:
        position, laps = await self._get_or_create_player(user_id)
        profile = await self.users_repo.get_user(user_id)
        dice_count = int(profile.get("dice_count", 0))
        board_file = await self._build_board_file(position)
        return position, laps, dice_count, board_file

    async def handle_roll(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        locale = await self.i18n.get_user_locale(user_id)
        if user_id in self._rolling_users:
            release_at = self._rolling_until.get(user_id, datetime.now(timezone.utc) + timedelta(seconds=6))
            await self._send_board_notice(
                interaction,
                self.texts.rolling_notice_title(locale),
                self.texts.rolling_notice(locale, discord.utils.format_dt(release_at, style="R")),
            )
            return

        self._rolling_users.add(user_id)
        try:
            await interaction.response.defer(ephemeral=True)
            async with self._get_lock(user_id):
                try:
                    await self.users_repo.spend_currency(user_id, "dice_count", 1)
                except ValueError:
                    await self._send_board_notice(
                        interaction,
                        self.texts.no_dice_notice_title(locale),
                        self.texts.no_dice_notice(locale),
                    )
                    return

                current_position, current_laps = await self._get_or_create_player(user_id)
                roll_value = random.randint(1, 6)
                animation_duration_seconds = get_roll_animation_duration_seconds(roll_value)
                self._rolling_until[user_id] = datetime.now(timezone.utc) + timedelta(seconds=animation_duration_seconds)

                raw_destination = current_position + roll_value
                laps_gained = raw_destination // self.total_tiles
                new_position = normalize_position(raw_destination, self.total_tiles)
                new_laps = current_laps + laps_gained

                await self._save_player(user_id, new_position, new_laps)
                if laps_gained > 0:
                    await self.mission_service.increment_guild_metric(
                        interaction.guild.id if interaction.guild else None,
                        "board_laps",
                        laps_gained,
                    )
                    await self.mission_service.increment_user_guild_metric(
                        interaction.guild.id if interaction.guild else None,
                        user_id,
                        "board_laps",
                        laps_gained,
                    )
                tile_type = self.board_tiles[new_position]
                milestone_lines = await self._collect_lap_milestone_lines(
                    user_id,
                    locale,
                    current_laps,
                    new_laps,
                    guild_id=interaction.guild.id if interaction.guild else None,
                )
                board_100_role = await self._grant_board_100_role_if_needed(interaction.guild, user_id, new_laps)
                mission_notices = await self.mission_service.evaluate_progress(
                    interaction.guild.id if interaction.guild else None,
                    user_id,
                )
                profile = await self.users_repo.get_user(user_id)
                dice_count = int(profile.get("dice_count", 0))

            roll_file = await self._build_roll_file(current_position, roll_value, tile_type)

            lines = []
            if laps_gained > 0:
                lines.append(self.i18n.t(locale, "board.reward.lap", laps=new_laps))

            await interaction.edit_original_response(
                content=None,
                attachments=[roll_file],
                view=BoardView(self, interaction.user.id, dice_count, new_laps, locale),
            )
            await asyncio.sleep(animation_duration_seconds)
            reward_message = await self._apply_tile_reward(
                user_id,
                tile_type,
                new_laps,
                guild_id=interaction.guild.id if interaction.guild else None,
                guild=interaction.guild,
            )
            notice_thumbnail_path = self._notice_thumbnail_path(tile_type)
            if reward_message:
                lines.append(reward_message)
            lines.extend(milestone_lines)
            lines.extend(mission_notices)
            if lines:
                notice_text = "\n".join(lines)
                century_lap = self._highest_special_lap(current_laps, new_laps, divisor=100)
                special_lap = self._highest_special_lap(current_laps, new_laps, divisor=10)
                if century_lap is not None:
                    await self._send_board_notice(
                        interaction,
                        self.texts.result_notice_title(locale),
                        notice_text,
                        auto_delete=False,
                        embed=self.texts.build_special_lap_embed(locale, century_lap, notice_text),
                        thumbnail_path=notice_thumbnail_path,
                    )
                    await self._send_public_century_lap_announcement(
                        interaction.guild,
                        locale,
                        interaction.user,
                        century_lap,
                        notice_text,
                        role_mention=board_100_role.mention if board_100_role is not None else None,
                    )
                elif special_lap is not None:
                    await self._send_board_notice(
                        interaction,
                        self.texts.result_notice_title(locale),
                        notice_text,
                        auto_delete=False,
                        embed=self.texts.build_special_lap_embed(locale, special_lap, notice_text),
                        thumbnail_path=notice_thumbnail_path,
                    )
                else:
                    await self._send_board_notice(
                        interaction,
                        self.texts.result_notice_title(locale),
                        notice_text,
                        thumbnail_path=notice_thumbnail_path,
                    )
        finally:
            self._rolling_users.discard(user_id)
            self._rolling_until.pop(user_id, None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TabuleiroCog(bot))
