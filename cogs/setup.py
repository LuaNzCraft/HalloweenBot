from __future__ import annotations

import inspect
import logging

import discord
from discord import app_commands
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from repositories.julgamento_repo import JulgamentoRepo
from services.audit_service import AuditService
from services.config_service import ConfigError, get_config_service
from services.item_catalog import ItemCatalog
from services.shop_service import ShopService
from views.setup_panel import SetupPanelView


LOGGER = logging.getLogger(__name__)
SETUP_COLOR = discord.Color.from_rgb(96, 181, 118)
CHANNEL_TYPES = {"leaderboard", "logs", "julgamento"}
ROLE_TYPES = {
    "loja",
    "tabuleiro_100",
    "notificacao",
    "cinefilo",
    "julgamento_ativo",
    "julgamento_mutado",
    "julgamento_espectador",
}


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.admin_repo = AdminRepo()
        self.julgamento_repo = JulgamentoRepo()
        self.audit_service = AuditService(bot, self.admin_repo)

    @app_commands.command(name="setup", description="Abre o painel de configuracao do Halloween Bot.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setup_panel(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        permissions = getattr(interaction.user, "guild_permissions", None)
        if guild is None or not bool(getattr(permissions, "manage_guild", False)):
            await interaction.response.send_message(
                "Voce precisa de Gerenciar Servidor para abrir o setup.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            panel = await SetupPanelView.create(
                self,
                guild=guild,
                moderator_id=interaction.user.id,
            )
            sent = await interaction.followup.send(
                view=panel,
                ephemeral=True,
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            panel.message = sent
        except Exception:
            LOGGER.exception("Falha ao abrir painel de setup da guild %s", guild.id)
            await interaction.followup.send(
                "Nao foi possivel abrir o painel de setup.",
                ephemeral=True,
            )

    async def update_setup_channel(
        self,
        guild_id: int | None,
        actor_id: int,
        channel_type: str,
        channel_id: int | None,
    ) -> None:
        if guild_id is None or channel_type not in CHANNEL_TYPES:
            raise ValueError("unknown_channel_type")
        await self.admin_repo.set_channel(guild_id, channel_type, channel_id)
        await self._audit_safely(
            guild_id=guild_id,
            actor_id=actor_id,
            action="setup.channel.set",
            payload={"type": channel_type, "channel_id": channel_id},
        )

    async def update_event_channels(
        self,
        guild_id: int | None,
        actor_id: int,
        channel_ids: list[int],
    ) -> None:
        if guild_id is None:
            raise ValueError("guild_required")
        await self.admin_repo.set_event_channels(guild_id, channel_ids)
        await self._audit_safely(
            guild_id=guild_id,
            actor_id=actor_id,
            action="setup.event_channels.set",
            payload={"channel_ids": channel_ids},
        )

    async def publish_leaderboard(
        self,
        interaction: discord.Interaction,
    ) -> tuple[bool, str]:
        guild = interaction.guild
        if guild is None:
            return False, "Use o setup dentro de um servidor."

        leaderboard_cog = self.bot.get_cog("LeaderboardCog")
        publisher = getattr(leaderboard_cog, "publish_or_update_panel", None)
        if not callable(publisher):
            return False, "O modulo de rankings nao esta disponivel."
        return await publisher(guild, actor_id=interaction.user.id)

    async def update_setup_role(
        self,
        guild_id: int | None,
        actor_id: int,
        role_type: str,
        role_id: int | None,
    ) -> None:
        if guild_id is None or role_type not in ROLE_TYPES:
            raise ValueError("unknown_role_type")
        await self.admin_repo.set_role(guild_id, role_type, role_id)
        await self._audit_safely(
            guild_id=guild_id,
            actor_id=actor_id,
            action="setup.role.set",
            payload={"type": role_type, "role_id": role_id},
        )

    async def reload_operational_config(
        self,
        interaction: discord.Interaction,
    ) -> tuple[bool, str]:
        if interaction.guild is None:
            return False, "Use o setup dentro de um servidor."

        service = get_config_service()
        try:
            config = service.reload()
        except ConfigError as exc:
            return False, f"A configuracao nao foi aplicada: `{str(exc)[:700]}`"

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

        await self._audit_safely(
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
            return False, "O YAML foi carregado, mas alguns componentes falharam: " + "; ".join(failures)[:700]
        return True, f"Configuracao recarregada. Versao: **{config.get('config_version', 'nao definida')}**."

    async def configure_carousel(self, interaction: discord.Interaction) -> tuple[bool, str]:
        guild = interaction.guild
        if guild is None:
            return False, "Use o setup dentro de um servidor."

        julgamento_runtime = await self.julgamento_repo.get_runtime(guild.id)
        if julgamento_runtime.get("active"):
            return False, "Nao e possivel refazer o setup enquanto o Carrossel esta ativo."

        config = await self.admin_repo.get_config(guild.id)
        active_role = await self._ensure_julgamento_role(
            guild,
            config,
            "julgamento_active_role_id",
            "Participante Ativo",
            discord.Color.red(),
        )
        muted_role = await self._ensure_julgamento_role(
            guild,
            config,
            "julgamento_muted_role_id",
            "Participante Mutado",
            discord.Color.dark_grey(),
        )
        spectator_role = await self._ensure_julgamento_role(
            guild,
            config,
            "julgamento_spectator_role_id",
            "Espectador",
            discord.Color.light_grey(),
        )
        if not all((active_role, muted_role, spectator_role)):
            return False, "Nao consegui criar ou localizar os cargos do Carrossel."

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
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

        existing_channel = (
            guild.get_channel(int(config["julgamento_channel_id"]))
            if config.get("julgamento_channel_id")
            else None
        )
        try:
            if isinstance(existing_channel, discord.TextChannel):
                channel = existing_channel
                merged_overwrites = dict(channel.overwrites)
                merged_overwrites.update(overwrites)
                await channel.edit(overwrites=merged_overwrites, reason="Atualizacao do setup do Carrossel")
            else:
                channel = await guild.create_text_channel(
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
                candidate = guild.get_thread(int(config["julgamento_thread_id"]))
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
            return False, "Nao consegui criar ou atualizar o Carrossel. Verifique minhas permissoes."

        await self.admin_repo.set_julgamento_structure(
            guild.id,
            channel_id=channel.id,
            thread_id=thread.id,
            base_message_id=base_message.id,
        )
        await self._audit_safely(
            guild_id=guild.id,
            actor_id=interaction.user.id,
            action="setup.julgamento.create",
            payload={"channel_id": channel.id, "thread_id": thread.id, "base_message_id": base_message.id},
        )
        return True, f"Carrossel configurado: {channel.mention} | Thread: {thread.mention}"

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

        mapped_type = {
            "julgamento_active_role_id": "julgamento_ativo",
            "julgamento_muted_role_id": "julgamento_mutado",
            "julgamento_spectator_role_id": "julgamento_espectador",
        }[config_key]
        for role in guild.roles:
            if role.name == role_name:
                await self.admin_repo.set_role(guild.id, mapped_type, role.id)
                return role

        try:
            role = await guild.create_role(
                name=role_name,
                colour=color,
                mentionable=False,
                reason="Criacao automatica dos cargos do Carrossel",
            )
        except (discord.Forbidden, discord.HTTPException):
            return None
        await self.admin_repo.set_role(guild.id, mapped_type, role.id)
        return role

    async def _audit_safely(
        self,
        *,
        guild_id: int,
        actor_id: int,
        action: str,
        payload: dict,
    ) -> None:
        try:
            await self.audit_service.log(
                guild_id=guild_id,
                actor_id=actor_id,
                action=action,
                payload=payload,
            )
        except Exception:
            LOGGER.exception("Configuracao salva, mas a auditoria falhou: %s", action)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))
