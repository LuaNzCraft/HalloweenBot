from __future__ import annotations

import asyncio
from collections import deque
import json
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import random
import math
import string
import unicodedata

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks

from repositories.admin_repo import AdminRepo
from repositories.chat_candy_repo import ChatCandyRepo
from repositories.observer_archives_repo import ObserverArchivesRepo
from repositories.public_events_repo import PublicEventsRepo
from repositories.reward_ledger_repo import RewardSettlement
from services.chat_candy_runtime import ChatCandyRuntimeService
from services.config_service import get_carousel_config, get_main_config, get_public_events_config
from services.public_event_runtime import PublicEventRuntimeService
from services.audit_service import AuditService
from services.event_runtime_service import EventRuntimeService
from services.observer_archive_service import ObserverArchiveService
from services.observer_question_catalog import ObserverQuestionCatalog
from services.i18n_service import I18nService
from services.inventory_service import InventoryService
from services.mission_service import MissionService
from services.public_event_texts import PublicEventTextsService
from services.reward_settlement_service import RewardSettlementService
from repositories.users_repo import UsersRepo
from views.public_event_views import ObserverTranslateView, PublicEventTranslateView, ChatMessageBonusView
from utils.event_config import get_achievements_config


ROOT_DIR = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger(__name__)
HORROR_MOVIES_PATH = ROOT_DIR / "content" / "halloween" / "horror_movies.json"
INVISIBLE_SPACE = "\u2060"
SCHEDULER_TIMEZONE = timezone(timedelta(hours=-3), "America/Sao_Paulo")
OBSERVER_CONFUSABLES = str.maketrans(
    {
        "\u0410": "A",
        "\u0430": "a",
        "\u0415": "E",
        "\u0435": "e",
        "\u041E": "O",
        "\u043E": "o",
        "\u0421": "C",
        "\u0441": "c",
    }
)

