from __future__ import annotations

from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from repositories.julgamento_repo import JulgamentoRepo
from repositories.public_events_repo import PublicEventsRepo
from services.audit_service import AuditService


EVENT_COLOR = discord.Color.from_rgb(245, 122, 39)


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
    @app_commands.choices(
        acao=[
            app_commands.Choice(name="Comecar - ativa o evento no servidor", value="comecar"),
            app_commands.Choice(name="Encerrar - fecha o evento no servidor", value="encerrar"),
            app_commands.Choice(name="Pausar - pausa eventos publicos do chat", value="pausar"),
            app_commands.Choice(name="Retomar - volta com os eventos publicos", value="retomar"),
            app_commands.Choice(name="Cancelar - cancela o evento publico atual", value="cancelar"),
            app_commands.Choice(name="Info - mostra o estado do evento", value="info"),
        ]
    )
    @app_commands.describe(
        acao="Escolha o que fazer no evento.",
        horas="Usado em Pausar: por quantas horas os eventos publicos ficam pausados.",
        motivo="Usado em Pausar: motivo opcional da pausa.",
    )
    async def evento(
        self,
        interaction: discord.Interaction,
        acao: app_commands.Choice[str],
        horas: app_commands.Range[int, 1, 24] = 2,
        motivo: str | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Use esse comando dentro de um servidor.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if acao.value == "comecar":
            await self._comecar(interaction)
            return
        if acao.value == "encerrar":
            await self._encerrar(interaction)
            return
        if acao.value == "pausar":
            await self._pausar(interaction, int(horas), motivo)
            return
        if acao.value == "retomar":
            await self._retomar(interaction)
            return
        if acao.value == "cancelar":
            await self._cancelar(interaction)
            return
        if acao.value == "info":
            await self._info(interaction)
            return

        await interaction.followup.send("Acao desconhecida.", ephemeral=True)

    async def _comecar(self, interaction: discord.Interaction) -> None:
        runtime = await self.admin_repo.get_runtime(interaction.guild.id)
        if runtime.get("event_active"):
            await interaction.followup.send("O evento global ja esta ativo neste servidor.", ephemeral=True)
            return

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
        await interaction.followup.send("Evento global iniciado no servidor.", ephemeral=True)

    async def _encerrar(self, interaction: discord.Interaction) -> None:
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
            await self.public_events_repo.reset_ghostface_state(interaction.guild.id)
            await self.public_events_repo.reset_spider_state(interaction.guild.id)
            julgamento_cog = self.bot.get_cog("JulgamentoCog")
            if julgamento_cog is not None and hasattr(julgamento_cog, "force_reset"):
                await julgamento_cog.force_reset(interaction.guild, refund_tickets=True)
            else:
                await self.julgamento_repo.reset_runtime(interaction.guild.id)
        await self._audit(interaction, "event.end", {"ended_at": now})
        await interaction.followup.send("Evento global encerrado no servidor.", ephemeral=True)

    async def _pausar(self, interaction: discord.Interaction, horas: int, motivo: str | None) -> None:
        runtime = await self.admin_repo.get_runtime(interaction.guild.id)
        if not runtime.get("event_active"):
            await interaction.followup.send("Ative o evento global antes de pausar os eventos publicos.", ephemeral=True)
            return

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
        await interaction.followup.send(
            f"Eventos publicos pausados ate {discord.utils.format_dt(paused_until, style='F')}.",
            ephemeral=True,
        )

    async def _retomar(self, interaction: discord.Interaction) -> None:
        runtime = await self.admin_repo.get_runtime(interaction.guild.id)
        if not runtime.get("event_active"):
            await interaction.followup.send("O evento global ainda nao esta ativo.", ephemeral=True)
            return
        if not runtime.get("events_paused_until"):
            await interaction.followup.send("Os eventos publicos ja estao liberados.", ephemeral=True)
            return

        await self.admin_repo.set_runtime(interaction.guild.id, events_paused_until=None, pause_reason=None)
        await self._audit(interaction, "event.resume", {})
        await interaction.followup.send("Eventos publicos retomados.", ephemeral=True)

    async def _cancelar(self, interaction: discord.Interaction) -> None:
        public_events_cog = self.bot.get_cog("PublicEventsCog")
        if public_events_cog is None or not hasattr(public_events_cog, "cancel_active_event"):
            await interaction.followup.send("PublicEventsCog nao esta carregada no momento.", ephemeral=True)
            return

        cancelled = await public_events_cog.cancel_active_event(interaction.guild)

        if not cancelled:
            await interaction.followup.send("Nao ha evento publico ativo para cancelar.", ephemeral=True)
            return

        await self._audit(interaction, "event.public.cancel", {})
        await interaction.followup.send("Evento publico cancelado.", ephemeral=True)

    async def _info(self, interaction: discord.Interaction) -> None:
        runtime = await self.admin_repo.get_runtime(interaction.guild.id)
        embed = discord.Embed(title="Evento Halloween", color=EVENT_COLOR)
        embed.add_field(name="Ativo", value="Sim" if runtime["event_active"] else "Nao", inline=True)
        embed.add_field(name="Iniciado em", value=self._format_timestamp(runtime["event_started_at"], "Nao iniciado"), inline=False)
        embed.add_field(name="Encerrado em", value=self._format_timestamp(runtime["event_ended_at"], "Nao encerrado"), inline=False)
        embed.add_field(name="Eventos pausados ate", value=self._format_timestamp(runtime["events_paused_until"], "Nao pausado"), inline=False)
        embed.add_field(name="Motivo da pausa", value=runtime["pause_reason"] or "Nenhum", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

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

    async def _audit(self, interaction: discord.Interaction, action: str, payload: dict) -> None:
        await self.audit_service.log(
            guild_id=interaction.guild.id if interaction.guild else None,
            actor_id=interaction.user.id,
            action=action,
            payload=payload,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventoCog(bot))
