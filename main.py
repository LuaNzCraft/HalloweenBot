from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

from services.config_service import get_config_service

ROOT_DIR = Path(__file__).resolve().parent
COGS_DIR = ROOT_DIR / "cogs"
ENV_PATH = ROOT_DIR / ".env"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


class HalloweenBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = True
        intents.reactions = True

        super().__init__(
            command_prefix="!",
            intents=intents,
        )

    async def setup_hook(self) -> None:
        config_service = get_config_service()
        config_service.load()
        logging.info("Configuracao carregada: %s (versao interna %s)", config_service.path, config_service.version)
        await self._load_extensions()

    async def _load_extensions(self) -> None:
        if not COGS_DIR.exists():
            logging.warning("Pasta de cogs não encontrada: %s", COGS_DIR)
            return

        for path in sorted(COGS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            if path.stem == "races":
                logging.info("Cog ignorada: %s", path.stem)
                continue

            extension = f"cogs.{path.stem}"
            try:
                await self.load_extension(extension)
                logging.info("Cog carregado: %s", extension)
            except Exception:
                logging.exception("Falha ao carregar cog: %s", extension)

    async def on_ready(self) -> None:
        if not self.user:
            return

        await self._sync_app_commands()

        logging.info("Bot online como %s (%s)", self.user, self.user.id)

    async def _sync_app_commands(self) -> None:
        guilds = list(self.guilds)
        if guilds:
            for guild in guilds:
                try:
                    synced = await self.tree.sync(guild=guild)
                    logging.info(
                        "Slash commands sincronizados na guild %s (%s): %s",
                        guild.name,
                        guild.id,
                        len(synced),
                    )
                except Exception:
                    logging.exception(
                        "Falha ao sincronizar slash commands na guild %s (%s)",
                        guild.name,
                        guild.id,
                    )

        try:
            synced = await self.tree.sync()
            logging.info("Slash commands globais sincronizados: %s", len(synced))
        except Exception:
            logging.exception("Falha ao sincronizar slash commands globais")


def load_dotenv(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


async def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Defina a variável de ambiente DISCORD_TOKEN antes de iniciar o bot.")

    bot = HalloweenBot()
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
