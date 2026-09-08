from __future__ import annotations

import random
import re
import unicodedata
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from repositories.effects_repo import EffectsRepo
from repositories.julgamento_repo import JulgamentoRepo
from repositories.public_events_repo import PublicEventsRepo
from services.audit_service import AuditService
from services.config_service import get_public_events_config
from services.ghostface_question_catalog import GhostfaceQuestionCatalog
from services.inventory_service import InventoryService
from services.i18n_service import I18nService
from services.items_texts import ItemsTextService
from services.mission_service import MissionService


ROOT_DIR = Path(__file__).resolve().parent.parent
INTERFERENCE_ROLE_NAME = "Interferencia"
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


class ItemsRuntimeService:
    def __init__(
        self,
        *,
        bot: commands.Bot,
        i18n: I18nService,
        admin_repo: AdminRepo,
        effects_repo: EffectsRepo,
        public_events_repo: PublicEventsRepo,
        julgamento_repo: JulgamentoRepo,
        audit_service: AuditService,
        inventory_service: InventoryService,
        mission_service: MissionService,
        effects_config: dict,
        texts: ItemsTextService,
    ) -> None:
        self.bot = bot
        self.i18n = i18n
        self.admin_repo = admin_repo
        self.effects_repo = effects_repo
        self.public_events_repo = public_events_repo
        self.julgamento_repo = julgamento_repo
        self.audit_service = audit_service
        self.inventory_service = inventory_service
        self.mission_service = mission_service
        self.effects_config = effects_config
        self.texts = texts
        self.ghostface_catalog = GhostfaceQuestionCatalog(ROOT_DIR)

    def load_public_events_config(self) -> dict:
        return get_public_events_config()

    def load_ghostface_questions(self) -> list[dict]:
        config = self.load_public_events_config()
        return self.ghostface_catalog.load(dict(config.get("ghostface") or {}))

    def parse_dt(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def admin_mute_deletes_messages(self, member: discord.Member) -> bool:
        return bool(
            member.guild_permissions.administrator
            and self.effects_config.get("admin_mute_delete_messages", True)
        )

    def normalize_text(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
        return " ".join(ascii_text.lower().split())

    def find_ghostface_question(
        self,
        *,
        question_id: str | None,
        question_text: str | None,
    ) -> dict | None:
        questions = self.load_ghostface_questions()
        question = self.ghostface_catalog.find_by_id(questions, question_id)
        if question is not None:
            return question
        return self.ghostface_catalog.find_by_text(questions, question_text, self.normalize_text)

    def find_ghostface_question_by_text(self, question_text: str) -> dict | None:
        return self.find_ghostface_question(question_id=None, question_text=question_text)

    def select_hint_answer(self, question: dict, locale: str) -> str:
        localized_keys = (
            ("hint_answer_en", "answer_en", "display_text_en")
            if locale == "en-US"
            else ("hint_answer_pt", "answer_pt", "display_text")
        )
        for key in localized_keys:
            value = str(question.get(key) or "").strip()
            if value:
                return value
        answers = [str(answer).strip() for answer in question.get("answers", []) if str(answer).strip()]
        if not answers:
            return ""
        return min(answers, key=lambda answer: (len(answer), answer.lower()))

    def build_hint_initials(self, answer: str) -> str:
        initials = []
        for word in answer.split():
            first_visible = next((char.upper() for char in word if char.isalnum()), None)
            if first_visible:
                initials.append(first_visible)
        return " / ".join(initials) if initials else "-"

    def build_hint_pattern(self, answer: str) -> str:
        return self.build_hint_pattern_with_reveals(answer, reveal_count=0, seed="")

    def build_hint_pattern_with_reveals(self, answer: str, *, reveal_count: int, seed: str) -> str:
        reveal_positions = self.select_reveal_positions(answer, reveal_count, seed)
        masked_words: list[str] = []
        alnum_index = 0
        for word in answer.split():
            visible_started = False
            masked_chars: list[str] = []
            for char in word:
                if char.isalnum():
                    should_reveal = alnum_index in reveal_positions
                    if not visible_started:
                        masked_chars.append(char.upper())
                        visible_started = True
                    elif should_reveal:
                        masked_chars.append(char.upper())
                    else:
                        masked_chars.append("_")
                    alnum_index += 1
                else:
                    masked_chars.append(char)
            masked_words.append("".join(masked_chars) or word)
        return " / ".join(masked_words) if masked_words else "-"

    def select_reveal_positions(self, answer: str, reveal_count: int, seed: str) -> set[int]:
        if reveal_count <= 0:
            return set()
        eligible_positions: list[int] = []
        alnum_index = 0
        for word in answer.split():
            first_visible = True
            for char in word:
                if not char.isalnum():
                    continue
                if first_visible:
                    first_visible = False
                else:
                    eligible_positions.append(alnum_index)
                alnum_index += 1
        if not eligible_positions:
            return set()
        digest = sha256(f"{seed}|{answer}".encode("utf-8")).digest()
        weighted = []
        for idx, position in enumerate(eligible_positions):
            weight = digest[idx % len(digest)]
            weighted.append((weight, position))
        weighted.sort()
        return {position for _, position in weighted[: min(reveal_count, len(weighted))]}

    def build_hint_extra(self, question: dict, locale: str) -> str | None:
        key = "hint_en" if locale == "en-US" else "hint_pt"
        value = str(question.get(key) or "").strip()
        return value or None

    def _scheduler_event_kind(self, next_event_name: str) -> str | None:
        normalized = self.normalize_text(next_event_name)
        if "aranha" in normalized or "spider" in normalized:
            return "spiders"
        if "julgamento" in normalized or "carousel" in normalized or "carrossel" in normalized:
            return "carousel"
        return None

    async def get_mute_guardrail_reason(self, guild_id: int, locale: str) -> str | None:
        now = datetime.now(timezone.utc)

        julgamento_runtime = await self.julgamento_repo.get_runtime(guild_id)
        if julgamento_runtime.get("active"):
            return self.i18n.t(locale, "mute.blocked.carousel")

        ghostface_state = await self.public_events_repo.get_ghostface_state(guild_id)
        if ghostface_state.get("active"):
            return self.i18n.t(locale, "mute.blocked.ghostface")

        public_cfg = self.load_public_events_config()
        scheduler_cfg = dict(public_cfg.get("scheduler") or {})
        announce_minutes_before = max(0, int(scheduler_cfg.get("announce_minutes_before", 0) or 0))
        if announce_minutes_before <= 0:
            return None

        scheduler_state = await self.public_events_repo.get_state(guild_id)
        next_event_name = str(scheduler_state.get("next_event_name") or "").strip().lower()
        next_run_at = self.parse_dt(scheduler_state.get("next_run_at"))
        if next_event_name != "ghostface" or next_run_at is None:
            return None

        announce_starts_at = next_run_at - timedelta(minutes=announce_minutes_before)
        if announce_starts_at <= now < next_run_at:
            return self.i18n.t(locale, "mute.blocked.ghostface_soon")
        return None

    async def get_interference_guardrail_reason(self, guild_id: int, locale: str) -> str | None:
        now = datetime.now(timezone.utc)

        julgamento_runtime = await self.julgamento_repo.get_runtime(guild_id)
        if julgamento_runtime.get("active"):
            return self.i18n.t(locale, "interference.blocked.carousel")

        spider_state = await self.public_events_repo.get_spider_state(guild_id)
        if spider_state.get("active"):
            return self.i18n.t(locale, "interference.blocked.spiders")

        scheduler_state = await self.public_events_repo.get_state(guild_id)
        next_run_at = self.parse_dt(scheduler_state.get("next_run_at"))
        next_event_kind = self._scheduler_event_kind(str(scheduler_state.get("next_event_name") or ""))
        if next_run_at is None or next_event_kind is None:
            return None
        if next_run_at - timedelta(minutes=10) <= now < next_run_at:
            if next_event_kind == "carousel":
                return self.i18n.t(locale, "interference.blocked.carousel_soon")
            if next_event_kind == "spiders":
                return self.i18n.t(locale, "interference.blocked.spiders_soon")
        return None

    async def handle_ghostface_hint(self, interaction: discord.Interaction, locale: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(self.i18n.t(locale, "ghostface_hint.guild_only"), ephemeral=True)
            return

        state = await self.public_events_repo.get_ghostface_state(interaction.guild.id)
        expires_at = self.parse_dt(state.get("expires_at"))
        if (
            not state.get("active")
            or not (state.get("question_id") or state.get("question_text"))
            or expires_at is None
            or expires_at <= datetime.now(timezone.utc)
        ):
            await interaction.response.send_message(self.i18n.t(locale, "ghostface_hint.no_active"), ephemeral=True)
            return

        question = self.find_ghostface_question(
            question_id=state.get("question_id"),
            question_text=state.get("question_text"),
        )
        if question is None:
            await interaction.response.send_message(self.i18n.t(locale, "ghostface_hint.unavailable"), ephemeral=True)
            return

        user = await self.inventory_service.users_repo.get_user(interaction.user.id)
        inventory = user.get("inventory", {})
        if int(inventory.get("ghostface_hint", 0)) <= 0:
            await interaction.response.send_message(self.i18n.t(locale, "items.not_owned"), ephemeral=True)
            return

        answer = self.select_hint_answer(question, locale)
        if not answer:
            await interaction.response.send_message(self.i18n.t(locale, "ghostface_hint.unavailable"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self.inventory_service.users_repo.remove_inventory_item(interaction.user.id, "ghostface_hint", 1)
        except ValueError:
            await interaction.followup.send(self.i18n.t(locale, "items.not_owned"), ephemeral=True)
            return

        question_id = str(question.get("id") or "")
        use_count = await self.public_events_repo.increment_ghostface_hint_use(
            interaction.guild.id,
            interaction.user.id,
            question_id,
        )
        extra_hint = self.build_hint_extra(question, locale)
        extra_reveals = 0 if extra_hint else min(max(0, use_count - 1), 2)
        embed = self.texts.ghostface_hint_embed(
            locale,
            word_count=len(answer.split()),
            initials=self.build_hint_initials(answer),
            pattern=self.build_hint_pattern_with_reveals(
                answer,
                reveal_count=extra_reveals,
                seed=f"{interaction.guild.id}:{interaction.user.id}:{question_id}",
            ),
            remaining_at=expires_at,
            extra_hint=extra_hint,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        await self.audit_service.log(
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            target_id=None,
            action="item.ghostface_hint.used",
            payload={
                "item": "ghostface_hint",
                "question_id": question_id,
                "use_count": use_count,
                "extra_reveals": extra_reveals,
            },
        )

    async def _ensure_interference_role(self, guild: discord.Guild) -> discord.Role | None:
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            return None

        role = discord.utils.get(guild.roles, name=INTERFERENCE_ROLE_NAME)
        if role is None:
            try:
                role = await guild.create_role(
                    name=INTERFERENCE_ROLE_NAME,
                    reason="Halloween Interferencia item",
                )
            except (discord.Forbidden, discord.HTTPException):
                return None
        if role >= bot_member.top_role:
            return None
        return role

    async def _resolve_interference_channels(self, guild: discord.Guild) -> list[discord.TextChannel]:
        # Interferencia e um guardrail de servidor inteiro contra abuso de GIF/link.
        # Mantemos o alvo falando normalmente, mas sem midia/reacao em todos os canais de texto.
        return [channel for channel in guild.channels if isinstance(channel, discord.TextChannel)]

    async def _is_interference_channel(self, channel: discord.abc.GuildChannel) -> bool:
        if isinstance(channel, discord.TextChannel):
            return True
        if isinstance(channel, discord.Thread):
            return isinstance(channel.parent, discord.TextChannel)
        return False

    async def _ensure_interference_overwrites(self, *, guild: discord.Guild, role: discord.Role) -> bool:
        channels = await self._resolve_interference_channels(guild)
        if not channels:
            return False

        for channel in channels:
            overwrite = channel.overwrites_for(role)
            overwrite.attach_files = False
            overwrite.embed_links = False
            overwrite.add_reactions = False
            overwrite.use_external_emojis = False
            overwrite.use_external_stickers = False
            try:
                await channel.set_permissions(role, overwrite=overwrite, reason="Halloween Interferencia item")
            except (discord.Forbidden, discord.HTTPException):
                return False
        return True

    async def _remove_interference_role_if_unused(
        self,
        *,
        guild: discord.Guild,
        member: discord.Member,
        skip_effect_id: int | None = None,
    ) -> None:
        active_effects = await self.effects_repo.list_active_effects(effect_type="interferencia", guild_id=guild.id)
        for effect in active_effects:
            if skip_effect_id is not None and int(effect.get("id") or 0) == skip_effect_id:
                continue
            if int(effect.get("target_id") or 0) == member.id:
                return

        role = discord.utils.get(guild.roles, name=INTERFERENCE_ROLE_NAME)
        bot_member = guild.me
        if role is None or bot_member is None or not bot_member.guild_permissions.manage_roles or role >= bot_member.top_role:
            return
        if role in member.roles:
            try:
                await member.remove_roles(role, reason="Halloween Interferencia expired")
            except (discord.Forbidden, discord.HTTPException):
                return

    async def _restore_interference(self, guild: discord.Guild, effect: dict) -> None:
        member = guild.get_member(int(effect.get("target_id") or 0))
        if member is None:
            try:
                member = await guild.fetch_member(int(effect.get("target_id") or 0))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None
        if member is not None:
            await self._remove_interference_role_if_unused(
                guild=guild,
                member=member,
                skip_effect_id=int(effect["id"]),
            )
        await self.effects_repo.deactivate_effect(int(effect["id"]))

    async def reconcile_interferencia_roles(self) -> None:
        active_effects = await self.effects_repo.list_active_effects(effect_type="interferencia")
        active_targets_by_guild: dict[int, set[int]] = {}
        for effect in active_effects:
            guild_id = int(effect.get("guild_id") or 0)
            target_id = int(effect.get("target_id") or 0)
            if guild_id <= 0 or target_id <= 0:
                continue
            active_targets_by_guild.setdefault(guild_id, set()).add(target_id)

        for guild in self.bot.guilds:
            role = discord.utils.get(guild.roles, name=INTERFERENCE_ROLE_NAME)
            if role is None:
                continue
            active_targets = active_targets_by_guild.get(guild.id, set())
            for member in tuple(role.members):
                if member.id in active_targets:
                    continue
                await self._remove_interference_role_if_unused(guild=guild, member=member)

    def build_switch_nickname(self, template: str, *, actor_name: str, target_name: str) -> str:
        rendered = template.replace("{actor_name}", actor_name).replace("{target_name}", target_name)
        collapsed = " ".join(rendered.split())
        if not collapsed:
            return ""
        return collapsed[:32].strip()

    def switch_nickname_templates(self) -> list[str]:
        raw_templates = self.effects_config.get("switch_nickname_pool") or []
        return [str(entry).strip() for entry in raw_templates if str(entry).strip()]

    async def apply_switch_nickname(
        self,
        *,
        guild: discord.Guild,
        member: discord.Member,
        effect: dict,
        now: datetime,
    ) -> bool:
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_nicknames:
            return False
        if member.top_role >= bot_member.top_role:
            return False

        payload = dict(effect.get("payload") or {})
        actor = guild.get_member(int(effect.get("actor_id") or 0))
        actor_name = actor.display_name if actor else "Desconhecido"
        target_name = member.display_name
        candidates = [
            self.build_switch_nickname(template, actor_name=actor_name, target_name=target_name)
            for template in self.switch_nickname_templates()
        ]
        candidates = [candidate for candidate in candidates if candidate]
        if not candidates:
            return False

        current_nick = member.nick or ""
        different_candidates = [candidate for candidate in candidates if candidate != current_nick]
        nickname = random.choice(different_candidates or candidates)

        try:
            await member.edit(nick=nickname, reason=f"Switch Name effect {effect['id']}")
        except (discord.Forbidden, discord.HTTPException):
            return False

        payload["last_applied_at"] = now.isoformat()
        payload["last_nickname"] = nickname
        await self.effects_repo.update_effect_payload(int(effect["id"]), payload)
        return True

    async def apply_board_mute_curse(
        self,
        *,
        guild: discord.Guild,
        target: discord.Member,
        duration_seconds: int,
    ) -> bool:
        bot_member = guild.me
        delete_messages = self.admin_mute_deletes_messages(target)
        if target.guild_permissions.administrator:
            if not delete_messages:
                return False
            if bot_member is None or not bot_member.guild_permissions.manage_messages:
                return False
        else:
            if bot_member is None or not bot_member.guild_permissions.moderate_members:
                return False
            if target.top_role >= bot_member.top_role:
                return False

        starts_at = datetime.now(timezone.utc)
        expires_at = starts_at + timedelta(seconds=duration_seconds)
        if not delete_messages:
            try:
                await target.timeout(expires_at, reason=f"Board curse mute ({target.id})")
            except (discord.Forbidden, discord.HTTPException):
                return False

        await self.effects_repo.create_effect(
            effect_type="mute",
            guild_id=guild.id,
            actor_id=target.id,
            target_id=target.id,
            starts_at=starts_at,
            expires_at=expires_at,
            payload={
                "duration_seconds": duration_seconds,
                "source": "board_curse",
                "mode": "delete_messages" if delete_messages else "discord_timeout",
            },
        )
        await self.audit_service.log(
            guild_id=guild.id,
            actor_id=target.id,
            target_id=target.id,
            action="board.curse.mute",
            payload={"duration_seconds": duration_seconds, "delete_messages": delete_messages},
        )
        return True

    async def apply_board_switch_curse(
        self,
        *,
        guild: discord.Guild,
        target: discord.Member,
        duration_seconds: int,
    ) -> bool:
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_nicknames:
            return False
        if target.top_role >= bot_member.top_role:
            return False
        if not self.switch_nickname_templates():
            return False

        active_effects = await self.effects_repo.list_active_effects(effect_type="switch", guild_id=guild.id)
        for effect in active_effects:
            if int(effect.get("target_id") or 0) == target.id:
                await self.restore_switch_nickname(guild, effect)

        starts_at = datetime.now(timezone.utc)
        expires_at = starts_at + timedelta(seconds=duration_seconds)
        effect_id = await self.effects_repo.create_effect(
            effect_type="switch",
            guild_id=guild.id,
            actor_id=target.id,
            target_id=target.id,
            starts_at=starts_at,
            expires_at=expires_at,
            payload={
                "last_applied_at": None,
                "original_nick": target.nick if target.nick is not None else "",
                "source": "board_curse",
            },
        )
        effect = {
            "id": effect_id,
            "actor_id": target.id,
            "target_id": target.id,
            "payload": {
                "last_applied_at": None,
                "original_nick": target.nick if target.nick is not None else "",
            },
        }
        applied = await self.apply_switch_nickname(guild=guild, member=target, effect=effect, now=starts_at)
        if not applied:
            await self.effects_repo.deactivate_effect(effect_id)
            return False

        await self.audit_service.log(
            guild_id=guild.id,
            actor_id=target.id,
            target_id=target.id,
            action="board.curse.switch",
            payload={"duration_seconds": duration_seconds, "effect_id": effect_id},
        )
        return True

    async def apply_board_interference_curse(
        self,
        *,
        guild: discord.Guild,
        target: discord.Member,
        duration_seconds: int,
    ) -> bool:
        bot_member = guild.me
        if (
            bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or not bot_member.guild_permissions.manage_channels
        ):
            return False

        active_effects = await self.effects_repo.list_active_effects(effect_type="interferencia", guild_id=guild.id)
        for effect in active_effects:
            if int(effect.get("target_id") or 0) == target.id:
                await self._restore_interference(guild, effect)

        role = await self._ensure_interference_role(guild)
        if role is None:
            return False
        overwrites_ready = await self._ensure_interference_overwrites(guild=guild, role=role)
        if not overwrites_ready:
            return False

        starts_at = datetime.now(timezone.utc)
        expires_at = starts_at + timedelta(seconds=duration_seconds)
        try:
            await target.add_roles(role, reason=f"Board curse interference ({target.id})")
        except (discord.Forbidden, discord.HTTPException):
            return False

        try:
            effect_id = await self.effects_repo.create_effect(
                effect_type="interferencia",
                guild_id=guild.id,
                actor_id=target.id,
                target_id=target.id,
                starts_at=starts_at,
                expires_at=expires_at,
                payload={"duration_seconds": duration_seconds, "role_id": role.id, "source": "board_curse"},
            )
        except Exception:
            try:
                await target.remove_roles(role, reason="Board curse interference rollback")
            except (discord.Forbidden, discord.HTTPException):
                pass
            return False

        await self.audit_service.log(
            guild_id=guild.id,
            actor_id=target.id,
            target_id=target.id,
            action="board.curse.interference",
            payload={"duration_seconds": duration_seconds, "effect_id": effect_id},
        )
        return True

    async def restore_switch_nickname(self, guild: discord.Guild, effect: dict) -> None:
        member = guild.get_member(int(effect.get("target_id") or 0))
        if member is None:
            try:
                member = await guild.fetch_member(int(effect.get("target_id") or 0))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None
        if member is None:
            await self.effects_repo.deactivate_effect(int(effect["id"]))
            return

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_nicknames:
            return
        if member.top_role >= bot_member.top_role:
            return

        payload = dict(effect.get("payload") or {})
        original_nick = payload.get("original_nick")
        if original_nick == "":
            original_nick = None

        try:
            await member.edit(nick=original_nick, reason=f"Switch Name effect expired {effect['id']}")
        except (discord.Forbidden, discord.HTTPException):
            return

        await self.effects_repo.deactivate_effect(int(effect["id"]))

    async def handle_mute(
        self,
        interaction: discord.Interaction,
        alvo: discord.Member | None,
        locale: str,
    ) -> None:
        if alvo is None or interaction.guild is None:
            await interaction.response.send_message(self.i18n.t(locale, "items.needs_target"), ephemeral=True)
            return

        reflected = alvo.bot
        effective_target = interaction.user if reflected else alvo
        if not isinstance(effective_target, discord.Member):
            await interaction.response.send_message(self.i18n.t(locale, "target.guild_only"), ephemeral=True)
            return

        bot_member = interaction.guild.me
        delete_messages = self.admin_mute_deletes_messages(effective_target)
        if effective_target.guild_permissions.administrator:
            if not delete_messages:
                await interaction.response.send_message(self.i18n.t(locale, "mute.admin_disabled"), ephemeral=True)
                return
            if bot_member is None or not bot_member.guild_permissions.manage_messages:
                await interaction.response.send_message(
                    self.i18n.t(locale, "mute.manage_messages_permission_missing"),
                    ephemeral=True,
                )
                return
        else:
            if bot_member is None or not bot_member.guild_permissions.moderate_members:
                await interaction.response.send_message(self.i18n.t(locale, "mute.permission_missing"), ephemeral=True)
                return
            if effective_target.top_role >= bot_member.top_role:
                await interaction.response.send_message(self.i18n.t(locale, "target.hierarchy"), ephemeral=True)
                return

        await interaction.response.defer(ephemeral=reflected)
        user = await self.inventory_service.users_repo.get_user(interaction.user.id)
        inventory = user.get("inventory", {})
        if int(inventory.get("mute", 0)) <= 0:
            embed = self.texts.mute_no_item_embed(locale, interaction.user.display_name, alvo.display_name)
            await interaction.followup.send(embed=embed, ephemeral=reflected)
            await self.audit_service.log(
                guild_id=interaction.guild.id,
                actor_id=interaction.user.id,
                target_id=alvo.id,
                action="item.mute.failed.no_item",
                payload={"item": "mute", "reflected": reflected},
            )
            return

        try:
            await self.inventory_service.users_repo.remove_inventory_item(interaction.user.id, "mute", 1)
        except ValueError:
            embed = self.texts.mute_no_item_embed(locale, interaction.user.display_name, alvo.display_name)
            await interaction.followup.send(embed=embed, ephemeral=reflected)
            return

        duration_seconds = int(self.effects_config.get("mute_duration_seconds", 60))
        starts_at = datetime.now(timezone.utc)
        expires_at = starts_at + timedelta(seconds=duration_seconds)
        if not delete_messages:
            try:
                await effective_target.timeout(
                    expires_at,
                    reason=f"Halloween mute used by {interaction.user} ({interaction.user.id})",
                )
            except (discord.Forbidden, discord.HTTPException):
                await self.inventory_service.users_repo.add_inventory_item(interaction.user.id, "mute", 1)
                await interaction.followup.send(self.i18n.t(locale, "mute.discord_failed"), ephemeral=reflected)
                await self.audit_service.log(
                    guild_id=interaction.guild.id,
                    actor_id=interaction.user.id,
                    target_id=effective_target.id,
                    action="item.mute.failed.discord",
                    payload={"item": "mute", "requested_target_id": alvo.id, "reflected": reflected, "duration_seconds": duration_seconds},
                )
                return

        effect_id = await self.effects_repo.create_effect(
            effect_type="mute",
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            target_id=effective_target.id,
            starts_at=starts_at,
            expires_at=expires_at,
            payload={
                "duration_seconds": duration_seconds,
                "requested_target_id": alvo.id,
                "reflected": reflected,
                "mode": "delete_messages" if delete_messages else "discord_timeout",
            },
        )
        await self.inventory_service.users_repo.increment_event_counter(interaction.user.id, "mute_used_total", 1)
        await self.inventory_service.users_repo.increment_tricks_applied_total(interaction.user.id, 1)
        await self.mission_service.increment_guild_metric(interaction.guild.id, "tricks_applied_total", 1)
        await self.mission_service.increment_user_guild_metric(interaction.guild.id, interaction.user.id, "mute_used_total", 1)
        await self.mission_service.increment_user_guild_metric(interaction.guild.id, interaction.user.id, "tricks_applied_total", 1)
        mission_notices = await self.mission_service.evaluate_progress(
            interaction.guild.id if interaction.guild else None,
            interaction.user.id,
        )
        embed = self.texts.mute_applied_embed(
            locale,
            actor_name=interaction.user.display_name,
            target_name=effective_target.display_name,
            reflected=reflected,
            delete_messages=delete_messages,
            duration_seconds=duration_seconds,
            expires_at=expires_at,
            mission_notices=mission_notices,
        )
        await interaction.followup.send(embed=embed, ephemeral=reflected)
        await self.audit_service.log(
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            target_id=effective_target.id,
            action="item.mute.applied",
            payload={
                "item": "mute",
                "duration_seconds": duration_seconds,
                "effect_id": effect_id,
                "requested_target_id": alvo.id,
                "reflected": reflected,
                "delete_messages": delete_messages,
            },
        )

    async def handle_interference(
        self,
        interaction: discord.Interaction,
        alvo: discord.Member,
        locale: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(self.i18n.t(locale, "target.guild_only"), ephemeral=True)
            return

        bot_member = interaction.guild.me
        if (
            bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or not bot_member.guild_permissions.manage_channels
        ):
            await interaction.response.send_message(self.i18n.t(locale, "interference.permission_missing"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        user = await self.inventory_service.users_repo.get_user(interaction.user.id)
        inventory = user.get("inventory", {})
        if int(inventory.get("interferencia", 0)) <= 0:
            await interaction.followup.send(self.i18n.t(locale, "items.not_owned"), ephemeral=True)
            return

        active_effects = await self.effects_repo.list_active_effects(effect_type="interferencia", guild_id=interaction.guild.id)
        if any(int(effect.get("target_id") or 0) == alvo.id for effect in active_effects):
            await interaction.followup.send(self.i18n.t(locale, "interference.already_active"), ephemeral=True)
            return

        role = await self._ensure_interference_role(interaction.guild)
        if role is None:
            await interaction.followup.send(self.i18n.t(locale, "interference.discord_failed"), ephemeral=True)
            return

        overwrites_ready = await self._ensure_interference_overwrites(guild=interaction.guild, role=role)
        if not overwrites_ready:
            await interaction.followup.send(self.i18n.t(locale, "interference.no_channels"), ephemeral=True)
            return

        try:
            await self.inventory_service.users_repo.remove_inventory_item(interaction.user.id, "interferencia", 1)
        except ValueError:
            await interaction.followup.send(self.i18n.t(locale, "items.not_owned"), ephemeral=True)
            return

        duration_seconds = int(self.effects_config.get("interference_duration_seconds", 600))
        starts_at = datetime.now(timezone.utc)
        expires_at = starts_at + timedelta(seconds=duration_seconds)

        try:
            await alvo.add_roles(role, reason=f"Interferencia used by {interaction.user} ({interaction.user.id})")
        except (discord.Forbidden, discord.HTTPException):
            await self.inventory_service.users_repo.add_inventory_item(interaction.user.id, "interferencia", 1)
            await interaction.followup.send(self.i18n.t(locale, "interference.discord_failed"), ephemeral=True)
            return

        try:
            effect_id = await self.effects_repo.create_effect(
                effect_type="interferencia",
                guild_id=interaction.guild.id,
                actor_id=interaction.user.id,
                target_id=alvo.id,
                starts_at=starts_at,
                expires_at=expires_at,
                payload={"duration_seconds": duration_seconds, "role_id": role.id},
            )
        except Exception:
            try:
                await alvo.remove_roles(role, reason="Interferencia rollback after persistence failure")
            except (discord.Forbidden, discord.HTTPException):
                pass
            await self.inventory_service.users_repo.add_inventory_item(interaction.user.id, "interferencia", 1)
            await interaction.followup.send(self.i18n.t(locale, "interference.discord_failed"), ephemeral=True)
            return
        await self.inventory_service.users_repo.increment_tricks_applied_total(interaction.user.id, 1)
        await self.mission_service.increment_guild_metric(interaction.guild.id, "tricks_applied_total", 1)
        await self.mission_service.increment_user_guild_metric(interaction.guild.id, interaction.user.id, "tricks_applied_total", 1)
        embed = self.texts.interference_applied_embed(
            locale,
            actor_name=interaction.user.display_name,
            target_name=alvo.display_name,
            duration_seconds=duration_seconds,
            expires_at=expires_at,
        )
        await interaction.followup.send(embed=embed, ephemeral=False)
        await self.audit_service.log(
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            target_id=alvo.id,
            action="item.interference.applied",
            payload={"item": "interferencia", "duration_seconds": duration_seconds, "effect_id": effect_id},
        )

    async def handle_switch(
        self,
        interaction: discord.Interaction,
        alvo: discord.Member,
        locale: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(self.i18n.t(locale, "target.guild_only"), ephemeral=True)
            return

        bot_member = interaction.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_nicknames:
            await interaction.response.send_message(self.i18n.t(locale, "switch.permission_missing"), ephemeral=True)
            return
        if alvo.top_role >= bot_member.top_role:
            await interaction.response.send_message(self.i18n.t(locale, "switch.hierarchy"), ephemeral=True)
            return
        if not self.switch_nickname_templates():
            await interaction.response.send_message(self.i18n.t(locale, "switch.no_valid_name"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        user = await self.inventory_service.users_repo.get_user(interaction.user.id)
        inventory = user.get("inventory", {})
        if int(inventory.get("switch", 0)) <= 0:
            await interaction.followup.send(self.i18n.t(locale, "items.not_owned"), ephemeral=True)
            return

        try:
            await self.inventory_service.users_repo.remove_inventory_item(interaction.user.id, "switch", 1)
        except ValueError:
            await interaction.followup.send(self.i18n.t(locale, "items.not_owned"), ephemeral=True)
            return

        duration_seconds = int(self.effects_config.get("switch_duration_seconds", 900))
        starts_at = datetime.now(timezone.utc)
        expires_at = starts_at + timedelta(seconds=duration_seconds)

        active_effects = await self.effects_repo.list_active_effects(effect_type="switch", guild_id=interaction.guild.id)
        for effect in active_effects:
            if int(effect.get("target_id") or 0) == alvo.id:
                await self.restore_switch_nickname(interaction.guild, effect)

        try:
            effect_id = await self.effects_repo.create_effect(
                effect_type="switch",
                guild_id=interaction.guild.id,
                actor_id=interaction.user.id,
                target_id=alvo.id,
                starts_at=starts_at,
                expires_at=expires_at,
                payload={
                    "last_applied_at": None,
                    "original_nick": alvo.nick if alvo.nick is not None else "",
                },
            )
        except Exception:
            await self.inventory_service.users_repo.add_inventory_item(interaction.user.id, "switch", 1)
            await interaction.followup.send(self.i18n.t(locale, "switch.discord_failed"), ephemeral=True)
            return

        effect = {
            "id": effect_id,
            "actor_id": interaction.user.id,
            "target_id": alvo.id,
            "payload": {
                "last_applied_at": None,
                "original_nick": alvo.nick if alvo.nick is not None else "",
            },
        }
        applied = await self.apply_switch_nickname(
            guild=interaction.guild,
            member=alvo,
            effect=effect,
            now=starts_at,
        )
        if not applied:
            await self.effects_repo.deactivate_effect(effect_id)
            await self.inventory_service.users_repo.add_inventory_item(interaction.user.id, "switch", 1)
            await interaction.followup.send(self.i18n.t(locale, "switch.discord_failed"), ephemeral=True)
            return

        embed = self.texts.switch_applied_embed(
            locale,
            actor_name=interaction.user.display_name,
            target_name=alvo.display_name,
            duration_seconds=duration_seconds,
            expires_at=expires_at,
        )
        await self.inventory_service.users_repo.increment_event_counter(interaction.user.id, "switch_used_total", 1)
        await self.inventory_service.users_repo.increment_tricks_applied_total(interaction.user.id, 1)
        await self.mission_service.increment_guild_metric(interaction.guild.id, "tricks_applied_total", 1)
        await self.mission_service.increment_user_guild_metric(interaction.guild.id, interaction.user.id, "switch_used_total", 1)
        await self.mission_service.increment_user_guild_metric(interaction.guild.id, interaction.user.id, "tricks_applied_total", 1)
        await interaction.followup.send(embed=embed, ephemeral=False)
        await self.audit_service.log(
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            target_id=alvo.id,
            action="item.switch.applied",
            payload={"item": "switch", "duration_seconds": duration_seconds, "effect_id": effect_id},
        )

    async def handle_switch_message(self, message: discord.Message) -> None:
        active_effects = await self.effects_repo.list_active_effects(effect_type="switch", guild_id=message.guild.id)
        if not active_effects:
            return

        target_effect = next((effect for effect in active_effects if int(effect.get("target_id") or 0) == message.author.id), None)
        if target_effect is None:
            return

        now = datetime.now(timezone.utc)
        last_applied_at = self.parse_dt((target_effect.get("payload") or {}).get("last_applied_at"))
        if last_applied_at is not None and now < last_applied_at + timedelta(seconds=30):
            return

        member = message.author if isinstance(message.author, discord.Member) else message.guild.get_member(message.author.id)
        if member is None:
            return
        await self.apply_switch_nickname(
            guild=message.guild,
            member=member,
            effect=target_effect,
            now=now,
        )

    async def handle_interference_message(self, message: discord.Message) -> None:
        if not URL_PATTERN.search(message.content or ""):
            return
        if not await self._is_interference_channel(message.channel):
            return

        active_effects = await self.effects_repo.list_active_effects(effect_type="interferencia", guild_id=message.guild.id)
        target_effect = next((effect for effect in active_effects if int(effect.get("target_id") or 0) == message.author.id), None)
        if target_effect is None:
            return

        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return

    async def handle_admin_mute_message(self, message: discord.Message) -> bool:
        member = message.author
        if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
            return False

        active_effects = await self.effects_repo.list_active_effects(
            effect_type="mute",
            guild_id=message.guild.id,
        )
        now = datetime.now(timezone.utc)
        should_delete = False
        for effect in active_effects:
            if int(effect.get("target_id") or 0) != member.id:
                continue
            if (effect.get("payload") or {}).get("mode") != "delete_messages":
                continue
            expires_at = self.parse_dt(effect.get("expires_at"))
            if expires_at is None or expires_at <= now:
                await self.effects_repo.deactivate_effect(int(effect["id"]))
                continue
            should_delete = True

        if not should_delete:
            return False
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return False
        return True

    async def cleanup_expired_switches(self) -> None:
        now = datetime.now(timezone.utc)
        active_effects = await self.effects_repo.list_active_effects(effect_type="switch")
        for effect in active_effects:
            expires_at = self.parse_dt(effect.get("expires_at"))
            if expires_at is None or expires_at > now:
                continue
            guild_id = int(effect.get("guild_id") or 0)
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                try:
                    guild = await self.bot.fetch_guild(guild_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    guild = None
            if guild is None:
                await self.effects_repo.deactivate_effect(int(effect["id"]))
                continue
            await self.restore_switch_nickname(guild, effect)

    async def cleanup_expired_interferencias(self) -> None:
        now = datetime.now(timezone.utc)
        active_effects = await self.effects_repo.list_active_effects(effect_type="interferencia")
        for effect in active_effects:
            expires_at = self.parse_dt(effect.get("expires_at"))
            if expires_at is None or expires_at > now:
                continue
            guild_id = int(effect.get("guild_id") or 0)
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                try:
                    guild = await self.bot.fetch_guild(guild_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    guild = None
            if guild is None:
                await self.effects_repo.deactivate_effect(int(effect["id"]))
                continue
            await self._restore_interference(guild, effect)

    async def cleanup_expired_admin_mutes(self) -> None:
        now = datetime.now(timezone.utc)
        active_effects = await self.effects_repo.list_active_effects(effect_type="mute")
        for effect in active_effects:
            if (effect.get("payload") or {}).get("mode") != "delete_messages":
                continue
            expires_at = self.parse_dt(effect.get("expires_at"))
            if expires_at is None or expires_at <= now:
                await self.effects_repo.deactivate_effect(int(effect["id"]))
