#!/usr/bin/env python3
"""
Movie Frame Tool
================
Editor rápido pra preparar imagens do bot "qual é o filme?".

Fluxo:  print do filme -> Ctrl+V -> reenquadrar -> efeitos -> Ctrl+S (1000x1000)

Dependências:   pip install pillow numpy
Uso:            python movie_frame_tool.py [imagem_opcional]

Atalhos:
  Ctrl+V  cola imagem da área de transferência
  Ctrl+O  abre arquivo
  Ctrl+S  salva
  Arrastar (botão esquerdo)  move o enquadramento
  Scroll                     zoom
  Botão direito (ou Shift+arrastar)  move o centro do círculo de luz
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import sys
import tkinter as tk
import unicodedata
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import (
    Image,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageGrab,
    ImageOps,
    ImageTk,
)

OUT_SIZE = 1000
PREVIEW_SIZE = 600

# Texto da pergunta que vai pro JSON (edite aqui se quiser outro texto)
QUESTION_PT = "Qual é o filme desta cena?"
QUESTION_EN = "Which movie is this scene from?"
DIFFICULTIES = ["easy", "medium", "hard"]
# Regras do ObserverQuestionCatalog: kind "movie_image", imagem dentro de Assets/Observador/Movies,
# extensão png/jpg/jpeg/webp e no máximo 8 MB (senão a pergunta é ignorada no carregamento).
MEDIA_PREFIX = "Assets/Observador/Movies"
KIND_DEFAULT = "movie_image"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
CONFIG_PATH = Path(__file__).with_name("movie_frame_tool.config.json")


def norm(text: str) -> str:
    """casefold + sem acentos, pra comparar títulos."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text).strip().casefold()


def first_existing(name: str, default_rel: str) -> str:
    here = Path(__file__).resolve().parent
    for base in (Path.cwd(), Path.cwd() / "content" / "halloween", here, here / "content" / "halloween"):
        if (base / name).exists():
            return str(base / name)
    return str(Path.cwd() / default_rel)


def dump_questions(data: list) -> str:
    """JSON indentado, mas com listas de strings numa linha só (igual ao observer_questions.json)."""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    text = re.sub(
        r'\[\s*\n\s*((?:"(?:[^"\\]|\\.)*"(?:,\s*\n\s*)?)+)\s*\n\s*\]',
        lambda m: "[" + re.sub(r",\s*\n\s*", ", ", m.group(1)) + "]",
        text,
    )
    return text + "\n"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

TINTS = ["Nenhum", "Sépia", "Verde (visão noturna)", "Azul frio", "Preto e branco"]

# Valores padrão de TODOS os efeitos (zoom/fill ficam separados)
EFFECT_DEFAULTS: dict = {
    # cor
    "brightness": 0, "contrast": 0, "saturation": 0,
    "tint": "Nenhum", "tint_strength": 60,
    # blur
    "blur": 0, "edge_blur": 0,
    # luz
    "spot_dark": 0, "spot_radius": 35, "spot_soft": 40,
    "spot_x": 50, "spot_y": 50, "vignette": 0,
    # ruído / tv
    "grain": 0, "grain_size": 2, "grain_color": False,
    "static": 0, "scanlines": 0, "glitch": 0, "chroma": 0,
    # overlay de câmera
    "rec": False, "viewfinder": False, "stamp": False,
    "rec_text": "REC", "stamp_text": "31 OCT 1994  23:47:12",
}

PRESETS: dict[str, dict] = {
    "Nenhum": {},
    "Câmera antiga": dict(
        contrast=15, saturation=-45, tint="Sépia", tint_strength=45,
        grain=35, grain_size=2, vignette=55, scanlines=12, blur=0.8,
    ),
    "Found footage (camcorder)": dict(
        saturation=-20, contrast=10, grain=30, grain_size=2, scanlines=25,
        chroma=3, glitch=15, vignette=40, rec=True, viewfinder=True, stamp=True,
    ),
    "Lanterna no escuro": dict(
        brightness=-5, spot_dark=93, spot_radius=26, spot_soft=35,
        vignette=20, grain=20, grain_size=2, contrast=10,
    ),
    "VHS": dict(
        saturation=25, contrast=10, chroma=6, scanlines=35, glitch=35,
        grain=25, static=12, blur=0.8, stamp=True, rec=True,
    ),
    "TV velha": dict(
        saturation=-100, static=35, scanlines=40, vignette=60, chroma=2,
        edge_blur=40, blur=0.8, contrast=15,
    ),
    "Visão noturna": dict(
        tint="Verde (visão noturna)", tint_strength=90, brightness=10,
        grain=45, grain_size=2, scanlines=20, vignette=50, spot_dark=40,
        spot_radius=60, spot_soft=50, viewfinder=True,
    ),
    "Pergunta borrada": dict(
        blur=14, saturation=-15, vignette=25, grain=18, grain_size=2, brightness=-5,
    ),
    "Terror pesado": dict(
        saturation=-60, contrast=25, brightness=-15, vignette=70,
        grain=30, edge_blur=30, spot_dark=35, spot_radius=55, spot_soft=60,
    ),
}


