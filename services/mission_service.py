from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from repositories.admin_repo import AdminRepo
from repositories.board_read_repo import BoardReadRepo
from repositories.db import DB_PATH
from repositories.guild_metrics_repo import DEFAULT_METRICS, GuildMetricsRepo
from repositories.missions_repo import MissionsRepo
from repositories.user_guild_metrics_repo import UserGuildMetricsRepo
from repositories.users_repo import UsersRepo
from services.i18n_service import I18nService


ROOT_DIR = Path(__file__).resolve().parent.parent
MISSIONS_PATH = ROOT_DIR / "content" / "halloween" / "missions.json"
MISSION_COLOR = discord.Color.from_rgb(245, 122, 39)


def _resolve_mission_timezone() -> timezone | ZoneInfo:
    try:
        return ZoneInfo("America/Sao_Paulo")
    except ZoneInfoNotFoundError:
        # Fallback para ambientes Windows sem pacote tzdata instalado.
        return timezone(timedelta(hours=-3))


MISSION_TIMEZONE = _resolve_mission_timezone()


@dataclass(frozen=True)
class MissionDefinition:
    id: str
    metric: str
    target: int
    rewards: dict[str, int]
    title_pt: str
    title_en: str
    description_pt: str
    description_en: str

    def title(self, locale: str) -> str:
        return self.title_pt if locale == "pt-BR" else self.title_en

    def description(self, locale: str) -> str:
        return self.description_pt if locale == "pt-BR" else self.description_en


@dataclass(frozen=True)
class MissionProgress:
    mission_type: str
    period_key: str
    mission: MissionDefinition
    progress: int
    target: int
    completed: bool
    reward_claimed: bool
    reward_just_granted: bool
    reward_text: str