class PublicEventsCog(commands.Cog):
    OBSERVER_COLOR = discord.Color.from_rgb(120, 12, 12)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.admin_repo = AdminRepo()
        self.audit_service = AuditService(bot, self.admin_repo)
        self.chat_candy_repo = ChatCandyRepo()
        self.public_events_repo = PublicEventsRepo()
        self.observer_archives_repo = ObserverArchivesRepo()
        self.event_runtime_service = EventRuntimeService(self.admin_repo)
        self.chat_candy_runtime = ChatCandyRuntimeService()
        self.public_event_runtime = PublicEventRuntimeService()
        self.observer_catalog = ObserverQuestionCatalog(ROOT_DIR)
        self.observer_archive_service = ObserverArchiveService(ROOT_DIR)
        self.users_repo = UsersRepo()
        self.i18n = I18nService(self.users_repo)
        self.inventory_service = InventoryService(users_repo=self.users_repo, i18n_service=self.i18n)
        self.mission_service = MissionService(users_repo=self.users_repo, i18n_service=self.i18n)
        self.reward_settlement_service = RewardSettlementService(
            users_repo=self.users_repo,
            guild_metrics_repo=self.mission_service.guild_metrics_repo,
            user_guild_metrics_repo=self.mission_service.user_guild_metrics_repo,
        )
        self.texts = PublicEventTextsService()
        self._resolving_observer_guilds: set[int] = set()
        self._observer_timeout_tasks: dict[int, asyncio.Task] = {}
        self._finishing_spider_guilds: set[int] = set()
        self._spider_expire_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._spider_finish_tasks: dict[int, asyncio.Task] = {}
        self._chat_bonus_progress_locks: dict[int, asyncio.Lock] = {}
        self._chat_bonus_message_windows: dict[int, deque[datetime]] = {}
        self._chat_candy_guild_locks: dict[int, asyncio.Lock] = {}
        self._chat_candy_expire_tasks: dict[int, asyncio.Task] = {}
        self._chat_candy_restored = False
        self._last_reward_reconciliation_at: datetime | None = None

    async def cog_load(self) -> None:
        await self.chat_candy_repo.init_db()
        await self.public_events_repo.init_db()
        await self.observer_archives_repo.init_db()
        await self._reset_public_events_on_boot()
        await self._restore_spider_finish_tasks()
        if not self.scheduler_loop.is_running():
            self.scheduler_loop.start()

    def cog_unload(self) -> None:
        if self.scheduler_loop.is_running():
            self.scheduler_loop.cancel()
        for task in self._observer_timeout_tasks.values():
            task.cancel()
        self._observer_timeout_tasks.clear()
        self._resolving_observer_guilds.clear()
        self._finishing_spider_guilds.clear()
        for task in self._spider_expire_tasks.values():
            task.cancel()
        self._spider_expire_tasks.clear()
        for task in self._spider_finish_tasks.values():
            task.cancel()
        self._spider_finish_tasks.clear()
        for task in self._chat_candy_expire_tasks.values():
            task.cancel()
        self._chat_candy_expire_tasks.clear()
        self._chat_candy_guild_locks.clear()

    def _load_config(self) -> dict:
        return get_public_events_config()

    def _load_main_config(self) -> dict:
        return get_main_config()

    async def get_next_scheduled_event(self, guild_id: int) -> tuple[str | None, datetime | None]:
        state = await self.public_events_repo.get_state(guild_id)
        config = self._load_config()
        scheduler = dict(config.get("scheduler") or {})
        now = datetime.now(timezone.utc)
        next_event_name, next_run_at = self._compute_next_scheduled_event(config, state, now)
        stored_next_event_name = str(state.get("next_event_name") or "") or None
        stored_next_run_at = self._parse_dt(state.get("next_run_at"))
        if stored_next_event_name != next_event_name or stored_next_run_at != next_run_at:
            await self.public_events_repo.set_state(
                guild_id,
                next_run_at=next_run_at.isoformat(),
                next_event_name=next_event_name,
                last_public_event_name=state.get("last_public_event_name"),
                last_announced_at=None,
                last_triggered_at=state.get("last_triggered_at"),
            )
        return next_event_name, next_run_at

    async def get_next_public_event(self, guild_id: int) -> tuple[str | None, datetime | None]:
        state = await self.public_events_repo.get_state(guild_id)
        config = self._load_config()
        next_event_name = self._pick_next_public_event(config, str(state.get("last_public_event_name") or "") or None)
        next_run_at = self._next_public_run_at(datetime.now(timezone.utc), dict(config.get("scheduler") or {}))
        return next_event_name, next_run_at

    def _display_event_name(self, value: str | None) -> str:
        normalized = str(value or "").strip().lower()
        if normalized == "julgamento":
            return "Carrossel"
        return str(value or "Indefinido")

    def _display_event_name_localized(self, locale: str, value: str | None) -> str:
        normalized = str(value or "").strip().lower()
        mapping = {
            "observer": "public_events.tempo.event.observer",
            "observador": "public_events.tempo.event.observer",
            "infestacao de aranhas": "public_events.tempo.event.spiders",
            "chuva de doces": "public_events.tempo.event.spiders",
            "julgamento": "public_events.tempo.event.carousel",
            "": "public_events.tempo.event.undefined",
        }
        key = mapping.get(normalized, "public_events.tempo.event.undefined")
        return self.i18n.t(locale, key)

    def _scheduler_display_localized(self, locale: str, scheduler: dict) -> str:
        interval = self._interval_minutes(scheduler)
        public_text = self.i18n.t(locale, "public_events.tempo.schedule.public", minutes=interval)
        julgamento_times = self._julgamento_daily_times(scheduler)
        if julgamento_times:
            formatted = ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in julgamento_times)
            return self.i18n.t(
                locale,
                "public_events.tempo.schedule.with_carousel",
                public=public_text,
                times=formatted,
            )
        return public_text

    @app_commands.command(name="tempo", description="Mostra quando o proximo evento publico vai comecar.")
    @app_commands.checks.cooldown(1, 5.0, key=lambda interaction: interaction.user.id)
    async def tempo(self, interaction: discord.Interaction) -> None:
        locale = await self.i18n.get_user_locale(interaction.user.id)
        if interaction.guild is None:
            await interaction.response.send_message(self.i18n.t(locale, "public_events.tempo.guild_only"), ephemeral=True)
            return

        config = self._load_config()
        scheduler = config.get("scheduler", {})
        enabled_event_names = self._enabled_event_names(config)
        runtime = await self.admin_repo.get_runtime(interaction.guild.id)

        if not scheduler.get("enabled", False):
            await interaction.response.send_message(
                embed=self._build_tempo_unavailable_embed(
                    self.i18n.t(locale, "public_events.tempo.title"),
                    self.i18n.t(locale, "public_events.tempo.scheduler_disabled"),
                ),
                ephemeral=True,
            )
            return

        if not enabled_event_names:
            await interaction.response.send_message(
                embed=self._build_tempo_unavailable_embed(
                    self.i18n.t(locale, "public_events.tempo.title"),
                    self.i18n.t(locale, "public_events.tempo.none_enabled"),
                ),
                ephemeral=True,
            )
            return

        if not runtime.get("event_active"):
            await interaction.response.send_message(
                embed=self._build_tempo_unavailable_embed(
                    self.i18n.t(locale, "public_events.tempo.title"),
                    self.i18n.t(locale, "public_events.tempo.event_inactive"),
                ),
                ephemeral=True,
            )
            return

        paused_until = self._parse_dt(runtime.get("events_paused_until"))
        if paused_until is not None and paused_until > datetime.now(timezone.utc):
            embed = self._build_tempo_unavailable_embed(
                self.i18n.t(locale, "public_events.tempo.title"),
                self.i18n.t(locale, "public_events.tempo.paused"),
            )
            embed.add_field(name=self.i18n.t(locale, "public_events.tempo.return_name"), value=discord.utils.format_dt(paused_until, style="F"), inline=False)
            embed.add_field(name=self.i18n.t(locale, "public_events.tempo.remaining_name"), value=discord.utils.format_dt(paused_until, style="R"), inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        next_event_name, next_run_at = await self.get_next_scheduled_event(interaction.guild.id)
        embed = discord.Embed(title=self.i18n.t(locale, "public_events.tempo.title"), color=self.OBSERVER_COLOR)
        embed.add_field(
            name=self.i18n.t(locale, "public_events.tempo.next_event_name"),
            value=self._display_event_name_localized(locale, next_event_name),
            inline=False,
        )
        embed.add_field(name=self.i18n.t(locale, "public_events.tempo.starts_at_name"), value=discord.utils.format_dt(next_run_at, style="F"), inline=False)
        embed.add_field(name=self.i18n.t(locale, "public_events.tempo.remaining_name"), value=discord.utils.format_dt(next_run_at, style="R"), inline=False)
        embed.add_field(name=self.i18n.t(locale, "public_events.tempo.schedule_name"), value=self._scheduler_display_localized(locale, scheduler), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="iniciar", description="Admin: inicia um evento publico especifico para teste.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(
        evento=[
            app_commands.Choice(name="Observador", value="observer"),
            app_commands.Choice(name="Chuva de Doces", value="aranhas"),
            app_commands.Choice(name="Carrossel", value="julgamento"),
        ]
    )
    async def iniciar(self, interaction: discord.Interaction, evento: app_commands.Choice[str]) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use esse comando dentro de um servidor.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        if await self._has_active_public_runtime(interaction.guild.id):
            await interaction.followup.send("Ja existe um evento publico ativo neste servidor.", ephemeral=True)
            return

        config = self._load_config()
        if evento.value == "observer":
            ok, reason = await self._start_observer(interaction.guild, config)
            if ok:
                await self._audit_manual_start(interaction, "observer")
            await interaction.followup.send(
                "Observador iniciado para teste." if ok else f"Nao foi possivel iniciar o Observador: {reason}",
                ephemeral=True,
            )
            return

        if evento.value == "julgamento":
            julgamento_cog = self.bot.get_cog("JulgamentoCog")
            if julgamento_cog is None or not hasattr(julgamento_cog, "start_scheduled_event"):
                await interaction.followup.send("A cog de Julgamento nao esta carregada.", ephemeral=True)
                return
            ok, reason = await julgamento_cog.start_scheduled_event(interaction.guild)
            if ok:
                await self._audit_manual_start(interaction, "carrossel")
            await interaction.followup.send(
                "Carrossel iniciado para teste." if ok else f"Nao foi possivel iniciar o Carrossel: {reason}",
                ephemeral=True,
            )
            return

        if evento.value == "aranhas":
            ok, reason = await self._start_spider_infestation(interaction.guild, config)
            if ok:
                await self._audit_manual_start(interaction, "chuva_de_doces")
            await interaction.followup.send(
                "Chuva de Doces iniciada para teste." if ok else f"Nao foi possivel iniciar Chuva de Doces: {reason}",
                ephemeral=True,
            )
            return

    async def _audit_manual_start(self, interaction: discord.Interaction, event_name: str) -> None:
        await self.audit_service.log(
            guild_id=interaction.guild.id if interaction.guild else None,
            actor_id=interaction.user.id,
            action="event.public.manual_start",
            payload={"event": event_name},
        )

    async def cancel_active_event(self, guild: discord.Guild) -> bool:
        cancelled = False
        if await self.chat_candy_runtime.cancel_guild_spawn(self, guild.id):
            cancelled = True
        observer_state = await self.public_events_repo.get_observer_state(guild.id)
        if observer_state.get("active"):
            timeout_task = self._observer_timeout_tasks.pop(guild.id, None)
            if timeout_task is not None:
                timeout_task.cancel()
            await self._mark_observer_cancelled(guild, observer_state)
            await self.public_events_repo.reset_observer_state(guild.id)
            cancelled = True

        spider_state = await self.public_events_repo.get_spider_state(guild.id)
        if spider_state.get("active"):
            await self._finish_spider_infestation(guild, cancelled=True)
            cancelled = True

        julgamento_cog = self.bot.get_cog("JulgamentoCog")
        if julgamento_cog is not None and hasattr(julgamento_cog, "is_active") and hasattr(julgamento_cog, "force_reset"):
            if await julgamento_cog.is_active(guild.id):
                await julgamento_cog.force_reset(guild, refund_tickets=True)
                cancelled = True

        return cancelled

    async def _mark_observer_cancelled(self, guild: discord.Guild, state: dict) -> None:
        channel_id = int(state.get("channel_id") or 0)
        message_id = int(state.get("message_id") or 0)
        if channel_id <= 0 or message_id <= 0:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(
                embed=discord.Embed(
                    title="📹 Observador cancelado",
                    description="A rodada foi encerrada pela administracao.",
                    color=self.OBSERVER_COLOR,
                ),
                view=None,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    def reset_chat_bonus_window(self, guild_id: int) -> None:
        self._chat_bonus_message_windows.pop(guild_id, None)

    @tasks.loop(seconds=5)
    async def scheduler_loop(self) -> None:
        config = self._load_config()
        now = datetime.now(timezone.utc)
        if (
            self._last_reward_reconciliation_at is None
            or now - self._last_reward_reconciliation_at >= timedelta(minutes=1)
        ):
            try:
                await self.public_event_runtime.reconcile_pending_observer_rewards(self)
                await self.chat_candy_runtime.reconcile_pending_rewards(self)
                self._last_reward_reconciliation_at = now
            except Exception:
                LOGGER.exception("Falha ao reconciliar recompensas de eventos publicos")
        scheduler = config.get("scheduler", {})
        if not scheduler.get("enabled", False):
            return

        announce_minutes_before = int(scheduler.get("announce_minutes_before", 0))
        enabled_event_names = self._enabled_event_names(config)
        if not enabled_event_names:
            return

        for guild in self.bot.guilds:
            await self._finish_spider_if_expired(guild)
            if not await self.event_runtime_service.can_run_public_event(guild.id):
                continue
            if await self._has_active_public_runtime(guild.id):
                continue

            state = await self.public_events_repo.get_state(guild.id)
            stored_next_event_name = str(state.get("next_event_name") or "")
            stored_next_run_at = self._parse_dt(state.get("next_run_at"))
            last_triggered_at = str(state.get("last_triggered_at") or "")
            if (
                stored_next_event_name
                and stored_next_run_at is not None
                and last_triggered_at != stored_next_run_at.isoformat()
                and self._is_pending_scheduled_slot_valid(now, stored_next_run_at)
            ):
                next_event_name = stored_next_event_name
                next_run_at = stored_next_run_at
            else:
                next_event_name, next_run_at = self._compute_next_scheduled_event(config, state, now)
                await self.public_events_repo.set_state(
                    guild.id,
                    next_run_at=next_run_at.isoformat(),
                    next_event_name=next_event_name,
                    last_public_event_name=state.get("last_public_event_name"),
                    last_announced_at=None,
                    last_triggered_at=state.get("last_triggered_at"),
                )
                state["next_event_name"] = next_event_name
                state["next_run_at"] = next_run_at.isoformat()

            if announce_minutes_before > 0:
                announce_at = next_run_at - timedelta(minutes=announce_minutes_before)
                if announce_at <= now < next_run_at and state.get("last_announced_at") != next_run_at.isoformat():
                    if self._is_observer_event(next_event_name):
                        channels = await self._get_event_channels(guild)
                        observer_cfg = config.get("observer", {})
                        role_content = await self._get_notification_role_mention(guild)
                        for channel in channels:
                            await self._send_observer_payload(
                                channel,
                                observer_cfg,
                                content=role_content,
                                embed=self._build_observer_announcement_embed(next_run_at),
                            )
                    elif next_event_name == "Julgamento":
                        julgamento_cog = self.bot.get_cog("JulgamentoCog")
                        if julgamento_cog is not None and hasattr(julgamento_cog, "announce_upcoming"):
                            await julgamento_cog.announce_upcoming(guild, next_run_at)
                    elif next_event_name == "Chuva de Doces":
                        await self._send_event_payload(
                            guild,
                            content=await self._get_notification_role_mention(guild),
                            embed=self._build_spider_announcement_embed(next_run_at),
                            view=PublicEventTranslateView(self._build_spider_announcement_embed_en(next_run_at)),
                        )
                    else:
                        await self._send_event_message(
                            guild,
                            f"Evento publico em breve: **{next_event_name}** comeca em {announce_minutes_before} minuto(s).",
                        )
                    await self.public_events_repo.set_state(
                        guild.id,
                        next_run_at=next_run_at.isoformat(),
                        next_event_name=next_event_name,
                        last_public_event_name=state.get("last_public_event_name"),
                        last_announced_at=next_run_at.isoformat(),
                        last_triggered_at=state.get("last_triggered_at"),
                    )
                    state["last_announced_at"] = next_run_at.isoformat()

            if now < next_run_at:
                continue

            if self._is_observer_event(next_event_name):
                await self._start_observer(guild, config)
            elif next_event_name == "Julgamento":
                julgamento_cog = self.bot.get_cog("JulgamentoCog")
                if julgamento_cog is not None and hasattr(julgamento_cog, "start_scheduled_event"):
                    await julgamento_cog.start_scheduled_event(guild)
            elif next_event_name == "Chuva de Doces":
                await self._start_spider_infestation(guild, config)
            else:
                await self._send_event_message(
                    guild,
                    f"Evento publico iniciado: **{next_event_name}**. Runtime ainda sera implementado nesta cog.",
                )
            if next_event_name in self._enabled_public_event_names(config):
                state["last_public_event_name"] = next_event_name
            next_event_name, next_cycle = self._compute_next_scheduled_event(config, state, now)
            await self.public_events_repo.set_state(
                guild.id,
                next_run_at=next_cycle.isoformat(),
                next_event_name=next_event_name,
                last_public_event_name=state.get("last_public_event_name"),
                last_announced_at=None,
                last_triggered_at=next_run_at.isoformat(),
            )

    @scheduler_loop.before_loop
    async def before_scheduler_loop(self) -> None:
        await self.bot.wait_until_ready()

    def _interval_minutes(self, scheduler: dict) -> int:
        if "interval_minutes" in scheduler:
            return max(1, int(scheduler["interval_minutes"]))
        return max(1, int(scheduler.get("interval_hours", 2)) * 60)

    def _fixed_daily_times(self, scheduler: dict) -> list[tuple[int, int]]:
        values = scheduler.get("daily_times") or scheduler.get("fixed_times") or []
        if not isinstance(values, list):
            return []
        times: list[tuple[int, int]] = []
        for value in values:
            parts = str(value).strip().split(":")
            if len(parts) != 2:
                continue
            try:
                hour = int(parts[0])
                minute = int(parts[1])
            except ValueError:
                continue
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                times.append((hour, minute))
        return sorted(set(times))

    def _scheduler_display(self, scheduler: dict) -> str:
        public_text = f"Publicos: a cada {self._interval_minutes(scheduler)} minuto(s)"
        julgamento_times = self._julgamento_daily_times(scheduler)
        if julgamento_times:
            formatted = ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in julgamento_times)
            return f"{public_text} | Carrossel: {formatted} (Sao Paulo)"
        return public_text

    def _is_allowed_scheduler_time(self, dt: datetime, scheduler: dict) -> bool:
        pause_after_hour = int(scheduler.get("pause_after_hour", -1))
        resume_hour = int(scheduler.get("resume_hour", 0))
        if pause_after_hour < 0:
            return True
        # Scheduler windows are configured in Sao Paulo local time.
        hour = dt.astimezone(SCHEDULER_TIMEZONE).hour
        if resume_hour <= pause_after_hour:
            return hour <= pause_after_hour or hour >= resume_hour
        return not (pause_after_hour < hour < resume_hour)

    def _is_configured_next_run(self, dt: datetime, scheduler: dict) -> bool:
        fixed_times = self._fixed_daily_times(scheduler)
        if not fixed_times:
            return True
        local_dt = dt.astimezone(SCHEDULER_TIMEZONE)
        return (local_dt.hour, local_dt.minute) in fixed_times

    def _pick_enabled_event(self, config: dict) -> str | None:
        enabled = self._enabled_event_names(config)
        return random.choice(enabled) if enabled else None

    def _pick_scheduled_event(self, config: dict) -> str | None:
        fixed_event_name = str(config.get("scheduler", {}).get("fixed_event_name") or "").strip()
        enabled = self._enabled_event_names(config)
        if fixed_event_name and fixed_event_name in enabled:
            return fixed_event_name
        return self._pick_enabled_event(config)

    def _pick_next_public_event(self, config: dict, last_public_event_name: str | None) -> str | None:
        enabled = self._enabled_public_event_names(config)
        if not enabled:
            return None
        if last_public_event_name not in enabled:
            return enabled[0]
        index = enabled.index(last_public_event_name)
        return enabled[(index + 1) % len(enabled)]

    def _enabled_event_names(self, config: dict) -> list[str]:
        enabled: list[str] = []
        if config.get("observer", {}).get("enabled"):
            enabled.append("Observador")
        if get_carousel_config().get("enabled"):
            enabled.append("Julgamento")
        if config.get("candy_shower", {}).get("enabled"):
            enabled.append("Chuva de Doces")
        return enabled

    def _enabled_public_event_names(self, config: dict) -> list[str]:
        enabled: list[str] = []
        if config.get("observer", {}).get("enabled"):
            enabled.append("Observador")
        if config.get("candy_shower", {}).get("enabled"):
            enabled.append("Chuva de Doces")
        return enabled

    def _is_observer_event(self, value: str | None) -> bool:
        normalized = str(value or "").strip().casefold()
        return normalized == "observador"

    def _parse_dt(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    async def _get_or_preview_next_run_at(self, guild_id: int, scheduler: dict) -> datetime:
        _next_event_name, next_run_at = await self.get_next_public_event(guild_id)
        return next_run_at

    def _next_aligned_run_at(self, now: datetime, interval_minutes: int) -> datetime:
        interval_minutes = max(1, int(interval_minutes))
        current_utc = now.astimezone(timezone.utc)
        day_start = current_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes_since_day_start = int((current_utc - day_start).total_seconds() // 60)
        next_slot_index = math.floor(minutes_since_day_start / interval_minutes) + 1
        next_slot_minutes = next_slot_index * interval_minutes
        return day_start + timedelta(minutes=next_slot_minutes)

    def _next_allowed_run_at(self, now: datetime, scheduler: dict) -> datetime:
        fixed_times = self._fixed_daily_times(scheduler)
        if fixed_times:
            local_now = now.astimezone(SCHEDULER_TIMEZONE)
            today = local_now.replace(second=0, microsecond=0)
            candidates: list[datetime] = []
            for day_offset in range(0, 8):
                base_day = today + timedelta(days=day_offset)
                for hour, minute in fixed_times:
                    local_candidate = base_day.replace(hour=hour, minute=minute)
                    if local_candidate > local_now:
                        candidates.append(local_candidate.astimezone(timezone.utc))
                if candidates:
                    return min(candidates)

        interval_minutes = self._interval_minutes(scheduler)
        candidate = self._next_aligned_run_at(now, interval_minutes)
        for _ in range(500):
            if self._is_allowed_scheduler_time(candidate, scheduler):
                return candidate
            candidate = self._next_aligned_run_at(candidate, interval_minutes)
        return candidate

    def _julgamento_daily_times(self, scheduler: dict) -> list[tuple[int, int]]:
        values = scheduler.get("julgamento_daily_times") or []
        if not values:
            fixed_event_name = str(scheduler.get("fixed_event_name") or "").strip()
            if fixed_event_name == "Julgamento":
                values = scheduler.get("daily_times") or []
        if not isinstance(values, list):
            return []
        times: list[tuple[int, int]] = []
        for value in values:
            parts = str(value).strip().split(":")
            if len(parts) != 2:
                continue
            try:
                hour = int(parts[0])
                minute = int(parts[1])
            except ValueError:
                continue
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                times.append((hour, minute))
        return sorted(set(times))

    def _next_fixed_run_at(self, now: datetime, times: list[tuple[int, int]]) -> datetime | None:
        if not times:
            return None
        local_now = now.astimezone(SCHEDULER_TIMEZONE)
        today = local_now.replace(second=0, microsecond=0)
        candidates: list[datetime] = []
        for day_offset in range(0, 8):
            base_day = today + timedelta(days=day_offset)
            for hour, minute in times:
                local_candidate = base_day.replace(hour=hour, minute=minute)
                if local_candidate > local_now:
                    candidates.append(local_candidate.astimezone(timezone.utc))
            if candidates:
                return min(candidates)
        return None

    def _next_julgamento_run_at(self, now: datetime, scheduler: dict) -> datetime | None:
        return self._next_fixed_run_at(now, self._julgamento_daily_times(scheduler))

    def _is_julgamento_slot(self, dt: datetime, scheduler: dict) -> bool:
        julgamento_times = self._julgamento_daily_times(scheduler)
        if not julgamento_times:
            return False
        local_dt = dt.astimezone(SCHEDULER_TIMEZONE)
        return (local_dt.hour, local_dt.minute) in julgamento_times

    def _next_public_run_at(self, now: datetime, scheduler: dict) -> datetime:
        candidate = self._next_allowed_run_at(now, scheduler)
        for _ in range(500):
            if not self._is_julgamento_slot(candidate, scheduler):
                return candidate
            candidate = self._next_allowed_run_at(candidate, scheduler)
        return candidate

    def _compute_next_scheduled_event(
        self,
        config: dict,
        state: dict,
        now: datetime,
    ) -> tuple[str | None, datetime]:
        scheduler = dict(config.get("scheduler") or {})
        next_julgamento_run = (
            self._next_julgamento_run_at(now, scheduler)
            if get_carousel_config().get("enabled")
            else None
        )
        next_public_run = self._next_public_run_at(now, scheduler)
        next_public_event_name = self._pick_next_public_event(
            config,
            str(state.get("last_public_event_name") or "") or None,
        )

        if next_julgamento_run is not None and next_julgamento_run <= next_public_run:
            return "Julgamento", next_julgamento_run
        return next_public_event_name, next_public_run

    def _is_pending_scheduled_slot_valid(self, now: datetime, scheduled_at: datetime) -> bool:
        return scheduled_at - timedelta(minutes=1) <= now <= scheduled_at + timedelta(seconds=15)

    async def _has_active_public_runtime(self, guild_id: int) -> bool:
        observer_state = await self.public_events_repo.get_observer_state(guild_id)
        if observer_state.get("active"):
            return True
        spider_state = await self.public_events_repo.get_spider_state(guild_id)
        if spider_state.get("active"):
            return True
        julgamento_cog = self.bot.get_cog("JulgamentoCog")
        if julgamento_cog is not None and hasattr(julgamento_cog, "is_active"):
            return bool(await julgamento_cog.is_active(guild_id))
        return False

    async def _reset_public_events_on_boot(self) -> None:
        for guild in self.bot.guilds:
            state = await self.public_events_repo.get_state(guild.id)
            timeout_task = self._observer_timeout_tasks.pop(guild.id, None)
            if timeout_task is not None:
                timeout_task.cancel()
            await self.public_events_repo.set_state(
                guild.id,
                next_run_at=None,
                next_event_name=None,
                last_public_event_name=state.get("last_public_event_name"),
                last_announced_at=None,
                last_triggered_at=None,
            )

    async def _send_event_message(self, guild: discord.Guild, content: str) -> None:
        channels = await self._get_event_channels(guild)
        for channel in channels:
            try:
                await channel.send(content)
            except discord.HTTPException:
                continue

    async def _send_embed_to_event_channels(self, guild: discord.Guild, embed: discord.Embed) -> None:
        channels = await self._get_event_channels(guild)
        for channel in channels:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                continue

    async def _send_event_payload(
        self,
        guild: discord.Guild,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        channels = await self._get_event_channels(guild)
        for channel in channels:
            try:
                await channel.send(content=content, embed=embed, view=view)
            except discord.HTTPException:
                continue

    async def _send_chat_bonus(
        self,
        message: discord.Message,
        *,
        message_count: int,
        bonus_cfg: dict,
    ) -> bool:
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return False
        timeout_seconds = max(1, int(bonus_cfg.get("button_timeout_seconds", 30) or 30))
        threshold = max(1, int(bonus_cfg.get("threshold_messages", 100) or 100))
        options = list(bonus_cfg.get("options") or [])
        if not options:
            return False
        locale = "pt-BR"
        embed = self._build_chat_bonus_embed(
            message_count=message_count,
            threshold=threshold,
            timeout_seconds=timeout_seconds,
            options=options,
        )

        for allow_emoji in (True, False):
            view = ChatMessageBonusView(
                self,
                guild_id=message.guild.id,
                bonus_cfg=bonus_cfg,
                options=options,
                locale=locale,
                timeout_seconds=timeout_seconds,
                allow_emoji=allow_emoji,
            )
            try:
                sent = await message.channel.send(embed=embed, view=view)
            except discord.HTTPException:
                continue
            view.message = sent
            return True
        return False

    async def _send_observer_payload(
        self,
        channel: discord.abc.MessageableChannel,
        observer_cfg: dict,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        attachment_path: Path | None = None,
        attachment_filename: str | None = None,
        wait: bool = False,
    ) -> discord.Message | discord.WebhookMessage | None:
        webhook = await self._get_or_create_observer_webhook(channel, observer_cfg)
        if webhook is not None:
            attachment = (
                discord.File(attachment_path, filename=attachment_filename)
                if attachment_path is not None and attachment_filename
                else None
            )
            try:
                send_kwargs = {
                    "content": content,
                    "embed": embed,
                    "username": str(observer_cfg.get("webhook_name") or "Observador"),
                    "thread": channel if isinstance(channel, discord.Thread) else discord.utils.MISSING,
                    "wait": wait,
                }
                if view is not None:
                    send_kwargs["view"] = view
                if attachment is not None:
                    send_kwargs["file"] = attachment
                return await webhook.send(**send_kwargs)
            except discord.HTTPException:
                pass
            finally:
                if attachment is not None:
                    attachment.close()

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None
        attachment = (
            discord.File(attachment_path, filename=attachment_filename)
            if attachment_path is not None and attachment_filename
            else None
        )
        try:
            send_kwargs = {"content": content, "embed": embed, "view": view}
            if attachment is not None:
                send_kwargs["file"] = attachment
            return await channel.send(**send_kwargs)
        except discord.HTTPException:
            return None
        finally:
            if attachment is not None:
                attachment.close()

    async def _get_or_create_observer_webhook(
        self,
        channel: discord.abc.MessageableChannel,
        observer_cfg: dict,
    ) -> discord.Webhook | None:
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None

        parent_channel = channel.parent if isinstance(channel, discord.Thread) else channel
        if not isinstance(parent_channel, discord.TextChannel):
            return None

        webhook_name = str(observer_cfg.get("webhook_name") or "Observador")
        avatar_bytes = self._load_observer_webhook_avatar_bytes(observer_cfg)
        try:
            webhooks = await parent_channel.webhooks()
        except (discord.Forbidden, discord.HTTPException):
            return None

        legacy_name = "".join(("Ghost", "face"))
        owned_webhooks = [webhook for webhook in webhooks if webhook.user == self.bot.user]
        current_webhook = next((webhook for webhook in owned_webhooks if webhook.name == webhook_name), None)
        legacy_webhooks = [webhook for webhook in owned_webhooks if webhook.name == legacy_name]

        if current_webhook is not None:
            if avatar_bytes is not None:
                try:
                    current_webhook = await current_webhook.edit(name=webhook_name, avatar=avatar_bytes)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            for legacy_webhook in legacy_webhooks:
                try:
                    await legacy_webhook.delete(reason="Webhook substituido pelo Observador")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            return current_webhook

        if legacy_webhooks:
            try:
                return await legacy_webhooks[0].edit(name=webhook_name, avatar=avatar_bytes)
            except (discord.Forbidden, discord.HTTPException):
                pass

        try:
            return await parent_channel.create_webhook(name=webhook_name, avatar=avatar_bytes)
        except (discord.Forbidden, discord.HTTPException):
            return None

    def _load_observer_webhook_avatar_bytes(self, observer_cfg: dict) -> bytes | None:
        relative_path = str(observer_cfg.get("webhook_avatar_path") or "").strip()
        if not relative_path:
            return None
        path = ROOT_DIR / relative_path
        if not path.exists() or not path.is_file():
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    async def _start_observer(self, guild: discord.Guild, config: dict) -> tuple[bool, str]:
        return await self.public_event_runtime.start_observer(self, guild, config)

    async def _finish_observer_if_unanswered(self, guild_id: int, expires_at: datetime) -> None:
        await self.public_event_runtime.finish_observer_if_unanswered(self, guild_id, expires_at)

    async def _restore_spider_finish_tasks(self) -> None:
        await self.public_event_runtime.restore_spider_finish_tasks(self)

    def _spider_config(self, config: dict | None = None) -> dict:
        return dict((config or self._load_config()).get("candy_shower") or {})

    def _chat_bonus_config(self, config: dict | None = None) -> dict:
        cfg = dict((config or self._load_main_config()).get("chat_bonus") or {})
        options = cfg.get("options")
        if isinstance(options, list):
            normalized: list[dict] = []
            for index, option in enumerate(options):
                if not isinstance(option, dict):
                    continue
                rewards = option.get("rewards")
                if not isinstance(rewards, list) or not rewards:
                    continue
                normalized.append(
                    {
                        "id": str(option.get("id") or f"option_{index + 1}"),
                        "label": str(option.get("label") or f"Opcao {index + 1}"),
                        "emoji": str(option.get("emoji") or "").strip() or None,
                        "rewards": rewards,
                    }
                )
            cfg["options"] = normalized
            return cfg

        rewards = cfg.get("rewards")
        if not isinstance(rewards, list):
            rewards = []
        cfg["options"] = [
            {
                "id": "default",
                "label": "Resgatar Bonus",
                "emoji": "🍬",
                "rewards": rewards,
            }
        ] if rewards else []
        return cfg

    def _chat_candy_config(self, config: dict | None = None) -> dict:
        raw = dict((config or self._load_main_config()).get("chat_candy") or {})
        return self.chat_candy_runtime.normalize_config(raw)

    def _spider_emoji_ids(self, spider_cfg: dict) -> list[int]:
        ids: list[int] = []
        values = spider_cfg.get("emoji_ids")
        if isinstance(values, list):
            for value in values:
                try:
                    emoji_id = int(value)
                except (TypeError, ValueError):
                    continue
                if emoji_id > 0 and emoji_id not in ids:
                    ids.append(emoji_id)

        value = spider_cfg.get("emoji_id")
        if value is not None:
            try:
                emoji_id = int(value)
            except (TypeError, ValueError):
                emoji_id = 0
            if emoji_id > 0 and emoji_id not in ids:
                ids.append(emoji_id)
        return ids

    def _spider_emoji_id(self, spider_cfg: dict) -> int | None:
        emoji_ids = self._spider_emoji_ids(spider_cfg)
        if emoji_ids:
            return random.choice(emoji_ids)
        return 0

    def _candy_drop_emoji(self, guild: discord.Guild, emoji_id: int) -> str | discord.Emoji | discord.PartialEmoji:
        if emoji_id <= 0:
            return "ðŸ¬"
        emoji = guild.get_emoji(emoji_id)
        if emoji is not None:
            return emoji
        return discord.PartialEmoji(name="doce", id=emoji_id)


    async def _start_spider_infestation(self, guild: discord.Guild, config: dict | None = None) -> tuple[bool, str]:
        return await self.public_event_runtime.start_spider_infestation(self, guild, config)

    def _schedule_spider_finish(self, guild_id: int, ends_at: datetime) -> None:
        self.public_event_runtime.schedule_spider_finish(self, guild_id, ends_at)

    async def _finish_spider_after_delay(self, guild_id: int, ends_at: datetime) -> None:
        await self.public_event_runtime.finish_spider_after_delay(self, guild_id, ends_at)

    async def _finish_spider_if_expired(self, guild: discord.Guild) -> None:
        await self.public_event_runtime.finish_spider_if_expired(self, guild)

    async def _finish_spider_infestation(self, guild: discord.Guild, *, cancelled: bool = False) -> None:
        await self.public_event_runtime.finish_spider_infestation(self, guild, cancelled=cancelled)

    async def _maybe_spawn_spider(self, message: discord.Message) -> None:
        await self.public_event_runtime.maybe_spawn_spider(self, message)

    def _schedule_spider_expire(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        spawn_id: int,
        emoji_id: int,
        expires_at: datetime,
    ) -> None:
        self.public_event_runtime.schedule_spider_expire(
            self,
            guild_id,
            channel_id,
            message_id,
            spawn_id,
            emoji_id,
            expires_at,
        )

    async def _expire_spider_spawn(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        spawn_id: int,
        emoji_id: int,
        expires_at: datetime,
    ) -> None:
        await self.public_event_runtime.expire_spider_spawn(
            self,
            guild_id,
            channel_id,
            message_id,
            spawn_id,
            emoji_id,
            expires_at,
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self.chat_candy_runtime.handle_reaction(self, payload)
        await self.public_event_runtime.handle_spider_reaction(self, payload)

    async def _grant_spider_rewards(
        self,
        user_id: int,
        guild_id: int,
        captures: int,
        spider_cfg: dict,
        event_key: str,
    ) -> str:
        settlement_key = f"candy_shower:{event_key}:{user_id}"
        settlement = await self.reward_settlement_service.get_settlement(settlement_key)
        per_capture = dict(spider_cfg.get("reward_per_capture") or {})
        currencies: dict[str, int] = {}
        items: dict[str, int] = {}
        if settlement is None:
            for field in ("candies", "trick_coins", "dice_count", "xp_bank"):
                amount_per_capture = int(per_capture.get(field, 0) or 0)
                amount = amount_per_capture * captures
                if amount > 0:
                    currencies[field] = amount

            item_rewards = spider_cfg.get("item_rewards") or []
            if isinstance(item_rewards, list):
                for reward in item_rewards:
                    if not isinstance(reward, dict):
                        continue
                    item_id = str(reward.get("item_id") or "").strip()
                    if not item_id:
                        continue
                    chance = float(reward.get("chance_percent_per_capture", 0) or 0)
                    amount = sum(1 for _ in range(captures) if random.uniform(0, 100) <= chance)
                    if amount > 0:
                        self._validate_reward_item(item_id)
                        items[item_id] = items.get(item_id, 0) + amount

            settlement = await self.reward_settlement_service.settle_once(
                settlement_key,
                "candy_shower",
                user_id,
                guild_id,
                currencies=currencies,
                items=items,
            )
        locale = await self.i18n.get_user_locale(user_id)
        lines = self._format_reward_settlement(locale, settlement)
        lines.extend(await self.mission_service.evaluate_progress(guild_id, user_id))
        return ", ".join(lines) if lines else "sem recompensa configurada"

    async def _maybe_handle_chat_bonus(self, message: discord.Message) -> None:
        runtime = await self.admin_repo.get_runtime(message.guild.id)
        if not runtime.get("event_active"):
            self._chat_bonus_message_windows.pop(message.guild.id, None)
            return

        bonus_cfg = self._chat_bonus_config()
        if not bonus_cfg.get("enabled", False):
            return

        config = await self.admin_repo.get_config(message.guild.id)
        event_channel_ids = {int(channel_id) for channel_id in (config.get("event_channel_ids") or []) if channel_id}
        channel_matches = message.channel.id in event_channel_ids
        if not channel_matches and isinstance(message.channel, discord.Thread) and message.channel.parent is not None:
            channel_matches = message.channel.parent.id in event_channel_ids
        if not channel_matches:
            return

        min_length = max(0, int(bonus_cfg.get("min_message_length", 0) or 0))
        if len(message.content.strip()) < min_length:
            return

        threshold = max(1, int(bonus_cfg.get("threshold_messages", 100) or 100))
        window_minutes = max(1, int(bonus_cfg.get("window_minutes", 10) or 10))
        window_delta = timedelta(minutes=window_minutes)
        now = discord.utils.utcnow()
        progress_lock = self._chat_bonus_progress_locks.setdefault(message.guild.id, asyncio.Lock())
        async with progress_lock:
            timestamps = self._chat_bonus_message_windows.setdefault(message.guild.id, deque())
            cutoff = now - window_delta
            while timestamps and timestamps[0] < cutoff:
                timestamps.popleft()
            timestamps.append(now)
            if len(timestamps) < threshold:
                return
            message_count = len(timestamps)
            sent = await self._send_chat_bonus(
                message,
                message_count=message_count,
                bonus_cfg=bonus_cfg,
            )
            if sent:
                timestamps.clear()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        await self._maybe_handle_chat_bonus(message)
        await self.chat_candy_runtime.maybe_spawn(self, message)
        await self._maybe_spawn_spider(message)
        await self.public_event_runtime.handle_observer_message(self, message)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._chat_candy_restored:
            return
        await self.public_event_runtime.restore_active_observer_timeouts(self)
        await self.public_event_runtime.reconcile_pending_observer_rewards(self)
        await self.chat_candy_runtime.restore_expiry_tasks(self)
        self._last_reward_reconciliation_at = datetime.now(timezone.utc)
        self._chat_candy_restored = True

    async def _grant_observer_rewards(
        self,
        user_id: int,
        config: dict,
        guild_id: int,
        difficulty: str,
        settlement_key: str,
    ) -> list[str]:
        locale = await self.i18n.get_user_locale(user_id)
        settlement = await self.reward_settlement_service.get_settlement(settlement_key)
        currencies: dict[str, int] = {}
        items: dict[str, int] = {}
        reward_pool = dict(config.get("reward_pool") or {})
        difficulty_defaults = dict(config.get("difficulty_defaults") or {})
        difficulty_cfg = dict(difficulty_defaults.get(difficulty) or difficulty_defaults.get("easy") or {})
        reward_count = max(1, int(difficulty_cfg.get("reward_count", 2)))
        allow_baskets = bool(difficulty_cfg.get("allow_baskets", False))

        category_names = [name for name in reward_pool if allow_baskets or name != "baskets"]
        if settlement is None and category_names:
            selected_names = random.sample(category_names, k=min(reward_count, len(category_names)))
            for category_name in selected_names:
                reward_cfg = dict(reward_pool.get(category_name) or {})
                reward_currencies, reward_items = self._plan_configured_reward_entry(reward_cfg)
                self._merge_reward_amounts(currencies, reward_currencies)
                self._merge_reward_amounts(items, reward_items)

        if settlement is None:
            settlement = await self.reward_settlement_service.settle_once(
                settlement_key,
                "observer",
                user_id,
                guild_id,
                currencies=currencies,
                items=items,
                counters={"horror_questions_correct_total": 1},
            )
        lines = self._format_reward_settlement(locale, settlement)
        lines.extend(await self.mission_service.evaluate_progress(guild_id, user_id))
        return lines

    def _plan_configured_reward_entry(self, reward_cfg: dict) -> tuple[dict[str, int], dict[str, int]]:
        currencies: dict[str, int] = {}
        items: dict[str, int] = {}
        grant_type = str(reward_cfg.get("grant_type") or "").strip().lower()
        if grant_type == "currency":
            grant_id = str(reward_cfg.get("grant_id") or "")
            amount = self._random_reward_amount(reward_cfg)
            if amount > 0:
                currencies[grant_id] = amount
        elif grant_type == "item":
            grant_id = str(reward_cfg.get("grant_id") or "")
            amount = self._random_reward_amount(reward_cfg)
            if amount > 0:
                self._validate_reward_item(grant_id)
                items[grant_id] = amount
        elif grant_type == "item_random":
            item_ids = [str(item_id) for item_id in reward_cfg.get("item_ids", []) if str(item_id).strip()]
            if item_ids:
                grant_id = random.choice(item_ids)
                self._validate_reward_item(grant_id)
                items[grant_id] = max(1, int(reward_cfg.get("amount", 1)))
        elif grant_type == "currency_bundle":
            candies = self._resolve_bundle_amount(reward_cfg, "candies")
            trick_coins = self._resolve_bundle_amount(reward_cfg, "trick_coins")
            if candies > 0:
                currencies["candies"] = candies
            if trick_coins > 0:
                currencies["trick_coins"] = trick_coins
        return currencies, items

    def _validate_reward_item(self, item_id: str) -> None:
        item = self.inventory_service.item_catalog.get(item_id)
        if item is None or not item.grantable:
            raise ValueError(f"Unsupported event reward item: {item_id}")

    def _merge_reward_amounts(self, target: dict[str, int], additions: dict[str, int]) -> None:
        for name, amount in additions.items():
            target[name] = target.get(name, 0) + amount

    def _format_reward_settlement(self, locale: str, settlement: RewardSettlement) -> list[str]:
        lines: list[str] = []
        reward_keys = {
            "candies": "missions.reward.candies",
            "trick_coins": "missions.reward.trick_coins",
            "dice_count": "missions.reward.dice",
            "xp_bank": "missions.reward.xp",
        }
        for field, amount in settlement.currencies.items():
            i18n_key = reward_keys.get(field)
            if i18n_key:
                lines.append(self.i18n.t(locale, i18n_key, amount=amount))
        for item_id, amount in settlement.items.items():
            item_name = self.i18n.maybe_t(locale, f"item.{item_id}.name") or item_id
            lines.append(f"{item_name} x{amount}")
        return lines

    def _random_reward_amount(self, reward_cfg: dict) -> int:
        if "amount" in reward_cfg:
            return int(reward_cfg.get("amount") or 0)
        min_amount = int(reward_cfg.get("min_amount", 0) or 0)
        max_amount = int(reward_cfg.get("max_amount", min_amount) or min_amount)
        if max_amount < min_amount:
            max_amount = min_amount
        return random.randint(min_amount, max_amount)

    def _resolve_bundle_amount(self, reward_cfg: dict, prefix: str) -> int:
        amount_key = f"{prefix}_amount"
        min_key = f"{prefix}_min_amount"
        max_key = f"{prefix}_max_amount"
        if amount_key in reward_cfg:
            return int(reward_cfg.get(amount_key) or 0)
        min_amount = int(reward_cfg.get(min_key, 0) or 0)
        max_amount = int(reward_cfg.get(max_key, min_amount) or min_amount)
        if max_amount < min_amount:
            max_amount = min_amount
        if max_amount <= 0:
            return 0
        return random.randint(min_amount, max_amount)

    async def _grant_chat_bonus_rewards(
        self,
        user_id: int,
        guild_id: int,
        option_cfg: dict,
        *,
        locale: str | None = None,
        settlement_key: str,
    ) -> str:
        locale = locale or await self.i18n.get_user_locale(user_id)
        rewards = option_cfg.get("rewards") or []
        settlement = await self.reward_settlement_service.get_settlement(settlement_key)
        if settlement is None:
            currencies: dict[str, int] = {}
            items: dict[str, int] = {}
            for reward_cfg in rewards:
                if not isinstance(reward_cfg, dict):
                    continue
                reward_currencies, reward_items = self._plan_configured_reward_entry(reward_cfg)
                self._merge_reward_amounts(currencies, reward_currencies)
                self._merge_reward_amounts(items, reward_items)
            settlement = await self.reward_settlement_service.settle_once(
                settlement_key,
                "chat_bonus",
                user_id,
                guild_id,
                currencies=currencies,
                items=items,
            )
        lines = self._format_reward_settlement(locale, settlement)
        lines.extend(await self.mission_service.evaluate_progress(guild_id, user_id))
        return ", ".join(lines) if lines else self.i18n.t(locale, "event.chat_bonus.no_reward")

    async def _get_event_channels(self, guild: discord.Guild) -> list[discord.abc.GuildChannel]:
        config = await self.admin_repo.get_config(guild.id)
        channels: list[discord.abc.GuildChannel] = []
        for channel_id in config.get("event_channel_ids") or []:
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(channel_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                channels.append(channel)
        return channels

    async def _message_is_in_event_channel(self, message: discord.Message) -> bool:
        config = await self.admin_repo.get_config(message.guild.id)
        event_channel_ids = {
            int(channel_id)
            for channel_id in (config.get("event_channel_ids") or [])
            if channel_id
        }
        if message.channel.id in event_channel_ids:
            return True
        return (
            isinstance(message.channel, discord.Thread)
            and message.channel.parent is not None
            and message.channel.parent.id in event_channel_ids
        )

    def _load_observer_questions(self, observer_cfg: dict) -> list[dict]:
        return self.observer_catalog.load(observer_cfg)

    def _observer_archives_config(self, observer_cfg: dict | None = None) -> dict:
        config = observer_cfg if observer_cfg is not None else self._load_config().get("observer", {})
        return dict((config or {}).get("archives") or {})

    async def _load_observer_archive_questions(self, guild_id: int, observer_cfg: dict) -> list[dict]:
        archives_cfg = self._observer_archives_config(observer_cfg)
        if not archives_cfg.get("enabled", True):
            return []
        questions: list[dict] = []
        for archive in await self.observer_archives_repo.list_active(guild_id):
            question = await self._prepare_observer_archive_question(archive, observer_cfg)
            if question is not None:
                questions.append(question)
        return questions

    async def _prepare_observer_archive_question(self, archive: dict, observer_cfg: dict) -> dict | None:
        archives_cfg = self._observer_archives_config(observer_cfg)
        cache_directory = str(archives_cfg.get("cache_directory") or "cache/observer_archives")
        question = self.observer_archive_service.archive_to_question(archive)
        hidden = self.observer_archive_service.resolve_cached_image(question["image_path"], cache_directory)
        revealed = self.observer_archive_service.resolve_cached_image(
            question["revealed_image_path"], cache_directory
        )
        if hidden is not None and revealed is not None:
            return question
        try:
            hidden_path, revealed_path = await self.observer_archive_service.render_archive_pair(
                self.observer_archive_service.draft_from_archive(archive),
                cache_directory,
            )
        except Exception:
            LOGGER.exception(
                "Falha ao regenerar cards do arquivo guild=%s public_id=%s",
                archive.get("guild_id"),
                archive.get("public_id"),
            )
            return None
        question["image_path"] = hidden_path
        question["revealed_image_path"] = revealed_path
        return question

    async def _pick_observer_question(
        self,
        guild_id: int,
        questions: list[dict],
        observer_cfg: dict | None = None,
    ) -> dict | None:
        valid_questions = [question for question in questions if str(question.get("id") or "").strip()]
        if not valid_questions:
            return None

        questions_by_kind: dict[str, list[dict]] = {}
        for question in valid_questions:
            questions_by_kind.setdefault(self.observer_catalog.kind(question), []).append(question)

        weights_config = dict((observer_cfg or {}).get("round_type_weights") or {})
        kinds = list(questions_by_kind)
        weights: list[float] = []
        for kind in kinds:
            try:
                weights.append(max(0.0, float(weights_config.get(kind, 1))))
            except (TypeError, ValueError):
                weights.append(1.0)
        selected_kind = random.choices(kinds, weights=weights, k=1)[0] if sum(weights) > 0 else random.choice(kinds)
        selected_pool = questions_by_kind[selected_kind]

        used_ids = await self.public_events_repo.get_observer_used_question_ids(guild_id)
        used_set = set(used_ids)
        available_questions = [
            question for question in selected_pool if str(question.get("id")) not in used_set
        ]
        if not available_questions:
            selected_ids = {str(question.get("id")) for question in selected_pool}
            used_ids = [question_id for question_id in used_ids if question_id not in selected_ids]
            used_set = set(used_ids)
            available_questions = selected_pool

        question = random.choice(available_questions)
        question_id = str(question.get("id"))
        if question_id not in used_set:
            used_ids.append(question_id)
        await self.public_events_repo.set_observer_used_question_ids(guild_id, used_ids)
        return question

    def _resolve_observer_image_path(self, question: dict) -> Path | None:
        if self.observer_catalog.kind(question) == "chat_quote":
            archives_cfg = self._observer_archives_config()
            return self.observer_archive_service.resolve_cached_image(
                str(question.get("image_path") or ""),
                str(archives_cfg.get("cache_directory") or "cache/observer_archives"),
            )
        return self.observer_catalog.image_path(question)

    def _resolve_observer_attachment_filename(self, question: dict) -> str | None:
        if self.observer_catalog.kind(question) == "chat_quote":
            public_id = str(question.get("archive_public_id") or "archive")
            return f"observer_{public_id}_hidden.png"
        return self.observer_catalog.attachment_filename(question)

    def _resolve_observer_revealed_image_path(self, question: dict | None) -> Path | None:
        if not question or self.observer_catalog.kind(question) != "chat_quote":
            return None
        archives_cfg = self._observer_archives_config()
        return self.observer_archive_service.resolve_cached_image(
            str(question.get("revealed_image_path") or ""),
            str(archives_cfg.get("cache_directory") or "cache/observer_archives"),
        )

    def _resolve_observer_revealed_attachment_filename(self, question: dict | None) -> str | None:
        if not question or self.observer_catalog.kind(question) != "chat_quote":
            return None
        public_id = str(question.get("archive_public_id") or "archive")
        return f"observer_{public_id}_revealed.png"

    def _expand_observer_answers(self, question: dict) -> list[str]:
        answers = [str(answer).strip() for answer in question.get("answers", []) if str(answer).strip()]
        pool_name = str(question.get("answer_pool") or "").strip().lower()
        if pool_name == "horror_movies":
            answers.extend(self._load_horror_movie_answers())
        seen: set[str] = set()
        unique_answers: list[str] = []
        for answer in answers:
            if answer not in seen:
                seen.add(answer)
                unique_answers.append(answer)
        return unique_answers

    def _load_horror_movie_answers(self) -> list[str]:
        if not HORROR_MOVIES_PATH.exists():
            return []
        data = json.loads(HORROR_MOVIES_PATH.read_text(encoding="utf-8"))
        answers: list[str] = []
        for item in data:
            if isinstance(item, str):
                value = item.strip()
                if value:
                    answers.append(value)
                continue
            if isinstance(item, dict):
                for title in item.get("titles", []):
                    value = str(title).strip()
                    if value:
                        answers.append(value)
        return answers

    def _resolve_observer_match_mode(self, question: dict) -> str:
        if bool(question.get("exact_match")):
            return "exact"
        mode = str(question.get("match_mode") or "normalized").strip().lower()
        if mode in {"exact", "normalized", "spaces", "spoiler"}:
            return mode
        return "normalized"

    def _resolve_observer_difficulty(self, question: dict) -> str:
        difficulty = str(question.get("difficulty") or "easy").strip().lower()
        if difficulty in {"easy", "medium", "hard"}:
            return difficulty
        return "easy"

    def _prepare_observer_answer(self, value: str, match_mode: str) -> str:
        trimmed = value.strip().translate(OBSERVER_CONFUSABLES)
        if match_mode in {"exact", "spaces", "spoiler"}:
            return trimmed.casefold()
        collapsed = " ".join(trimmed.split())
        normalized = unicodedata.normalize("NFD", collapsed.lower())
        no_accents = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
        no_punctuation = no_accents.translate(str.maketrans("", "", string.punctuation))
        return " ".join(no_punctuation.split())

    def _resolve_observer_display_text(self, question: dict, match_mode: str) -> str | None:
        if match_mode not in {"spaces", "spoiler"}:
            return None
        display_text = str(question.get("display_text") or "").strip()
        if not display_text:
            answers = question.get("answers", [])
            if answers:
                display_text = str(answers[0]).strip()
        if not display_text:
            return None
        if match_mode == "spaces":
            return self._observer_disguise_spaces(display_text)
        return self._observer_spoiler_words(display_text)

    def _resolve_observer_display_text_en(self, question: dict, match_mode: str) -> str | None:
        if match_mode not in {"spaces", "spoiler"}:
            return None
        display_text = str(question.get("display_text_en") or "").strip()
        if not display_text:
            display_text = str(question.get("display_text") or "").strip()
        if not display_text:
            return None
        if match_mode == "spaces":
            return self._observer_disguise_spaces(display_text)
        return self._observer_spoiler_words(display_text)

    def _find_observer_question(
        self,
        observer_cfg: dict,
        *,
        question_id: str | None,
        question_text: str | None,
    ) -> dict | None:
        questions = self._load_observer_questions(observer_cfg)
        question = self.observer_catalog.find_by_id(questions, question_id)
        if question is not None:
            return question
        return self.observer_catalog.find_by_text(
            questions,
            question_text,
            lambda value: self._prepare_observer_answer(value, "normalized"),
        )

    async def _find_observer_question_for_state(
        self,
        guild_id: int,
        observer_cfg: dict,
        *,
        question_id: str | None,
        question_text: str | None,
    ) -> dict | None:
        question = self._find_observer_question(
            observer_cfg,
            question_id=question_id,
            question_text=question_text,
        )
        if question is not None:
            return question
        archive = await self.observer_archives_repo.get_by_question_id(guild_id, question_id)
        if archive is None:
            return None
        return await self._prepare_observer_archive_question(archive, observer_cfg)

    def _find_observer_question_by_text(self, observer_cfg: dict, question_text: str) -> dict | None:
        return self._find_observer_question(
            observer_cfg,
            question_id=None,
            question_text=question_text,
        )

    def _resolve_observer_timeout_answer(self, question: dict | None) -> tuple[str, str]:
        if question is None:
            return "Resposta correta", "Nao consegui recuperar a resposta desta pergunta."
        if self.observer_catalog.kind(question) == "chat_quote":
            return "Identidade confirmada", str(question.get("author_display_name") or "Autor desconhecido")
        pool_name = str(question.get("answer_pool") or "").strip().lower()
        if pool_name == "horror_movies":
            return "Respostas aceitas", "Varios titulos de filmes de terror eram aceitos nesta pergunta."
        for key in ("hint_answer_pt", "display_text", "answer_pt"):
            value = str(question.get(key) or "").strip()
            if value:
                return "Resposta correta", f"`{value}`"
        answers = [str(answer).strip() for answer in question.get("answers", []) if str(answer).strip()]
        if answers:
            return "Resposta correta", f"`{answers[0]}`"
        return "Resposta correta", "Nao consegui recuperar a resposta desta pergunta."

    def _observer_disguise_spaces(self, value: str) -> str:
        return value.replace(" ", f" {INVISIBLE_SPACE}")

    def _observer_spoiler_words(self, value: str) -> str:
        return f" {INVISIBLE_SPACE}".join(f"||{part}||" for part in value.split(" "))

    async def _get_notification_role_mention(self, guild: discord.Guild) -> str | None:
        config = await self.admin_repo.get_config(guild.id)
        if not config.get("notification_role_id"):
            return None
        role = guild.get_role(int(config["notification_role_id"]))
        if role is None:
            return None
        return role.mention

    def _build_observer_announcement_embed(self, starts_at: datetime) -> discord.Embed:
        return self.texts.build_observer_announcement_embed(self.OBSERVER_COLOR, starts_at)

    def _build_spider_announcement_embed(self, starts_at: datetime) -> discord.Embed:
        return self.texts.build_spider_announcement_embed(discord.Color.from_rgb(115, 82, 168), starts_at)

    def _build_spider_announcement_embed_en(self, starts_at: datetime) -> discord.Embed:
        return self.texts.build_spider_announcement_embed_en(discord.Color.from_rgb(115, 82, 168), starts_at)

    def _build_spider_start_embed(self, ends_at: datetime) -> discord.Embed:
        return self.texts.build_spider_start_embed(discord.Color.from_rgb(115, 82, 168), ends_at=ends_at)

    def _build_spider_start_embed_en(self, ends_at: datetime) -> discord.Embed:
        return self.texts.build_spider_start_embed_en(discord.Color.from_rgb(115, 82, 168), ends_at=ends_at)

    def _build_spider_finish_embed(self, *, body: str, total_spawns: int) -> discord.Embed:
        return self.texts.build_spider_finish_embed(
            discord.Color.from_rgb(115, 82, 168),
            body=body,
            total_spawns=total_spawns,
        )

    def _build_spider_finish_embed_en(self, *, body: str, total_spawns: int) -> discord.Embed:
        return self.texts.build_spider_finish_embed_en(
            discord.Color.from_rgb(115, 82, 168),
            body=body,
            total_spawns=total_spawns,
        )

    def _build_chat_bonus_embed(
        self,
        *,
        message_count: int,
        threshold: int,
        timeout_seconds: int,
        options: list[dict],
    ) -> discord.Embed:
        return self.texts.build_chat_bonus_embed(
            discord.Color.from_rgb(115, 82, 168),
            message_count=message_count,
            threshold=threshold,
            timeout_seconds=timeout_seconds,
            options=options,
        )

    def _build_observer_question_embed(
        self,
        question_text: str,
        deadline_at: datetime,
        match_mode: str,
        display_text: str | None,
        difficulty: str,
        attachment_filename: str | None = None,
    ) -> discord.Embed:
        return self.texts.build_observer_question_embed(
            self.OBSERVER_COLOR,
            question_text=question_text,
            deadline_at=deadline_at,
            match_mode=match_mode,
            display_text=display_text,
            difficulty=difficulty,
            attachment_filename=attachment_filename,
        )

    def build_observer_translation_embed(self, question: dict, deadline_at: datetime) -> discord.Embed | None:
        question_en = str(question.get("question_en") or "").strip()
        if not question_en:
            return None
        match_mode = self._resolve_observer_match_mode(question)
        difficulty = self._resolve_observer_difficulty(question)
        display_text = self._resolve_observer_display_text_en(question, match_mode)
        return self.texts.build_observer_translation_embed(
            self.OBSERVER_COLOR,
            question_en=question_en,
            deadline_at=deadline_at,
            match_mode=match_mode,
            display_text=display_text,
            difficulty=difficulty,
        )

    def _build_observer_timeout_embed(self, *, answer_label: str, answer_value: str) -> discord.Embed:
        return self.texts.build_observer_timeout_embed(
            self.OBSERVER_COLOR,
            answer_label=answer_label,
            answer_value=answer_value,
        )

    def _build_observer_winner_embed(self, winner_mention: str, summary: str) -> discord.Embed:
        return self.texts.build_observer_winner_embed(self.OBSERVER_COLOR, winner_mention, summary)

    def _build_tempo_unavailable_embed(self, title: str, description: str) -> discord.Embed:
        return self.texts.build_tempo_unavailable_embed(title, description)

    async def _grant_cinefilo_role_if_eligible(self, guild: discord.Guild, user_id: int, total: int) -> None:
        config = await self.admin_repo.get_config(guild.id)
        role_id = config.get("cinefilo_role_id")
        required = max(1, int(get_achievements_config().get("cinefilo_correct_answers_required", 50) or 50))
        if not role_id or total < required:
            return
        role = guild.get_role(int(role_id))
        member = guild.get_member(user_id)
        if role is None or member is None or role in member.roles:
            return
        try:
            await member.add_roles(role, reason=f"Cinefilo: {total} respostas corretas")
        except (discord.Forbidden, discord.HTTPException):
            return


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PublicEventsCog(bot))
