from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Literal

import discord


if TYPE_CHECKING:
    from cogs.evento import EventoCog


LOGGER = logging.getLogger(__name__)
EVENT_COLOR = discord.Color.from_rgb(245, 122, 39)
EventPage = Literal["overview", "control", "public"]
DangerAction = Literal["end", "cancel"]

PAGE_LABELS: tuple[tuple[EventPage, str, str], ...] = (
    ("overview", "Resumo", "📋"),
    ("control", "Controle", "⚙️"),
    ("public", "Eventos", "🎭"),
)


class EventPageButton(discord.ui.Button):
    def __init__(self, page: EventPage, label: str, emoji: str, *, active: bool) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            disabled=active,
            custom_id=f"event:page:{page}",
        )
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, EventPanelView):
            return
        await interaction.response.defer()
        panel.confirmation = None
        await panel.refresh(page=self.page)
        await interaction.edit_original_response(
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class EventRefreshButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Atualizar",
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            custom_id="event:refresh",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, EventPanelView):
            return
        await interaction.response.defer()
        await panel.refresh(page=panel.page)
        await interaction.edit_original_response(
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class EventControlButton(discord.ui.Button):
    def __init__(
        self,
        action: str,
        label: str,
        emoji: str,
        style: discord.ButtonStyle,
        *,
        disabled: bool,
    ) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            disabled=disabled,
            custom_id=f"event:control:{action}",
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, EventPanelView):
            return
        if self.action == "pause":
            await interaction.response.send_modal(EventPauseModal(panel))
            return
        if self.action == "end":
            await panel.request_confirmation(interaction, "end", page="control")
            return

        method = {
            "start": panel.cog._comecar,
            "resume": panel.cog._retomar,
        }[self.action]

        async def operation() -> tuple[bool, str]:
            return await method(interaction)

        await panel.run_action(interaction, operation, page="control")


class PublicEventButton(discord.ui.Button):
    def __init__(
        self,
        event_name: str,
        label: str,
        emoji: str,
        style: discord.ButtonStyle,
        *,
        disabled: bool,
    ) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            disabled=disabled,
            custom_id=f"event:public:start:{event_name}",
        )
        self.event_name = event_name

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, EventPanelView):
            return

        async def operation() -> tuple[bool, str]:
            return await panel.cog.start_public_event(interaction, self.event_name)

        await panel.run_action(interaction, operation, page="public")


class CancelPublicEventButton(discord.ui.Button):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(
            label="Cancelar evento atual",
            emoji="⏹️",
            style=discord.ButtonStyle.danger,
            disabled=disabled,
            custom_id="event:public:cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, EventPanelView):
            return
        await panel.request_confirmation(interaction, "cancel", page="public")


class EventConfirmationButton(discord.ui.Button):
    def __init__(self, *, confirm: bool, action: DangerAction) -> None:
        super().__init__(
            label="Confirmar" if confirm else "Voltar",
            emoji="✅" if confirm else "↩️",
            style=discord.ButtonStyle.danger if confirm else discord.ButtonStyle.secondary,
            custom_id=f"event:confirm:{action}:{'yes' if confirm else 'no'}",
        )
        self.confirm = confirm
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, EventPanelView):
            return
        if not self.confirm:
            await interaction.response.defer()
            panel.confirmation = None
            await panel.refresh(page=panel.page, notice="Ação cancelada.")
            await interaction.edit_original_response(
                view=panel,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        async def operation() -> tuple[bool, str]:
            if self.action == "end":
                return await panel.cog._encerrar(interaction)
            return await panel.cog._cancelar(interaction)

        panel.confirmation = None
        await panel.run_action(interaction, operation, page=panel.page)


class EventPauseModal(discord.ui.Modal):
    def __init__(self, panel: EventPanelView) -> None:
        super().__init__(title="Pausar eventos públicos", timeout=300)
        self.panel = panel
        self.hours = discord.ui.TextInput(
            label="Duração em horas",
            placeholder="De 1 a 24",
            default="2",
            min_length=1,
            max_length=2,
        )
        self.reason = discord.ui.TextInput(
            label="Motivo",
            placeholder="Opcional",
            required=False,
            max_length=300,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.hours)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            hours = int(str(self.hours.value).strip())
        except ValueError:
            hours = 0
        if not 1 <= hours <= 24:
            await interaction.response.send_message(
                "Informe uma duração entre 1 e 24 horas.",
                ephemeral=True,
            )
            return
        reason = str(self.reason.value or "").strip() or None

        async def operation() -> tuple[bool, str]:
            return await self.panel.cog._pausar(interaction, hours, reason)

        await self.panel.run_action(interaction, operation, page="control")


class EventPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: EventoCog,
        *,
        guild: discord.Guild,
        moderator_id: int,
        timeout: float = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.guild_id = int(guild.id)
        self.moderator_id = int(moderator_id)
        self.page: EventPage = "overview"
        self.state: dict = {}
        self.notice: str | None = None
        self.confirmation: DangerAction | None = None
        self.message: discord.Message | None = None
        self._action_lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        cog: EventoCog,
        *,
        guild: discord.Guild,
        moderator_id: int,
    ) -> EventPanelView:
        panel = cls(cog, guild=guild, moderator_id=moderator_id)
        await panel.refresh()
        return panel

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        allowed = (
            interaction.user.id == self.moderator_id
            and interaction.guild_id == self.guild_id
            and bool(getattr(permissions, "manage_guild", False))
        )
        if not allowed:
            await interaction.response.send_message(
                "Somente o administrador que abriu este painel pode usá-lo.",
                ephemeral=True,
            )
        return allowed

    async def refresh(
        self,
        *,
        page: EventPage | None = None,
        notice: str | None = None,
    ) -> None:
        if page is not None:
            self.page = page
        self.notice = notice
        self.state = await self.cog.get_event_panel_state(self.guild_id)
        self.clear_items()
        self.add_item(self._build_container())

    async def run_action(
        self,
        interaction: discord.Interaction,
        operation: Callable[[], Awaitable[tuple[bool, str]]],
        *,
        page: EventPage,
    ) -> None:
        await interaction.response.defer()
        async with self._action_lock:
            try:
                ok, message = await operation()
                notice = f"{'✅' if ok else '❌'} {message}"
            except Exception:
                LOGGER.exception("Falha em uma acao do painel de evento da guild %s", self.guild_id)
                notice = "❌ Não foi possível concluir essa ação. Consulte os logs do bot."
            self.confirmation = None
            await self.refresh(page=page, notice=notice)
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def request_confirmation(
        self,
        interaction: discord.Interaction,
        action: DangerAction,
        *,
        page: EventPage,
    ) -> None:
        await interaction.response.defer()
        self.confirmation = action
        await self.refresh(page=page)
        await interaction.edit_original_response(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _build_container(self) -> discord.ui.Container:
        container = discord.ui.Container(accent_color=EVENT_COLOR)
        container.add_item(
            discord.ui.TextDisplay(
                "# 🎃 Controle do Evento\n"
                "Acompanhe o Halloween e controle os eventos públicos por um único painel."
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                *(
                    EventPageButton(page, label, emoji, active=self.page == page)
                    for page, label, emoji in PAGE_LABELS
                ),
                EventRefreshButton(),
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        if self.notice:
            container.add_item(discord.ui.TextDisplay(self.notice[:1000]))
            container.add_item(discord.ui.Separator())

        {
            "overview": self._add_overview,
            "control": self._add_control,
            "public": self._add_public_events,
        }[self.page](container)
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "-# Use `/setup` para canais e cargos. Os Arquivos do Observador continuam em "
                "`/observador arquivo`."
            )
        )
        return container

    def _add_overview(self, container: discord.ui.Container) -> None:
        runtime = self.state.get("runtime") or {}
        event_active = bool(runtime.get("event_active"))
        paused = bool(self.state.get("paused"))
        public_name = str(self.state.get("active_public_event") or "Nenhum")
        status = "🟢 Ativo" if event_active else "⚫ Inativo"
        public_status = f"🎭 {public_name}" if public_name != "Nenhum" else "⚪ Nenhum"
        pause_status = (
            self.cog._format_timestamp(runtime.get("events_paused_until"), "Não pausado")
            if paused
            else "Não pausada"
        )
        container.add_item(
            discord.ui.TextDisplay(
                "## Visão geral\n"
                f"**Evento global:** {status}\n"
                f"**Evento público em andamento:** {public_status}\n"
                f"**Agenda pública:** {'Pausada' if paused else 'Liberada'}\n\n"
                f"**Iniciado em:** {self.cog._format_timestamp(runtime.get('event_started_at'), 'Não iniciado')}\n"
                f"**Último encerramento:** {self.cog._format_timestamp(runtime.get('event_ended_at'), 'Nunca encerrado')}\n"
                f"**Pausa até:** {pause_status}\n"
                f"**Motivo:** {discord.utils.escape_markdown(str(runtime.get('pause_reason') or 'Nenhum'))}"
            )
        )

    def _add_control(self, container: discord.ui.Container) -> None:
        runtime = self.state.get("runtime") or {}
        active = bool(runtime.get("event_active"))
        paused = bool(self.state.get("paused"))
        container.add_item(
            discord.ui.TextDisplay(
                "## Controle global\n"
                "Iniciar libera as mecânicas do evento. Pausar interrompe apenas a agenda de eventos "
                "públicos; encerrar também cancela a rodada pública atual."
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                EventControlButton(
                    "start", "Iniciar evento", "▶️", discord.ButtonStyle.success, disabled=active
                ),
                EventControlButton(
                    "pause", "Pausar agenda", "⏸️", discord.ButtonStyle.secondary,
                    disabled=not active or paused,
                ),
                EventControlButton(
                    "resume", "Retomar agenda", "🔄", discord.ButtonStyle.primary,
                    disabled=not active or not paused,
                ),
                EventControlButton(
                    "end", "Encerrar evento", "⏹️", discord.ButtonStyle.danger, disabled=not active
                ),
            )
        )
        if self.confirmation == "end":
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    "### Confirmar encerramento\n"
                    "Isso desativa o Halloween e cancela o evento publico que estiver em andamento."
                )
            )
            container.add_item(
                discord.ui.ActionRow(
                    EventConfirmationButton(confirm=True, action="end"),
                    EventConfirmationButton(confirm=False, action="end"),
                )
            )

    def _add_public_events(self, container: discord.ui.Container) -> None:
        active_name = str(self.state.get("active_public_event") or "Nenhum")
        has_active = active_name != "Nenhum"
        container.add_item(
            discord.ui.TextDisplay(
                "## Eventos públicos\n"
                f"**Em andamento:** {active_name}\n\n"
                "Inicie uma rodada manual para teste ou para um momento especial. Apenas uma pode ficar "
                "ativa por vez; a agenda automática continua configurada em `halloween.yml`."
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                PublicEventButton(
                    "observer", "Observador", "📹", discord.ButtonStyle.danger, disabled=has_active
                ),
                PublicEventButton(
                    "aranhas", "Chuva de Doces", "🍬", discord.ButtonStyle.primary, disabled=has_active
                ),
                PublicEventButton(
                    "julgamento", "Carrossel", "🎠", discord.ButtonStyle.success, disabled=has_active
                ),
                CancelPublicEventButton(disabled=not has_active),
            )
        )
        if self.confirmation == "cancel":
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    "### Confirmar cancelamento\n"
                    f"A rodada **{discord.utils.escape_markdown(active_name)}** sera encerrada agora."
                )
            )
            container.add_item(
                discord.ui.ActionRow(
                    EventConfirmationButton(confirm=True, action="cancel"),
                    EventConfirmationButton(confirm=False, action="cancel"),
                )
            )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
        /,
    ) -> None:
        LOGGER.exception("Erro no painel de evento item=%s", item, exc_info=error)
        message = "Não foi possível processar essa ação do evento."
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
