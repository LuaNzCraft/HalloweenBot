from __future__ import annotations

import inspect

import discord
from discord import app_commands
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from repositories.julgamento_repo import JulgamentoRepo
from services.audit_service import AuditService
from services.config_service import ConfigError, get_config_service
from services.item_catalog import ItemCatalog
from services.shop_service import ShopService


SETUP_COLOR = discord.Color.from_rgb(96, 181, 118)


class SetupCog(commands.GroupCog, name="setup"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.admin_repo = AdminRepo()
        self.julgamento_repo = JulgamentoRepo()
        self.audit_service = AuditService(bot, self.admin_repo)

    async def setup_tipo_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        categoria = str(getattr(interaction.namespace, "categoria", "") or "").strip().lower()
        choices_by_category = {
            "canal": [
                app_commands.Choice(name="Leaderboards", value="leaderboard"),
                app_commands.Choice(name="Logs", value="logs"),
                app_commands.Choice(name="Eventos", value="eventos"),
                app_commands.Choice(name="Julgamento", value="julgamento"),
            ],
            "cargo": [
                app_commands.Choice(name="Loja", value="loja"),
                app_commands.Choice(name="Tabuleiro 100", value="tabuleiro_100"),
                app_commands.Choice(name="Notificacao", value="notificacao"),
                app_commands.Choice(name="Cinefilo", value="cinefilo"),
                app_commands.Choice(name="Julgamento Ativo", value="julgamento_ativo"),
                app_commands.Choice(name="Julgamento Mutado", value="julgamento_mutado"),
                app_commands.Choice(name="Julgamento Espectador", value="julgamento_espectador"),
            ],
        }
        available = choices_by_category.get(categoria, [])
        if not current:
            return available[:25]
        current_lower = current.lower()
        return [choice for choice in available if current_lower in choice.name.lower()][:25]

    @app_commands.command(name="configurar", description="Configura canais e cargos principais do evento.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(
        categoria=[
            app_commands.Choice(name="Canal", value="canal"),
            app_commands.Choice(name="Cargo", value="cargo"),
        ]
    )
    @app_commands.describe(
        categoria="Escolha se voce vai configurar um canal ou um cargo.",
        tipo="Tipo especifico dentro da categoria escolhida.",
        canal="Preencha quando o tipo for um canal.",
        cargo="Preencha quando o tipo for um cargo.",
    )
    @app_commands.autocomplete(tipo=setup_tipo_autocomplete)
    async def configurar(
        self,
        interaction: discord.Interaction,
        categoria: app_commands.Choice[str],
        tipo: str,
        canal: discord.TextChannel | None = None,
        cargo: discord.Role | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Use esse comando dentro de um servidor.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        channel_types = {"leaderboard", "logs", "eventos", "julgamento"}
        role_types = {"loja", "tabuleiro_100", "notificacao", "cinefilo", "julgamento_ativo", "julgamento_mutado", "julgamento_espectador"}
        tipo_value = tipo.strip().lower()

        if categoria.value == "canal":
            if tipo_value not in channel_types:
                await interaction.followup.send(
                    "Tipo de canal invalido. Escolha um tipo listado no autocomplete.",
                    ephemeral=True,
                )
                return
            if canal is None:
                await interaction.followup.send(
                    "Para configurar um canal, preencha o campo `canal`.",
                    ephemeral=True,
                )
                return
            if cargo is not None:
                await interaction.followup.send(
                    "Esse tipo usa apenas o campo `canal`.",
                    ephemeral=True,
                )
                return

            await self.admin_repo.set_channel(interaction.guild.id, tipo_value, canal.id)
            await self.audit_service.log(
                guild_id=interaction.guild.id,
                actor_id=interaction.user.id,
                action="setup.channel.set",
                payload={"type": tipo_value, "channel_id": canal.id},
            )
            await interaction.followup.send(f"Canal configurado: **{tipo}** -> {canal.mention}", ephemeral=True)
            return

        if categoria.value == "cargo":
            if tipo_value not in role_types:
                await interaction.followup.send(
                    "Tipo de cargo invalido. Escolha um tipo listado no autocomplete.",
                    ephemeral=True,
                )
                return
            if cargo is None:
                await interaction.followup.send(
                    "Para configurar um cargo, preencha o campo `cargo`.",
                    ephemeral=True,
                )
                return
            if canal is not None:
                await interaction.followup.send(
                    "Esse tipo usa apenas o campo `cargo`.",
                    ephemeral=True,
                )
                return

            await self.admin_repo.set_role(interaction.guild.id, tipo_value, cargo.id)
            await self.audit_service.log(
                guild_id=interaction.guild.id,
                actor_id=interaction.user.id,
                action="setup.role.set",
                payload={"type": tipo_value, "role_id": cargo.id},
            )
            await interaction.followup.send(f"Cargo configurado: **{tipo}** -> {cargo.mention}", ephemeral=True)
            return

        await interaction.followup.send("Categoria de configuracao desconhecida.", ephemeral=True)

    @app_commands.command(name="remover_canal_evento", description="Remove um canal da lista de canais de eventos.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def remover_canal_evento(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Use esse comando dentro de um servidor.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        removed = await self.admin_repo.remove_event_channel(interaction.guild.id, canal.id)
        if not removed:
            await interaction.followup.send(
                f"{canal.mention} nao estava na lista de canais de eventos.",
                ephemeral=True,
            )
            return
        await self.audit_service.log(
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            action="setup.event_channel.remove",
            payload={"channel_id": canal.id},
        )
        await interaction.followup.send(f"Canal removido dos eventos: {canal.mention}", ephemeral=True)

    @app_commands.command(name="ver", description="Mostra a configuracao atual do evento.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def ver(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Use esse comando dentro de um servidor.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        config = await self.admin_repo.get_config(interaction.guild.id)
        runtime = await self.admin_repo.get_runtime(interaction.guild.id)
        embed = self._build_setup_embed(interaction.guild, config, runtime)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="recarregar_config", description="Valida e recarrega o config/halloween.yml.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def recarregar_config(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Use esse comando dentro de um servidor.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        service = get_config_service()
        try:
            config = service.reload()
        except ConfigError as exc:
            await interaction.followup.send(
                f"A configuracao nao foi aplicada. Corrija o erro abaixo:\n```text\n{exc}\n```",
                ephemeral=True,
            )
            return

        refreshed: list[str] = []
        failures: list[str] = []
        ItemCatalog.invalidate_all()
        try:
            ShopService.reload_all()
        except Exception as exc:
            failures.append(f"ShopService: {type(exc).__name__}: {exc}")
        for cog_name, cog in self.bot.cogs.items():
            hook = getattr(cog, "reload_config", None)
            if hook is None:
                continue
            try:
                result = hook()
                if inspect.isawaitable(result):
                    await result
                refreshed.append(cog_name)
            except Exception as exc:
                failures.append(f"{cog_name}: {type(exc).__name__}: {exc}")

        await self.audit_service.log(
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            action="setup.config.reload",
            payload={
                "config_version": config.get("config_version"),
                "runtime_version": service.version,
                "refreshed_cogs": refreshed,
                "failures": failures,
            },
        )
        if failures:
            details = "\n".join(failures)
            await interaction.followup.send(
                "O YAML foi validado e carregado, mas alguns componentes nao atualizaram:\n"
                f"```text\n{details[:1500]}\n```",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            (
                f"Configuracao recarregada com sucesso. Versao do arquivo: "
                f"**{config.get('config_version', 'nao definida')}**."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="julgamento", description="Cria ou atualiza a estrutura do Carrossel.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def julgamento(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Use esse comando dentro de um servidor.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        julgamento_runtime = await self.julgamento_repo.get_runtime(interaction.guild.id)
        if julgamento_runtime.get("active"):
            await interaction.followup.send(
                "Nao e possivel refazer o setup enquanto o Carrossel esta ativo.",
                ephemeral=True,
            )
            return

        config = await self.admin_repo.get_config(interaction.guild.id)
        active_role = await self._ensure_julgamento_role(
            interaction.guild,
            config,
            "julgamento_active_role_id",
            "Participante Ativo",
            discord.Color.red(),
        )
        muted_role = await self._ensure_julgamento_role(
            interaction.guild,
            config,
            "julgamento_muted_role_id",
            "Participante Mutado",
            discord.Color.dark_grey(),
        )
        spectator_role = await self._ensure_julgamento_role(
            interaction.guild,
            config,
            "julgamento_spectator_role_id",
            "Espectador",
            discord.Color.light_grey(),
        )
        if not all((active_role, muted_role, spectator_role)):
            await interaction.followup.send("Nao consegui criar ou localizar os cargos do Carrossel.", ephemeral=True)
            return

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_threads=True,
                read_message_history=True,
            ),
            active_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                send_messages_in_threads=False,
                read_message_history=True,
            ),
            muted_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                send_messages_in_threads=True,
                read_message_history=True,
            ),
            spectator_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                send_messages_in_threads=True,
                read_message_history=True,
            ),
        }

        existing_channel = interaction.guild.get_channel(int(config["julgamento_channel_id"])) if config.get("julgamento_channel_id") else None
        try:
            if isinstance(existing_channel, discord.TextChannel):
                channel = existing_channel
                merged_overwrites = dict(channel.overwrites)
                merged_overwrites.update(overwrites)
                await channel.edit(overwrites=merged_overwrites, reason="Atualizacao do Carrossel")
            else:
                channel = await interaction.guild.create_text_channel(
                    name="carrossel",
                    overwrites=overwrites,
                    reason="Criacao do canal do Carrossel",
                )

            base_message = None
            if config.get("julgamento_base_message_id"):
                try:
                    base_message = await channel.fetch_message(int(config["julgamento_base_message_id"]))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    base_message = None

            thread = None
            if config.get("julgamento_thread_id"):
                candidate = interaction.guild.get_thread(int(config["julgamento_thread_id"]))
                if candidate is None:
                    try:
                        fetched = await self.bot.fetch_channel(int(config["julgamento_thread_id"]))
                        candidate = fetched if isinstance(fetched, discord.Thread) else None
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        candidate = None
                if isinstance(candidate, discord.Thread) and candidate.parent_id == channel.id:
                    thread = candidate

            base_embed = self._build_julgamento_setup_embed()
            if base_message is not None and thread is not None:
                await base_message.edit(embed=base_embed)
                if thread.archived:
                    await thread.edit(archived=False, reason="Atualizacao do setup do Carrossel")
            else:
                base_message = await channel.send(embed=base_embed)
                thread = await base_message.create_thread(name="💬 Galera do Carrossel", auto_archive_duration=1440)
                await thread.send(
                    "💬 Chat livre do Carrossel. Comentem aqui durante o evento; "
                    "as respostas oficiais acontecem no canal principal."
                )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Nao consegui criar ou atualizar o canal e a thread do Carrossel. Verifique minhas permissoes.",
                ephemeral=True,
            )
            return

        await self.admin_repo.set_julgamento_structure(
            interaction.guild.id,
            channel_id=channel.id,
            thread_id=thread.id,
            base_message_id=base_message.id,
        )
        await self.audit_service.log(
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            action="setup.julgamento.create",
            payload={"channel_id": channel.id, "thread_id": thread.id, "base_message_id": base_message.id},
        )
        await interaction.followup.send(
            f"Carrossel configurado: {channel.mention} | Thread: {thread.mention}",
            ephemeral=True,
        )

    def _build_julgamento_setup_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎠 Carrossel",
            description=(
                "Este canal sera usado para as rodadas oficiais do Carrossel.\n\n"
                "Quando o evento abrir, os participantes responderao aqui enquanto a thread fica como chat livre."
            ),
            color=SETUP_COLOR,
        )
        embed.add_field(
            name="Como funciona",
            value=(
                "1. As inscricoes abrem por tempo limitado.\n"
                "2. O bot inicia rodadas de digitacao.\n"
                "3. Os mais lentos ou quem nao responder a tempo podem cair.\n"
                "4. No fim, sobra apenas um vencedor."
            ),
            inline=False,
        )
        embed.add_field(
            name="Chat paralelo",
            value="A thread abaixo e so para conversar durante o Carrossel.",
            inline=False,
        )
        return embed

    async def _ensure_julgamento_role(
        self,
        guild: discord.Guild,
        config: dict,
        config_key: str,
        role_name: str,
        color: discord.Color,
    ) -> discord.Role | None:
        role_id = config.get(config_key)
        if role_id:
            role = guild.get_role(int(role_id))
            if role is not None:
                return role

        for role in guild.roles:
            if role.name == role_name:
                mapped_type = {
                    "julgamento_active_role_id": "julgamento_ativo",
                    "julgamento_muted_role_id": "julgamento_mutado",
                    "julgamento_spectator_role_id": "julgamento_espectador",
                }[config_key]
                await self.admin_repo.set_role(guild.id, mapped_type, role.id)
                return role

        try:
            role = await guild.create_role(
                name=role_name,
                colour=color,
                mentionable=False,
                reason="Criacao automatica dos cargos do Julgamento",
            )
        except (discord.Forbidden, discord.HTTPException):
            return None
        mapped_type = {
            "julgamento_active_role_id": "julgamento_ativo",
            "julgamento_muted_role_id": "julgamento_mutado",
            "julgamento_spectator_role_id": "julgamento_espectador",
        }[config_key]
        await self.admin_repo.set_role(guild.id, mapped_type, role.id)
        return role

    def _build_setup_embed(self, guild: discord.Guild, config: dict, runtime: dict) -> discord.Embed:
        embed = discord.Embed(title="Setup Halloween", color=SETUP_COLOR)
        embed.add_field(name="Evento ativo", value="Sim" if runtime["event_active"] else "Nao", inline=True)
        embed.add_field(name="Pausado ate", value=runtime["events_paused_until"] or "Nao pausado", inline=True)
        embed.add_field(name="Canal de leaderboard", value=self._channel(guild, config["leaderboard_channel_id"]), inline=False)
        embed.add_field(name="Canal de logs", value=self._channel(guild, config["log_channel_id"]), inline=False)
        event_channels = config["event_channel_ids"]
        embed.add_field(
            name="Canais de eventos",
            value="\n".join(self._channel(guild, channel_id) for channel_id in event_channels) if event_channels else "Nenhum",
            inline=False,
        )
        embed.add_field(name="Canal do Carrossel", value=self._channel(guild, config["julgamento_channel_id"]), inline=False)
        embed.add_field(name="Cargo da Loja", value=self._role(guild, config["shop_role_id"]), inline=True)
        embed.add_field(name="Cargo Tabuleiro 100", value=self._role(guild, config["board_100_role_id"]), inline=True)
        embed.add_field(name="Cargo de notificacao", value=self._role(guild, config["notification_role_id"]), inline=True)
        embed.add_field(name="Cargo Cinefilo", value=self._role(guild, config["cinefilo_role_id"]), inline=True)
        embed.add_field(name="Carrossel Ativo", value=self._role(guild, config["julgamento_active_role_id"]), inline=True)
        embed.add_field(name="Carrossel Mutado", value=self._role(guild, config["julgamento_muted_role_id"]), inline=True)
        embed.add_field(name="Carrossel Espectador", value=self._role(guild, config["julgamento_spectator_role_id"]), inline=True)
        return embed

    def _channel(self, guild: discord.Guild, channel_id: int | None) -> str:
        if not channel_id:
            return "Nao configurado"
        channel = guild.get_channel(channel_id)
        return channel.mention if channel else f"`{channel_id}`"

    def _role(self, guild: discord.Guild, role_id: int | None) -> str:
        if not role_id:
            return "Nao configurado"
        role = guild.get_role(role_id)
        return role.mention if role else f"`{role_id}`"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))
