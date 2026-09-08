from __future__ import annotations

from io import BytesIO
import logging
from pathlib import Path
import re

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from repositories.observer_archives_repo import ObserverArchivesRepo
from services.audit_service import AuditService
from services.config_service import get_public_events_config
from services.observer_archive_service import ObserverArchiveDraft, ObserverArchiveService
from views.observer_archive_views import ObserverArchivePreviewView


ROOT_DIR = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger(__name__)
ARCHIVE_COLOR = discord.Color.from_rgb(120, 12, 12)
PUBLIC_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class ObserverArchivesCog(commands.GroupCog, name="observador"):
    arquivo = app_commands.Group(
        name="arquivo",
        description="Administra as mensagens arquivadas pelo Observador.",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.repo = ObserverArchivesRepo()
        self.admin_repo = AdminRepo()
        self.audit_service = AuditService(bot, self.admin_repo)
        self.archive_service = ObserverArchiveService(ROOT_DIR)

    async def cog_load(self) -> None:
        await self.repo.init_db()

    def _archives_config(self) -> dict:
        observer_cfg = dict(get_public_events_config().get("observer") or {})
        return dict(observer_cfg.get("archives") or {})

    @staticmethod
    def _can_manage_guild(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(getattr(permissions, "manage_guild", False))

    @arquivo.command(
        name="adicionar",
        description="Transforma uma mensagem do servidor em pergunta do Observador.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        mensagem="Link da mensagem que sera arquivada.",
        aliases="Nomes adicionais aceitos, separados por virgula.",
    )
    async def arquivar_mensagem(
        self,
        interaction: discord.Interaction,
        mensagem: str,
        aliases: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not self._can_manage_guild(interaction):
            await interaction.response.send_message("Voce precisa de Gerenciar Servidor para arquivar mensagens.", ephemeral=True)
            return

        archives_cfg = self._archives_config()
        if not archives_cfg.get("enabled", True):
            await interaction.response.send_message("Os Arquivos do Observador estao desativados na configuracao.", ephemeral=True)
            return

        parsed = self.archive_service.parse_message_link(mensagem)
        if parsed is None:
            await interaction.response.send_message("Envie um link valido de mensagem do Discord.", ephemeral=True)
            return
        link_guild_id, channel_id, message_id = parsed
        if link_guild_id != guild.id:
            await interaction.response.send_message("A mensagem precisa pertencer a este servidor.", ephemeral=True)
            return
        if await self.repo.get_by_source_message(guild.id, message_id) is not None:
            await interaction.response.send_message("Essa mensagem ja foi arquivada.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        source_channel = guild.get_channel_or_thread(channel_id)
        if source_channel is None:
            try:
                source_channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                source_channel = None
        if not isinstance(source_channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("Nao consegui acessar o canal da mensagem.", ephemeral=True)
            return
        if source_channel.guild.id != guild.id:
            await interaction.followup.send("A mensagem precisa pertencer a este servidor.", ephemeral=True)
            return
        try:
            source_message = await source_channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await interaction.followup.send("Nao consegui acessar essa mensagem.", ephemeral=True)
            return

        avatar_blob: bytes | None = None
        try:
            avatar_blob = await source_message.author.display_avatar.read()
        except (AttributeError, discord.HTTPException, OSError):
            pass
        global_name = str(getattr(source_message.author, "global_name", "") or "")
        combined_aliases = ",".join(value for value in (global_name, aliases or "") if value)
        max_message_length = int(archives_cfg.get("max_message_length", 500))
        try:
            draft = self.archive_service.build_draft(
                guild_id=guild.id,
                source_channel_id=source_channel.id,
                source_message_id=source_message.id,
                author_id=source_message.author.id,
                author_display_name=source_message.author.display_name,
                author_username=source_message.author.name,
                message_text=source_message.content,
                created_by=interaction.user.id,
                avatar_blob=avatar_blob,
                extra_aliases=combined_aliases,
                max_message_length=max_message_length,
            )
            preview = await self.archive_service.render_preview(draft)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            LOGGER.exception("Falha ao montar previa do arquivo guild=%s message=%s", guild.id, message_id)
            await interaction.followup.send("Nao foi possivel gerar a previa do arquivo.", ephemeral=True)
            return

        view = ObserverArchivePreviewView(
            self,
            draft=draft,
            moderator_id=interaction.user.id,
            max_message_length=max_message_length,
            timeout_seconds=int(archives_cfg.get("preview_timeout_seconds", 300)),
        )
        file = discord.File(BytesIO(preview), filename=f"observer_{draft.public_id}_preview.png")
        sent = await interaction.followup.send(
            embed=self.build_preview_embed(draft),
            file=file,
            view=view,
            ephemeral=True,
            wait=True,
        )
        view.message = sent

    @arquivo.command(name="listar", description="Lista os arquivos recentes do Observador.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def listar_arquivos(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not self._can_manage_guild(interaction):
            await interaction.response.send_message(
                "Voce precisa de Gerenciar Servidor para listar os arquivos.",
                ephemeral=True,
            )
            return
        archives = await self.repo.list_all(interaction.guild.id, limit=20)
        if not archives:
            await interaction.response.send_message("Ainda nao existem arquivos neste servidor.", ephemeral=True)
            return
        lines = []
        for archive in archives:
            status = "ativo" if archive["active"] else "inativo"
            excerpt = discord.utils.escape_markdown(str(archive["message_text"]).replace("\n", " ")[:70])
            author = discord.utils.escape_markdown(str(archive["author_display_name"]))
            lines.append(
                f"`{archive['public_id']}` · **{status}** · {author}\n> {excerpt}"
            )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Arquivos do Observador",
                description="\n\n".join(lines),
                color=ARCHIVE_COLOR,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @arquivo.command(name="status", description="Ativa ou desativa um arquivo do Observador.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(arquivo="ID exibido em /observador arquivo listar.", ativo="Novo estado do arquivo.")
    async def definir_status(self, interaction: discord.Interaction, arquivo: str, ativo: bool) -> None:
        if interaction.guild is None or not self._can_manage_guild(interaction):
            await interaction.response.send_message(
                "Voce precisa de Gerenciar Servidor para alterar os arquivos.",
                ephemeral=True,
            )
            return
        public_id = arquivo.strip().lower()
        if PUBLIC_ID_RE.fullmatch(public_id) is None:
            await interaction.response.send_message("Informe um ID de arquivo valido.", ephemeral=True)
            return
        changed = await self.repo.set_active(interaction.guild.id, public_id, ativo)
        if not changed:
            await interaction.response.send_message("Arquivo nao encontrado neste servidor.", ephemeral=True)
            return
        try:
            await self.audit_service.log(
                guild_id=interaction.guild.id,
                actor_id=interaction.user.id,
                action="observer.archive.status_changed",
                payload={"public_id": public_id, "active": ativo},
            )
        except Exception:
            LOGGER.exception("Status do arquivo %s alterado, mas a auditoria falhou", public_id)
        await interaction.response.send_message(
            f"Arquivo `{public_id}` agora esta **{'ativo' if ativo else 'inativo'}**.",
            ephemeral=True,
        )

    def build_preview_embed(self, draft: ObserverArchiveDraft) -> discord.Embed:
        embed = discord.Embed(
            title="Previa do Arquivo do Observador",
            description=(
                "O card publico esconde completamente nome e avatar.\n"
                "Confirme para adicionar a mensagem a pool `chat_quote`."
            ),
            color=ARCHIVE_COLOR,
        )
        embed.add_field(
            name="Autor capturado",
            value=discord.utils.escape_markdown(draft.author_display_name),
            inline=True,
        )
        embed.add_field(name="Arquivo", value=f"`{draft.public_id}`", inline=True)
        embed.add_field(
            name="Respostas aceitas",
            value=discord.utils.escape_markdown(", ".join(draft.aliases)),
            inline=False,
        )
        embed.set_image(url=f"attachment://observer_{draft.public_id}_preview.png")
        return embed

    async def persist_archive(self, draft: ObserverArchiveDraft) -> dict:
        archives_cfg = self._archives_config()
        cache_directory = str(archives_cfg.get("cache_directory") or "cache/observer_archives")
        if await self.repo.get_by_source_message(draft.guild_id, draft.source_message_id) is not None:
            raise ValueError("Essa mensagem ja foi arquivada.")
        hidden_path, revealed_path = await self.archive_service.render_archive_pair(draft, cache_directory)
        try:
            archive = await self.repo.create_archive(
                {
                    **{field: getattr(draft, field) for field in draft.__dataclass_fields__},
                    "aliases": list(draft.aliases),
                    "hidden_image_path": hidden_path,
                    "revealed_image_path": revealed_path,
                }
            )
        except aiosqlite.IntegrityError as exc:
            await self.archive_service.remove_cached_images((hidden_path, revealed_path), cache_directory)
            raise ValueError("Essa mensagem ja foi arquivada.") from exc
        except Exception:
            await self.archive_service.remove_cached_images((hidden_path, revealed_path), cache_directory)
            raise
        try:
            await self.audit_service.log(
                guild_id=draft.guild_id,
                actor_id=draft.created_by,
                target_id=draft.author_id,
                action="observer.archive.created",
                payload={
                    "public_id": draft.public_id,
                    "source_channel_id": draft.source_channel_id,
                    "source_message_id": draft.source_message_id,
                    "aliases": list(draft.aliases),
                },
            )
        except Exception:
            LOGGER.exception("Arquivo %s salvo, mas o registro de auditoria falhou", draft.public_id)
        return archive


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ObserverArchivesCog(bot))
