from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import string
import unicodedata

import discord
from discord import app_commands
from discord.ext import commands, tasks

from repositories.admin_repo import AdminRepo
from repositories.julgamento_repo import JulgamentoRepo
from repositories.users_repo import UsersRepo
from services.ghostface_question_catalog import GhostfaceQuestionCatalog
from services.config_service import get_carousel_config
from services.i18n_service import I18nService
from services.mission_service import MissionService
from services.reward_settlement_service import RewardSettlementService
from services.julgamento_runtime import JulgamentoRuntimeService
from services.julgamento_texts import JulgamentoTextsService
from views.julgamento_views import JulgamentoSignupView, StaticTranslateView
from utils.event_config import get_achievements_config


ROOT_DIR = Path(__file__).resolve().parent.parent
JULGAMENTO_COLOR = discord.Color.from_rgb(171, 52, 40)
LOGGER = logging.getLogger(__name__)
GHOSTFACE_CONFUSABLES = str.maketrans(
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

class JulgamentoCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.admin_repo = AdminRepo()
        self.repo = JulgamentoRepo()
        self.users_repo = UsersRepo()
        self.i18n = I18nService(self.users_repo)
        self.mission_service = MissionService(users_repo=self.users_repo, i18n_service=self.i18n)
        self.reward_settlement_service = RewardSettlementService(
            users_repo=self.users_repo,
            guild_metrics_repo=self.mission_service.guild_metrics_repo,
            user_guild_metrics_repo=self.mission_service.user_guild_metrics_repo,
        )
        self.runtime_service = JulgamentoRuntimeService()
        self.texts_service = JulgamentoTextsService()
        self.question_catalog = GhostfaceQuestionCatalog(ROOT_DIR)
        self._resolving_guilds: set[int] = set()
        self._finishing_guilds: set[int] = set()
        self._starting_round_guilds: set[int] = set()
        self._launching_guilds: set[int] = set()
        self._countdown_tasks: dict[int, asyncio.Task] = {}
        self._pending_questions: dict[int, dict] = {}
        self._avatar_cache_bytes: bytes | None = None
        self._avatar_cache_key: tuple[str, int] | None = None
        self._startup_reconciled = False
        self._startup_reconcile_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        await self.repo.init_db()
        if not self.runtime_loop.is_running():
            self.runtime_loop.start()

    async def _reconcile_after_ready(self) -> None:
        if self._startup_reconciled:
            return
        async with self._startup_reconcile_lock:
            if self._startup_reconciled:
                return
            for guild in self.bot.guilds:
                try:
                    runtime = await self.repo.get_runtime(guild.id)
                    event_runtime = await self.admin_repo.get_runtime(guild.id)
                    if runtime.get("active"):
                        await self._edit_signup_message_cancelled(guild, runtime)
                        await self.force_reset(guild)
                        event_channels = await self._get_event_channels(
                            guild,
                            await self.admin_repo.get_config(guild.id),
                        )
                        if event_channels:
                            await self._send_silas_message(
                                event_channels[0],
                                embed=self._build_interrupted_embed("pt-BR"),
                                view=self._build_public_translate_view(self._build_interrupted_embed_en()),
                            )
                    elif not event_runtime.get("event_active"):
                        await self.force_reset(guild)
                except Exception:
                    LOGGER.exception("Falha ao reconciliar Carrossel apos reinicio na guild %s", guild.id)
            self._startup_reconciled = True

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._reconcile_after_ready()

    def cog_unload(self) -> None:
        if self.runtime_loop.is_running():
            self.runtime_loop.cancel()
        for task in self._countdown_tasks.values():
            task.cancel()
        self._countdown_tasks.clear()

    def _load_config(self) -> dict:
        return get_carousel_config()

    def reload_config(self) -> None:
        self._avatar_cache_bytes = None
        self._avatar_cache_key = None

    async def _increment_horror_correct_answers(self, guild: discord.Guild, user_id: int) -> int:
        total = await self.users_repo.increment_event_counter(user_id, "horror_questions_correct_total", 1)
        await self.mission_service.increment_guild_metric(guild.id, "horror_questions_correct_total", 1)
        await self.mission_service.increment_user_guild_metric(guild.id, user_id, "horror_questions_correct_total", 1)
        config = await self.admin_repo.get_config(guild.id)
        role_id = config.get("cinefilo_role_id")
        required = max(1, int(get_achievements_config().get("cinefilo_correct_answers_required", 50) or 50))
        if not role_id or total < required:
            return total
        role = guild.get_role(int(role_id))
        member = guild.get_member(user_id)
        if role is None or member is None or role in member.roles:
            return total
        try:
            await member.add_roles(role, reason=f"Cinefilo: {total} respostas corretas")
        except (discord.Forbidden, discord.HTTPException):
            return total
        return total

    def _signup_duration(self, cfg: dict) -> timedelta:
        if cfg.get("signup_seconds") is not None:
            return timedelta(seconds=max(1, int(cfg.get("signup_seconds") or 1)))
        return timedelta(minutes=float(cfg.get("signup_minutes", 5)))

    def _build_event_base_embed(self, locale: str = "pt-BR") -> discord.Embed:
        return self.texts_service.build_event_base_embed(self._jt, JULGAMENTO_COLOR, locale)

    def _build_event_base_embed_en(self) -> discord.Embed:
        return self._build_event_base_embed("en-US")

    def _build_public_translate_view(self, embed_en: discord.Embed) -> discord.ui.View:
        return StaticTranslateView(embed_en)

    def _build_base_message_runtime_embed(
        self,
        locale: str,
        runtime: dict,
        participants: list[dict],
        *,
        finished: bool = False,
        winner: discord.Member | None = None,
    ) -> discord.Embed:
        alive_count = sum(1 for item in participants if str(item.get("status")) == "alive")
        spectator_count = sum(1 for item in participants if str(item.get("status")) == "spectator")
        embed = discord.Embed(title=self._jt(locale, "base_message.title"), color=JULGAMENTO_COLOR)
        if finished:
            embed.description = self._jt(
                locale,
                "base_message.finished_description",
                winner=winner.mention if winner else ("nenhum sobrevivente" if locale == "pt-BR" else "no survivors"),
            )
        elif runtime.get("active") and runtime.get("phase") == "signup":
            embed.description = self._jt(locale, "base_message.signup_description")
        elif runtime.get("active") and runtime.get("phase") == "countdown":
            embed.description = self._jt(locale, "base_message.countdown_description")
        elif runtime.get("active") and runtime.get("phase") == "running":
            embed.description = self._jt(locale, "base_message.running_description")
        else:
            embed.description = self._jt(locale, "base_message.idle_description")
        embed.add_field(name=self._jt(locale, "base_message.players_name"), value=str(len(participants)), inline=True)
        embed.add_field(name=self._jt(locale, "base_message.alive_name"), value=str(alive_count), inline=True)
        embed.add_field(name=self._jt(locale, "base_message.spectators_name"), value=str(spectator_count), inline=True)
        if runtime.get("active") and runtime.get("current_question_text"):
            embed.add_field(name=self._jt(locale, "base_message.round_name"), value=str(runtime.get("round_number") or 0), inline=True)
            embed.add_field(
                name=self._jt(locale, "base_message.mode_name"),
                value=(
                    self._jt(locale, "base_message.mode_individual")
                    if runtime.get("current_player_id")
                    else self._jt(locale, "base_message.mode_collective")
                ),
                inline=True,
            )
        embed.add_field(name=self._jt(locale, "base_message.audience_name"), value=self._jt(locale, "base_message.audience_value"), inline=False)
        return embed

    async def _get_locale(self, user_id: int) -> str:
        return await self.i18n.get_user_locale(user_id)

    def _jt(self, locale: str, key: str, **kwargs: object) -> str:
        return self.i18n.t(locale, f"julgamento.{key}", **kwargs)

    def _t(self, locale: str, pt: str, en: str) -> str:
        return pt if locale == "pt-BR" else en

    def _build_interrupted_embed(self, locale: str = "pt-BR") -> discord.Embed:
        return self.texts_service.build_interrupted_embed(self._jt, JULGAMENTO_COLOR, locale)

    def _build_interrupted_embed_en(self) -> discord.Embed:
        return self._build_interrupted_embed("en-US")

    def _build_announcement_embed(self, locale: str, starts_at: datetime) -> discord.Embed:
        return self.texts_service.build_announcement_embed(self._jt, JULGAMENTO_COLOR, locale, starts_at)

    def _build_announcement_embed_en(self, starts_at: datetime) -> discord.Embed:
        return self._build_announcement_embed("en-US", starts_at)

    def _build_round_announcement_embed(
        self,
        locale: str,
        next_round: int,
        alive_count: int,
        mode_name: str,
        countdown_ends_at: datetime,
        responder_id: int | None,
    ) -> discord.Embed:
        return self.texts_service.build_round_announcement_embed(
            self._jt,
            JULGAMENTO_COLOR,
            locale,
            next_round,
            alive_count,
            mode_name,
            countdown_ends_at,
            responder_id,
        )

    def _build_round_announcement_embed_en(
        self,
        next_round: int,
        alive_count: int,
        mode_name: str,
        countdown_ends_at: datetime,
        responder_id: int | None,
    ) -> discord.Embed:
        return self._build_round_announcement_embed(
            "en-US",
            next_round,
            alive_count,
            mode_name,
            countdown_ends_at,
            responder_id,
        )

    def _build_individual_correct_embed(self, locale: str, member_mention: str) -> discord.Embed:
        return self.texts_service.build_individual_correct_embed(self._jt, JULGAMENTO_COLOR, locale, member_mention)

    def _build_individual_correct_embed_en(self, member_mention: str) -> discord.Embed:
        return self._build_individual_correct_embed("en-US", member_mention)

    def _build_timeout_embed(self, locale: str, member_mention: str) -> discord.Embed:
        return self.texts_service.build_timeout_embed(self._jt, JULGAMENTO_COLOR, locale, member_mention)

    def _build_timeout_embed_en(self, member_mention: str) -> discord.Embed:
        return self._build_timeout_embed("en-US", member_mention)

    def _build_elimination_summary_embed_en(
        self,
        question_text: str,
        eliminated_mentions: str,
        fastest_mentions: str,
        alive_count: int,
    ) -> discord.Embed:
        return self.texts_service.build_elimination_summary_embed(
            self._jt,
            JULGAMENTO_COLOR,
            "en-US",
            question_text,
            eliminated_mentions,
            fastest_mentions,
            alive_count,
        )

    def _build_finish_embed(self, locale: str, winner_mention: str | None) -> discord.Embed:
        return self.texts_service.build_finish_embed(self._jt, JULGAMENTO_COLOR, locale, winner_mention)

    def _build_finish_embed_en(self, winner_mention: str | None) -> discord.Embed:
        return self._build_finish_embed("en-US", winner_mention)

    def _build_reward_summary_embed(self, locale: str, winner_mention: str, reward_lines: list[str]) -> discord.Embed:
        title = self._jt(locale, "rewards.title")
        description = self._jt(locale, "rewards.description", winner=winner_mention) + "\n" + "\n".join(
            f"- {line}" for line in reward_lines
        )
        return discord.Embed(title=title, description=description, color=JULGAMENTO_COLOR)

    async def _grant_carrossel_winner_rewards(
        self,
        guild_id: int,
        user_id: int,
        settlement_key: str,
    ) -> tuple[list[str], list[str]]:
        cfg = self._load_config()
        rewards = dict(cfg.get("winner_rewards") or {})
        locale = await self.i18n.get_user_locale(user_id)
        settlement = await self.reward_settlement_service.get_settlement(settlement_key)
        currencies: dict[str, int] = {}
        items: dict[str, int] = {}
        if settlement is None:
            for reward_type, amount_raw in rewards.items():
                amount = int(amount_raw or 0)
                if amount <= 0:
                    continue
                if reward_type in {"candies", "trick_coins", "dice_count", "xp_bank"}:
                    currencies[reward_type] = amount
                else:
                    items[reward_type] = amount

            settlement = await self.reward_settlement_service.settle_once(
                settlement_key,
                "carrossel",
                user_id,
                guild_id,
                currencies=currencies,
                items=items,
            )
        reward_lines: list[str] = []
        reward_keys = {
            "candies": "missions.reward.candies",
            "trick_coins": "missions.reward.trick_coins",
            "dice_count": "missions.reward.dice",
            "xp_bank": "missions.reward.xp",
        }
        for reward_type, amount in settlement.currencies.items():
            reward_key = reward_keys.get(reward_type)
            if reward_key:
                reward_lines.append(self.i18n.t(locale, reward_key, amount=amount))
        for reward_type, amount in settlement.items.items():
            item_name = self.i18n.maybe_t(locale, f"shop.{reward_type}.name") or reward_type
            reward_lines.append(f"{item_name} x{amount}")

        mission_notices = await self.mission_service.evaluate_progress(guild_id, user_id)
        return reward_lines, mission_notices

    def _build_ticket_required_embed(self, locale: str, *, inventory_missing: bool = False) -> discord.Embed:
        return self.texts_service.build_ticket_required_embed(
            self._jt,
            JULGAMENTO_COLOR,
            locale,
            inventory_missing=inventory_missing,
        )

    def _build_join_confirmed_embed(
        self,
        locale: str,
        *,
        remaining_tickets: int,
        channel_link: str,
        thread_link: str,
    ) -> discord.Embed:
        return self.texts_service.build_join_confirmed_embed(
            self._jt,
            JULGAMENTO_COLOR,
            locale,
            remaining_tickets=remaining_tickets,
            channel_link=channel_link,
            thread_link=thread_link,
        )

    async def _safe_edit_message(self, message: discord.Message, **kwargs) -> bool:
        for _ in range(3):
            try:
                await message.edit(**kwargs)
                return True
            except discord.DiscordServerError:
                await asyncio.sleep(1)
                continue
            except discord.HTTPException:
                return False
        return False

    async def _send_silas_message(
        self,
        channel: discord.TextChannel | discord.Thread,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        wait: bool = False,
    ) -> discord.Message | discord.WebhookMessage | None:
        cfg = self._load_config()
        webhook = await self._get_or_create_silas_webhook(channel, cfg)
        if webhook is not None:
            try:
                send_kwargs = {
                    "content": content,
                    "embed": embed,
                    "username": str(cfg.get("webhook_name") or "Silas Dyrkon"),
                    "thread": channel if isinstance(channel, discord.Thread) else discord.utils.MISSING,
                    "wait": wait,
                }
                if view is not None:
                    send_kwargs["view"] = view
                return await webhook.send(
                    **send_kwargs,
                )
            except discord.HTTPException:
                pass
        try:
            return await channel.send(content=content, embed=embed, view=view)
        except discord.HTTPException:
            return None

    async def _send_round_question_payload(
        self,
        channel: discord.TextChannel,
        *,
        embed: discord.Embed,
        view: discord.ui.View,
        question: dict,
    ) -> discord.Message | None:
        attachment_path = self.question_catalog.image_path(question)
        attachment_filename = self.question_catalog.attachment_filename(question)
        for include_view in (True, False):
            attachment = (
                discord.File(attachment_path, filename=attachment_filename)
                if attachment_path is not None and attachment_filename
                else None
            )
            try:
                send_kwargs = {"embed": embed}
                if include_view:
                    send_kwargs["view"] = view
                if attachment is not None:
                    send_kwargs["file"] = attachment
                return await channel.send(**send_kwargs)
            except discord.HTTPException:
                if not include_view:
                    return None
            finally:
                if attachment is not None:
                    attachment.close()
        return None

    async def _get_or_create_silas_webhook(
        self,
        channel: discord.TextChannel | discord.Thread,
        cfg: dict,
    ) -> discord.Webhook | None:
        parent_channel = channel.parent if isinstance(channel, discord.Thread) else channel
        if not isinstance(parent_channel, discord.TextChannel):
            return None
        webhook_name = str(cfg.get("webhook_name") or "Silas Dyrkon")
        avatar_bytes = self._load_silas_webhook_avatar_bytes(cfg)
        try:
            webhooks = await parent_channel.webhooks()
        except (discord.Forbidden, discord.HTTPException):
            return None
        for webhook in webhooks:
            if webhook.name == webhook_name and webhook.user == self.bot.user:
                return webhook
        try:
            return await parent_channel.create_webhook(name=webhook_name, avatar=avatar_bytes)
        except (discord.Forbidden, discord.HTTPException):
            return None

    def _load_silas_webhook_avatar_bytes(self, cfg: dict) -> bytes | None:
        relative_path = str(cfg.get("webhook_avatar_path") or "").strip()
        if not relative_path:
            self._avatar_cache_bytes = None
            self._avatar_cache_key = None
            return None
        path = ROOT_DIR / relative_path
        if not path.exists() or not path.is_file():
            self._avatar_cache_bytes = None
            self._avatar_cache_key = None
            return None
        mtime_ns = path.stat().st_mtime_ns
        cache_key = (relative_path, mtime_ns)
        if self._avatar_cache_key == cache_key:
            return self._avatar_cache_bytes
        try:
            avatar_bytes = path.read_bytes()
        except OSError:
            return None
        self._avatar_cache_bytes = avatar_bytes
        self._avatar_cache_key = cache_key
        return avatar_bytes

    async def is_active(self, guild_id: int) -> bool:
        runtime = await self.repo.get_runtime(guild_id)
        return bool(runtime.get("active"))

    async def force_reset(self, guild: discord.Guild, *, refund_tickets: bool = False) -> None:
        config = await self.admin_repo.get_config(guild.id)
        self._pending_questions.pop(guild.id, None)
        countdown_task = self._countdown_tasks.pop(guild.id, None)
        if countdown_task is not None:
            countdown_task.cancel()
        if refund_tickets:
            await self._refund_cancelled_carrossel_tickets(guild, config)
        await self._clear_event_roles(guild, config)
        await self.repo.reset_runtime(guild.id)
        await self._refresh_base_message(guild)

    async def announce_upcoming(self, guild: discord.Guild, starts_at: datetime) -> None:
        config = await self.admin_repo.get_config(guild.id)
        if not self._is_configured(config):
            return

        await self._clear_event_roles(guild, config)
        event_channels = await self._get_event_channels(guild, config)
        if not event_channels:
            return

        embed = self._build_announcement_embed("pt-BR", starts_at)
        embed_en = self._build_announcement_embed_en(starts_at)
        role_content = None
        if config.get("notification_role_id"):
            role = guild.get_role(int(config["notification_role_id"]))
            if role is not None:
                role_content = role.mention
        for channel in event_channels:
            try:
                message = await self._send_silas_message(
                    channel,
                    content=role_content,
                    embed=embed,
                    view=self._build_public_translate_view(embed_en),
                    wait=True,
                )
                if message is None:
                    continue
                await self.repo.set_runtime(guild.id, announcement_message_id=message.id)
            except discord.HTTPException:
                continue

    async def start_scheduled_event(self, guild: discord.Guild) -> tuple[bool, str]:
        config = await self.admin_repo.get_config(guild.id)
        if not self._is_configured(config):
            return False, "Configuracao incompleta do Carrossel."

        runtime = await self.repo.get_runtime(guild.id)
        if runtime.get("active"):
            return False, "O Carrossel ja esta ativo."

        event_channels = await self._get_event_channels(guild, config)
        julgamento_channel = await self._resolve_text_channel(guild, config.get("julgamento_channel_id"))
        if (
            not event_channels
            or not isinstance(julgamento_channel, discord.TextChannel)
        ):
            return False, "Canal do Carrossel nao encontrado."

        cfg = self._load_config()
        await self._clear_event_roles(guild, config)
        await self._cleanup_channel_once(julgamento_channel)

        try:
            base_message = await julgamento_channel.send(
                embed=self._build_event_base_embed(),
                view=self._build_public_translate_view(self._build_event_base_embed_en()),
            )
            thread = await base_message.create_thread(
                name=str(cfg.get("thread_name") or "💬 Galera do Carrossel"),
                auto_archive_duration=1440,
            )
            await thread.send(self._jt("pt-BR", "thread.welcome"))
        except discord.HTTPException:
            return False, "Nao consegui criar a thread do Carrossel."
        await self.admin_repo.set_julgamento_structure(
            guild.id,
            channel_id=julgamento_channel.id,
            thread_id=thread.id,
            base_message_id=base_message.id,
        )

        signup_end = datetime.now(timezone.utc) + self._signup_duration(cfg)
        signup_channel = event_channels[0]
        signup_embed = await self._build_signup_embed(guild, signup_end, signup_open=True)
        signup_message = await signup_channel.send(
            embed=signup_embed,
            view=JulgamentoSignupView(self, guild.id, allow_join=True, allow_spectate=False),
        )

        await self.repo.set_runtime(
            guild.id,
            active=True,
            phase="signup",
            channel_id=julgamento_channel.id,
            thread_id=thread.id,
            signup_message_channel_id=signup_channel.id,
            signup_message_id=signup_message.id,
            round_number=0,
            current_player_id=None,
            current_question_text=None,
            current_answers=[],
            current_correct_order=[],
            current_match_mode=None,
            answer_deadline_at=None,
            started_at=datetime.now(timezone.utc).isoformat(),
            signup_ends_at=signup_end.isoformat(),
            spectator_ends_at=None,
            winner_user_id=None,
        )
        await self.repo.clear_participants(guild.id)
        self._pending_questions.pop(guild.id, None)
        return True, "ok"

    async def handle_join(self, interaction: discord.Interaction) -> None:
        locale = await self._get_locale(interaction.user.id)
        if interaction.guild is None:
            await interaction.response.send_message(
                self._jt(locale, "join.guild_only"),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        runtime = await self.repo.get_runtime(interaction.guild.id)
        if not runtime.get("active") or runtime.get("phase") != "signup":
            await interaction.followup.send(
                self._jt(locale, "join.signup_closed"),
                ephemeral=True,
            )
            return
        signup_ends_at = self._parse_dt(runtime.get("signup_ends_at"))
        if signup_ends_at is None or signup_ends_at <= datetime.now(timezone.utc):
            await self._close_signup(interaction.guild, runtime)
            await interaction.followup.send(
                self._jt(locale, "join.signup_closed"),
                ephemeral=True,
            )
            return
        participants = await self.repo.get_participants(interaction.guild.id)
        already_in = any(int(item["user_id"]) == interaction.user.id for item in participants)
        if already_in:
            await interaction.followup.send(
                self._jt(locale, "join.already_in"),
                ephemeral=True,
            )
            return

        profile = await self.users_repo.get_user(interaction.user.id)
        inventory = profile.get("inventory", {})
        if int(inventory.get("ticket_carrossel", 0)) <= 0:
            await interaction.followup.send(embed=self._build_ticket_required_embed(locale), ephemeral=True)
            return

        config = await self.admin_repo.get_config(interaction.guild.id)
        muted_role = interaction.guild.get_role(int(config["julgamento_muted_role_id"])) if config.get("julgamento_muted_role_id") else None
        spectator_role = interaction.guild.get_role(int(config["julgamento_spectator_role_id"])) if config.get("julgamento_spectator_role_id") else None
        active_role = interaction.guild.get_role(int(config["julgamento_active_role_id"])) if config.get("julgamento_active_role_id") else None
        if muted_role is None:
            await interaction.followup.send(self._jt(locale, "join.muted_role_missing"), ephemeral=True)
            return

        roles_to_remove = [role for role in (spectator_role, active_role) if role is not None and role in interaction.user.roles]
        try:
            if roles_to_remove:
                await interaction.user.remove_roles(*roles_to_remove, reason="Carrossel: entrada no evento")
            if muted_role not in interaction.user.roles:
                await interaction.user.add_roles(muted_role, reason="Carrossel: inscricao")
        except discord.Forbidden:
            await interaction.followup.send(self._jt(locale, "join.role_update_failed"), ephemeral=True)
            return

        try:
            remaining_tickets = await self.users_repo.remove_inventory_item(interaction.user.id, "ticket_carrossel", 1)
        except ValueError:
            await interaction.followup.send(embed=self._build_ticket_required_embed(locale, inventory_missing=True), ephemeral=True)
            return

        await self.repo.add_participant(
            interaction.guild.id,
            interaction.user.id,
            "alive",
            datetime.now(timezone.utc).isoformat(),
        )
        await self._refresh_signup_message(interaction.guild)
        await self._refresh_base_message(interaction.guild)

        channel_link = f"https://discord.com/channels/{interaction.guild.id}/{runtime['channel_id']}"
        thread_link = f"https://discord.com/channels/{interaction.guild.id}/{runtime['thread_id']}"
        await interaction.followup.send(
            embed=self._build_join_confirmed_embed(
                locale,
                remaining_tickets=remaining_tickets,
                channel_link=channel_link,
                thread_link=thread_link,
            ),
            ephemeral=True,
        )

    async def handle_spectate(self, interaction: discord.Interaction) -> None:
        locale = await self._get_locale(interaction.user.id)
        if interaction.guild is None:
            await interaction.response.send_message(
                self._jt(locale, "spectate.guild_only"),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        runtime = await self.repo.get_runtime(interaction.guild.id)
        if not runtime.get("active"):
            await interaction.followup.send(self._jt(locale, "spectate.not_active"), ephemeral=True)
            return
        participants = await self.repo.get_participants(interaction.guild.id)
        participant_statuses = {
            int(item["user_id"]): str(item.get("status") or "")
            for item in participants
        }
        if participant_statuses.get(interaction.user.id) == "alive":
            await interaction.followup.send(self._jt(locale, "spectate.alive_blocked"), ephemeral=True)
            return

        config = await self.admin_repo.get_config(interaction.guild.id)
        spectator_role = interaction.guild.get_role(int(config["julgamento_spectator_role_id"])) if config.get("julgamento_spectator_role_id") else None
        if spectator_role is None:
            await interaction.followup.send(self._jt(locale, "spectate.role_missing"), ephemeral=True)
            return

        try:
            if spectator_role not in interaction.user.roles:
                await interaction.user.add_roles(spectator_role, reason="Carrossel: espectador")
        except discord.Forbidden:
            await interaction.followup.send(self._jt(locale, "spectate.role_failed"), ephemeral=True)
            return

        channel_link = f"https://discord.com/channels/{interaction.guild.id}/{runtime['channel_id']}"
        thread_link = f"https://discord.com/channels/{interaction.guild.id}/{runtime['thread_id']}"
        await interaction.followup.send(
            self._jt(locale, "spectate.confirmed", channel=channel_link, thread=thread_link),
            ephemeral=True,
        )

    @tasks.loop(seconds=5)
    async def runtime_loop(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self._process_runtime_tick(guild)
            except Exception:
                LOGGER.exception("Falha no loop do Carrossel para guild %s", guild.id)

    async def _process_runtime_tick(self, guild: discord.Guild) -> None:
        runtime = await self.repo.get_runtime(guild.id)
        if not runtime.get("active"):
            return
        event_runtime = await self.admin_repo.get_runtime(guild.id)
        if not event_runtime.get("event_active"):
            await self.force_reset(guild)
            return
        if runtime.get("phase") == "signup":
            signup_ends_at = self._parse_dt(runtime.get("signup_ends_at"))
            if signup_ends_at is None or signup_ends_at > datetime.now(timezone.utc):
                return
            await self._close_signup(guild, runtime)
            return
        if runtime.get("phase") == "countdown":
            countdown_ends_at = self._parse_dt(runtime.get("answer_deadline_at"))
            if countdown_ends_at is None:
                await self._recover_broken_countdown(guild, runtime)
                return
            if countdown_ends_at > datetime.now(timezone.utc):
                return
            await self._launch_countdown_question(guild, runtime)
            return
        if runtime.get("phase") == "running":
            await self._progress_running_round(guild, runtime)
            return
        if runtime.get("phase") == "finished":
            spectator_ends_at = self._parse_dt(runtime.get("spectator_ends_at"))
            if spectator_ends_at is None or spectator_ends_at > datetime.now(timezone.utc):
                return
            await self._clear_event_roles(guild, await self.admin_repo.get_config(guild.id))
            await self.repo.reset_runtime(guild.id)
            await self._refresh_base_message(guild)

    async def _recover_broken_countdown(self, guild: discord.Guild, runtime: dict) -> None:
        LOGGER.warning("Recuperando countdown quebrado do Carrossel para guild %s", guild.id)
        previous_round = max(0, int(runtime.get("round_number") or 0) - 1)
        await self.repo.set_runtime(
            guild.id,
            phase="running",
            round_number=previous_round,
            current_player_id=None,
            current_question_text=None,
            current_answers=[],
            current_correct_order=[],
            current_match_mode=None,
            answer_deadline_at=None,
        )
        updated_runtime = await self.repo.get_runtime(guild.id)
        await self._progress_running_round(guild, updated_runtime)

    @runtime_loop.before_loop
    async def before_runtime_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self._reconcile_after_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        runtime = await self.repo.get_runtime(message.guild.id)
        if not runtime.get("active") or runtime.get("phase") != "running":
            return
        if int(runtime.get("channel_id") or 0) != message.channel.id:
            return
        participant_statuses = {
            int(item["user_id"]): str(item.get("status") or "")
            for item in await self.repo.get_participants(message.guild.id)
        }
        if participant_statuses.get(message.author.id) != "alive":
            return
        if runtime.get("current_player_id") and int(runtime.get("current_player_id") or 0) != message.author.id:
            return

        deadline = self._parse_dt(runtime.get("answer_deadline_at"))
        if deadline is None or deadline <= datetime.now(timezone.utc):
            return

        match_mode = str(runtime.get("current_match_mode") or "normalized")
        attempt = self._prepare_answer(message.content, match_mode)
        accepted_answers = {
            self._prepare_answer(answer, match_mode)
            for answer in runtime.get("current_answers", [])
        }
        if attempt not in accepted_answers:
            return

        correct_order = [int(user_id) for user_id in runtime.get("current_correct_order", [])]
        if message.author.id in correct_order:
            return

        if runtime.get("current_player_id"):
            await self._handle_individual_correct_answer(message)
            return

        await self._handle_collective_correct_answer(message, correct_order)

    async def _close_signup(self, guild: discord.Guild, runtime: dict) -> None:
        alive = await self._alive_participants(guild.id)
        if not alive:
            event_channels = await self._get_event_channels(guild, await self.admin_repo.get_config(guild.id))
            if event_channels:
                await self._send_silas_message(event_channels[0], content=self._jt("pt-BR", "public.no_signup_content"))
            await self._edit_signup_message_cancelled(guild, runtime)
            await self.repo.reset_runtime(guild.id)
            await self._refresh_base_message(guild)
            return
        await self.repo.set_runtime(guild.id, phase="running", signup_ends_at=runtime.get("signup_ends_at"))
        channel = guild.get_channel(int(runtime["signup_message_channel_id"])) if runtime.get("signup_message_channel_id") else None
        if isinstance(channel, discord.TextChannel) and runtime.get("signup_message_id"):
            try:
                message = await channel.fetch_message(int(runtime["signup_message_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
            if message is not None:
                embed = await self._build_signup_embed(guild, self._parse_dt(runtime.get("signup_ends_at")), signup_open=False)
                await self._safe_edit_message(
                    message,
                    embed=embed,
                    view=JulgamentoSignupView(self, guild.id, allow_join=False, allow_spectate=True),
                )
        await self._sync_round_roles(guild, round_open=False)
        await self._refresh_base_message(guild)
        updated_runtime = await self.repo.get_runtime(guild.id)
        await self._progress_running_round(guild, updated_runtime)

    async def _edit_signup_message_cancelled(self, guild: discord.Guild, runtime: dict) -> None:
        channel = guild.get_channel(int(runtime["signup_message_channel_id"])) if runtime.get("signup_message_channel_id") else None
        if not isinstance(channel, discord.TextChannel) or not runtime.get("signup_message_id"):
            return
        try:
            message = await channel.fetch_message(int(runtime["signup_message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        embed = discord.Embed(
            title=self._jt("pt-BR", "signup_cancelled.title"),
            description=self._jt("pt-BR", "signup_cancelled.description"),
            color=JULGAMENTO_COLOR,
        )
        await self._safe_edit_message(
            message,
            embed=embed,
            view=JulgamentoSignupView(self, guild.id, allow_join=False, allow_spectate=False),
        )

    async def _progress_running_round(self, guild: discord.Guild, runtime: dict) -> None:
        if guild.id in self._resolving_guilds or guild.id in self._starting_round_guilds:
            return
        self._resolving_guilds.add(guild.id)
        try:
            alive = await self._alive_participants(guild.id)
            if len(alive) <= 1:
                await self._finish_event(guild, alive[0]["user_id"] if alive else None)
                return

            deadline = self._parse_dt(runtime.get("answer_deadline_at"))
            if runtime.get("current_question_text") and deadline is not None:
                if deadline <= datetime.now(timezone.utc):
                    await self._resolve_round(guild, runtime)
                return

            await self._start_round(guild, runtime, alive)
        finally:
            self._resolving_guilds.discard(guild.id)

    async def _start_round(self, guild: discord.Guild, runtime: dict, alive: list[dict]) -> None:
        await self.runtime_service.start_round(self, guild, runtime, alive)

    def _schedule_countdown_launch(self, guild_id: int, round_number: int, delay_seconds: int) -> None:
        self.runtime_service.schedule_countdown_launch(self, guild_id, round_number, delay_seconds)

    async def _launch_countdown_after_delay(self, guild_id: int, round_number: int, delay_seconds: int) -> None:
        await self.runtime_service.launch_countdown_after_delay(self, guild_id, round_number, delay_seconds)

    async def _launch_countdown_question(self, guild: discord.Guild, runtime: dict) -> None:
        await self.runtime_service.launch_countdown_question(self, guild, runtime)

    async def _resolve_round(self, guild: discord.Guild, runtime: dict) -> None:
        await self.runtime_service.resolve_round(self, guild, runtime)

    async def _handle_individual_correct_answer(self, message: discord.Message) -> None:
        await self.runtime_service.handle_individual_correct_answer(self, message)

    async def _handle_collective_correct_answer(self, message: discord.Message, correct_order: list[int]) -> None:
        await self.runtime_service.handle_collective_correct_answer(self, message, correct_order)

    async def _resolve_individual_round_timeout(self, guild: discord.Guild, current_player_id: int) -> None:
        await self.runtime_service.resolve_individual_round_timeout(self, guild, current_player_id)

    async def _resolve_collective_round(
        self,
        guild: discord.Guild,
        runtime: dict,
        alive_ids: list[int],
        correct_order: list[int],
    ) -> None:
        await self.runtime_service.resolve_collective_round(self, guild, runtime, alive_ids, correct_order)

    async def _clear_round_state(self, guild_id: int) -> None:
        self._pending_questions.pop(guild_id, None)
        await self.repo.set_runtime(
            guild_id,
            current_player_id=None,
            current_question_id=None,
            current_question_text=None,
            current_answers=[],
            current_correct_order=[],
            current_match_mode=None,
            answer_deadline_at=None,
        )

    async def _announce_elimination_summary(
        self,
        guild: discord.Guild,
        runtime: dict,
        eliminated_ids: list[int],
        correct_order: list[int],
    ) -> None:
        await self.runtime_service.announce_elimination_summary(self, guild, runtime, eliminated_ids, correct_order)

    async def _finish_event(self, guild: discord.Guild, winner_user_id: int | None) -> None:
        if guild.id in self._finishing_guilds:
            return
        self._finishing_guilds.add(guild.id)
        try:
            await self.runtime_service.finish_event(self, guild, winner_user_id)
        finally:
            self._finishing_guilds.discard(guild.id)

    async def _refresh_signup_message(self, guild: discord.Guild) -> None:
        runtime = await self.repo.get_runtime(guild.id)
        if not runtime.get("active") or not runtime.get("signup_message_id"):
            return
        channel = guild.get_channel(int(runtime["signup_message_channel_id"])) if runtime.get("signup_message_channel_id") else None
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(int(runtime["signup_message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        embed = await self._build_signup_embed(guild, self._parse_dt(runtime.get("signup_ends_at")), signup_open=True)
        await self._safe_edit_message(
            message,
            embed=embed,
            view=JulgamentoSignupView(self, guild.id, allow_join=True, allow_spectate=False),
        )

    async def _refresh_base_message(
        self,
        guild: discord.Guild,
        *,
        winner_user_id: int | None = None,
        finished: bool = False,
    ) -> None:
        config = await self.admin_repo.get_config(guild.id)
        runtime = await self.repo.get_runtime(guild.id)
        channel = guild.get_channel(int(config["julgamento_channel_id"])) if config.get("julgamento_channel_id") else None
        if not isinstance(channel, discord.TextChannel) or not config.get("julgamento_base_message_id"):
            return
        try:
            message = await channel.fetch_message(int(config["julgamento_base_message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        participants = await self.repo.get_participants(guild.id)
        winner = guild.get_member(int(winner_user_id)) if finished and winner_user_id else None
        embed = self._build_base_message_runtime_embed("pt-BR", runtime, participants, finished=finished, winner=winner)
        await self._safe_edit_message(
            message,
            embed=embed,
            view=self._build_public_translate_view(
                self._build_base_message_runtime_embed("en-US", runtime, participants, finished=finished, winner=winner)
            ),
        )

    async def _build_signup_embed(
        self,
        guild: discord.Guild,
        signup_ends_at: datetime | None,
        *,
        signup_open: bool,
        locale: str = "pt-BR",
    ) -> discord.Embed:
        participants = await self.repo.get_participants(guild.id)
        embed = discord.Embed(
            title=self._jt(locale, "signup.title"),
            description=self._jt(locale, "signup.description"),
            color=JULGAMENTO_COLOR,
        )
        embed.add_field(name=self._jt(locale, "signup.signed_name"), value=str(len(participants)), inline=True)
        embed.add_field(
            name=self._jt(locale, "signup.phase_name"),
            value=self._jt(locale, "signup.phase_open" if signup_open else "signup.phase_started"),
            inline=True,
        )
        if signup_ends_at is not None:
            embed.add_field(
                name=self._jt(locale, "signup.closes_name"),
                value=discord.utils.format_dt(signup_ends_at, style="R") if signup_open else self._jt(locale, "signup.closed_value"),
                inline=False,
            )
        if participants:
            mentions = []
            for item in participants[:15]:
                member = guild.get_member(int(item["user_id"]))
                mentions.append(member.mention if member else f"<@{int(item['user_id'])}>")
            embed.add_field(name=self._jt(locale, "signup.players_name"), value=", ".join(mentions), inline=False)
        return embed

    async def _alive_participants(self, guild_id: int) -> list[dict]:
        participants = await self.repo.get_participants(guild_id)
        return [item for item in participants if str(item.get("status")) == "alive"]

    async def _alive_count(self, guild_id: int) -> int:
        return len(await self._alive_participants(guild_id))

    def _difficulty_for_round(self, round_number: int, alive_count: int) -> str:
        if alive_count <= 2:
            return "hard"
        if round_number <= 1:
            return "easy"
        if round_number == 2:
            return random.choice(["easy", "medium"])
        return "medium"

    def _load_questions(self) -> list[dict]:
        cfg = self._load_config()
        return self.question_catalog.load(cfg)

    def _pick_question_by_difficulty(self, difficulty: str) -> dict | None:
        questions = self._load_questions()
        filtered = [
            item for item in questions
            if str(item.get("difficulty") or "easy").strip().lower() == difficulty
        ]
        if not filtered:
            filtered = questions
        if not filtered:
            return None

        questions_by_kind: dict[str, list[dict]] = {}
        for question in filtered:
            questions_by_kind.setdefault(self.question_catalog.kind(question), []).append(question)
        kinds = list(questions_by_kind)
        weight_config = dict(self._load_config().get("round_type_weights") or {})
        weights: list[float] = []
        for kind in kinds:
            try:
                weight = float(weight_config.get(kind, 1))
            except (TypeError, ValueError):
                weight = 1.0
            weights.append(weight if math.isfinite(weight) and weight >= 0 else 1.0)
        selected_kind = random.choices(kinds, weights=weights, k=1)[0] if sum(weights) > 0 else random.choice(kinds)
        selected = questions_by_kind[selected_kind]

        if difficulty == "hard" and selected_kind == "text":
            exact_filtered = [
                item for item in selected
                if str(item.get("match_mode") or "normalized") in {"exact", "spaces", "spoiler"}
            ]
            if exact_filtered:
                selected = exact_filtered
        return random.choice(selected)

    def _find_question_by_id(self, question_id: str | None) -> dict | None:
        return self.question_catalog.find_by_id(self._load_questions(), question_id)

    def _pick_individual_responder(self, runtime: dict, alive: list[dict], round_number: int) -> int | None:
        alive_ids = [int(item["user_id"]) for item in alive]
        if not alive_ids:
            return None
        if len(alive_ids) == 1:
            return alive_ids[0]
        return alive_ids[(round_number - 1) % len(alive_ids)]

    def _build_round_question_embed(
        self,
        runtime: dict,
        question: dict,
        responder_id: int | None,
        timeout_seconds: int,
        locale: str = "pt-BR",
    ) -> discord.Embed:
        return self.texts_service.build_round_question_embed(
            self._jt,
            JULGAMENTO_COLOR,
            runtime,
            question,
            responder_id,
            timeout_seconds,
            locale,
            attachment_filename=self.question_catalog.attachment_filename(question),
        )

    def _build_question_translation_embed(self, question: dict, timeout_seconds: int) -> discord.Embed | None:
        return self.texts_service.build_question_translation_embed(
            self._jt,
            JULGAMENTO_COLOR,
            question,
            timeout_seconds,
        )

    def _expand_answers(self, question: dict) -> list[str]:
        answers = [str(answer).strip() for answer in question.get("answers", []) if str(answer).strip()]
        pool_name = str(question.get("answer_pool") or "").strip().lower()
        if pool_name == "horror_movies":
            horror_path = ROOT_DIR / "content" / "halloween" / "horror_movies.json"
            if horror_path.exists():
                data = json.loads(horror_path.read_text(encoding="utf-8"))
                for item in data:
                    if isinstance(item, str):
                        value = item.strip()
                        if value:
                            answers.append(value)
                    elif isinstance(item, dict):
                        for title in item.get("titles", []):
                            value = str(title).strip()
                            if value:
                                answers.append(value)
        seen: set[str] = set()
        unique: list[str] = []
        for answer in answers:
            if answer not in seen:
                seen.add(answer)
                unique.append(answer)
        return unique

    def _prepare_answer(self, value: str, match_mode: str) -> str:
        trimmed = value.strip().translate(GHOSTFACE_CONFUSABLES)
        if match_mode in {"exact", "spaces", "spoiler"}:
            return trimmed.casefold()
        collapsed = " ".join(trimmed.split())
        normalized = unicodedata.normalize("NFD", collapsed.lower())
        no_accents = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
        no_punctuation = no_accents.translate(str.maketrans("", "", string.punctuation))
        return " ".join(no_punctuation.split())

    async def _sync_round_roles(self, guild: discord.Guild, round_open: bool, active_user_id: int | None = None) -> None:
        config = await self.admin_repo.get_config(guild.id)
        active_role = guild.get_role(int(config["julgamento_active_role_id"])) if config.get("julgamento_active_role_id") else None
        muted_role = guild.get_role(int(config["julgamento_muted_role_id"])) if config.get("julgamento_muted_role_id") else None
        spectator_role = guild.get_role(int(config["julgamento_spectator_role_id"])) if config.get("julgamento_spectator_role_id") else None
        participants = await self.repo.get_participants(guild.id)
        for item in participants:
            member = guild.get_member(int(item["user_id"]))
            if member is None:
                continue
            status = str(item.get("status"))
            try:
                if status == "spectator":
                    if spectator_role and spectator_role not in member.roles:
                        await member.add_roles(spectator_role, reason="Julgamento: espectador")
                    if active_role and active_role in member.roles:
                        await member.remove_roles(active_role, reason="Julgamento: ajuste")
                    if muted_role and muted_role in member.roles:
                        await member.remove_roles(muted_role, reason="Julgamento: ajuste")
                elif round_open and (active_user_id is None or int(item["user_id"]) == active_user_id):
                    if active_role and active_role not in member.roles:
                        await member.add_roles(active_role, reason="Julgamento: rodada aberta")
                    if muted_role and muted_role in member.roles:
                        await member.remove_roles(muted_role, reason="Julgamento: ajuste")
                else:
                    if muted_role and muted_role not in member.roles:
                        await member.add_roles(muted_role, reason="Julgamento: aguardando rodada")
                    if active_role and active_role in member.roles:
                        await member.remove_roles(active_role, reason="Julgamento: ajuste")
            except discord.Forbidden:
                continue

    async def _move_member_to_spectator(self, guild: discord.Guild, member: discord.Member) -> None:
        config = await self.admin_repo.get_config(guild.id)
        active_role = guild.get_role(int(config["julgamento_active_role_id"])) if config.get("julgamento_active_role_id") else None
        muted_role = guild.get_role(int(config["julgamento_muted_role_id"])) if config.get("julgamento_muted_role_id") else None
        spectator_role = guild.get_role(int(config["julgamento_spectator_role_id"])) if config.get("julgamento_spectator_role_id") else None
        roles_to_remove = [role for role in (active_role, muted_role) if role is not None and role in member.roles]
        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Julgamento: eliminado")
            if spectator_role and spectator_role not in member.roles:
                await member.add_roles(spectator_role, reason="Julgamento: eliminado")
        except discord.Forbidden:
            return

    async def _cleanup_channel_once(self, channel: discord.TextChannel) -> None:
        try:
            await channel.purge(limit=100, bulk=True)
        except discord.HTTPException:
            async for message in channel.history(limit=100):
                try:
                    await message.delete()
                except discord.HTTPException:
                    continue

    async def _clear_event_roles(self, guild: discord.Guild, config: dict) -> None:
        role_ids = [
            config.get("julgamento_active_role_id"),
            config.get("julgamento_muted_role_id"),
            config.get("julgamento_spectator_role_id"),
        ]
        for role_id in role_ids:
            if not role_id:
                continue
            role = guild.get_role(int(role_id))
            if role is None:
                continue
            for member in list(role.members):
                try:
                    await member.remove_roles(role, reason="Julgamento: limpeza preventiva")
                except discord.Forbidden:
                    continue

    async def _refund_cancelled_carrossel_tickets(self, guild: discord.Guild, config: dict) -> int:
        muted_role_id = config.get("julgamento_muted_role_id")
        if not muted_role_id:
            return 0

        muted_role = guild.get_role(int(muted_role_id))
        if muted_role is None:
            return 0

        refunded_count = 0
        for member in list(muted_role.members):
            await self.users_repo.add_inventory_item(member.id, "ticket_carrossel", 1)
            refunded_count += 1
        return refunded_count

    async def _get_event_channels(self, guild: discord.Guild, config: dict) -> list[discord.TextChannel]:
        channels: list[discord.TextChannel] = []
        for channel_id in config.get("event_channel_ids") or []:
            channel = await self._resolve_text_channel(guild, channel_id)
            if isinstance(channel, discord.TextChannel):
                channels.append(channel)
        return channels

    async def _resolve_text_channel(self, guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
        if not channel_id:
            return None
        channel = guild.get_channel(int(channel_id))
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def _resolve_thread(self, guild: discord.Guild, channel_id: int | None) -> discord.Thread | None:
        if not channel_id:
            return None
        channel = guild.get_channel(int(channel_id))
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    def _is_configured(self, config: dict) -> bool:
        required = [
            config.get("julgamento_channel_id"),
            config.get("julgamento_active_role_id"),
            config.get("julgamento_muted_role_id"),
            config.get("julgamento_spectator_role_id"),
        ]
        return all(required) and bool(config.get("event_channel_ids"))

    def _parse_dt(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JulgamentoCog(bot))
