from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
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
from repositories.public_events_repo import PublicEventsRepo
from services.event_runtime_service import EventRuntimeService
from services.config_service import get_carousel_config, get_public_events_config
from services.i18n_service import I18nService
from services.inventory_service import InventoryService
from services.mission_service import MissionService
from repositories.users_repo import UsersRepo


ROOT_DIR = Path(__file__).resolve().parent.parent
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


class ObserverTranslateView(discord.ui.View):
    def __init__(self, cog: "PublicEventsCog", question: dict, timeout_seconds: int) -> None:
        super().__init__(timeout=max(1, timeout_seconds))
        self.cog = cog
        self.question = question
        self.timeout_seconds = timeout_seconds

    @discord.ui.button(label="Translate", style=discord.ButtonStyle.secondary)
    async def translate_en(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        embed = self.cog.build_observer_translation_embed(self.question, self.timeout_seconds)
        if embed is None:
            await interaction.response.send_message(
                "This question does not have an English version yet.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)


class PublicEventsCog(commands.Cog):
    OBSERVER_COLOR = discord.Color.from_rgb(120, 12, 12)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.admin_repo = AdminRepo()
        self.public_events_repo = PublicEventsRepo()
        self.event_runtime_service = EventRuntimeService(self.admin_repo)
        self.users_repo = UsersRepo()
        self.i18n = I18nService(self.users_repo)
        self.inventory_service = InventoryService(users_repo=self.users_repo, i18n_service=self.i18n)
        self.mission_service = MissionService(users_repo=self.users_repo, i18n_service=self.i18n)
        self._spider_expire_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._spider_finish_tasks: dict[int, asyncio.Task] = {}

    async def cog_load(self) -> None:
        await self.public_events_repo.init_db()
        await self._cleanup_stale_public_runtimes()
        await self._restore_spider_finish_tasks()
        if not self.scheduler_loop.is_running():
            self.scheduler_loop.start()

    def cog_unload(self) -> None:
        if self.scheduler_loop.is_running():
            self.scheduler_loop.cancel()
        for task in self._spider_expire_tasks.values():
            task.cancel()
        self._spider_expire_tasks.clear()
        for task in self._spider_finish_tasks.values():
            task.cancel()
        self._spider_finish_tasks.clear()

    def _load_config(self) -> dict:
        return get_public_events_config()

    async def get_next_scheduled_event(self, guild_id: int) -> tuple[str | None, datetime | None]:
        state = await self.public_events_repo.get_state(guild_id)
        next_event_name = str(state.get("next_event_name") or "") or None
        next_run_at = self._parse_dt(state.get("next_run_at"))
        config = self._load_config()
        scheduler = config.get("scheduler", {})
        if next_run_at is None or next_run_at <= datetime.now(timezone.utc) or not self._is_configured_next_run(next_run_at, scheduler):
            next_event_name = self._pick_scheduled_event(config)
            next_run_at = self._next_allowed_run_at(datetime.now(timezone.utc), scheduler)
            await self.public_events_repo.set_state(
                guild_id,
                next_run_at=next_run_at.isoformat(),
                next_event_name=next_event_name,
                last_announced_at=None,
                last_triggered_at=state.get("last_triggered_at"),
            )
        return next_event_name, next_run_at

    def _display_event_name(self, value: str | None) -> str:
        normalized = str(value or "").strip().lower()
        if normalized == "julgamento":
            return "Carrossel"
        return str(value or "Indefinido")

    @app_commands.command(name="tempo", description="Mostra quando o proximo evento publico vai comecar.")
    @app_commands.checks.cooldown(1, 5.0, key=lambda interaction: interaction.user.id)
    async def tempo(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use esse comando dentro de um servidor.", ephemeral=True)
            return

        config = self._load_config()
        scheduler = config.get("scheduler", {})
        enabled_event_name = self._pick_enabled_event(config)
        runtime = await self.admin_repo.get_runtime(interaction.guild.id)

        if not scheduler.get("enabled", False):
            await interaction.response.send_message(
                embed=self._build_tempo_unavailable_embed(
                    "Tempo dos eventos",
                    "O scheduler de eventos publicos esta desligado no config.",
                ),
                ephemeral=True,
            )
            return

        if not enabled_event_name:
            await interaction.response.send_message(
                embed=self._build_tempo_unavailable_embed(
                    "Tempo dos eventos",
                    "Nenhum evento publico esta habilitado no momento.",
                ),
                ephemeral=True,
            )
            return

        if not runtime.get("event_active"):
            await interaction.response.send_message(
                embed=self._build_tempo_unavailable_embed(
                    "Tempo dos eventos",
                    "O evento principal nao esta ativo. Use `/evento` para iniciar quando quiser.",
                ),
                ephemeral=True,
            )
            return

        paused_until = self._parse_dt(runtime.get("events_paused_until"))
        if paused_until is not None and paused_until > datetime.now(timezone.utc):
            embed = self._build_tempo_unavailable_embed(
                "Tempo dos eventos",
                "Os eventos publicos estao pausados no momento.",
            )
            embed.add_field(name="Retorno", value=discord.utils.format_dt(paused_until, style="F"), inline=False)
            embed.add_field(name="Falta", value=discord.utils.format_dt(paused_until, style="R"), inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        next_run_at = await self._get_or_preview_next_run_at(interaction.guild.id, scheduler)
        state = await self.public_events_repo.get_state(interaction.guild.id)
        next_event_name = str(state.get("next_event_name") or self._pick_scheduled_event(config) or "Indefinido")
        embed = discord.Embed(title="Tempo dos eventos", color=self.OBSERVER_COLOR)
        embed.add_field(name="Proximo evento", value=self._display_event_name(next_event_name), inline=False)
        embed.add_field(name="Comeca em", value=discord.utils.format_dt(next_run_at, style="F"), inline=False)
        embed.add_field(name="Falta", value=discord.utils.format_dt(next_run_at, style="R"), inline=False)
        embed.add_field(name="Agenda", value=self._scheduler_display(scheduler), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="iniciar", description="Admin: inicia um evento publico especifico para teste.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(
        evento=[
            app_commands.Choice(name="Observador", value="observer"),
            app_commands.Choice(name="Aranhas", value="aranhas"),
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
            await self._start_observer(interaction.guild, config)
            await interaction.followup.send("Observador iniciado para teste.", ephemeral=True)
            return

        if evento.value == "julgamento":
            julgamento_cog = self.bot.get_cog("JulgamentoCog")
            if julgamento_cog is None or not hasattr(julgamento_cog, "start_scheduled_event"):
                await interaction.followup.send("A cog de Julgamento nao esta carregada.", ephemeral=True)
                return
            ok, reason = await julgamento_cog.start_scheduled_event(interaction.guild)
            await interaction.followup.send(
                "Carrossel iniciado para teste." if ok else f"Nao foi possivel iniciar o Carrossel: {reason}",
                ephemeral=True,
            )
            return

        if evento.value == "aranhas":
            ok, reason = await self._start_spider_infestation(interaction.guild, config)
            await interaction.followup.send(
                "Infestacao de Aranhas iniciada para teste." if ok else f"Nao foi possivel iniciar Aranhas: {reason}",
                ephemeral=True,
            )
            return

    async def cancel_active_event(self, guild: discord.Guild) -> bool:
        cancelled = False
        observer_state = await self.public_events_repo.get_observer_state(guild.id)
        if observer_state.get("active"):
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

    @tasks.loop(minutes=1)
    async def scheduler_loop(self) -> None:
        config = self._load_config()
        scheduler = config.get("scheduler", {})
        if not scheduler.get("enabled", False):
            return

        announce_minutes_before = int(scheduler.get("announce_minutes_before", 0))
        enabled_event_names = self._enabled_event_names(config)
        if not enabled_event_names:
            return

        now = datetime.now(timezone.utc)
        for guild in self.bot.guilds:
            await self._finish_spider_if_expired(guild)
            if not await self.event_runtime_service.can_run_public_event(guild.id):
                continue
            if await self._has_active_public_runtime(guild.id):
                continue

            state = await self.public_events_repo.get_state(guild.id)
            next_run_at = self._parse_dt(state.get("next_run_at"))
            next_event_name = str(state.get("next_event_name") or "")
            if next_run_at is None:
                next_event_name = self._pick_scheduled_event(config)
                next_run_at = self._next_allowed_run_at(now, scheduler)
                await self.public_events_repo.set_state(
                    guild.id,
                    next_run_at=next_run_at.isoformat(),
                    next_event_name=next_event_name,
                    last_announced_at=None,
                    last_triggered_at=state.get("last_triggered_at"),
                )
                continue
            if next_run_at <= now - timedelta(minutes=1) or not self._is_configured_next_run(next_run_at, scheduler):
                next_event_name = self._pick_scheduled_event(config)
                next_run_at = self._next_allowed_run_at(now, scheduler)
                await self.public_events_repo.set_state(
                    guild.id,
                    next_run_at=next_run_at.isoformat(),
                    next_event_name=next_event_name,
                    last_announced_at=None,
                    last_triggered_at=state.get("last_triggered_at"),
                )
                continue

            if announce_minutes_before > 0:
                announce_at = next_run_at - timedelta(minutes=announce_minutes_before)
                if now >= announce_at and state.get("last_announced_at") != next_run_at.isoformat():
                    if next_event_name == "Observador":
                        channels = await self._get_event_channels(guild)
                        observer_cfg = config.get("observer", {})
                        for channel in channels:
                            await self._send_observer_payload(
                                channel,
                                observer_cfg,
                                embed=self._build_observer_announcement_embed(announce_minutes_before),
                            )
                    elif next_event_name == "Julgamento":
                        julgamento_cog = self.bot.get_cog("JulgamentoCog")
                        if julgamento_cog is not None and hasattr(julgamento_cog, "announce_upcoming"):
                            await julgamento_cog.announce_upcoming(guild, announce_minutes_before)
                    else:
                        await self._send_event_message(
                            guild,
                            f"Evento publico em breve: **{next_event_name}** comeca em {announce_minutes_before} minuto(s).",
                        )
                    await self.public_events_repo.set_state(
                        guild.id,
                        next_run_at=next_run_at.isoformat(),
                        next_event_name=next_event_name,
                        last_announced_at=next_run_at.isoformat(),
                        last_triggered_at=state.get("last_triggered_at"),
                    )
                    state["last_announced_at"] = next_run_at.isoformat()

            if now < next_run_at:
                continue

            if next_event_name == "Observador":
                await self._start_observer(guild, config)
            elif next_event_name == "Julgamento":
                julgamento_cog = self.bot.get_cog("JulgamentoCog")
                if julgamento_cog is not None and hasattr(julgamento_cog, "start_scheduled_event"):
                    await julgamento_cog.start_scheduled_event(guild)
            elif next_event_name == "Infestacao de Aranhas":
                await self._start_spider_infestation(guild, config)
            else:
                await self._send_event_message(
                    guild,
                    f"Evento publico iniciado: **{next_event_name}**. Runtime ainda sera implementado nesta cog.",
                )
            next_event_name = self._pick_scheduled_event(config)
            next_cycle = self._next_allowed_run_at(now, scheduler)
            await self.public_events_repo.set_state(
                guild.id,
                next_run_at=next_cycle.isoformat(),
                next_event_name=next_event_name,
                last_announced_at=None,
                last_triggered_at=now.isoformat(),
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
        fixed_times = self._fixed_daily_times(scheduler)
        if fixed_times:
            formatted = ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in fixed_times)
            return f"Horarios fixos: {formatted} (Sao Paulo)"
        return f"Intervalo: {self._interval_minutes(scheduler)} minuto(s)"

    def _is_allowed_scheduler_time(self, dt: datetime, scheduler: dict) -> bool:
        pause_after_hour = int(scheduler.get("pause_after_hour", -1))
        resume_hour = int(scheduler.get("resume_hour", 0))
        if pause_after_hour < 0:
            return True
        hour = dt.astimezone(timezone.utc).hour
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

    def _enabled_event_names(self, config: dict) -> list[str]:
        enabled: list[str] = []
        if config.get("observer", {}).get("enabled"):
            enabled.append("Observador")
        if get_carousel_config().get("enabled"):
            enabled.append("Julgamento")
        if config.get("candy_shower", {}).get("enabled"):
            enabled.append("Infestacao de Aranhas")
        return enabled

    def _parse_dt(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    async def _get_or_preview_next_run_at(self, guild_id: int, scheduler: dict) -> datetime:
        state = await self.public_events_repo.get_state(guild_id)
        next_run_at = self._parse_dt(state.get("next_run_at"))
        if next_run_at is not None and next_run_at > datetime.now(timezone.utc) and self._is_configured_next_run(next_run_at, scheduler):
            return next_run_at
        next_run_at = self._next_allowed_run_at(datetime.now(timezone.utc), scheduler)
        next_event_name = str(state.get("next_event_name") or "") or self._pick_scheduled_event(self._load_config())
        await self.public_events_repo.set_state(
            guild_id,
            next_run_at=next_run_at.isoformat(),
            next_event_name=next_event_name,
            last_announced_at=None,
            last_triggered_at=state.get("last_triggered_at"),
        )
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

    async def _cleanup_stale_public_runtimes(self) -> None:
        for guild in self.bot.guilds:
            runtime = await self.admin_repo.get_runtime(guild.id)
            if runtime.get("event_active"):
                continue
            await self.public_events_repo.reset_observer_state(guild.id)
            await self.public_events_repo.reset_spider_state(guild.id)
            julgamento_cog = self.bot.get_cog("JulgamentoCog")
            if julgamento_cog is not None and hasattr(julgamento_cog, "force_reset"):
                await julgamento_cog.force_reset(guild)

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

    async def _send_observer_payload(
        self,
        channel: discord.abc.MessageableChannel,
        observer_cfg: dict,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        wait: bool = False,
    ) -> discord.Message | discord.WebhookMessage | None:
        webhook = await self._get_or_create_observer_webhook(channel, observer_cfg)
        if webhook is not None:
            try:
                return await webhook.send(
                    content=content,
                    embed=embed,
                    username=str(observer_cfg.get("webhook_name") or "Observador"),
                    thread=channel if isinstance(channel, discord.Thread) else discord.utils.MISSING,
                    wait=wait,
                )
            except discord.HTTPException:
                pass

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None
        try:
            return await channel.send(content=content, embed=embed)
        except discord.HTTPException:
            return None

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
        try:
            webhooks = await parent_channel.webhooks()
        except (discord.Forbidden, discord.HTTPException):
            return None

        avatar_bytes = self._load_observer_webhook_avatar_bytes(observer_cfg)
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

    async def _start_observer(self, guild: discord.Guild, config: dict) -> None:
        observer_cfg = config.get("observer", {})
        channels = await self._get_event_channels(guild)
        if not channels:
            return

        active_state = await self.public_events_repo.get_observer_state(guild.id)
        if active_state.get("active"):
            return

        questions = self._load_observer_questions(observer_cfg)
        if not questions:
            return

        question = await self._pick_observer_question(guild.id, questions)
        if question is None:
            return
        match_mode = self._resolve_observer_match_mode(question)
        difficulty = self._resolve_observer_difficulty(question)
        timeout_seconds = int(observer_cfg.get("answer_timeout_seconds", 90))
        accepted_answers = [
            self._prepare_observer_answer(str(answer), match_mode)
            for answer in self._expand_observer_answers(question)
        ]
        display_text = self._resolve_observer_display_text(question, match_mode)
        channel = channels[0]
        embed = self._build_observer_question_embed(
            str(question["question"]),
            timeout_seconds,
            match_mode,
            display_text,
            difficulty,
        )
        message = await self._send_observer_payload(
            channel,
            observer_cfg,
            embed=embed,
            wait=True,
        )
        if message is None:
            return
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=timeout_seconds)
        await self.public_events_repo.set_observer_state(
            guild.id,
            active=True,
            channel_id=channel.id,
            question_text=str(question["question"]),
            accepted_answers=accepted_answers,
            answer_match_mode=match_mode,
            question_difficulty=difficulty,
            started_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            message_id=message.id,
            winner_user_id=None,
        )
        self.bot.loop.create_task(self._finish_observer_if_unanswered(guild.id, expires_at))

    async def _finish_observer_if_unanswered(self, guild_id: int, expires_at: datetime) -> None:
        delay = max(0.0, (expires_at - datetime.now(timezone.utc)).total_seconds())
        await discord.utils.sleep_until(expires_at) if delay > 0 else None
        state = await self.public_events_repo.get_observer_state(guild_id)
        if not state.get("active"):
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await self.public_events_repo.set_observer_state(
                guild_id,
                active=False,
                channel_id=state.get("channel_id"),
                question_text=state.get("question_text"),
                accepted_answers=state.get("accepted_answers", []),
                answer_match_mode=state.get("answer_match_mode", "normalized"),
                question_difficulty=state.get("question_difficulty", "easy"),
                started_at=state.get("started_at"),
                expires_at=state.get("expires_at"),
                message_id=state.get("message_id"),
                winner_user_id=None,
            )
            return
        channel = guild.get_channel(int(state["channel_id"])) if state.get("channel_id") else None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await self._send_observer_payload(
                    channel,
                    self._load_config().get("observer", {}),
                    embed=self._build_observer_timeout_embed(),
                )
            except discord.HTTPException:
                pass
        await self.public_events_repo.set_observer_state(
            guild_id,
            active=False,
            channel_id=state.get("channel_id"),
            question_text=state.get("question_text"),
            accepted_answers=state.get("accepted_answers", []),
            answer_match_mode=state.get("answer_match_mode", "normalized"),
            question_difficulty=state.get("question_difficulty", "easy"),
            started_at=state.get("started_at"),
            expires_at=state.get("expires_at"),
            message_id=state.get("message_id"),
            winner_user_id=None,
        )

    async def _restore_spider_finish_tasks(self) -> None:
        for guild in self.bot.guilds:
            state = await self.public_events_repo.get_spider_state(guild.id)
            if not state.get("active"):
                continue
            ends_at = self._parse_dt(state.get("ends_at"))
            if ends_at is None or ends_at <= datetime.now(timezone.utc):
                await self._finish_spider_infestation(guild)
                continue
            self._schedule_spider_finish(guild.id, ends_at)

    def _spider_config(self, config: dict | None = None) -> dict:
        return dict((config or self._load_config()).get("candy_shower") or {})

    def _spider_emoji_id(self, spider_cfg: dict) -> int | None:
        value = spider_cfg.get("emoji_id")
        if value is None:
            return 0
        try:
            emoji_id = int(value)
        except (TypeError, ValueError):
            return 0
        return emoji_id if emoji_id > 0 else 0

    def _spider_emoji(self, guild: discord.Guild, emoji_id: int) -> str | discord.Emoji | discord.PartialEmoji:
        if emoji_id <= 0:
            return "🕷️"
        emoji = guild.get_emoji(emoji_id)
        if emoji is not None:
            return emoji
        return discord.PartialEmoji(name="aranha", id=emoji_id)

    async def _start_spider_infestation(self, guild: discord.Guild, config: dict | None = None) -> tuple[bool, str]:
        spider_cfg = self._spider_config(config)
        emoji_id = self._spider_emoji_id(spider_cfg)
        channels = await self._get_event_channels(guild)
        if not channels:
            return False, "Nenhum canal de evento configurado."
        state = await self.public_events_repo.get_spider_state(guild.id)
        if state.get("active"):
            return False, "A infestacao ja esta ativa."

        now = datetime.now(timezone.utc)
        duration_minutes = max(1, int(spider_cfg.get("duration_minutes", 15)))
        ends_at = now + timedelta(minutes=duration_minutes)
        await self.public_events_repo.reset_spider_state(guild.id)
        await self.public_events_repo.set_spider_state(
            guild.id,
            active=True,
            started_at=now.isoformat(),
            ends_at=ends_at.isoformat(),
            total_spawns=0,
        )
        self._schedule_spider_finish(guild.id, ends_at)
        await self._send_event_message(
            guild,
            f"🕷️ **Infestacao de Aranhas iniciada!** Reaja nas aranhas que aparecerem nas mensagens ate {discord.utils.format_dt(ends_at, style='R')}.",
        )
        return True, "ok"

    def _schedule_spider_finish(self, guild_id: int, ends_at: datetime) -> None:
        existing = self._spider_finish_tasks.pop(guild_id, None)
        if existing is not None:
            existing.cancel()
        self._spider_finish_tasks[guild_id] = asyncio.create_task(self._finish_spider_after_delay(guild_id, ends_at))

    async def _finish_spider_after_delay(self, guild_id: int, ends_at: datetime) -> None:
        try:
            await discord.utils.sleep_until(ends_at)
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                await self._finish_spider_infestation(guild)
        except asyncio.CancelledError:
            raise
        finally:
            current_task = asyncio.current_task()
            if self._spider_finish_tasks.get(guild_id) is current_task:
                self._spider_finish_tasks.pop(guild_id, None)

    async def _finish_spider_if_expired(self, guild: discord.Guild) -> None:
        state = await self.public_events_repo.get_spider_state(guild.id)
        if not state.get("active"):
            return
        ends_at = self._parse_dt(state.get("ends_at"))
        if ends_at is not None and ends_at <= datetime.now(timezone.utc):
            await self._finish_spider_infestation(guild)

    async def _finish_spider_infestation(self, guild: discord.Guild, *, cancelled: bool = False) -> None:
        state = await self.public_events_repo.get_spider_state(guild.id)
        if not state.get("active"):
            return

        finish_task = self._spider_finish_tasks.pop(guild.id, None)
        if finish_task is not None:
            finish_task.cancel()

        counts = await self.public_events_repo.list_spider_capture_counts(guild.id)
        spider_cfg = self._spider_config()
        if cancelled:
            await self.public_events_repo.reset_spider_state(guild.id)
            await self._send_event_message(guild, "🕷️ Infestacao de Aranhas cancelada.")
            return

        reward_lines: list[str] = []
        for row in counts:
            user_id = int(row["user_id"])
            captures = int(row["captures"])
            if captures <= 0:
                continue
            reward_summary = await self._grant_spider_rewards(user_id, guild.id, captures, spider_cfg)
            member = guild.get_member(user_id)
            mention = member.mention if member else f"<@{user_id}>"
            reward_lines.append(f"{mention}: **{captures}** aranha(s) - {reward_summary}")

        total_spawns = int(state.get("total_spawns") or 0)
        if reward_lines:
            body = "\n".join(reward_lines[:15])
            if len(reward_lines) > 15:
                body += f"\n...e mais {len(reward_lines) - 15} participante(s)."
        else:
            body = "Ninguem capturou aranhas desta vez."
        await self._send_embed_to_event_channels(
            guild,
            discord.Embed(
                title="🕷️ Infestacao de Aranhas encerrada",
                description=body,
                color=self.OBSERVER_COLOR,
            ).add_field(name="Aranhas que apareceram", value=str(total_spawns), inline=False),
        )
        await self.public_events_repo.reset_spider_state(guild.id)

    async def _maybe_spawn_spider(self, message: discord.Message) -> None:
        state = await self.public_events_repo.get_spider_state(message.guild.id)
        if not state.get("active"):
            return
        ends_at = self._parse_dt(state.get("ends_at"))
        if ends_at is None or ends_at <= datetime.now(timezone.utc):
            await self._finish_spider_infestation(message.guild)
            return

        spider_cfg = self._spider_config()
        emoji_id = self._spider_emoji_id(spider_cfg)
        channels = await self._get_event_channels(message.guild)
        if message.channel.id not in {channel.id for channel in channels}:
            return
        min_length = int(spider_cfg.get("min_message_length", 3))
        if len(message.content.strip()) < min_length:
            return
        max_spawns = int(spider_cfg.get("max_spawns_per_event", 0) or 0)
        if max_spawns > 0 and int(state.get("total_spawns") or 0) >= max_spawns:
            return
        spawn_chance = float(spider_cfg.get("spawn_chance_percent", 8))
        if random.uniform(0, 100) > spawn_chance:
            return

        capture_seconds = max(1, float(spider_cfg.get("capture_seconds", 3)))
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=capture_seconds)
        spawn_id = await self.public_events_repo.create_spider_spawn(
            message.guild.id,
            message.channel.id,
            message.id,
            emoji_id,
            now.isoformat(),
            expires_at.isoformat(),
        )
        try:
            await message.add_reaction(self._spider_emoji(message.guild, emoji_id))
        except discord.HTTPException:
            await self.public_events_repo.close_spider_spawn(message.guild.id, spawn_id)
            return
        await self.public_events_repo.increment_spider_spawns(message.guild.id)
        self._schedule_spider_expire(message.guild.id, message.channel.id, message.id, spawn_id, emoji_id, expires_at)

    def _schedule_spider_expire(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        spawn_id: int,
        emoji_id: int,
        expires_at: datetime,
    ) -> None:
        key = (guild_id, spawn_id)
        existing = self._spider_expire_tasks.pop(key, None)
        if existing is not None:
            existing.cancel()
        self._spider_expire_tasks[key] = asyncio.create_task(
            self._expire_spider_spawn(guild_id, channel_id, message_id, spawn_id, emoji_id, expires_at)
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
        try:
            await discord.utils.sleep_until(expires_at)
            guild = self.bot.get_guild(guild_id)
            channel = self.bot.get_channel(channel_id)
            if guild is not None and isinstance(channel, (discord.TextChannel, discord.Thread)):
                message: discord.Message | None = None
                try:
                    message = await channel.fetch_message(message_id)
                    await message.clear_reaction(self._spider_emoji(guild, emoji_id))
                except discord.Forbidden:
                    if message is not None and self.bot.user is not None:
                        try:
                            await message.remove_reaction(self._spider_emoji(guild, emoji_id), self.bot.user)
                        except discord.HTTPException:
                            pass
                except discord.HTTPException:
                    pass
            await self.public_events_repo.close_spider_spawn(guild_id, spawn_id)
        except asyncio.CancelledError:
            raise
        finally:
            current_task = asyncio.current_task()
            key = (guild_id, spawn_id)
            if self._spider_expire_tasks.get(key) is current_task:
                self._spider_expire_tasks.pop(key, None)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return
        spider_cfg = self._spider_config()
        emoji_id = self._spider_emoji_id(spider_cfg)
        if emoji_id > 0 and payload.emoji.id != emoji_id:
            return
        if emoji_id <= 0 and str(payload.emoji.name) != "🕷️":
            return

        state = await self.public_events_repo.get_spider_state(payload.guild_id)
        if not state.get("active"):
            return
        ends_at = self._parse_dt(state.get("ends_at"))
        if ends_at is None or ends_at <= datetime.now(timezone.utc):
            guild = self.bot.get_guild(payload.guild_id)
            if guild is not None:
                await self._finish_spider_infestation(guild)
            return

        spawn = await self.public_events_repo.get_active_spider_spawn(payload.guild_id, payload.message_id, emoji_id)
        if spawn is None:
            return
        expires_at = self._parse_dt(spawn.get("expires_at"))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            await self.public_events_repo.close_spider_spawn(payload.guild_id, int(spawn["spawn_id"]))
            return

        max_captures = int(spider_cfg.get("max_captures_per_user", 0) or 0)
        if max_captures > 0:
            current = await self.public_events_repo.count_spider_captures_for_user(payload.guild_id, payload.user_id)
            if current >= max_captures:
                return

        await self.public_events_repo.add_spider_capture(
            payload.guild_id,
            int(spawn["spawn_id"]),
            payload.user_id,
            datetime.now(timezone.utc).isoformat(),
        )

    async def _grant_spider_rewards(self, user_id: int, guild_id: int, captures: int, spider_cfg: dict) -> str:
        per_capture = dict(spider_cfg.get("reward_per_capture") or {})
        lines: list[str] = []
        labels = {
            "candies": "doces",
            "trick_coins": "moedas travessas",
            "dice_count": "dados",
            "xp_bank": "XP bank",
        }
        for field, label in labels.items():
            amount_per_capture = int(per_capture.get(field, 0) or 0)
            amount = amount_per_capture * captures
            if amount <= 0:
                continue
            await self.users_repo.add_currency(user_id, field, amount)
            lines.append(f"{amount} {label}")

        item_rewards = spider_cfg.get("item_rewards") or []
        if isinstance(item_rewards, list):
            locale = await self.i18n.get_user_locale(user_id)
            for reward in item_rewards:
                if not isinstance(reward, dict):
                    continue
                item_id = str(reward.get("item_id") or "").strip()
                if not item_id:
                    continue
                chance = float(reward.get("chance_percent_per_capture", 0) or 0)
                amount = sum(1 for _ in range(captures) if random.uniform(0, 100) <= chance)
                if amount <= 0:
                    continue
                total = await self.inventory_service.grant_item(user_id, item_id, amount)
                item_name = self.i18n.maybe_t(locale, f"item.{item_id}.name") or item_id
                lines.append(f"{amount}x {item_name} (total {total})")

        lines.extend(await self.mission_service.evaluate_progress(guild_id, user_id))
        return ", ".join(lines) if lines else "sem recompensa configurada"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        await self._maybe_spawn_spider(message)

        state = await self.public_events_repo.get_observer_state(message.guild.id)
        if not state.get("active"):
            return
        if state.get("channel_id") != message.channel.id:
            return
        expires_at = self._parse_dt(state.get("expires_at"))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            return

        match_mode = str(state.get("answer_match_mode") or "normalized")
        prepared_answer = self._prepare_observer_answer(message.content, match_mode)
        answers = {str(answer) for answer in state.get("accepted_answers", [])}
        if prepared_answer not in answers:
            return

        config = self._load_config().get("observer", {})
        await self.public_events_repo.set_observer_state(
            message.guild.id,
            active=False,
            channel_id=state.get("channel_id"),
            question_text=state.get("question_text"),
            accepted_answers=state.get("accepted_answers", []),
            answer_match_mode=match_mode,
            question_difficulty=state.get("question_difficulty", "easy"),
            started_at=state.get("started_at"),
            expires_at=state.get("expires_at"),
            message_id=state.get("message_id"),
            winner_user_id=message.author.id,
        )
        reward_lines = await self._grant_observer_rewards(
            message.author.id,
            config,
            message.guild.id,
            str(state.get("question_difficulty") or "easy"),
        )
        summary = "\n".join(reward_lines) if reward_lines else "Sem recompensa configurada."
        try:
            await self._send_observer_payload(
                message.channel,
                config,
                embed=self._build_observer_winner_embed(message.author.mention, summary),
            )
        except discord.HTTPException:
            pass

    async def _grant_observer_rewards(
        self,
        user_id: int,
        config: dict,
        guild_id: int,
        difficulty: str,
    ) -> list[str]:
        locale = await self.i18n.get_user_locale(user_id)
        lines: list[str] = []
        reward_pool = dict(config.get("reward_pool") or {})
        difficulty_defaults = dict(config.get("difficulty_defaults") or {})
        difficulty_cfg = dict(difficulty_defaults.get(difficulty) or difficulty_defaults.get("easy") or {})
        reward_count = max(1, int(difficulty_cfg.get("reward_count", 2)))
        allow_baskets = bool(difficulty_cfg.get("allow_baskets", False))

        category_names = [name for name in reward_pool if allow_baskets or name != "baskets"]
        if category_names:
            selected_names = random.sample(category_names, k=min(reward_count, len(category_names)))
            for category_name in selected_names:
                reward_cfg = dict(reward_pool.get(category_name) or {})
                reward_line = await self._grant_observer_reward_entry(user_id, locale, reward_cfg)
                if reward_line:
                    lines.append(reward_line)

        lines.extend(await self.mission_service.evaluate_progress(guild_id, user_id))
        return lines

    async def _grant_observer_reward_entry(self, user_id: int, locale: str, reward_cfg: dict) -> str | None:
        grant_type = str(reward_cfg.get("grant_type") or "").strip().lower()
        if grant_type == "currency":
            grant_id = str(reward_cfg.get("grant_id") or "")
            amount = self._random_reward_amount(reward_cfg)
            if amount <= 0:
                return None
            await self.users_repo.add_currency(user_id, grant_id, amount)
            reward_keys = {
                "candies": "missions.reward.candies",
                "trick_coins": "missions.reward.trick_coins",
                "dice_count": "missions.reward.dice",
                "xp_bank": "missions.reward.xp",
            }
            i18n_key = reward_keys.get(grant_id)
            return self.i18n.t(locale, i18n_key, amount=amount) if i18n_key else None

        if grant_type == "item":
            grant_id = str(reward_cfg.get("grant_id") or "")
            amount = self._random_reward_amount(reward_cfg)
            if amount <= 0:
                return None
            total = await self.inventory_service.grant_item(user_id, grant_id, amount)
            item_name = self.i18n.maybe_t(locale, f"item.{grant_id}.name") or grant_id
            return self.i18n.t(locale, "baskets.reward.item", item_name=item_name, amount=amount, total=total)

        if grant_type == "item_random":
            item_ids = [str(item_id) for item_id in reward_cfg.get("item_ids", []) if str(item_id).strip()]
            if not item_ids:
                return None
            grant_id = random.choice(item_ids)
            amount = max(1, int(reward_cfg.get("amount", 1)))
            total = await self.inventory_service.grant_item(user_id, grant_id, amount)
            item_name = self.i18n.maybe_t(locale, f"item.{grant_id}.name") or grant_id
            return self.i18n.t(locale, "baskets.reward.item", item_name=item_name, amount=amount, total=total)

        return None

    def _random_reward_amount(self, reward_cfg: dict) -> int:
        if "amount" in reward_cfg:
            return int(reward_cfg.get("amount") or 0)
        min_amount = int(reward_cfg.get("min_amount", 0) or 0)
        max_amount = int(reward_cfg.get("max_amount", min_amount) or min_amount)
        if max_amount < min_amount:
            max_amount = min_amount
        return random.randint(min_amount, max_amount)

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

    def _load_observer_questions(self, observer_cfg: dict) -> list[dict]:
        relative_path = str(observer_cfg.get("question_source") or "content/halloween/observer_questions.json")
        if relative_path.startswith("content/"):
            path = ROOT_DIR / relative_path
        else:
            path = ROOT_DIR / relative_path
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Observer question source not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    async def _pick_observer_question(self, guild_id: int, questions: list[dict]) -> dict | None:
        valid_questions = [question for question in questions if str(question.get("id") or "").strip()]
        if not valid_questions:
            return None

        used_ids = await self.public_events_repo.get_observer_used_question_ids(guild_id)
        used_set = set(used_ids)
        available_questions = [
            question for question in valid_questions if str(question.get("id")) not in used_set
        ]
        if not available_questions:
            used_ids = []
            used_set = set()
            available_questions = valid_questions

        question = random.choice(available_questions)
        question_id = str(question.get("id"))
        if question_id not in used_set:
            used_ids.append(question_id)
        await self.public_events_repo.set_observer_used_question_ids(guild_id, used_ids)
        return question

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

    def _observer_disguise_spaces(self, value: str) -> str:
        return value.replace(" ", f" {INVISIBLE_SPACE}")

    def _observer_spoiler_words(self, value: str) -> str:
        return f" {INVISIBLE_SPACE}".join(f"||{part}||" for part in value.split(" "))

    def _build_observer_announcement_embed(self, minutes: int) -> discord.Embed:
        embed = discord.Embed(
            title="📹 *sinal de gravacao detectado...*",
            description=(
                f"O Observador vai abrir um novo arquivo em **{minutes} minuto(s)**.\n\n"
                "Uma pergunta sera registrada. A primeira resposta certa no chat vence.\n"
                "Ele ja esta olhando."
            ),
            color=self.OBSERVER_COLOR,
        )
        embed.set_footer(text="Toda mensagem deixa um rastro.")
        return embed

    def _build_observer_question_embed(
        self,
        question_text: str,
        timeout_seconds: int,
        match_mode: str,
        display_text: str | None,
        difficulty: str,
    ) -> discord.Embed:
        exact = match_mode in {"exact", "spaces", "spoiler"}
        validation = "Exato — acentos e maiusculas nao importam." if exact else "Acentos e pontuacao podem variar."

        description_lines = [f"*{question_text}*"]
        if match_mode == "spaces" and display_text:
            description_lines.append(f"\n> Digite exatamente: **'{display_text}'**")
        if match_mode == "spoiler" and display_text:
            description_lines.append(f"\n> Revele e digite: {display_text}")

        embed = discord.Embed(
            title="📹 O Observador esta gravando",
            description="\n".join(description_lines),
            color=self.OBSERVER_COLOR,
        )
        embed.add_field(
            name="\u200b",
            value=f"⏱️ **{timeout_seconds}s** · {validation} · {difficulty.capitalize()}",
            inline=False,
        )
        embed.set_footer(text="Primeira resposta certa no chat vence.")
        return embed

    def build_observer_translation_embed(self, question: dict, timeout_seconds: int) -> discord.Embed | None:
        question_en = str(question.get("question_en") or "").strip()
        if not question_en:
            return None
        match_mode = self._resolve_observer_match_mode(question)
        difficulty = self._resolve_observer_difficulty(question)
        display_text = self._resolve_observer_display_text_en(question, match_mode)
        exact = match_mode in {"exact", "spaces", "spoiler"}
        validation = "Exact text required. Case does not matter." if exact else "Accents and punctuation may vary."

        description_lines = [f"*{question_en}*"]
        if match_mode == "spaces" and display_text:
            description_lines.append(f"\n> Type exactly: **'{display_text}'**")
        if match_mode == "spoiler" and display_text:
            description_lines.append(f"\n> Reveal and type: {display_text}")

        embed = discord.Embed(
            title="📹 The Observer is recording",
            description="\n".join(description_lines),
            color=self.OBSERVER_COLOR,
        )
        embed.add_field(
            name="\u200b",
            value=f"⏱️ **{timeout_seconds}s** · {validation} · {difficulty.capitalize()}",
            inline=False,
        )
        embed.set_footer(text="This translation is private to you.")
        return embed

    def _build_observer_timeout_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📼 *gravacao encerrada*",
            description="Ninguem respondeu antes de o arquivo ser fechado.",
            color=self.OBSERVER_COLOR,
        )
        embed.set_footer(text="O Observador continua atento.")
        return embed

    def _build_observer_winner_embed(self, winner_mention: str, summary: str) -> discord.Embed:
        embed = discord.Embed(
            title="🩸 Resposta certa.",
            description=f"{winner_mention} foi o primeiro a acertar.",
            color=self.OBSERVER_COLOR,
        )
        embed.add_field(name="Recompensas", value=summary, inline=False)
        embed.set_footer(text="Voce escapou do foco desta vez.")
        return embed

    def _build_tempo_unavailable_embed(self, title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.orange())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PublicEventsCog(bot))
