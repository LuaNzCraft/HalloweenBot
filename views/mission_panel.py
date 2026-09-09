from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

import discord

from services.mission_service import MISSION_COLOR, MissionPanelData, MissionProgress


if TYPE_CHECKING:
    from cogs.missao import MissaoCog


LOGGER = logging.getLogger(__name__)
MissionPage = Literal["overview", "personal", "global", "event"]


class MissionTabButton(discord.ui.Button):
    def __init__(self, page: MissionPage, label: str, emoji: str, *, active: bool) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.danger if active else discord.ButtonStyle.secondary,
            disabled=active,
            custom_id=f"missions:tab:{page}",
        )
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, MissionPanelView):
            return
        await interaction.response.defer()
        panel.refresh(page=self.page)
        await interaction.edit_original_response(
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class MissionRefreshButton(discord.ui.Button):
    def __init__(self, label: str) -> None:
        super().__init__(
            label=label,
            emoji="🔄",
            style=discord.ButtonStyle.danger,
            custom_id="missions:refresh",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, MissionPanelView):
            return
        await interaction.response.defer()
        data = await panel.cog.mission_service.get_panel_data(
            interaction.guild_id,
            panel.owner_id,
        )
        panel.refresh(
            data=data,
            notice=panel.cog.mission_service.i18n.t(data.locale, "missions.updated"),
        )
        await interaction.edit_original_response(
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class MissionNavigationButton(discord.ui.Button):
    TARGETS = {
        "profile": ("ProfileCog", "show_profile_panel", "common.not_loaded.profile"),
        "shop": ("ShopCog", "show_shop_panel", "common.not_loaded.shop"),
        "board": ("TabuleiroCog", "send_board_panel", "common.not_loaded.board"),
    }

    def __init__(self, target: str, label: str, emoji: str) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.danger,
            custom_id=f"missions:navigation:{target}",
        )
        self.target = target

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, MissionPanelView):
            return
        cog_name, method_name, error_key = self.TARGETS[self.target]
        target_cog = interaction.client.get_cog(cog_name)
        if target_cog is None:
            await interaction.response.send_message(
                panel.cog.mission_service.i18n.t(panel.locale, error_key),
                ephemeral=True,
            )
            return
        await getattr(target_cog, method_name)(interaction, panel.owner_id)


class MissionPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: MissaoCog,
        *,
        owner_id: int,
        data: MissionPanelData,
        timeout: float = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.data = data
        self.locale = data.locale
        self.page: MissionPage = "overview"
        self.notice: str | None = None
        self.message: discord.Message | None = None
        self._rebuild()

    def refresh(
        self,
        *,
        data: MissionPanelData | None = None,
        page: MissionPage | None = None,
        notice: str | None = None,
    ) -> None:
        if data is not None:
            self.data = data
            self.locale = data.locale
        if page is not None:
            self.page = page
        self.notice = notice
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            self.cog.mission_service.i18n.t(self.locale, "common.not_owner.missions"),
            ephemeral=True,
        )
        return False

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(self._build_container())
        self.add_item(self._build_navigation_row())

    def _build_container(self) -> discord.ui.Container:
        i18n = self.cog.mission_service.i18n
        container = discord.ui.Container(accent_color=MISSION_COLOR)
        container.add_item(
            discord.ui.TextDisplay(
                f"# 🎃 {i18n.t(self.locale, 'missions.title')}\n"
                f"{i18n.t(self.locale, 'missions.description')}"
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                MissionTabButton(
                    "overview",
                    i18n.t(self.locale, "missions.tab.overview"),
                    "📋",
                    active=self.page == "overview",
                ),
                MissionTabButton(
                    "personal",
                    i18n.t(self.locale, "missions.tab.personal"),
                    "🕯️",
                    active=self.page == "personal",
                ),
                MissionTabButton(
                    "global",
                    i18n.t(self.locale, "missions.tab.global"),
                    "🎃",
                    active=self.page == "global",
                ),
                MissionTabButton(
                    "event",
                    i18n.t(self.locale, "missions.tab.event"),
                    "👻",
                    active=self.page == "event",
                ),
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        if self.notice:
            container.add_item(discord.ui.TextDisplay(f"✅ {self.notice}"))
            container.add_item(discord.ui.Separator())

        if self.page == "overview":
            self._add_overview(container)
        else:
            progress = {
                "personal": self.data.personal,
                "global": self.data.global_daily,
                "event": self.data.event_mission,
            }[self.page]
            self._add_details(container, self.page, progress)

        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                f"-# {i18n.t(self.locale, 'missions.reset_footer', date=self.data.personal.period_key)}"
            )
        )
        return container

    def _add_overview(self, container: discord.ui.Container) -> None:
        i18n = self.cog.mission_service.i18n
        container.add_item(
            discord.ui.TextDisplay(
                f"## {i18n.t(self.locale, 'missions.tab.overview')}\n"
                f"{i18n.t(self.locale, 'missions.overview_hint')}"
            )
        )
        entries = (
            ("personal", "🕯️", self.data.personal),
            ("global", "🎃", self.data.global_daily),
            ("event", "👻", self.data.event_mission),
        )
        for index, (page, emoji, progress) in enumerate(entries):
            if index:
                container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(self._overview_text(page, emoji, progress))
            )

    def _add_details(
        self,
        container: discord.ui.Container,
        page: MissionPage,
        progress: MissionProgress | None,
    ) -> None:
        i18n = self.cog.mission_service.i18n
        emoji = {"personal": "🕯️", "global": "🎃", "event": "👻"}[page]
        section_key = {
            "personal": "missions.section.personal",
            "global": "missions.section.global",
            "event": "missions.section.event",
        }[page]
        if progress is None:
            container.add_item(
                discord.ui.TextDisplay(
                    f"## {emoji} {i18n.t(self.locale, section_key)}\n"
                    f"{i18n.t(self.locale, 'missions.event_inactive')}"
                )
            )
            return

        status = self._status(progress)
        container.add_item(
            discord.ui.TextDisplay(
                f"## {emoji} {i18n.t(self.locale, section_key)}\n"
                f"### {discord.utils.escape_markdown(progress.mission.title(self.locale))}\n"
                f"{progress.mission.description(self.locale)}"
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                f"**{i18n.t(self.locale, 'missions.progress_label')}**\n"
                f"`{self._progress_bar(progress)}`  **{progress.progress}/{progress.target}**\n\n"
                f"**{i18n.t(self.locale, 'missions.reward_label')}:** {progress.reward_text}\n"
                f"**{i18n.t(self.locale, 'missions.status_label')}:** {status}"
            )
        )

    def _overview_text(
        self,
        page: MissionPage,
        emoji: str,
        progress: MissionProgress | None,
    ) -> str:
        i18n = self.cog.mission_service.i18n
        section_key = {
            "personal": "missions.section.personal",
            "global": "missions.section.global",
            "event": "missions.section.event",
        }[page]
        heading = f"### {emoji} {i18n.t(self.locale, section_key)}"
        if progress is None:
            return f"{heading}\n{i18n.t(self.locale, 'missions.event_inactive')}"
        title = discord.utils.escape_markdown(progress.mission.title(self.locale))
        return (
            f"{heading}\n"
            f"**{title}**\n"
            f"`{self._progress_bar(progress)}`  **{progress.progress}/{progress.target}**"
            f"  •  {self._status(progress)}"
        )

    def _status(self, progress: MissionProgress) -> str:
        if progress.reward_claimed:
            key = "missions.status.rewarded"
        elif progress.completed:
            key = "missions.status.completed"
        else:
            key = "missions.status.in_progress"
        return self.cog.mission_service.i18n.t(self.locale, key)

    @staticmethod
    def _progress_bar(progress: MissionProgress) -> str:
        if progress.target <= 0:
            filled = 10
        else:
            ratio = min(1.0, max(0.0, progress.progress / progress.target))
            filled = int(ratio * 10)
        return "█" * filled + "░" * (10 - filled)

    def _build_navigation_row(self) -> discord.ui.ActionRow:
        i18n = self.cog.mission_service.i18n
        return discord.ui.ActionRow(
            MissionNavigationButton(
                "profile",
                i18n.t(self.locale, "common.profile"),
                "👤",
            ),
            MissionNavigationButton(
                "shop",
                i18n.t(self.locale, "common.shop"),
                "🛒",
            ),
            MissionNavigationButton(
                "board",
                i18n.t(self.locale, "common.board"),
                "🎲",
            ),
            MissionRefreshButton(i18n.t(self.locale, "missions.refresh")),
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
        /,
    ) -> None:
        LOGGER.exception("Erro no painel de missoes item=%s", item, exc_info=error)
        message = self.cog.mission_service.i18n.t(self.locale, "missions.panel_failed")
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        for item in self.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass
