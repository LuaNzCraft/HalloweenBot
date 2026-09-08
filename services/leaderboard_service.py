from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import discord
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from repositories.db import DB_PATH
from repositories.user_guild_metrics_repo import UserGuildMetricsRepo


MAX_TRACKED_MESSAGE_EDITS = 40
LEADERBOARD_PANEL_TYPE = "painel"

LEADERBOARD_ORDER = [
    "tabuleiro",
    "doces",
    "travessuras",
    "baskets_opened",
    "mute_used",
    "switch_used",
]

LEADERBOARD_DEFINITIONS = {
    "tabuleiro": {
        "title": "Exploradores do Tabuleiro",
        "description": "Ranking por quantidade total de voltas completas no evento.",
        "value_label": "voltas",
        "page_label": "Tabuleiro",
    },
    "doces": {
        "title": "Fortuna Doce",
        "description": "Ranking por quantidade total de Moedas Doce recebidas no evento.",
        "value_label": "Moedas Doce recebidas",
        "page_label": "Doces",
    },
    "travessuras": {
        "title": "Mestres da Travessura",
        "description": "Ranking por quantidade total de travessuras aplicadas no evento.",
        "value_label": "travessuras",
        "page_label": "Travessuras",
    },
    "baskets_opened": {
        "title": "Cestas Abertas",
        "description": "Ranking por quantidade total de cestas abertas no evento.",
        "value_label": "cestas",
        "page_label": "Cestas Abertas",
    },
    "mute_used": {
        "title": "Mutes Usados",
        "description": "Ranking por quantidade total de mutes usados no evento.",
        "value_label": "mutes",
        "page_label": "Mutes Usados",
    },
    "switch_used": {
        "title": "Switchs Usados",
        "description": "Ranking por quantidade total de switchs usados no evento.",
        "value_label": "switchs",
        "page_label": "Switchs Usados",
    },
}

MEDALS = {
    1: "🥇",
    2: "🥈",
    3: "🥉",
}


@dataclass(frozen=True)
class LeaderboardRow:
    user_id: int
    value: int


