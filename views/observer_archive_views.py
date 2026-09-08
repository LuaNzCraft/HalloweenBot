from __future__ import annotations

import asyncio
from io import BytesIO
import logging
from typing import TYPE_CHECKING

import discord

from services.observer_archive_service import ObserverArchiveDraft


if TYPE_CHECKING:
    from cogs.observer_archives import ObserverArchivesCog


LOGGER = logging.getLogger(__name__)


class ObserverArchiveEditModal(discord.ui.Modal, title="Editar arquivo do Observador"):
    def __init__(self, archive_view: "ObserverArchivePreviewView") -> None:
        super().__init__(timeout=300)
        self.archive_view = archive_view
        self.message_text = discord.ui.TextInput(
            label="Texto da mensagem",
            style=discord.TextStyle.paragraph,
            default=archive_view.draft.message_text,
            min_length=1,
            max_length=archive_view.max_message_length,
            required=True,
        )
        self.aliases = discord.ui.TextInput(
            label="Nomes aceitos, separados por virgula",
            style=discord.TextStyle.paragraph,
            default=", ".join(archive_view.draft.aliases),
            min_length=1,
            max_length=600,
            required=True,
        )
        self.add_item(self.message_text)
        self.add_item(self.aliases)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.archive_view.moderator_id:
            await interaction.response.send_message("Somente quem abriu a previa pode edita-la.", ephemeral=True)
            return
        if self.archive_view._completed:
            await interaction.response.send_message("Essa previa ja foi encerrada.", ephemeral=True)
            return
        aliases = self.archive_view.cog.archive_service.normalize_aliases(extra=str(self.aliases.value))
        if not aliases:
            await interaction.response.send_message("Informe pelo menos um nome aceito.", ephemeral=True)
            return
        message_text = " ".join(str(self.message_text.value).replace("\x00", "").split())
        if not message_text:
            await interaction.response.send_message("O texto nao pode ficar vazio.", ephemeral=True)
            return
        edited_draft = self.archive_view.draft.edited(
            message_text=message_text,
            aliases=aliases,
        )
        await interaction.response.defer(ephemeral=True)
        try:
            preview = await self.archive_view.cog.archive_service.render_preview(edited_draft)
        except Exception:
            LOGGER.exception("Falha ao atualizar a previa do arquivo %s", edited_draft.public_id)
            await interaction.followup.send("Nao foi possivel gerar a previa editada.", ephemeral=True)
            return
        self.archive_view.draft = edited_draft
        file = discord.File(BytesIO(preview), filename=f"observer_{edited_draft.public_id}_preview.png")
        await interaction.edit_original_response(
            embed=self.archive_view.cog.build_preview_embed(self.archive_view.draft),
            attachments=[file],
            view=self.archive_view,
        )


class ObserverArchivePreviewView(discord.ui.View):
    def __init__(
        self,
        cog: "ObserverArchivesCog",
        *,
        draft: ObserverArchiveDraft,
        moderator_id: int,
        max_message_length: int,
        timeout_seconds: int,
    ) -> None:
        super().__init__(timeout=max(30, int(timeout_seconds)))
        self.cog = cog
        self.draft = draft
        self.moderator_id = int(moderator_id)
        self.max_message_length = int(max_message_length)
        self.message: discord.Message | None = None
        self._completed = False
        self._confirm_lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.moderator_id:
            return True
        await interaction.response.send_message("Somente quem abriu a previa pode usa-la.", ephemeral=True)
        return False

    @discord.ui.button(label="Confirmar arquivo", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self._confirm_lock:
            if self._completed:
                await interaction.followup.send("Essa previa ja foi encerrada.", ephemeral=True)
                return
            try:
                archive = await self.cog.persist_archive(self.draft)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            except Exception:
                LOGGER.exception("Falha ao confirmar arquivo do Observador %s", self.draft.public_id)
                await interaction.followup.send("Nao foi possivel salvar o arquivo.", ephemeral=True)
                return
            self._completed = True
            self.stop()
        embed = discord.Embed(
            title="Arquivo confirmado",
            description=(
                f"O arquivo `{archive['public_id']}` entrou na pool do Observador.\n"
                "Os jogadores responderao digitando livremente o nome do autor."
            ),
            color=discord.Color.green(),
        )
        await interaction.edit_original_response(embed=embed, attachments=[], view=None)

    @discord.ui.button(label="Editar", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ObserverArchiveEditModal(self))

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self._confirm_lock:
            if self._completed:
                await interaction.followup.send("Essa previa ja foi encerrada.", ephemeral=True)
                return
            self._completed = True
            self.stop()
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Arquivamento cancelado",
                    description="Nenhuma pergunta foi criada.",
                    color=discord.Color.red(),
                ),
                attachments=[],
                view=None,
            )

    async def on_timeout(self) -> None:
        if self._completed or self.message is None:
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass
