from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Literal

import discord


if TYPE_CHECKING:
    from cogs.setup import SetupCog


LOGGER = logging.getLogger(__name__)
SETUP_COLOR = discord.Color.from_rgb(96, 181, 118)
SetupPage = Literal["overview", "channels", "roles", "carousel", "system"]

PAGE_LABELS: tuple[tuple[SetupPage, str, str], ...] = (
    ("overview", "Resumo", "📋"),
    ("channels", "Canais", "#️⃣"),
    ("roles", "Cargos", "🏷️"),
    ("carousel", "Carrossel", "🎠"),
    ("system", "Sistema", "⚙️"),
)

CHANNEL_SETTINGS = (
    ("leaderboard", "leaderboard_channel_id", "Canal de leaderboards"),
    ("logs", "log_channel_id", "Canal de logs"),
    ("julgamento", "julgamento_channel_id", "Canal do Carrossel"),
)

ROLE_SETTINGS = (
    ("loja", "shop_role_id", "Cargo da Loja"),
    ("tabuleiro_100", "board_100_role_id", "Cargo Tabuleiro 100"),
    ("notificacao", "notification_role_id", "Cargo de notificação"),
    ("cinefilo", "cinefilo_role_id", "Cargo Cinéfilo"),
    ("julgamento_ativo", "julgamento_active_role_id", "Carrossel — participante ativo"),
    ("julgamento_mutado", "julgamento_muted_role_id", "Carrossel — participante mutado"),
    ("julgamento_espectador", "julgamento_spectator_role_id", "Carrossel — espectador"),
)


def _role_default(guild: discord.Guild, role_id: int | None) -> list[discord.SelectDefaultValue]:
    if not role_id or guild.get_role(int(role_id)) is None:
        return []
    return [
        discord.SelectDefaultValue(
            id=int(role_id),
            type=discord.SelectDefaultValueType.role,
        )
    ]


class SetupPageButton(discord.ui.Button):
    def __init__(self, page: SetupPage, label: str, emoji: str, *, active: bool) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            disabled=active,
            custom_id=f"setup:page:{page}",
        )
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, SetupPanelView):
            return
        await interaction.response.defer()
        await panel.refresh(page=self.page)
        await interaction.edit_original_response(
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class SetupChannelSelect(discord.ui.ChannelSelect):
    def __init__(
        self,
        *,
        setting: str,
        label: str,
        guild: discord.Guild,
        current_ids: list[int],
        multiple: bool = False,
    ) -> None:
        defaults = [
            discord.SelectDefaultValue(
                id=int(channel_id),
                type=discord.SelectDefaultValueType.channel,
            )
            for channel_id in current_ids[:25]
            if guild.get_channel(int(channel_id)) is not None
        ]
        super().__init__(
            custom_id=f"setup:channel:{setting}",
            channel_types=[discord.ChannelType.text],
            placeholder=f"Selecionar {label.lower()}",
            min_values=0,
            max_values=25 if multiple else 1,
            default_values=defaults,
        )
        self.setting = setting
        self.label = label
        self.multiple = multiple

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, SetupPanelView):
            return
        selected_ids = [int(channel.id) for channel in self.values]

        async def operation() -> str:
            if self.multiple:
                await panel.cog.update_event_channels(
                    interaction.guild_id,
                    interaction.user.id,
                    selected_ids,
                )
                return f"✅ {self.label} atualizados: **{len(selected_ids)}** selecionado(s)."
            channel_id = selected_ids[0] if selected_ids else None
            await panel.cog.update_setup_channel(
                interaction.guild_id,
                interaction.user.id,
                self.setting,
                channel_id,
            )
            return f"✅ {self.label} atualizado."

        await panel.run_action(interaction, operation, page="channels")


class SetupRoleSelect(discord.ui.RoleSelect):
    def __init__(
        self,
        *,
        setting: str,
        label: str,
        guild: discord.Guild,
        role_id: int | None,
    ) -> None:
        super().__init__(
            custom_id=f"setup:role:{setting}",
            placeholder=f"Selecionar {label.lower()}",
            min_values=0,
            max_values=1,
            default_values=_role_default(guild, role_id),
        )
        self.setting = setting
        self.label = label

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, SetupPanelView):
            return
        role_id = int(self.values[0].id) if self.values else None

        async def operation() -> str:
            if role_id == panel.guild_id:
                return "❌ O cargo `@everyone` nao pode ser usado na configuracao do evento."
            await panel.cog.update_setup_role(
                interaction.guild_id,
                interaction.user.id,
                self.setting,
                role_id,
            )
            return f"✅ {self.label} atualizado."

        await panel.run_action(interaction, operation, page="roles")


class SetupCarouselButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Criar ou atualizar Carrossel",
            emoji="🎠",
            style=discord.ButtonStyle.success,
            custom_id="setup:carousel:build",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, SetupPanelView):
            return

        async def operation() -> str:
            ok, message = await panel.cog.configure_carousel(interaction)
            return f"{'✅' if ok else '❌'} {message}"

        await panel.run_action(interaction, operation, page="carousel")


class SetupReloadButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Recarregar halloween.yml",
            emoji="🔄",
            style=discord.ButtonStyle.primary,
            custom_id="setup:system:reload",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, SetupPanelView):
            return

        async def operation() -> str:
            ok, message = await panel.cog.reload_operational_config(interaction)
            return f"{'✅' if ok else '❌'} {message}"

        await panel.run_action(interaction, operation, page="system")


class SetupPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: SetupCog,
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
        self.page: SetupPage = "overview"
        self.config: dict = {}
        self.runtime: dict = {}
        self.notice: str | None = None
        self.message: discord.Message | None = None

    @classmethod
    async def create(
        cls,
        cog: SetupCog,
        *,
        guild: discord.Guild,
        moderator_id: int,
    ) -> SetupPanelView:
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
                "Somente o administrador que abriu este setup pode usar o painel.",
                ephemeral=True,
            )
        return allowed

    async def refresh(self, *, page: SetupPage | None = None, notice: str | None = None) -> None:
        if page is not None:
            self.page = page
        self.notice = notice
        self.config = await self.cog.admin_repo.get_config(self.guild_id)
        self.runtime = await self.cog.admin_repo.get_runtime(self.guild_id)
        self.clear_items()
        self.add_item(self._build_container())

    async def run_action(
        self,
        interaction: discord.Interaction,
        operation: Callable[[], Awaitable[str]],
        *,
        page: SetupPage,
    ) -> None:
        await interaction.response.defer()
        try:
            notice = await operation()
        except Exception:
            LOGGER.exception("Falha em uma acao do painel de setup da guild %s", self.guild_id)
            notice = "❌ Não foi possível concluir essa alteração. Consulte os logs do bot."
        await self.refresh(page=page, notice=notice)
        await interaction.edit_original_response(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _build_container(self) -> discord.ui.Container:
        container = discord.ui.Container(accent_color=SETUP_COLOR)
        container.add_item(
            discord.ui.TextDisplay(
                "# 🎃 Setup Halloween\n"
                "Configure o servidor sem editar IDs manualmente. As alterações abaixo são salvas no SQLite."
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                *(
                    SetupPageButton(page, label, emoji, active=self.page == page)
                    for page, label, emoji in PAGE_LABELS
                )
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        if self.notice:
            container.add_item(discord.ui.TextDisplay(self.notice[:1000]))
            container.add_item(discord.ui.Separator())

        builders = {
            "overview": self._add_overview,
            "channels": self._add_channels,
            "roles": self._add_roles,
            "carousel": self._add_carousel,
            "system": self._add_system,
        }
        builders[self.page](container)
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "-# Painel visível somente para você. Para alterar tempos, chances e recompensas, edite "
                "`config/halloween.yml` e use a página **Sistema**."
            )
        )
        return container

    def _add_overview(self, container: discord.ui.Container) -> None:
        event_channels = self.config.get("event_channel_ids") or []
        channel_lines = "\n".join(f"- {self._channel(channel_id)}" for channel_id in event_channels)
        if not channel_lines:
            channel_lines = "- Não configurado"
        container.add_item(
            discord.ui.TextDisplay(
                "## Visão geral\n"
                f"**Evento ativo:** {'Sim' if self.runtime.get('event_active') else 'Não'}\n"
                f"**Pausado até:** {self.runtime.get('events_paused_until') or 'Não pausado'}\n\n"
                "### Canais\n"
                f"**Leaderboards:** {self._channel(self.config.get('leaderboard_channel_id'))}\n"
                f"**Logs:** {self._channel(self.config.get('log_channel_id'))}\n"
                f"**Carrossel:** {self._channel(self.config.get('julgamento_channel_id'))}\n"
                f"**Eventos:**\n{channel_lines}\n\n"
                "### Cargos principais\n"
                f"**Loja:** {self._role(self.config.get('shop_role_id'))}\n"
                f"**Notificação:** {self._role(self.config.get('notification_role_id'))}\n"
                f"**Cinéfilo:** {self._role(self.config.get('cinefilo_role_id'))}\n"
                f"**Tabuleiro 100:** {self._role(self.config.get('board_100_role_id'))}"
            )
        )

    def _add_channels(self, container: discord.ui.Container) -> None:
        container.add_item(
            discord.ui.TextDisplay(
                "## Canais\nSelecione um canal para salvar. Limpe a seleção para remover uma configuração."
            )
        )
        for setting, config_key, label in CHANNEL_SETTINGS:
            current_id = self.config.get(config_key)
            container.add_item(discord.ui.TextDisplay(f"**{label}** — {self._channel(current_id)}"))
            container.add_item(
                discord.ui.ActionRow(
                    SetupChannelSelect(
                        setting=setting,
                        label=label,
                        guild=self.guild,
                        current_ids=[int(current_id)] if current_id else [],
                    )
                )
            )
        event_ids = [int(channel_id) for channel_id in self.config.get("event_channel_ids") or []]
        container.add_item(
            discord.ui.TextDisplay(
                f"**Canais de eventos** — {len(event_ids)} selecionado(s)\n"
                "Todos os canais escolhidos aqui recebem ou contabilizam as mecânicas públicas do evento."
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                SetupChannelSelect(
                    setting="eventos",
                    label="Canais de eventos",
                    guild=self.guild,
                    current_ids=event_ids,
                    multiple=True,
                )
            )
        )

    def _add_roles(self, container: discord.ui.Container) -> None:
        container.add_item(
            discord.ui.TextDisplay(
                "## Cargos\nSelecione um cargo para salvar. Limpe a seleção para remover uma configuração."
            )
        )
        for setting, config_key, label in ROLE_SETTINGS:
            role_id = self.config.get(config_key)
            container.add_item(discord.ui.TextDisplay(f"**{label}** — {self._role(role_id)}"))
            container.add_item(
                discord.ui.ActionRow(
                    SetupRoleSelect(
                        setting=setting,
                        label=label,
                        guild=self.guild,
                        role_id=role_id,
                    )
                )
            )

    def _add_carousel(self, container: discord.ui.Container) -> None:
        container.add_item(
            discord.ui.TextDisplay(
                "## 🎠 Estrutura do Carrossel\n"
                "Cria ou atualiza os três cargos, o canal privado, a mensagem-base e a thread de conversa. "
                "A estrutura existente é reaproveitada sempre que possível.\n\n"
                f"**Canal:** {self._channel(self.config.get('julgamento_channel_id'))}\n"
                f"**Participante ativo:** {self._role(self.config.get('julgamento_active_role_id'))}\n"
                f"**Participante mutado:** {self._role(self.config.get('julgamento_muted_role_id'))}\n"
                f"**Espectador:** {self._role(self.config.get('julgamento_spectator_role_id'))}"
            )
        )
        container.add_item(discord.ui.ActionRow(SetupCarouselButton()))

    def _add_system(self, container: discord.ui.Container) -> None:
        container.add_item(
            discord.ui.TextDisplay(
                "## Sistema\n"
                "Valida e recarrega `config/halloween.yml` sem reiniciar o bot. Uma configuração inválida "
                "não substitui a última versão válida. Alterações em código Python ainda exigem reinício."
            )
        )
        container.add_item(discord.ui.ActionRow(SetupReloadButton()))

    def _channel(self, channel_id: int | None) -> str:
        if not channel_id:
            return "Não configurado"
        channel = self.guild.get_channel(int(channel_id))
        return channel.mention if channel is not None else f"`{channel_id}` (não encontrado)"

    def _role(self, role_id: int | None) -> str:
        if not role_id:
            return "Não configurado"
        role = self.guild.get_role(int(role_id))
        return role.mention if role is not None else f"`{role_id}` (não encontrado)"

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
        /,
    ) -> None:
        LOGGER.exception("Erro no painel de setup item=%s", item, exc_info=error)
        message = "Não foi possível processar essa ação do setup."
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