# ----------------------------------------------------------------------------
# Efeitos
# ----------------------------------------------------------------------------
def _smoothstep(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _shift_x(a: np.ndarray, dx: int) -> np.ndarray:
    """Desloca horizontalmente (eixo 1) repetindo a borda."""
    w = a.shape[1]
    dx = int(max(-(w - 1), min(w - 1, dx)))
    if dx == 0:
        return a
    out = np.empty_like(a)
    if dx > 0:
        out[:, dx:] = a[:, :-dx]
        out[:, :dx] = a[:, :1]
    else:
        dx = -dx
        out[:, :-dx] = a[:, dx:]
        out[:, -dx:] = a[:, -1:]
    return out


def _get_font(px: int) -> ImageFont.ImageFont:
    for name in (
        "cour.ttf", "Courier New.ttf", "DejaVuSansMono-Bold.ttf",
        "LiberationMono-Bold.ttf", "Menlo.ttc", "arialbd.ttf",
    ):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=px)
    except TypeError:  # Pillow antigo
        return ImageFont.load_default()


def _text_shadow(d: ImageDraw.ImageDraw, xy, text, font, fill=(255, 255, 255)) -> None:
    x, y = xy
    d.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0))
    d.text((x, y), text, font=font, fill=fill)


def _draw_overlay(img: Image.Image, p: dict, scale: float) -> Image.Image:
    s = img.size[0]
    d = ImageDraw.Draw(img)
    white = (235, 235, 235)
    m = int(s * 0.05)
    pad = int(s * 0.025)
    fsz = max(10, int(s * 0.04))
    font = _get_font(fsz)

    if p["viewfinder"]:
        length = int(s * 0.07)
        t = max(2, int(3 * scale))
        for x, y, sx, sy in [(m, m, 1, 1), (s - m, m, -1, 1), (m, s - m, 1, -1), (s - m, s - m, -1, -1)]:
            d.line([(x, y), (x + sx * length, y)], fill=white, width=t)
            d.line([(x, y), (x, y + sy * length)], fill=white, width=t)
        c, ln = s // 2, int(s * 0.02)
        d.line([(c - ln, c), (c + ln, c)], fill=white, width=max(1, t - 1))
        d.line([(c, c - ln), (c, c + ln)], fill=white, width=max(1, t - 1))

    x0, y0 = m + pad, m + pad
    if p["rec"]:
        r = int(fsz * 0.35)
        cy = y0 + fsz // 2
        d.ellipse([x0, cy - r, x0 + 2 * r, cy + r], fill=(220, 20, 20))
        _text_shadow(d, (x0 + 2 * r + int(fsz * 0.4), y0), str(p["rec_text"]), font)
    if p["stamp"] and str(p["stamp_text"]).strip():
        _text_shadow(d, (x0, s - m - pad - fsz), str(p["stamp_text"]), font)
    return img