class MissionService:
    def __init__(
        self,
        users_repo: UsersRepo | None = None,
        board_repo: BoardReadRepo | None = None,
        missions_repo: MissionsRepo | None = None,
        guild_metrics_repo: GuildMetricsRepo | None = None,
        user_guild_metrics_repo: UserGuildMetricsRepo | None = None,
        admin_repo: AdminRepo | None = None,
        i18n_service: I18nService | None = None,
        path: Path = DB_PATH,
        missions_path: Path = MISSIONS_PATH,
    ) -> None:
        self.path = path
        self.users_repo = users_repo or UsersRepo(path)
        self.board_repo = board_repo or BoardReadRepo(path)
        self.missions_repo = missions_repo or MissionsRepo(path)
        self.guild_metrics_repo = guild_metrics_repo or GuildMetricsRepo(path)
        self.user_guild_metrics_repo = user_guild_metrics_repo or UserGuildMetricsRepo(path)
        self.admin_repo = admin_repo or AdminRepo(path)
        self.i18n = i18n_service or I18nService(self.users_repo)
        self.missions_path = missions_path
        self._catalog: dict[str, list[MissionDefinition]] | None = None

    async def build_embed(self, guild_id: int | None, user_id: int) -> discord.Embed:
        await self.missions_repo.init_db()
        locale = await self.i18n.get_user_locale(user_id)

        personal = await self.get_personal_daily(user_id, locale, guild_id)
        global_daily = await self.get_global_daily(guild_id, user_id, locale)
        event_mission = await self.get_event_mission(guild_id, user_id, locale)

        embed = discord.Embed(
            title=self.i18n.t(locale, "missions.title"),
            description=self.i18n.t(locale, "missions.description"),
            color=MISSION_COLOR,
        )
        embed.add_field(
            name=f"🕯️ {self.i18n.t(locale, 'missions.section.personal')}",
            value=self._format_mission_block(locale, personal),
            inline=False,
        )
        embed.add_field(
            name=f"🎃 {self.i18n.t(locale, 'missions.section.global')}",
            value=self._format_mission_block(locale, global_daily),
            inline=False,
        )
        embed.add_field(
            name=f"👻 {self.i18n.t(locale, 'missions.section.event')}",
            value=self._format_mission_block(locale, event_mission),
            inline=False,
        )
        embed.set_footer(text=self.i18n.t(locale, "missions.reset_footer", date=personal.period_key))
        return embed

    async def evaluate_progress(self, guild_id: int | None, user_id: int) -> list[str]:
        locale = await self.i18n.get_user_locale(user_id)
        notices: list[str] = []

        personal = await self.get_personal_daily(user_id, locale, guild_id)
        if personal.reward_just_granted:
            notices.append(self._build_reward_notice(locale, "missions.section.personal", personal))

        global_daily = await self.get_global_daily(guild_id, user_id, locale)
        if global_daily.reward_just_granted:
            notices.append(self._build_reward_notice(locale, "missions.section.global", global_daily))

        event_mission = await self.get_event_mission(guild_id, user_id, locale)
        if event_mission and event_mission.reward_just_granted:
            notices.append(self._build_reward_notice(locale, "missions.section.event", event_mission))

        return notices

    async def get_personal_daily(
        self,
        user_id: int,
        locale: str | None = None,
        guild_id: int | None = None,
    ) -> MissionProgress:
        await self.missions_repo.init_db()
        if locale is None:
            locale = await self.i18n.get_user_locale(user_id)

        mission_type = "daily_personal"
        period_key = self._today_key()
        catalog = self._load_catalog()[mission_type]
        mission = catalog[(self._day_seed() + user_id) % len(catalog)]

        current = await self._build_user_snapshot(user_id)
        state = await self.missions_repo.get_user_state(user_id, mission_type, period_key)
        if state is None or state["mission_id"] != mission.id:
            await self.missions_repo.upsert_user_state(user_id, mission_type, period_key, mission.id, current)
            state = {
                "mission_id": mission.id,
                "baseline": current,
                "completed_at": None,
            }

        progress = max(0, current.get(mission.metric, 0) - state["baseline"].get(mission.metric, 0))
        completed = progress >= mission.target
        reward_claimed = bool(state.get("reward_granted_at"))
        reward_just_granted = False

        if completed and not state.get("completed_at"):
            await self.missions_repo.mark_user_completed(
                user_id,
                mission_type,
                period_key,
                datetime.now(timezone.utc).isoformat(),
            )

        if completed and not reward_claimed:
            await self._prepare_reward_tables(guild_id)
            reward_just_granted = await self.missions_repo.grant_user_reward_once(
                user_id,
                mission_type,
                period_key,
                mission.rewards,
                guild_id,
                datetime.now(timezone.utc).isoformat(),
            )
            fresh_state = await self.missions_repo.get_user_state(user_id, mission_type, period_key)
            reward_claimed = reward_just_granted or bool(fresh_state and fresh_state.get("reward_granted_at"))

        return MissionProgress(
            mission_type=mission_type,
            period_key=period_key,
            mission=mission,
            progress=min(progress, mission.target),
            target=mission.target,
            completed=completed,
            reward_claimed=reward_claimed,
            reward_just_granted=reward_just_granted,
            reward_text=self._format_rewards(locale, mission.rewards),
        )

    async def get_global_daily(self, guild_id: int | None, user_id: int, locale: str | None = None) -> MissionProgress:
        await self.missions_repo.init_db()
        if locale is None:
            locale = await self.i18n.get_user_locale(user_id)

        mission_type = "daily_global"
        period_key = self._today_key()
        catalog = self._load_catalog()[mission_type]
        guild_seed = guild_id or 0
        mission = catalog[(self._day_seed() + guild_seed) % len(catalog)]

        current = await self._build_global_snapshot(guild_id)
        state = await self.missions_repo.get_guild_state(guild_seed, mission_type, period_key)
        if state is None or state["mission_id"] != mission.id:
            await self.missions_repo.upsert_guild_state(guild_seed, mission_type, period_key, mission.id, current)
            state = {
                "mission_id": mission.id,
                "baseline": current,
                "completed_at": None,
            }

        progress = max(0, current.get(mission.metric, 0) - state["baseline"].get(mission.metric, 0))
        completed = progress >= mission.target
        reward_claimed = False
        reward_just_granted = False

        if completed and not state.get("completed_at"):
            await self.missions_repo.mark_guild_completed(
                guild_seed,
                mission_type,
                period_key,
                datetime.now(timezone.utc).isoformat(),
            )

        if guild_id is not None:
            reward_claimed = await self.missions_repo.has_guild_reward_granted(guild_id, user_id, mission_type, period_key)

        if completed and guild_id is not None and not reward_claimed:
            await self._prepare_reward_tables(guild_id)
            reward_just_granted = await self.missions_repo.grant_guild_reward_once(
                guild_id,
                user_id,
                mission_type,
                period_key,
                mission.rewards,
                datetime.now(timezone.utc).isoformat(),
            )
            reward_claimed = reward_just_granted or await self.missions_repo.has_guild_reward_granted(
                guild_id,
                user_id,
                mission_type,
                period_key,
            )

        return MissionProgress(
            mission_type=mission_type,
            period_key=period_key,
            mission=mission,
            progress=min(progress, mission.target),
            target=mission.target,
            completed=completed,
            reward_claimed=reward_claimed,
            reward_just_granted=reward_just_granted,
            reward_text=self._format_rewards(locale, mission.rewards),
        )

    async def get_event_mission(self, guild_id: int | None, user_id: int, locale: str | None = None) -> MissionProgress | None:
        if guild_id is None:
            return None

        await self.missions_repo.init_db()
        if locale is None:
            locale = await self.i18n.get_user_locale(user_id)

        guild_seed = guild_id or 0
        runtime = await self.admin_repo.get_runtime(guild_id)
        if not runtime.get("event_active"):
            return None

        mission_type = "event_global"
        event_anchor = str(runtime.get("event_started_at") or "event_active")
        catalog = self._load_catalog()[mission_type]
        mission_index = sum(ord(char) for char in event_anchor) % len(catalog)
        mission = catalog[mission_index]

        current = await self._build_global_snapshot(guild_id)
        state = await self.missions_repo.get_guild_state(guild_seed, mission_type, event_anchor)
        if state is None or state["mission_id"] != mission.id:
            await self.missions_repo.upsert_guild_state(guild_seed, mission_type, event_anchor, mission.id, current)
            state = {
                "mission_id": mission.id,
                "baseline": current,
                "completed_at": None,
            }

        progress = max(0, current.get(mission.metric, 0) - state["baseline"].get(mission.metric, 0))
        completed = progress >= mission.target
        reward_claimed = False
        reward_just_granted = False

        if completed and not state.get("completed_at"):
            await self.missions_repo.mark_guild_completed(
                guild_seed,
                mission_type,
                event_anchor,
                datetime.now(timezone.utc).isoformat(),
            )

        reward_claimed = await self.missions_repo.has_guild_reward_granted(guild_id, user_id, mission_type, event_anchor)

        if completed and not reward_claimed:
            await self._prepare_reward_tables(guild_id)
            reward_just_granted = await self.missions_repo.grant_guild_reward_once(
                guild_id,
                user_id,
                mission_type,
                event_anchor,
                mission.rewards,
                datetime.now(timezone.utc).isoformat(),
            )
            reward_claimed = reward_just_granted or await self.missions_repo.has_guild_reward_granted(
                guild_id,
                user_id,
                mission_type,
                event_anchor,
            )

        return MissionProgress(
            mission_type=mission_type,
            period_key=event_anchor,
            mission=mission,
            progress=min(progress, mission.target),
            target=mission.target,
            completed=completed,
            reward_claimed=reward_claimed,
            reward_just_granted=reward_just_granted,
            reward_text=self._format_rewards(locale, mission.rewards),
        )

    def _format_mission_block(self, locale: str, mission_progress: MissionProgress | None) -> str:
        if mission_progress is None:
            return self.i18n.t(locale, "missions.event_inactive")

        if mission_progress.reward_claimed:
            status_key = "missions.status.rewarded"
        else:
            status_key = "missions.status.completed" if mission_progress.completed else "missions.status.in_progress"
        status = self.i18n.t(locale, status_key)
        metric_emoji = self._metric_emoji(mission_progress.mission.metric)
        return self.i18n.t(
            locale,
            "missions.block",
            title=f"{metric_emoji} {mission_progress.mission.title(locale)}",
            description=mission_progress.mission.description(locale),
            progress=mission_progress.progress,
            target=mission_progress.target,
            rewards=mission_progress.reward_text,
            status=status,
        )

    def _format_rewards(self, locale: str, rewards: dict[str, int]) -> str:
        parts: list[str] = []
        for reward_type, amount in rewards.items():
            if reward_type == "candies":
                parts.append(self.i18n.t(locale, "missions.reward.candies", amount=amount))
            elif reward_type == "trick_coins":
                parts.append(self.i18n.t(locale, "missions.reward.trick_coins", amount=amount))
            elif reward_type == "dice_count":
                parts.append(self.i18n.t(locale, "missions.reward.dice", amount=amount))
            elif reward_type == "xp_bank":
                parts.append(self.i18n.t(locale, "missions.reward.xp", amount=amount))
        return ", ".join(parts) if parts else self.i18n.t(locale, "missions.reward.none")

    def _build_reward_notice(self, locale: str, section_key: str, mission_progress: MissionProgress) -> str:
        return self.i18n.t(
            locale,
            "missions.notice.reward_granted",
            section=self.i18n.t(locale, section_key),
            title=mission_progress.mission.title(locale),
            rewards=mission_progress.reward_text,
        )

    async def _prepare_reward_tables(self, guild_id: int | None) -> None:
        await self.users_repo.init_db()
        await self.missions_repo.init_db()
        if guild_id is not None:
            await self.guild_metrics_repo.init_db()
            await self.user_guild_metrics_repo.init_db()

    def _metric_emoji(self, metric: str) -> str:
        if metric == "tricks_applied_total":
            return "😈"
        if metric == "candies_total":
            return "🍬"
        if metric == "horror_questions_correct_total":
            return "📞"
        if metric == "board_laps":
            return "🎲"
        if metric == "xp_bank":
            return "✨"
        return "🕸️"

    async def _build_user_snapshot(self, user_id: int) -> dict[str, int]:
        user = await self.users_repo.get_user(user_id)
        _, board_laps = await self.board_repo.get_board_state(user_id)
        return {
            "candies_total": int(user.get("candies_total", 0)),
            "profile_requests_answered_total": int(user.get("profile_requests_answered_total", 0)),
            "profile_requests_sent_total": int(user.get("profile_requests_sent_total", 0)),
            "trick_coins_total": int(user.get("trick_coins_total", 0)),
            "tricks_applied_total": int(user.get("tricks_applied_total", 0)),
            "baskets_opened_total": int(user.get("baskets_opened_total", 0)),
            "mute_used_total": int(user.get("mute_used_total", 0)),
            "switch_used_total": int(user.get("switch_used_total", 0)),
            "horror_questions_correct_total": int(user.get("horror_questions_correct_total", 0)),
            "board_laps": int(board_laps),
            "xp_bank": int(user.get("xp_bank", 0)),
        }

    async def _build_global_snapshot(self, guild_id: int | None) -> dict[str, int]:
        if guild_id is None:
            return dict(DEFAULT_METRICS)
        metrics = await self.guild_metrics_repo.get_metrics(guild_id)
        return {key: int(metrics.get(key, 0)) for key in DEFAULT_METRICS}

    async def increment_guild_metric(self, guild_id: int | None, metric: str, amount: int = 1) -> int:
        if guild_id is None or amount == 0:
            return 0
        return await self.guild_metrics_repo.increment_metric(guild_id, metric, amount)

    async def increment_user_guild_metric(
        self,
        guild_id: int | None,
        user_id: int | None,
        metric: str,
        amount: int = 1,
    ) -> int:
        if guild_id is None or user_id is None or amount == 0:
            return 0
        return await self.user_guild_metrics_repo.increment_metric(guild_id, user_id, metric, amount)

    async def increment_guild_currency(self, guild_id: int | None, currency: str, amount: int) -> int:
        mapping = {
            "candies": "candies_total",
            "trick_coins": "trick_coins_total",
            "xp_bank": "xp_bank",
        }
        metric = mapping.get(currency)
        if metric is None:
            return 0
        return await self.increment_guild_metric(guild_id, metric, amount)

    def _load_catalog(self) -> dict[str, list[MissionDefinition]]:
        if self._catalog is None:
            raw = json.loads(self.missions_path.read_text(encoding="utf-8"))
            self._catalog = {
                mission_type: [MissionDefinition(**entry) for entry in entries]
                for mission_type, entries in raw.items()
            }
        return self._catalog

    def _today_key(self) -> str:
        return datetime.now(MISSION_TIMEZONE).date().isoformat()

    def _day_seed(self) -> int:
        return datetime.now(MISSION_TIMEZONE).toordinal()
