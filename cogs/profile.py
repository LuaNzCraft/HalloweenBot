from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from repositories.profile_requests_repo import (
    ProfileRequestInsufficientFunds,
    ProfileRequestNotPending,
    ProfileRequestsRepo,
)
from services.mission_service import MissionService
from services.profile_service import ProfileService
from services.i18n_service import I18nService
from services.profile_request_settlement_service import ProfileRequestSettlementService
from utils.event_config import get_achievements_config, get_profile_requests_config
from views.profile_panel import (
    ProfilePanelData,
    ProfilePanelView,
    build_public_profile_container,
)



def _resolve_profile_timezone() -> timezone | ZoneInfo:
    try:
        return ZoneInfo("America/Sao_Paulo")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=-3))


PROFILE_REQUESTS_TIMEZONE = _resolve_profile_timezone()
LOGGER = logging.getLogger(__name__)


class PublicProfileRequestButton(discord.ui.Button):
    def __init__(self, label: str, *, disabled: bool) -> None:
        super().__init__(
            label=label,
            emoji="🎃",
            style=discord.ButtonStyle.danger,
            disabled=disabled,
            custom_id="profile:request:candy",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, PublicProfileView):
            return
        await panel.submit_request(interaction, self)


class PublicProfileView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "ProfileCog",
        guild_id: int | None,
        viewer_id: int,
        target_id: int,
        locale: str,
        data: ProfilePanelData,
        *,
        can_request: bool,
    ) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.guild_id = guild_id
        self.viewer_id = viewer_id
        self.target_id = target_id
        self.locale = locale
        self.data = data
        self.message: discord.Message | None = None
        self.request_button = PublicProfileRequestButton(
            cog.i18n.t(locale, "profile.trick_or_treat"),
            disabled=not can_request,
        )
        self.add_item(build_public_profile_container(cog, locale, data))
        self.add_item(discord.ui.ActionRow(self.request_button))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.viewer_id:
            return True
        await interaction.response.send_message(
            self.cog.i18n.t(self.locale, "common.not_owner.profile"),
            ephemeral=True,
        )
        return False

    async def submit_request(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                self.cog.i18n.t(self.locale, "target.guild_only"),
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        if self.cog._is_bot_target(self.target_id):
            await self.cog.handle_bot_easter_egg(interaction, self, button)
            return
        guild_id = interaction.guild.id
        now_dt = datetime.now(PROFILE_REQUESTS_TIMEZONE)
        now_iso = now_dt.isoformat()
        cooldown_remaining = await self.cog.profile_requests_repo.get_cooldown_remaining_seconds(guild_id, interaction.user.id, now_iso)
        if cooldown_remaining > 0:
            await interaction.followup.send(
                self.cog.i18n.t(
                    self.locale,
                    "profile.trick_or_treat_on_cooldown",
                    seconds=cooldown_remaining,
                ),
                ephemeral=True,
            )
            return
        request_timeout_seconds = int(self.cog.profile_requests_config.get("request_timeout_seconds", 1800))
        created_at = now_iso
        expires_at = (now_dt + timedelta(seconds=request_timeout_seconds)).isoformat()
        active_created = await self.cog.profile_requests_repo.try_create_active_request(
            guild_id=guild_id,
            requester_id=interaction.user.id,
            target_id=self.target_id,
            created_at=created_at,
            expires_at=expires_at,
            now_iso=now_iso,
            channel_id=interaction.channel.id,
        )
        if not active_created:
            await interaction.followup.send(
                self.cog.i18n.t(self.locale, "profile.trick_or_treat_pending_exists"),
                ephemeral=True,
            )
            return
        if not await self.cog._is_recently_active_in_event(guild_id, self.target_id):
            await self.cog.profile_requests_repo.delete_active_request(guild_id, interaction.user.id)
            await interaction.followup.send(
                self.cog.i18n.t(self.locale, "profile.trick_or_treat_target_inactive"),
                ephemeral=True,
            )
            return
        target_profile = await self.cog.profile_service.get_profile(self.target_id)
        candy_amount = int(self.cog.profile_requests_config.get("candy_amount", 10))
        trick_amount = int(self.cog.profile_requests_config.get("trick_coins_amount", 5))
        can_give_candy = int(target_profile.get("candies", 0)) >= candy_amount
        can_give_trick = int(target_profile.get("trick_coins", 0)) >= trick_amount
        if not can_give_candy and not can_give_trick:
            await self.cog.profile_requests_repo.delete_active_request(guild_id, interaction.user.id)
            await interaction.followup.send(
                self.cog.i18n.t(self.locale, "profile.trick_or_treat_target_unavailable"),
                ephemeral=True,
            )
            return
        date_key = now_dt.date().isoformat()
        registered = await self.cog.profile_requests_repo.register_daily_request(
            guild_id=guild_id,
            requester_id=interaction.user.id,
            target_id=self.target_id,
            date_key=date_key,
            created_at=created_at,
            daily_limit=int(self.cog.profile_requests_config.get("daily_requests_per_pair", 1)),
        )
        if not registered:
            await self.cog.profile_requests_repo.delete_active_request(guild_id, interaction.user.id)
            await interaction.followup.send(
                self.cog.i18n.t(self.locale, "profile.trick_or_treat_already_requested_today"),
                ephemeral=True,
            )
            return
        try:
            target_user = interaction.guild.get_member(self.target_id) or await interaction.guild.fetch_member(self.target_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await self.cog.profile_requests_repo.delete_active_request(guild_id, interaction.user.id)
            await self.cog.profile_requests_repo.delete_daily_request(
                guild_id=guild_id,
                requester_id=interaction.user.id,
                target_id=self.target_id,
                date_key=date_key,
            )
            await interaction.followup.send(
                self.cog.i18n.t(self.locale, "profile.trick_or_treat_send_failed"),
                ephemeral=True,
            )
            return
        target_locale = await self.cog.i18n.get_user_locale(self.target_id)
        embed = self.cog.build_trick_or_treat_request_embed(
            requester=interaction.user,
            target=target_user,
            locale=target_locale,
        )
        view = TrickOrTreatRequestView(
            cog=self.cog,
            guild_id=guild_id,
            requester_id=interaction.user.id,
            requester_name=interaction.user.display_name,
            target_id=self.target_id,
            locale=target_locale,
            can_give_candy=can_give_candy,
            can_give_trick=can_give_trick,
            timeout_seconds=request_timeout_seconds,
            request_created_at=created_at,
        )
        try:
            sent_message = await interaction.channel.send(
                content=target_user.mention,
                embed=embed,
                view=view,
            )
        except discord.HTTPException:
            await self.cog.profile_requests_repo.delete_active_request(guild_id, interaction.user.id)
            await self.cog.profile_requests_repo.delete_daily_request(
                guild_id=guild_id,
                requester_id=interaction.user.id,
                target_id=self.target_id,
                date_key=date_key,
            )
            await interaction.followup.send(
                self.cog.i18n.t(self.locale, "profile.trick_or_treat_send_failed"),
                ephemeral=True,
            )
            return
        view.message = sent_message
        await self.cog.profile_requests_repo.mark_active_request_published(
            guild_id,
            interaction.user.id,
            created_at,
            sent_message.id,
        )
        await self.cog.profile_service.users_repo.increment_event_counter(
            interaction.user.id,
            "profile_requests_sent_total",
            1,
        )

        button.disabled = True
        button.label = self.cog.i18n.t(self.locale, "profile.trick_or_treat_sent")
        await interaction.edit_original_response(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await interaction.followup.send(
            self.cog.i18n.t(self.locale, "profile.trick_or_treat_request_sent", target=target_user.display_name),
            ephemeral=True,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
        /,
    ) -> None:
        message = self.cog.i18n.t(self.locale, "profile.request_failed")
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class TrickOrTreatRequestView(discord.ui.View):
    def __init__(
        self,
        cog: "ProfileCog",
        guild_id: int,
        requester_id: int,
        requester_name: str,
        target_id: int,
        locale: str,
        can_give_candy: bool,
        can_give_trick: bool,
        timeout_seconds: int | None,
        request_created_at: str,
    ) -> None:
        super().__init__(timeout=timeout_seconds)
        self.cog = cog
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.requester_name = requester_name
        self.target_id = target_id
        self.request_created_at = request_created_at
        self.locale = locale
        self.message: discord.Message | None = None
        self._resolving = asyncio.Lock()
        self._resolved = False
        self.give_candy.label = cog.i18n.t(locale, "profile.give_candy")
        self.give_candy.disabled = not can_give_candy
        self.give_trick.label = cog.i18n.t(locale, "profile.give_trick")
        self.give_trick.disabled = not can_give_trick
        self.ignore_request.label = cog.i18n.t(locale, "profile.ignore_request")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.target_id:
            return True
        await interaction.response.send_message(
            self.cog.i18n.t(self.locale, "common.not_owner.profile"),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Dar Doce", style=discord.ButtonStyle.success, custom_id="profile_request_give_candy")
    async def give_candy(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, "candy")

    @discord.ui.button(label="Dar Travessura", style=discord.ButtonStyle.danger, custom_id="profile_request_give_trick")
    async def give_trick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, "trick")

    @discord.ui.button(label="Ignorar", style=discord.ButtonStyle.secondary, custom_id="profile_request_ignore")
    async def ignore_request(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, "ignore")

    async def _finish(self, interaction: discord.Interaction, outcome: str) -> None:
        await interaction.response.defer()
        async with self._resolving:
            if self._resolved:
                await interaction.followup.send(
                    self.cog.i18n.t(self.locale, "profile.trick_or_treat_already_resolved"),
                    ephemeral=True,
                )
                return

            self._resolved = True
            try:
                await self._finish_locked(interaction, outcome)
            except Exception:
                self._resolved = False
                raise

    async def _finish_locked(self, interaction: discord.Interaction, outcome: str) -> None:
        amount = 0
        currency = ""
        if outcome == "candy":
            amount = int(self.cog.profile_requests_config.get("candy_amount", 10))
            currency = "candies"
        elif outcome == "trick":
            amount = int(self.cog.profile_requests_config.get("trick_coins_amount", 5))
            currency = "trick_coins"

        cooldown_until = None
        if outcome == "ignore":
            ignore_cooldown_seconds = int(self.cog.profile_requests_config.get("ignore_cooldown_seconds", 180))
            cooldown_until = (
                datetime.now(PROFILE_REQUESTS_TIMEZONE) + timedelta(seconds=ignore_cooldown_seconds)
            ).isoformat()

        milestones = self.cog.achievements_config.get("generosity_candies_milestones") or []
        if not isinstance(milestones, list):
            milestones = []
        try:
            settlement = await self.cog.profile_request_settlement_service.settle_once(
                guild_id=self.guild_id,
                requester_id=self.requester_id,
                target_id=self.target_id,
                request_created_at=self.request_created_at,
                outcome=outcome,
                amount=amount,
                generosity_milestones=milestones,
                cooldown_until=cooldown_until,
            )
        except ProfileRequestInsufficientFunds:
            self._resolved = False
            await interaction.followup.send(
                self.cog.i18n.t(
                    self.locale,
                    "profile.trick_or_treat_not_enough",
                    currency=self.cog.i18n.t(self.locale, f"currency.{currency}"),
                    amount=amount,
                ),
                ephemeral=True,
            )
            return
        except ProfileRequestNotPending:
            await interaction.followup.send(
                self.cog.i18n.t(self.locale, "profile.trick_or_treat_already_resolved"),
                ephemeral=True,
            )
            return

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        embed = self.cog.build_trick_or_treat_result_embed(
            locale=self.locale,
            target_name=interaction.user.display_name,
            outcome=settlement.outcome,
            amount=settlement.amount,
        )
        if settlement.milestone_items:
            generosity_notice = "\n".join(
                self.cog._format_generosity_reward_line(self.locale, item_id, quantity)
                for item_id, quantity in settlement.milestone_items.items()
            )
            embed.add_field(
                name=self.cog.i18n.t(self.locale, "profile.generosity_milestone_title"),
                value=self.cog.i18n.t(
                    self.locale,
                    "profile.generosity_milestone_body",
                    target=interaction.user.display_name,
                    rewards=generosity_notice,
                ),
                inline=False,
            )
        await interaction.edit_original_response(
            content=self.cog.i18n.t(
                self.locale,
                "profile.trick_or_treat_request_resolved",
                requester=self.requester_name,
            ),
            embed=embed,
            view=self,
        )
        self.stop()

    async def on_timeout(self) -> None:
        if self._resolved:
            return
        await self.cog.profile_requests_repo.delete_active_request(
            self.guild_id,
            self.requester_id,
            created_at=self.request_created_at,
        )
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(
                content=self.cog.i18n.t(self.locale, "profile.trick_or_treat_request_expired"),
                view=self,
            )
        except discord.HTTPException:
            return


class ProfileCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.i18n = I18nService()
        self.profile_service = ProfileService(i18n_service=self.i18n)
        self.mission_service = MissionService(i18n_service=self.i18n)
        self.admin_repo = AdminRepo()
        self.profile_requests_repo = ProfileRequestsRepo()
        self.profile_request_settlement_service = ProfileRequestSettlementService(
            requests_repo=self.profile_requests_repo,
            users_repo=self.profile_service.users_repo,
            guild_metrics_repo=self.mission_service.guild_metrics_repo,
        )
        self.achievements_config = get_achievements_config()
        self.profile_requests_config = get_profile_requests_config()
        self._profile_requests_restored = False
        self._restored_request_expiry_tasks: dict[str, asyncio.Task] = {}

    def cog_unload(self) -> None:
        for task in self._restored_request_expiry_tasks.values():
            task.cancel()
        self._restored_request_expiry_tasks.clear()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._profile_requests_restored:
            return
        await self._restore_active_request_views()
        self._profile_requests_restored = True

    async def _restore_active_request_views(self) -> None:
        now = datetime.now(PROFILE_REQUESTS_TIMEZONE)
        for request in await self.profile_requests_repo.list_active_requests():
            guild_id = int(request["guild_id"])
            requester_id = int(request["requester_id"])
            target_id = int(request["target_id"])
            created_at = str(request["created_at"])
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            try:
                expires_at = datetime.fromisoformat(str(request["expires_at"]))
            except ValueError:
                expires_at = now
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=PROFILE_REQUESTS_TIMEZONE)
            channel_id = int(request.get("channel_id") or 0)
            message_id = int(request.get("message_id") or 0)
            channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
            message = None
            if isinstance(channel, (discord.TextChannel, discord.Thread)) and message_id:
                try:
                    message = await channel.fetch_message(message_id)
                except discord.NotFound:
                    await self.profile_requests_repo.delete_active_request(
                        guild_id,
                        requester_id,
                        created_at=created_at,
                    )
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    message = None
            if expires_at <= now or not message_id:
                await self.profile_requests_repo.delete_active_request(
                    guild_id,
                    requester_id,
                    created_at=created_at,
                )
                if message is not None:
                    try:
                        locale = await self.i18n.get_user_locale(target_id)
                        await message.edit(
                            content=self.i18n.t(locale, "profile.trick_or_treat_request_expired"),
                            view=None,
                        )
                    except discord.HTTPException:
                        pass
                continue

            requester = guild.get_member(requester_id)
            requester_name = requester.display_name if requester is not None else f"<@{requester_id}>"
            target_profile = await self.profile_service.get_profile(target_id)
            locale = await self.i18n.get_user_locale(target_id)
            view = TrickOrTreatRequestView(
                cog=self,
                guild_id=guild_id,
                requester_id=requester_id,
                requester_name=requester_name,
                target_id=target_id,
                locale=locale,
                can_give_candy=int(target_profile.get("candies", 0)) >= int(
                    self.profile_requests_config.get("candy_amount", 10)
                ),
                can_give_trick=int(target_profile.get("trick_coins", 0)) >= int(
                    self.profile_requests_config.get("trick_coins_amount", 5)
                ),
                timeout_seconds=None,
                request_created_at=created_at,
            )
            view.message = message
            self.bot.add_view(view, message_id=message_id)
            task_key = f"{guild_id}:{requester_id}:{created_at}"
            self._restored_request_expiry_tasks[task_key] = asyncio.create_task(
                self._expire_restored_request_view(task_key, view, expires_at)
            )

    async def _expire_restored_request_view(
        self,
        task_key: str,
        view: TrickOrTreatRequestView,
        expires_at: datetime,
    ) -> None:
        try:
            delay = max(0.0, (expires_at - datetime.now(expires_at.tzinfo)).total_seconds())
            await asyncio.sleep(delay)
            await view.on_timeout()
        except asyncio.CancelledError:
            raise
        finally:
            self._restored_request_expiry_tasks.pop(task_key, None)

    def reload_config(self) -> None:
        self.achievements_config = get_achievements_config()
        self.profile_requests_config = get_profile_requests_config()

    @app_commands.command(name="perfil", description="Mostra seu perfil do evento Halloween.")
    async def perfil(self, interaction: discord.Interaction, usuario: discord.Member | None = None) -> None:
        await interaction.response.defer(ephemeral=True)

        member = usuario or interaction.user
        profile = await self.profile_service.get_profile(member.id)
        viewer_locale = await self.i18n.get_user_locale(interaction.user.id)
        if member.id == interaction.user.id:
            panel = self._build_profile_panel(member, profile, viewer_locale)
            message = await interaction.followup.send(
                view=panel,
                ephemeral=True,
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            panel.message = message
            return

        data = self._build_profile_data(member, profile)
        can_request = False
        if interaction.guild is not None:
            if self._is_bot_target(member.id):
                can_request = self._bot_easter_egg_role(interaction.user) is not None
            else:
                can_request = await self._is_recently_active_in_event(interaction.guild.id, member.id)
        view = PublicProfileView(
            self,
            interaction.guild.id if interaction.guild else None,
            interaction.user.id,
            member.id,
            viewer_locale,
            data,
            can_request=can_request,
        )
        message = await interaction.followup.send(
            view=view,
            ephemeral=True,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = message

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        config = await self.admin_repo.get_config(message.guild.id)
        event_channel_ids = {int(channel_id) for channel_id in (config.get("event_channel_ids") or []) if channel_id}
        if message.channel.id not in event_channel_ids:
            return
        await self.profile_requests_repo.set_last_active_at(
            message.guild.id,
            message.author.id,
            datetime.now(PROFILE_REQUESTS_TIMEZONE).isoformat(),
        )

    async def show_profile_panel(self, interaction: discord.Interaction, owner_id: int) -> None:
        await interaction.response.defer()
        profile = await self.profile_service.get_profile(owner_id)
        locale = await self.i18n.get_user_locale(owner_id)
        member = interaction.guild.get_member(owner_id) if interaction.guild else None
        user = member or interaction.user
        panel = self._build_profile_panel(user, profile, locale)
        message = await interaction.edit_original_response(
            content=None,
            embed=None,
            attachments=[],
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        panel.message = message

    def _build_profile_panel(
        self,
        member: discord.abc.User,
        profile: dict,
        locale: str,
    ) -> ProfilePanelView:
        data = self._build_profile_data(member, profile)
        return ProfilePanelView(
            self,
            owner_id=member.id,
            locale=locale,
            data=data,
        )

    @staticmethod
    def _build_profile_data(
        member: discord.abc.User,
        profile: dict,
    ) -> ProfilePanelData:
        return ProfilePanelData(
            display_name=member.display_name,
            avatar_url=member.display_avatar.url,
            xp_bank=int(profile.get("xp_bank", 0)),
            candies=int(profile.get("candies", 0)),
            trick_coins=int(profile.get("trick_coins", 0)),
            dice_count=int(profile.get("dice_count", 0)),
            board_position=int(profile.get("board_position", 0)),
            board_laps=int(profile.get("board_laps", 0)),
            candies_total=int(profile.get("candies_total", 0)),
            tricks_applied_total=int(profile.get("tricks_applied_total", 0)),
            inventory_summary=str(profile.get("inventory_summary") or ""),
        )

    def build_trick_or_treat_request_embed(
        self,
        requester: discord.abc.User,
        target: discord.abc.User,
        locale: str,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=self.i18n.t(locale, "profile.trick_or_treat_request_title"),
            description=self.i18n.t(
                locale,
                "profile.trick_or_treat_request_description",
                requester=requester.display_name,
            ),
            color=discord.Color.from_rgb(245, 122, 39),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=self.i18n.t(locale, "profile.trick_or_treat_request_footer"))
        return embed

    def build_trick_or_treat_result_embed(
        self,
        locale: str,
        target_name: str,
        outcome: str,
        amount: int | None = None,
    ) -> discord.Embed:
        description_key = {
            "candy": "profile.trick_or_treat_result_candy",
            "trick": "profile.trick_or_treat_result_trick",
            "ignore": "profile.trick_or_treat_result_ignored",
        }[outcome]
        color = {
            "candy": discord.Color.green(),
            "trick": discord.Color.orange(),
            "ignore": discord.Color.dark_grey(),
        }[outcome]
        return discord.Embed(
            title=self.i18n.t(locale, "profile.trick_or_treat_result_title"),
            description=self.i18n.t(locale, description_key, target=target_name, amount=amount or 0),
            color=color,
        )

    async def _is_recently_active_in_event(self, guild_id: int, user_id: int) -> bool:
        config = await self.admin_repo.get_config(guild_id)
        event_channel_ids = [int(channel_id) for channel_id in (config.get("event_channel_ids") or []) if channel_id]
        if not event_channel_ids:
            return False
        last_active_at = await self.profile_requests_repo.get_last_active_at(guild_id, user_id)
        if not last_active_at:
            return False
        try:
            last_active = datetime.fromisoformat(str(last_active_at))
        except ValueError:
            return False
        window_minutes = int(self.profile_requests_config.get("active_window_minutes", 30))
        now = datetime.now(last_active.tzinfo or PROFILE_REQUESTS_TIMEZONE)
        return last_active >= now - timedelta(minutes=window_minutes)

    # ---- Easter egg: pedir doce ao proprio bot ----

    def _is_bot_target(self, user_id: int) -> bool:
        return self.bot.user is not None and int(user_id) == self.bot.user.id

    def _bot_easter_egg_config(self) -> dict | None:
        cfg = self.profile_requests_config.get("easter_egg")
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return None
        try:
            role_id = int(cfg.get("role_id") or 0)
        except (TypeError, ValueError):
            return None
        if role_id <= 0:
            return None
        return {**cfg, "role_id": role_id}

    def _bot_easter_egg_role(self, viewer: discord.abc.User | discord.Member | None) -> discord.Role | None:
        """Devolve o cargo se a pessoa ainda pode ganhar; None se nao (desligado, sem cargo, ja tem)."""
        cfg = self._bot_easter_egg_config()
        if cfg is None or not isinstance(viewer, discord.Member):
            return None
        role = viewer.guild.get_role(cfg["role_id"])
        if role is None or role in viewer.roles:
            return None
        return role

    async def handle_bot_easter_egg(
        self,
        interaction: discord.Interaction,
        view: "PublicProfileView",
        button: discord.ui.Button,
    ) -> None:
        # Resposta neutra: e a mesma que a pessoa ja recebia ao pedir doce a alguem inativo.
        generic = self.i18n.t(view.locale, "profile.trick_or_treat_target_inactive")
        role = self._bot_easter_egg_role(interaction.user)
        if role is None:
            await interaction.followup.send(generic, ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="profile")
        except discord.HTTPException:
            LOGGER.warning(
                "easter egg: nao consegui dar o cargo %s para o usuario %s",
                role.id,
                interaction.user.id,
                exc_info=True,
            )
            await interaction.followup.send(generic, ephemeral=True)
            return
        button.disabled = True
        try:
            await interaction.edit_original_response(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass
        cfg = self._bot_easter_egg_config() or {}
        message = str(cfg.get("message") or "").strip()
        if str(view.locale).lower().startswith("en"):
            message = str(cfg.get("message_en") or "").strip() or message
        await interaction.followup.send(
            message or "\U0001F383",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _format_generosity_reward_line(self, locale: str, item_id: str, quantity: int) -> str:
        item_name = self.i18n.maybe_t(locale, f"item.{item_id}.name") or item_id
        return f"- {item_name} x{quantity}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProfileCog(bot))