def apply_effects(img: Image.Image, p: dict, scale: float, seed: int) -> Image.Image:
    """
    Aplica todos os efeitos numa imagem quadrada.
    `scale` = tamanho_atual / 1000, pra preview e saída final ficarem parecidos.
    """
    img = img.convert("RGB")
    w, h = img.size
    rng = np.random.default_rng(seed)

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    # distância normalizada ao centro (canto = 1)
    dn = np.hypot(xx - w / 2, yy - h / 2) / (np.hypot(w, h) / 2)

    # --- cor ---------------------------------------------------------------
    if p["brightness"]:
        img = ImageEnhance.Brightness(img).enhance(max(0.0, 1 + p["brightness"] / 100))
    if p["contrast"]:
        img = ImageEnhance.Contrast(img).enhance(max(0.0, 1 + p["contrast"] / 100))
    if p["saturation"]:
        img = ImageEnhance.Color(img).enhance(max(0.0, 1 + p["saturation"] / 100))

    # --- blur --------------------------------------------------------------
    if p["blur"] > 0:
        img = img.filter(ImageFilter.GaussianBlur(p["blur"] * scale))
    if p["edge_blur"] > 0:
        blurred = img.filter(ImageFilter.GaussianBlur(p["edge_blur"] / 100 * 25 * scale))
        mask = Image.fromarray((_smoothstep((dn - 0.25) / 0.75) * 255).astype(np.uint8), "L")
        img = Image.composite(blurred, img, mask)

    arr = np.asarray(img, dtype=np.float32).copy()

    # --- tom colorido ------------------------------------------------------
    tint = p["tint"]
    if tint != "Nenhum":
        lum = (arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32))[..., None]
        tones = {
            "Sépia": (1.10, 0.88, 0.62),
            "Verde (visão noturna)": (0.35, 1.15, 0.40),
            "Azul frio": (0.75, 0.95, 1.20),
            "Preto e branco": (1.0, 1.0, 1.0),
        }
        k = p["tint_strength"] / 100
        toned = lum * np.array(tones[tint], dtype=np.float32)
        arr = arr * (1 - k) + toned * k

    # --- luz: círculo + vinheta -------------------------------------------
    if p["spot_dark"] > 0:
        d = np.hypot(xx - p["spot_x"] / 100 * w, yy - p["spot_y"] / 100 * h) / (w / 2)
        radius = max(0.03, p["spot_radius"] / 100)
        soft = max(0.02, p["spot_soft"] / 100 * 0.8)
        outside = _smoothstep((d - radius) / soft)
        arr *= (1 - p["spot_dark"] / 100 * outside)[..., None]
    if p["vignette"] > 0:
        arr *= (1 - p["vignette"] / 100 * _smoothstep((dn - 0.3) / 0.7))[..., None]

    # --- aberração cromática ----------------------------------------------
    if p["chroma"] > 0:
        n = int(round(p["chroma"] * scale))
        if n:
            arr[..., 0] = _shift_x(arr[..., 0], n)
            arr[..., 2] = _shift_x(arr[..., 2], -n)

    # --- glitch (faixas deslocadas) ---------------------------------------
    if p["glitch"] > 0:
        for _ in range(int(2 + p["glitch"] / 100 * 10)):
            bh = int(rng.integers(2, max(4, int(30 * scale) + 3)))
            y = int(rng.integers(0, max(1, h - bh)))
            mag = int((rng.integers(5, int(6 + 60 * p["glitch"] / 100)) * scale))
            sign = 1 if rng.random() < 0.5 else -1
            arr[y:y + bh] = _shift_x(arr[y:y + bh], sign * max(1, mag))

    # --- overlay de câmera -------------------------------------------------
    if p["rec"] or p["viewfinder"] or p["stamp"]:
        tmp = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        arr = np.asarray(_draw_overlay(tmp, p, scale), dtype=np.float32).copy()

    # --- grão ---------------------------------------------------------------
    if p["grain"] > 0:
        gs = max(1, int(round(p["grain_size"] * scale)))
        ch = 3 if p["grain_color"] else 1
        small = rng.normal(0, 1, (-(-h // gs), -(-w // gs), ch)).astype(np.float32)
        noise = small.repeat(gs, 0).repeat(gs, 1)[:h, :w]
        arr += noise * (p["grain"] / 100 * 70)

    # --- estática de TV ----------------------------------------------------
    if p["static"] > 0:
        t = p["static"] / 100
        gs = max(1, int(round(1.5 * scale)))
        small = rng.random((-(-h // gs), -(-w // gs), 1)).astype(np.float32) * 255
        noise = small.repeat(gs, 0).repeat(gs, 1)[:h, :w]
        a = 0.55 * t
        arr = arr * (1 - a) + noise * a
        for _ in range(int(t * 8)):
            bh = int(rng.integers(2, max(4, int(40 * scale))))
            y = int(rng.integers(0, max(1, h - bh)))
            band = rng.random((bh, w, 1)).astype(np.float32) * 255
            ba = 0.5 + 0.4 * t
            arr[y:y + bh] = arr[y:y + bh] * (1 - ba) + band * ba

    # --- scanlines ---------------------------------------------------------
    if p["scanlines"] > 0:
        period = max(2, int(round(4 * scale)))
        f = np.ones(h, dtype=np.float32)
        f[(np.arange(h) % period) < period // 2] = 1 - p["scanlines"] / 100 * 0.6
        arr *= f[:, None, None]

    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


# ----------------------------------------------------------------------------
# Interface
# ----------------------------------------------------------------------------
def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()


class App:
    def __init__(self, root: tk.Tk, initial: str | None = None) -> None:
        self.root = root
        root.title("Movie Frame Tool — 1000x1000")
        self.src: Image.Image | None = None
        self.cx = self.cy = 0.0
        self.seed = random.randrange(2**31)
        self._job = None
        self._drag = None
        self.photo = None

        # variáveis
        self.vars: dict[str, tk.Variable] = {}
        for key, val in EFFECT_DEFAULTS.items():
            if isinstance(val, bool):
                self.vars[key] = tk.BooleanVar(value=val)
            elif isinstance(val, str):
                self.vars[key] = tk.StringVar(value=val)
            else:
                self.vars[key] = tk.DoubleVar(value=val)
        self.vars["zoom"] = tk.DoubleVar(value=100)
        self.vars["fill"] = tk.StringVar(value="Blur")
        for v in self.vars.values():
            v.trace_add("write", self.schedule)

        self.show_orig = tk.BooleanVar(value=False)
        self.show_orig.trace_add("write", self.schedule)
        cfg = load_config()
        self.movies: list[list[str]] = []
        self.movie_var = tk.StringVar()
        self.extras_var = tk.StringVar()
        self.diff_var = tk.StringVar(value="medium")
        self.answers_var = tk.StringVar()
        self.add_json_var = tk.BooleanVar(value=True)
        self.reveal_var = tk.BooleanVar(value=cfg.get("reveal", True))
        media_dir = Path.cwd() / "Assets" / "Observador" / "Movies"
        self.out_var = tk.StringVar(
            value=cfg.get("out") or str(media_dir if media_dir.is_dir() else Path.cwd() / "output"))
        self.json_var = tk.StringVar(
            value=cfg.get("json") or first_existing("observer_movie_questions.json", "observer_movie_questions.json"))
        self.movies_var = tk.StringVar(
            value=cfg.get("movies") or first_existing("horror_movies.json", "horror_movies.json"))
        old_prefix = cfg.get("prefix")
        self.prefix_var = tk.StringVar(
            value=MEDIA_PREFIX if old_prefix in (None, "", "content/halloween/movies") else old_prefix)
        self.kind_var = tk.StringVar(value=cfg.get("kind") or KIND_DEFAULT)
        self.fmt_var = tk.StringVar(value=cfg.get("fmt") or "PNG")
        self.clear_var = tk.BooleanVar(value=True)
        self.movie_var.trace_add("write", self._update_answers)
        self.extras_var.trace_add("write", self._update_answers)
        self.status = tk.StringVar(value="Cole um print com Ctrl+V ou abra um arquivo.")

        self._build_ui()
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.reload_movies()
        if initial:
            self.open_path(initial)
        self.schedule()

    # ---- construção da UI -------------------------------------------------
    def _build_ui(self) -> None:
        left = ttk.Frame(self.root, padding=8)
        left.grid(row=0, column=0, sticky="n")
        right = ttk.Frame(self.root, padding=(0, 8, 8, 8))
        right.grid(row=0, column=1, sticky="ns")

        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Colar (Ctrl+V)", command=self.paste).pack(side="left")
        ttk.Button(bar, text="Abrir…", command=self.open_dialog).pack(side="left", padx=4)
        ttk.Checkbutton(bar, text="Ver original", variable=self.show_orig).pack(side="right")

        self.canvas = tk.Canvas(left, width=PREVIEW_SIZE, height=PREVIEW_SIZE,
                                bg="#111", highlightthickness=0, cursor="fleur")
        self.canvas.pack()
        self.img_id = self.canvas.create_image(0, 0, anchor="nw")
        self.hint_id = self.canvas.create_text(
            PREVIEW_SIZE // 2, PREVIEW_SIZE // 2, fill="#888", justify="center",
            text="Cole um print (Ctrl+V)\nou abra uma imagem", font=("TkDefaultFont", 14))
        ttk.Label(left, foreground="#666", wraplength=PREVIEW_SIZE,
                  text="Arraste = mover enquadramento · Scroll = zoom · "
                       "Botão direito = mover círculo de luz").pack(pady=(6, 0))

        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag_move)
        for seq in ("<ButtonPress-3>", "<B3-Motion>", "<ButtonPress-2>", "<B2-Motion>"):
            c.bind(seq, self._move_spot)
        c.bind("<Shift-ButtonPress-1>", self._move_spot)
        c.bind("<Shift-B1-Motion>", self._move_spot)
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", lambda e: self._zoom_by(1.08))
        c.bind("<Button-5>", lambda e: self._zoom_by(1 / 1.08))

        # presets
        pr = ttk.Frame(right)
        pr.pack(fill="x", pady=(0, 6))
        ttk.Label(pr, text="Preset:").pack(side="left")
        self.preset_var = tk.StringVar(value="Nenhum")
        cb = ttk.Combobox(pr, textvariable=self.preset_var, values=list(PRESETS),
                          state="readonly", width=26)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.apply_preset(self.preset_var.get()))
        ttk.Button(pr, text="Novo ruído", command=self.new_noise).pack(side="left")

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        tabs = {n: ttk.Frame(nb, padding=10) for n in
                ("Enquadrar", "Cor e blur", "Luz", "Ruído e TV", "Câmera", "Projeto")}
        for name, fr in tabs.items():
            nb.add(fr, text=name)

        # Enquadrar
        t = tabs["Enquadrar"]
        self._slider(t, "zoom", "Zoom (%)", 20, 600)
        row = ttk.Frame(t)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="Preencher", command=self.fit_cover).pack(side="left")
        ttk.Button(row, text="Imagem inteira", command=self.fit_contain).pack(side="left", padx=4)
        ttk.Button(row, text="Centralizar", command=self.center).pack(side="left")
        row = ttk.Frame(t)
        row.pack(fill="x", pady=6)
        ttk.Label(row, text="Sobra nas bordas:").pack(side="left")
        ttk.Combobox(row, textvariable=self.vars["fill"], values=["Blur", "Preto"],
                     state="readonly", width=8).pack(side="left", padx=6)
        ttk.Label(t, foreground="#666", wraplength=330,
                  text="“Preencher” corta pra um quadrado. “Imagem inteira” mostra o print "
                       "completo e preenche o resto com blur ou preto.").pack(anchor="w", pady=6)

        # Cor e blur
        t = tabs["Cor e blur"]
        self._slider(t, "brightness", "Brilho", -100, 100)
        self._slider(t, "contrast", "Contraste", -100, 100)
        self._slider(t, "saturation", "Saturação", -100, 100)
        row = ttk.Frame(t)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Tom:").pack(side="left")
        ttk.Combobox(row, textvariable=self.vars["tint"], values=TINTS,
                     state="readonly", width=22).pack(side="left", padx=6)
        self._slider(t, "tint_strength", "Intensidade do tom", 0, 100)
        ttk.Separator(t).pack(fill="x", pady=8)
        self._slider(t, "blur", "Blur geral", 0, 40, "{:.1f}")
        self._slider(t, "edge_blur", "Blur nas bordas (foco central)", 0, 100)

        # Luz
        t = tabs["Luz"]
        self._slider(t, "spot_dark", "Escuridão fora do círculo", 0, 100)
        self._slider(t, "spot_radius", "Raio do círculo", 3, 100)
        self._slider(t, "spot_soft", "Suavidade da borda", 0, 100)
        self._slider(t, "spot_x", "Posição X", 0, 100)
        self._slider(t, "spot_y", "Posição Y", 0, 100)
        ttk.Separator(t).pack(fill="x", pady=8)
        self._slider(t, "vignette", "Vinheta", 0, 100)

        # Ruído e TV
        t = tabs["Ruído e TV"]
        self._slider(t, "grain", "Grão / noise", 0, 100)
        self._slider(t, "grain_size", "Tamanho do grão", 1, 6)
        ttk.Checkbutton(t, text="Grão colorido", variable=self.vars["grain_color"]).pack(anchor="w")
        ttk.Separator(t).pack(fill="x", pady=8)
        self._slider(t, "static", "Estática de TV", 0, 100)
        self._slider(t, "scanlines", "Scanlines", 0, 100)
        self._slider(t, "glitch", "Glitch (faixas)", 0, 100)
        self._slider(t, "chroma", "Aberração cromática", 0, 20, "{:.1f}")

        # Câmera
        t = tabs["Câmera"]
        ttk.Checkbutton(t, text="Mostrar ● REC", variable=self.vars["rec"]).pack(anchor="w")
        ttk.Entry(t, textvariable=self.vars["rec_text"], width=30).pack(anchor="w", pady=(2, 8))
        ttk.Checkbutton(t, text="Mostrar data/hora", variable=self.vars["stamp"]).pack(anchor="w")
        ttk.Entry(t, textvariable=self.vars["stamp_text"], width=30).pack(anchor="w", pady=(2, 8))
        ttk.Checkbutton(t, text="Moldura de câmera (cantos + mira)",
                        variable=self.vars["viewfinder"]).pack(anchor="w")

        # Projeto (caminhos)
        t = tabs["Projeto"]

        def path_row(label, var, cmd=None):
            ttk.Label(t, text=label).pack(anchor="w", pady=(6, 0))
            r = ttk.Frame(t)
            r.pack(fill="x")
            ttk.Entry(r, textvariable=var).pack(side="left", fill="x", expand=True)
            if cmd:
                ttk.Button(r, text="…", width=3, command=cmd).pack(side="left", padx=(4, 0))

        path_row("Pasta onde salvar as imagens:", self.out_var, self.pick_folder)
        path_row("JSON de perguntas (onde adicionar):", self.json_var, self.pick_json)
        path_row("image_path no JSON (precisa estar em Assets/Observador/Movies):", self.prefix_var)
        path_row("horror_movies.json (lista de filmes):", self.movies_var, self.pick_movies)
        path_row("Campo \"kind\" (o bot exige movie_image pra usar a imagem):", self.kind_var)
        r = ttk.Frame(t)
        r.pack(fill="x", pady=(10, 0))
        ttk.Label(r, text="Formato da imagem:").pack(side="left")
        ttk.Combobox(r, textvariable=self.fmt_var, values=["PNG", "JPG"],
                     state="readonly", width=6).pack(side="left", padx=6)

        # salvar
        sv = ttk.LabelFrame(right, text="Salvar", padding=8)
        sv.pack(fill="x", pady=(8, 0))
        ttk.Label(sv, text="Filme:").grid(row=0, column=0, sticky="w")
        self.movie_cb = ttk.Combobox(sv, textvariable=self.movie_var, width=34)
        self.movie_cb.grid(row=0, column=1, sticky="ew", pady=2)
        self.movie_cb.bind("<KeyRelease>", self._filter_movies)
        ttk.Label(sv, text="Respostas extras (;):").grid(row=1, column=0, sticky="w")
        ttk.Entry(sv, textvariable=self.extras_var).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(sv, text="Dificuldade:").grid(row=2, column=0, sticky="w")
        ttk.Combobox(sv, textvariable=self.diff_var, values=DIFFICULTIES,
                     state="readonly", width=8).grid(row=2, column=1, sticky="w", pady=2)
        ttk.Label(sv, textvariable=self.answers_var, foreground="#555", wraplength=330).grid(
            row=3, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(sv, text="Adicionar ao JSON de perguntas",
                        variable=self.add_json_var).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(sv, text="Salvar também a versão revelada (sem efeitos)",
                        variable=self.reveal_var).grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(sv, text="Limpar imagem e filme após salvar",
                        variable=self.clear_var).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Button(sv, text="Salvar 1000x1000 (Ctrl+S)", command=self.save).grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        sv.columnconfigure(1, weight=1)
        ttk.Label(right, textvariable=self.status, wraplength=400, foreground="#2a6").pack(
            anchor="w", pady=(6, 0))

    def _slider(self, parent, key, label, lo, hi, fmt="{:.0f}") -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, width=26).grid(row=0, column=0, sticky="w")
        val = ttk.Label(row, width=5, anchor="e")
        val.grid(row=0, column=2)
        ttk.Scale(row, from_=lo, to=hi, variable=self.vars[key], orient="horizontal").grid(
            row=1, column=0, columnspan=3, sticky="ew")
        row.columnconfigure(0, weight=1)

        def upd(*_):
            try:
                val.config(text=fmt.format(self.vars[key].get()))
            except tk.TclError:
                pass

        self.vars[key].trace_add("write", upd)
        upd()

    def _bind_keys(self) -> None:
        def guarded(fn):
            def handler(e):
                if isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry, ttk.Combobox)):
                    return None  # deixa o paste normal nos campos de texto
                fn()
                return "break"
            return handler

        self.root.bind("<Control-v>", guarded(self.paste))
        self.root.bind("<Control-o>", lambda e: self.open_dialog())
        self.root.bind("<Control-s>", lambda e: self.save())
        try:
            self.root.bind("<Command-v>", guarded(self.paste))
            self.root.bind("<Command-s>", lambda e: self.save())
        except tk.TclError:
            pass

    # ---- carregar imagem ---------------------------------------------------
    def load_image(self, im: Image.Image) -> None:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
            im = Image.alpha_composite(bg, im)
        self.src = im.convert("RGB")
        self.vars["zoom"].set(100)
        self.center()
        self.status.set(f"Imagem carregada: {self.src.size[0]}x{self.src.size[1]}")

    def open_path(self, path: str) -> None:
        try:
            self.load_image(Image.open(path))
        except Exception as exc:
            messagebox.showerror("Erro", f"Não consegui abrir a imagem:\n{exc}")

    def open_dialog(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("Imagens", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"), ("Todos", "*.*")])
        if path:
            self.open_path(path)

    def paste(self) -> None:
        try:
            data = ImageGrab.grabclipboard()
        except Exception as exc:
            messagebox.showerror(
                "Clipboard",
                f"Não consegui ler a área de transferência:\n{exc}\n\n"
                "No Linux instale xclip (X11) ou wl-clipboard (Wayland).")
            return
        if isinstance(data, Image.Image):
            self.load_image(data)
        elif isinstance(data, list) and data:
            self.open_path(str(data[0]))
        else:
            self.status.set("Nenhuma imagem na área de transferência.")

    # ---- enquadramento -----------------------------------------------------
    def center(self) -> None:
        if self.src:
            self.cx, self.cy = self.src.size[0] / 2, self.src.size[1] / 2
        self.schedule()

    def fit_cover(self) -> None:
        self.vars["zoom"].set(100)
        self.center()

    def fit_contain(self) -> None:
        if self.src:
            w, h = self.src.size
            self.vars["zoom"].set(max(20, min(w, h) / max(w, h) * 100))
        self.center()

    def _side(self) -> float:
        w, h = self.src.size
        return min(w, h) / max(0.2, self.vars["zoom"].get() / 100)

    def _press(self, e) -> None:
        self._drag = (e.x, e.y, self.cx, self.cy)

    def _drag_move(self, e) -> None:
        if not self.src or not self._drag:
            return
        sx, sy, cx0, cy0 = self._drag
        k = self._side() / PREVIEW_SIZE
        w, h = self.src.size
        self.cx = min(max(cx0 - (e.x - sx) * k, 0), w)
        self.cy = min(max(cy0 - (e.y - sy) * k, 0), h)
        self.schedule()

    def _move_spot(self, e) -> None:
        self.vars["spot_x"].set(min(max(e.x / PREVIEW_SIZE * 100, 0), 100))
        self.vars["spot_y"].set(min(max(e.y / PREVIEW_SIZE * 100, 0), 100))
        if self.vars["spot_dark"].get() == 0:
            self.vars["spot_dark"].set(90)

    def _zoom_by(self, f: float) -> None:
        z = min(max(self.vars["zoom"].get() * f, 20), 600)
        self.vars["zoom"].set(z)

    def _wheel(self, e) -> None:
        self._zoom_by(1.08 if e.delta > 0 else 1 / 1.08)

    def frame(self, size: int, resample) -> Image.Image:
        """Recorta/reenquadra a imagem original num quadrado size x size."""
        w, h = self.src.size
        L = self._side()
        x0, y0 = self.cx - L / 2, self.cy - L / 2
        x1, y1 = x0 + L, y0 + L
        outside = x0 < 0 or y0 < 0 or x1 > w or y1 > h
        if outside and self.vars["fill"].get() == "Blur":
            canvas = ImageOps.fit(self.src, (size, size), Image.BILINEAR)
            canvas = canvas.filter(ImageFilter.GaussianBlur(25 * size / OUT_SIZE))
            canvas = ImageEnhance.Brightness(canvas).enhance(0.5)
        else:
            canvas = Image.new("RGB", (size, size), (0, 0, 0))
        ix0, iy0, ix1, iy1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
        if ix1 - ix0 > 0.5 and iy1 - iy0 > 0.5:
            k = size / L
            tw, th = max(1, round((ix1 - ix0) * k)), max(1, round((iy1 - iy0) * k))
            piece = self.src.resize((tw, th), resample, box=(ix0, iy0, ix1, iy1))
            canvas.paste(piece, (round((ix0 - x0) * k), round((iy0 - y0) * k)))
        return canvas

    # ---- render ------------------------------------------------------------
    def params(self) -> dict:
        out = {}
        for key in EFFECT_DEFAULTS:
            try:
                out[key] = self.vars[key].get()
            except tk.TclError:
                out[key] = EFFECT_DEFAULTS[key]
        return out

    def schedule(self, *_) -> None:
        if self._job is not None:
            self.root.after_cancel(self._job)
        self._job = self.root.after(30, self.render_preview)

    def render_preview(self) -> None:
        self._job = None
        if self.src is None:
            self.canvas.itemconfigure(self.img_id, image="")
            self.canvas.itemconfigure(self.hint_id, state="normal")
            return
        self.canvas.itemconfigure(self.hint_id, state="hidden")
        im = self.frame(PREVIEW_SIZE, Image.BILINEAR)
        if not self.show_orig.get():
            im = apply_effects(im, self.params(), PREVIEW_SIZE / OUT_SIZE, self.seed)
        self.photo = ImageTk.PhotoImage(im)
        self.canvas.itemconfigure(self.img_id, image=self.photo)

    # ---- presets / ações ---------------------------------------------------
    def apply_preset(self, name: str) -> None:
        values = {**EFFECT_DEFAULTS, **PRESETS.get(name, {})}
        for key, val in values.items():
            self.vars[key].set(val)

    def new_noise(self) -> None:
        self.seed = random.randrange(2**31)
        self.schedule()

    def pick_folder(self) -> None:
        d = filedialog.askdirectory(initialdir=self.out_var.get() or ".")
        if d:
            self.out_var.set(d)

    def pick_json(self) -> None:
        f = filedialog.askopenfilename(title="JSON de perguntas",
                                       filetypes=[("JSON", "*.json"), ("Todos", "*.*")])
        if f:
            self.json_var.set(f)

    def pick_movies(self) -> None:
        f = filedialog.askopenfilename(title="horror_movies.json",
                                       filetypes=[("JSON", "*.json"), ("Todos", "*.*")])
        if f:
            self.movies_var.set(f)
            self.reload_movies()

    # ---- filmes / respostas -----------------------------------------------
    def reload_movies(self) -> None:
        self.movies = []
        path = Path(self.movies_var.get())
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data:
                if isinstance(item, str):
                    titles = [item]
                elif isinstance(item, dict):
                    titles = item.get("titles") or ([item["title"]] if item.get("title") else [])
                else:
                    continue
                titles = [str(t).strip() for t in titles if str(t).strip()]
                if titles:
                    self.movies.append(titles)
        except (OSError, ValueError, TypeError):
            self.status.set(f"horror_movies.json não encontrado em {path} — dá pra digitar o filme "
                            "à mão (ajuste o caminho na aba Projeto).")
        self.movie_cb["values"] = [t[0] for t in self.movies]
        self._update_answers()

    def _filter_movies(self, e) -> None:
        if e.keysym in ("Up", "Down", "Return", "Tab", "Escape", "Left", "Right"):
            return
        txt = norm(self.movie_var.get())
        self.movie_cb["values"] = [t[0] for t in self.movies if not txt or any(txt in norm(x) for x in t)]

    def compute_answers(self) -> list[str]:
        typed = self.movie_var.get().strip()
        if not typed:
            return []
        key = norm(typed)
        titles = [typed]
        for entry in self.movies:
            if any(norm(t) == key for t in entry):
                titles = list(entry)
                break
        extras = [x.strip() for x in self.extras_var.get().split(";") if x.strip()]
        seen, out = set(), []
        for t in titles + extras:
            if t.casefold() not in seen:
                seen.add(t.casefold())
                out.append(t)
        return out

    def _update_answers(self, *_) -> None:
        ans = self.compute_answers()
        self.answers_var.set("Respostas aceitas: " + " | ".join(ans) if ans else "")

    # ---- salvar ------------------------------------------------------------
    def _config_dict(self) -> dict:
        return {"out": self.out_var.get(), "json": self.json_var.get(), "movies": self.movies_var.get(),
                "prefix": self.prefix_var.get(), "kind": self.kind_var.get(), "fmt": self.fmt_var.get(),
                "reveal": self.reveal_var.get()}

    def on_close(self) -> None:
        try:
            CONFIG_PATH.write_text(json.dumps(self._config_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        self.root.destroy()

    def save(self) -> None:
        if self.src is None:
            self.status.set("Nada pra salvar — cole ou abra uma imagem primeiro.")
            return
        answers = self.compute_answers()
        add_json = self.add_json_var.get()
        if add_json and not answers:
            self.status.set("Escolha ou digite o filme antes de salvar (ele vira a resposta no JSON).")
            return

        ext = "png" if self.fmt_var.get() == "PNG" else "jpg"
        folder = Path(self.out_var.get() or ".")
        jpath = Path(self.json_var.get())
        data: list = []
        qid = None

        # 1) valida o JSON ANTES de gravar qualquer coisa
        if add_json:
            if jpath.exists():
                try:
                    data = json.loads(jpath.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    messagebox.showerror("JSON inválido", f"Não consegui ler {jpath}:\n{exc}\n\nNada foi salvo.")
                    return
                if not isinstance(data, list):
                    messagebox.showerror("JSON inválido", f"{jpath} não é uma lista. Nada foi salvo.")
                    return
            nums = []
            for it in data:
                m = re.fullmatch(r"movie_(\d+)", str(it.get("id", ""))) if isinstance(it, dict) else None
                if m:
                    nums.append(int(m.group(1)))
            n = max(nums, default=0) + 1
            while (folder / f"movie_{n:03d}.{ext}").exists() or \
                    (folder / f"movie_{n:03d}_revealed.{ext}").exists():
                n += 1
            qid = f"movie_{n:03d}"

        if add_json:
            pre = self.prefix_var.get().strip().replace("\\", "/").strip("/")
            if not (pre == MEDIA_PREFIX or pre.startswith(MEDIA_PREFIX + "/")):
                if not messagebox.askyesno(
                        "image_path fora da pasta permitida",
                        f"O bot só aceita imagens dentro de “{MEDIA_PREFIX}”. Com “{pre or '(vazio)'}” "
                        "a pergunta será ignorada no carregamento.\n\nSalvar mesmo assim?"):
                    return
            if self.kind_var.get().strip() != KIND_DEFAULT and not messagebox.askyesno(
                    "kind diferente de movie_image",
                    "Sem kind “movie_image” o bot trata a pergunta como texto e não envia a imagem.\n\n"
                    "Salvar mesmo assim?"):
                return

        reveal = self.reveal_var.get()
        if reveal and self.params()["blur"] < 3 and not messagebox.askyesno(
                "Blur baixo",
                "A versão da pergunta está quase nítida (blur geral < 3).\n"
                "Como a imagem normal só aparece no acerto, o ideal é a da pergunta ficar borrada "
                "(use o preset “Pergunta borrada” ou o slider “Blur geral”).\n\nSalvar mesmo assim?"):
            return

        # 2) imagem. O arquivo leva o ID (e não o título) pra não entregar a resposta no nome.
        stem = qid or slugify(self.movie_var.get()) or dt.datetime.now().strftime("frame_%Y%m%d_%H%M%S")
        clean = self.frame(OUT_SIZE, Image.LANCZOS)          # só reenquadrada, sem efeito
        im = apply_effects(clean, self.params(), 1.0, self.seed)  # versão da pergunta (com efeitos/blur)
        rev_path = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{stem}.{ext}"
            k = 2
            while path.exists():
                path = folder / f"{stem}_{k}.{ext}"
                k += 1
            if ext == "png":
                im.save(path, optimize=True)
            else:
                im.save(path, quality=93, subsampling=0)
            if reveal:
                rev_path = path.with_name(f"{path.stem}_revealed{path.suffix}")
                if ext == "png":
                    clean.save(rev_path, optimize=True)
                else:
                    clean.save(rev_path, quality=95, subsampling=0)
        except OSError as exc:
            messagebox.showerror("Erro ao salvar a imagem", str(exc))
            return

        # 3) entrada no JSON (gravação atômica)
        msg = f"Salvo: {path.name}" + (f" + {rev_path.name}" if rev_path else "")
        if add_json:
            prefix = self.prefix_var.get().strip().strip("/\\")
            entry = {
                "id": qid,
                "kind": self.kind_var.get().strip() or KIND_DEFAULT,
                "difficulty": self.diff_var.get(),
                "question": QUESTION_PT,
                "question_en": QUESTION_EN,
                "image_path": f"{prefix}/{path.name}" if prefix else path.name,
                "match_mode": "normalized",
                "answers": answers,
            }
            if rev_path is not None:
                entry["revealed_image_path"] = f"{prefix}/{rev_path.name}" if prefix else rev_path.name
            data.append(entry)
            try:
                jpath.parent.mkdir(parents=True, exist_ok=True)
                tmp = jpath.with_suffix(jpath.suffix + ".tmp")
                tmp.write_text(dump_questions(data), encoding="utf-8")
                os.replace(tmp, jpath)
                msg += f"  ·  {qid} adicionado em {jpath.name} ({len(data)} perguntas)"
            except OSError as exc:
                messagebox.showerror("Erro no JSON", f"A imagem foi salva, mas não consegui gravar o JSON:\n{exc}")
                msg += "  ·  JSON NÃO atualizado"

        too_big = [f.name for f in (path, rev_path) if f is not None and f.stat().st_size > MAX_IMAGE_BYTES]
        if too_big:
            messagebox.showwarning(
                "Imagem acima de 8 MB",
                f"{', '.join(too_big)} passa de 8 MB e o bot vai ignorar a pergunta.\n"
                "Regrave em JPG (aba Projeto → Formato) ou reduza o ruído.")
            msg += "  ·  ATENÇÃO: > 8 MB"
        self.status.set(msg)
        self.seed = random.randrange(2**31)  # ruído novo pra próxima imagem
        if self.clear_var.get():
            self.src = None
            self.movie_var.set("")
            self.extras_var.set("")
            self.schedule()


def main() -> None:
    try:  # nitidez em telas HiDPI no Windows
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.mainloop()


if __name__ == "__main__":
    main()