class LeaderboardService:
    def __init__(
        self,
        bot: commands.Bot,
        admin_repo: AdminRepo | None = None,
        user_guild_metrics_repo: UserGuildMetricsRepo | None = None,
        path: Path = DB_PATH,
    ) -> None:
        self.bot = bot
        self.admin_repo = admin_repo or AdminRepo(path)
        self.user_guild_metrics_repo = user_guild_metrics_repo or UserGuildMetricsRepo(path)
        self.path = path

    def normalize_leaderboard_type(self, leaderboard_type: object) -> str:
        if isinstance(leaderboard_type, str) and leaderboard_type in LEADERBOARD_DEFINITIONS:
            return leaderboard_type
        return LEADERBOARD_ORDER[0]

    def get_total_pages(self) -> int:
        return len(LEADERBOARD_ORDER)

    def get_page_index(self, leaderboard_type: str) -> int:
        normalized = self.normalize_leaderboard_type(leaderboard_type)
        return LEADERBOARD_ORDER.index(normalized)

    def get_page_by_index(self, page_index: int) -> str:
        if page_index < 0:
            return LEADERBOARD_ORDER[0]
        if page_index >= len(LEADERBOARD_ORDER):
            return LEADERBOARD_ORDER[-1]
        return LEADERBOARD_ORDER[page_index]

    async def build_embed(self, leaderboard_type: str, guild: discord.Guild | None = None) -> discord.Embed:
        normalized_type = self.normalize_leaderboard_type(leaderboard_type)
        definition = LEADERBOARD_DEFINITIONS[normalized_type]
        rows = await self.get_rows(normalized_type, guild)
        description = definition["description"]
        if guild is not None:
            runtime = await self.admin_repo.get_runtime(guild.id)
            if runtime.get("event_ended_at"):
                description = f"{description}\nEncerrou: {self._format_dt(runtime['event_ended_at'])}"
            else:
                description = f"{description}\nEncerra: data ainda nao configurada"

        embed = discord.Embed(
            title=definition["title"],
            description=description,
            color=discord.Color.from_rgb(245, 122, 39),
        )
        if not rows:
            embed.add_field(name="Top Jogadores", value="Ainda sem dados.", inline=False)
            embed.set_footer(text=self._footer_text(normalized_type))
            return embed

        lines = []
        for index, row in enumerate(rows, start=1):
            prefix = MEDALS.get(index, f"{index}.")
            display_name = self._display_name(guild, row.user_id)
            lines.append(f"{prefix} {display_name} - **{row.value}** {definition['value_label']}")
        embed.add_field(name="Top Jogadores", value="\n".join(lines), inline=False)
        embed.set_footer(text=self._footer_text(normalized_type))
        return embed

    async def get_rows(self, leaderboard_type: str, guild: discord.Guild | None = None, limit: int = 15) -> list[LeaderboardRow]:
        if guild is not None:
            return await self._query_user_guild_rows(guild.id, leaderboard_type, limit)
        if leaderboard_type == "tabuleiro":
            return await self._query_players("voltas", limit)
        if leaderboard_type == "doces":
            return await self._query_users("candies_total", limit)
        if leaderboard_type == "travessuras":
            return await self._query_users("tricks_applied_total", limit)
        if leaderboard_type == "baskets_opened":
            return await self._query_users("baskets_opened_total", limit)
        if leaderboard_type == "mute_used":
            return await self._query_users("mute_used_total", limit)
        if leaderboard_type == "switch_used":
            return await self._query_users("switch_used_total", limit)
        raise ValueError("unknown_leaderboard_type")

    async def _query_user_guild_rows(self, guild_id: int, leaderboard_type: str, limit: int) -> list[LeaderboardRow]:
        metric_mapping = {
            "tabuleiro": "board_laps",
            "doces": "candies_total",
            "travessuras": "tricks_applied_total",
            "baskets_opened": "baskets_opened_total",
            "mute_used": "mute_used_total",
            "switch_used": "switch_used_total",
        }
        metric = metric_mapping.get(leaderboard_type)
        if metric is None:
            raise ValueError("unknown_leaderboard_type")
        rows = await self.user_guild_metrics_repo.get_top_rows(guild_id, metric, limit)
        return [LeaderboardRow(user_id=int(row["user_id"]), value=int(row["value"])) for row in rows]

    async def publish_or_update_panel(self, guild: discord.Guild, view: discord.ui.View) -> discord.Message:
        config = await self.admin_repo.get_config(guild.id)
        channel_id = config.get("leaderboard_channel_id")
        if not channel_id:
            raise ValueError("leaderboard_channel_not_configured")

        channel = guild.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise ValueError("leaderboard_channel_invalid")

        tracked = await self.admin_repo.get_leaderboard_message(guild.id, LEADERBOARD_PANEL_TYPE)
        current_page = self.normalize_leaderboard_type((tracked or {}).get("current_page"))
        embed = await self.build_embed(current_page, guild)
        if tracked and int(tracked.get("edit_count", 0)) < MAX_TRACKED_MESSAGE_EDITS:
            try:
                message = await channel.fetch_message(int(tracked["message_id"]))
                await message.edit(embed=embed, view=view)
                await self.admin_repo.increment_leaderboard_edit_count(guild.id, LEADERBOARD_PANEL_TYPE)
                return message
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        message = await channel.send(embed=embed, view=view)
        await self.admin_repo.set_leaderboard_message(
            guild.id,
            LEADERBOARD_PANEL_TYPE,
            channel.id,
            message.id,
            edit_count=0,
            current_page=current_page,
        )
        return message

    async def set_panel_page(self, guild_id: int, leaderboard_type: str) -> None:
        await self.admin_repo.set_leaderboard_current_page(
            guild_id,
            LEADERBOARD_PANEL_TYPE,
            self.normalize_leaderboard_type(leaderboard_type),
        )

    async def get_panel_page(self, guild_id: int) -> str | None:
        tracked = await self.admin_repo.get_leaderboard_message(guild_id, LEADERBOARD_PANEL_TYPE)
        if not tracked:
            return None
        return self.normalize_leaderboard_type(tracked.get("current_page"))

    async def get_all_panels(self) -> list[tuple[int, int, int, str]]:
        tracked = await self.admin_repo.get_all_leaderboard_messages(LEADERBOARD_PANEL_TYPE)
        result = []
        for row in tracked:
            guild_id = int(row["guild_id"])
            channel_id = int(row["channel_id"])
            message_id = int(row["message_id"])
            active = self.normalize_leaderboard_type(row.get("current_page"))
            result.append((guild_id, channel_id, message_id, active))
        return result

    def _display_name(self, guild: discord.Guild | None, user_id: int) -> str:
        if guild is None:
            return f"<@{user_id}>"
        member = guild.get_member(user_id)
        return member.mention if member else f"<@{user_id}>"

    def _format_dt(self, value: str) -> str:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return discord.utils.format_dt(dt, style="R")

    def _footer_text(self, leaderboard_type: str) -> str:
        definition = LEADERBOARD_DEFINITIONS[leaderboard_type]
        return (
            f"Atualizacao automatica - pagina "
            f"{self.get_page_index(leaderboard_type) + 1}/{self.get_total_pages()}: {definition['page_label']}"
        )

    async def _query_users(self, field: str, limit: int) -> list[LeaderboardRow]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                f"""
                SELECT user_id, {field}
                FROM users
                WHERE {field} > 0
                ORDER BY {field} DESC, user_id ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [LeaderboardRow(user_id=int(row[0]), value=int(row[1])) for row in rows]

    async def _query_players(self, field: str, limit: int) -> list[LeaderboardRow]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                f"""
                SELECT user_id, {field}
                FROM players
                WHERE {field} > 0
                ORDER BY {field} DESC, user_id ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [LeaderboardRow(user_id=int(row[0]), value=int(row[1])) for row in rows]
