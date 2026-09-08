from __future__ import annotations

import json
import logging
from typing import Any

import discord
from discord.ext import commands

from repositories.admin_repo import AdminRepo


LOGGER = logging.getLogger(__name__)
AUDIT_COLOR = discord.Color.from_rgb(96, 181, 118)


class AuditService:
    def __init__(self, bot: commands.Bot, admin_repo: AdminRepo | None = None) -> None:
        self.bot = bot
        self.admin_repo = admin_repo or AdminRepo()

    async def log(
        self,
        *,
        guild_id: int | None,
        actor_id: int | None,
        action: str,
        target_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self.admin_repo.add_audit_log(
            guild_id=guild_id,
            actor_id=actor_id,
            target_id=target_id,
            action=action,
            payload=payload,
        )
        await self._send_to_log_channel(guild_id, actor_id, action, target_id, payload or {})

    async def _send_to_log_channel(
        self,
        guild_id: int | None,
        actor_id: int | None,
        action: str,
        target_id: int | None,
        payload: dict[str, Any],
    ) -> None:
        if guild_id is None:
            return

        config = await self.admin_repo.get_config(guild_id)
        log_channel_id = config.get("log_channel_id")
        if not log_channel_id:
            return

        channel = self.bot.get_channel(int(log_channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(log_channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.exception("Falha ao buscar canal de logs %s", log_channel_id)
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        embed = discord.Embed(title="Audit Log", color=AUDIT_COLOR)
        embed.add_field(name="Ação", value=f"`{action}`", inline=False)
        if actor_id is not None:
            embed.add_field(name="Autor", value=f"<@{actor_id}> (`{actor_id}`)", inline=False)
        if target_id is not None:
            embed.add_field(name="Alvo", value=f"<@{target_id}> (`{target_id}`)", inline=False)
        if payload:
            payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            embed.add_field(name="Payload", value=f"```json\n{payload_text[:900]}\n```", inline=False)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            LOGGER.exception("Falha ao enviar audit log para canal %s", log_channel_id)
