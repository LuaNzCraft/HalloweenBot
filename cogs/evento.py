from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

import discord
from discord import app_commands
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from repositories.julgamento_repo import JulgamentoRepo
from repositories.public_events_repo import PublicEventsRepo
from services.audit_service import AuditService
from views.event_panel import EventPanelView


LOGGER = logging.getLogger(__name__)


class EventoCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.admin_repo = AdminRepo()
        self.public_events_repo = PublicEventsRepo()
        self.julgamento_repo = JulgamentoRepo()
        self.audit_service = AuditService(bot, self.admin_repo)

    @app_commands.command(name="evento", description="Admin: controla o evento Halloween no servidor.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def evento(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        permissions = getattr(interaction.user, "guild_permissions", None)
        if guild is None or not bool(getattr(permissions, "manage_guild", False)):
            await interaction.response.send_message(
                "Voce precisa de Gerenciar Servidor para abrir o painel do evento.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            panel = await EventPanelView.create(
                self,
                guild=guild,
                moderator_id=interaction.user.id,
            )
            sent = await interaction.followup.send(
                view=panel,
                ephemeral=True,
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            panel.message = sent
        except Exception:
            LOGGER.exception("Falha ao abrir painel de evento da guild %s", guild.id)
            await interaction.followup.send(
                "Nao foi possivel abrir o painel do evento.",
                ephemeral=True,
            )

    async def _comecar(self, interaction: discord.Interaction) -> tuple[bool, str]:
        runtime = await self.admin_repo.get_runtime(interaction.guild.id)
        if runtime.get("event_active"):
            return False, "O evento global ja esta ativo neste servidor."

        now = datetime.now(timezone.utc).isoformat()
        await self.admin_repo.set_runtime(
            interaction.guild.id,
            event_active=True,
            event_started_at=now,
            event_ended_at=None,
            events_paused_until=None,
            pause_reason=None,
        )
        public_events_cog = self.bot.get_cog("PublicEventsCog")
        if public_events_cog is not None and hasattr(public_events_cog, "reset_chat_bonus_window"):
            public_events_cog.reset_chat_bonus_window(interaction.guild.id)
        await self._audit(interaction, "event.start", {"started_at": now})
        return True, "Evento global iniciado no servidor."

    async def _encerrar(self, interaction: discord.Interaction) -> tuple[bool, str]:
        runtime = await self.admin_repo.get_runtime(interaction.guild.id)
        if not runtime.get("event_active"):
            return False, "O evento global ja esta encerrado neste servidor."
        now = datetime.now(timezone.utc).isoformat()
        await self.admin_repo.set_runtime(
            interaction.guild.id,
            event_active=False,
            event_ended_at=now,
            events_paused_until=None,
            pause_reason=None,
        )
        public_events_cog = self.bot.get_cog("PublicEventsCog")
        if public_events_cog is not None and hasattr(public_events_cog, "reset_chat_bonus_window"):
            public_events_cog.reset_chat_bonus_window(interaction.guild.id)
        if public_events_cog is not None and hasattr(public_events_cog, "cancel_active_event"):
            await public_events_cog.cancel_active_event(interaction.guild)
        else:
            await self.public_events_repo.reset_observer_state(interaction.guild.id)
            await self.public_events_repo.reset_spider_state(interaction.guild.id)
            julgamento_cog = self.bot.get_cog("JulgamentoCog")
            if julgamento_cog is not None and hasattr(julgamento_cog, "force_reset"):
                await julgamento_cog.force_reset(interaction.guild, refund_tickets=True)
            else:
                await self.julgamento_repo.reset_runtime(interaction.guild.id)
        await self._audit(interaction, "event.end", {"ended_at": now})
        return True, "Evento global encerrado no servidor."

    async def _pausar(
        self,
        interaction: discord.Interaction,
        horas: int,
        motivo: str | None,
    ) -> tuple[bool, str]:
        runtime = await self.admin_repo.get_runtime(interaction.guild.id)
        if not runtime.get("event_active"):
            return False, "Ative o evento global antes de pausar os eventos publicos."

        paused_until = datetime.now(timezone.utc) + timedelta(hours=horas)
        await self.admin_repo.set_runtime(
            interaction.guild.id,
            events_paused_until=paused_until.isoformat(),
            pause_reason=motivo,
        )
        await self._audit(
            interaction,
            "event.pause",
            {"paused_until": paused_until.isoformat(), "hours": horas, "reason": motivo},
        )
        return (
            True,
            f"Eventos publicos pausados ate {discord.utils.format_dt(paused_until, style='F')}.",
        )

    async def _retomar(self, interaction: discord.Interaction) -> tuple[bool, str]:
        runtime = await self.admin_repo.get_runtime(interaction.guild.id)
        if not runtime.get("event_active"):
            return False, "O evento global ainda nao esta ativo."
        if not runtime.get("events_paused_until"):
            return False, "Os eventos publicos ja estao liberados."

        await self.admin_repo.set_runtime(interaction.guild.id, events_paused_until=None, pause_reason=None)
        await self._audit(interaction, "event.resume", {})
        return True, "Eventos publicos retomados."

    async def _cancelar(self, interaction: discord.Interaction) -> tuple[bool, str]:
        public_events_cog = self.bot.get_cog("PublicEventsCog")
        if public_events_cog is None or not hasattr(public_events_cog, "cancel_active_event"):
            return False, "PublicEventsCog nao esta carregada no momento."

        cancelled = await public_events_cog.cancel_active_event(interaction.guild)

        if not cancelled:
            return False, "Nao ha evento publico ativo para cancelar."

        await self._audit(interaction, "event.public.cancel", {})
        return True, "Evento publico cancelado."

    async def start_public_event(
        self,
        interaction: discord.Interaction,
        event_name: str,
    ) -> tuple[bool, str]:
        public_events_cog = self.bot.get_cog("PublicEventsCog")
        if public_events_cog is None or not hasattr(public_events_cog, "start_manual_event"):
            return False, "PublicEventsCog nao esta carregada no momento."
        return await public_events_cog.start_manual_event(interaction, event_name)

    async def get_event_panel_state(self, guild_id: int) -> dict:
        runtime, observer, spider, carousel = await asyncio.gather(
            self.admin_repo.get_runtime(guild_id),
            self.public_events_repo.get_observer_state(guild_id),
            self.public_events_repo.get_spider_state(guild_id),
            self.julgamento_repo.get_runtime(guild_id),
        )
        active_names: list[str] = []
        if observer.get("active"):
            active_names.append("Observador")
        if spider.get("active"):
            active_names.append("Chuva de Doces")
        if carousel.get("active"):
            active_names.append("Carrossel")

        paused_until = self._parse_timestamp(runtime.get("events_paused_until"))
        return {
            "runtime": runtime,
            "paused": paused_until is not None and paused_until > datetime.now(timezone.utc),
            "active_public_event": ", ".join(active_names) if active_names else "Nenhum",
        }

    def _format_timestamp(self, value: str | None, fallback: str) -> str:
        if not value:
            return fallback
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return discord.utils.format_dt(parsed, style="F")

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    async def _audit(self, interaction: discord.Interaction, action: str, payload: dict) -> None:
        await self.audit_service.log(
            guild_id=interaction.guild.id if interaction.guild else None,
            actor_id=interaction.user.id,
            action=action,
            payload=payload,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventoCog(bot))
