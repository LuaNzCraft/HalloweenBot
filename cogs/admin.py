from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from repositories.admin_repo import AdminRepo
from repositories.guild_metrics_repo import GuildMetricsRepo
from repositories.user_guild_metrics_repo import UserGuildMetricsRepo
from repositories.users_repo import UsersRepo
from services.audit_service import AuditService
from services.inventory_service import InventoryService
from services.item_catalog import ItemCatalog


RESOURCE_FIELDS = {
    "doces": ("candies", "Moedas Doce"),
    "moedas_doces": ("candies", "Moedas Doce"),
    "travessas": ("trick_coins", "Moedas Travessas"),
    "moedas_travessas": ("trick_coins", "Moedas Travessas"),
    "dados": ("dice_count", "dados"),
    "xp": ("xp_bank", "XP bank"),
}

RESOURCE_CHOICES = [
    app_commands.Choice(name="Moedas Doce", value="doces"),
    app_commands.Choice(name="Moedas Travessas", value="travessas"),
    app_commands.Choice(name="Dados", value="dados"),
    app_commands.Choice(name="XP bank", value="xp"),
]

ROOT_DIR = Path(__file__).resolve().parent.parent
CLOSURE_DIR = ROOT_DIR / "data" / "closures"


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.admin_repo = AdminRepo()
        self.audit_service = AuditService(bot, self.admin_repo)
        self.users_repo = UsersRepo()
        self.guild_metrics_repo = GuildMetricsRepo()
        self.user_guild_metrics_repo = UserGuildMetricsRepo()
        self.catalog = ItemCatalog()
        self.inventory_service = InventoryService(self.users_repo, self.catalog)

    async def admin_tipo_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        categoria = str(getattr(interaction.namespace, "categoria", "") or "").strip().lower()
        current_lower = current.lower()

        if categoria == "recurso":
            return [
                choice
                for choice in RESOURCE_CHOICES
                if not current_lower or current_lower in choice.name.lower() or current_lower in choice.value.lower()
            ][:25]

        if categoria == "item":
            choices: list[app_commands.Choice[str]] = []
            for item_id, item in self.catalog.grantable_items().items():
                label = f"{item.name} ({item_id})"
                if current_lower and current_lower not in item_id.lower() and current_lower not in item.name.lower():
                    continue
                choices.append(app_commands.Choice(name=label[:100], value=item_id))
                if len(choices) >= 25:
                    break
            return choices

        return []

    @app_commands.command(name="admin", description="Admin: da ou remove recursos e itens de um usuario.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(
        acao=[
            app_commands.Choice(name="Dar", value="dar"),
            app_commands.Choice(name="Remover", value="remover"),
        ],
        categoria=[
            app_commands.Choice(name="Recurso", value="recurso"),
            app_commands.Choice(name="Item", value="item"),
        ],
    )
    @app_commands.describe(
        acao="Escolha se voce quer dar ou remover.",
        categoria="Escolha se o ajuste sera em recurso ou item.",
        tipo="Tipo especifico do recurso ou item.",
        usuario="Usuario que vai receber ou perder o ajuste.",
        quantidade="Quantidade a dar ou remover.",
    )
    @app_commands.autocomplete(tipo=admin_tipo_autocomplete)
    async def admin(
        self,
        interaction: discord.Interaction,
        acao: app_commands.Choice[str],
        categoria: app_commands.Choice[str],
        tipo: str,
        usuario: discord.Member,
        quantidade: app_commands.Range[int, 1, 100000] = 1,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use esse comando dentro de um servidor.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        normalized_tipo = tipo.strip().lower()
        quantity = int(quantidade)

        if categoria.value == "recurso":
            if normalized_tipo not in RESOURCE_FIELDS:
                await interaction.followup.send(
                    "Recurso invalido. Escolha um tipo listado no autocomplete.",
                    ephemeral=True,
                )
                return
            field, label = RESOURCE_FIELDS[normalized_tipo]
            if acao.value == "dar":
                adjusted_quantity = quantity
                total = await self.users_repo.add_currency(usuario.id, field, quantity)
                response = f"Entreguei **{quantity} {label}** para {usuario.mention}. Total atual: **{total}**."
            else:
                adjusted_quantity, total = await self.users_repo.remove_currency_up_to(
                    usuario.id,
                    field,
                    quantity,
                )
                if adjusted_quantity < quantity:
                    response = (
                        f"{usuario.mention} tinha apenas **{adjusted_quantity} {label}**. "
                        f"Removi esse saldo e o total atual e **{total}**."
                    )
                else:
                    response = f"Removi **{adjusted_quantity} {label}** de {usuario.mention}. Total atual: **{total}**."
            if field == "xp_bank" and adjusted_quantity:
                metric_delta = adjusted_quantity if acao.value == "dar" else -adjusted_quantity
                await self.guild_metrics_repo.increment_metric(interaction.guild.id, "xp_bank", metric_delta)
                await self.user_guild_metrics_repo.increment_metric(
                    interaction.guild.id,
                    usuario.id,
                    "xp_bank",
                    metric_delta,
                )
            await interaction.followup.send(response, ephemeral=True)
            await self._audit(
                interaction,
                f"admin.{acao.value}.resource",
                usuario.id,
                {
                    "item": normalized_tipo,
                    "field": field,
                    "requested_quantity": quantity,
                    "quantity": adjusted_quantity,
                    "total": total,
                },
            )
            return

        if categoria.value == "item":
            item_def = self.catalog.get(normalized_tipo)
            if item_def is None or not item_def.grantable:
                await interaction.followup.send(
                    "Item invalido. Escolha um item listado no autocomplete.",
                    ephemeral=True,
                )
                return

            if acao.value == "dar":
                total = await self.inventory_service.grant_item(usuario.id, normalized_tipo, quantity)
                await interaction.followup.send(
                    f"Entreguei **{item_def.name} x{quantity}** para {usuario.mention}. Total no inventario: **{total}**.",
                    ephemeral=True,
                )
            else:
                try:
                    total = await self.users_repo.remove_inventory_item(usuario.id, normalized_tipo, quantity)
                except ValueError:
                    await interaction.followup.send("O usuario nao tem itens suficientes.", ephemeral=True)
                    return
                await interaction.followup.send(
                    f"Removi **{item_def.name} x{quantity}** de {usuario.mention}. Restante: **{total}**.",
                    ephemeral=True,
                )

            await self._audit(
                interaction,
                f"admin.{acao.value}.item",
                usuario.id,
                {"item": normalized_tipo, "quantity": quantity, "total": total},
            )
            return

        await interaction.followup.send("Categoria desconhecida.", ephemeral=True)

    @app_commands.command(name="fechamento", description="Admin: exporta e zera o XP bank do evento.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def fechamento(
        self,
        interaction: discord.Interaction,
        confirmar: bool = False,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use esse comando dentro de um servidor.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        member_ids = {member.id for member in interaction.guild.members}
        if confirmar:
            xp_rows = await self.users_repo.close_xp_bank(
                guild_id=interaction.guild.id,
                member_ids=member_ids,
            )
        else:
            xp_rows = await self.users_repo.list_xp_bank(
                only_nonzero=True,
                guild_id=interaction.guild.id,
                member_ids=member_ids,
            )
        if not xp_rows:
            await interaction.followup.send("Nao ha XP bank para fechar no momento.", ephemeral=True)
            return

        total_xp_bank = sum(row["xp_bank"] for row in xp_rows)
        if not confirmar:
            preview_lines = [
                f"Jogadores com XP: **{len(xp_rows)}**",
                f"XP total a exportar: **{total_xp_bank}**",
                "Nenhum valor foi zerado ainda.",
                "Rode novamente com `confirmar:true` para executar o fechamento.",
            ]
            await interaction.followup.send("\n".join(preview_lines), ephemeral=True)
            return

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        command_lines = [
            f"/modifyexp add member:<@{row['user_id']}> amount:{row['xp_bank']}"
            for row in xp_rows
        ]
        commands_text = "\n".join(command_lines) + "\n"
        backup_payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "guild_id": interaction.guild.id,
            "entries": xp_rows,
            "total_xp_bank": total_xp_bank,
        }

        CLOSURE_DIR.mkdir(parents=True, exist_ok=True)
        commands_path = CLOSURE_DIR / f"xp_bank_commands_{interaction.guild.id}_{timestamp}.txt"
        backup_path = CLOSURE_DIR / f"xp_bank_backup_{interaction.guild.id}_{timestamp}.json"
        commands_path.write_text(commands_text, encoding="utf-8")
        backup_path.write_text(json.dumps(backup_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        reset_count = len(xp_rows)
        await self._send_closure_files_to_log(
            interaction.guild.id,
            commands_text,
            json.dumps(backup_payload, ensure_ascii=False, indent=2),
            commands_path.name,
            backup_path.name,
        )
        await self._audit(
            interaction,
            "admin.closure.xp_bank",
            interaction.user.id,
            {
                "entries": len(xp_rows),
                "total_xp_bank": backup_payload["total_xp_bank"],
                "commands_file": commands_path.name,
                "backup_file": backup_path.name,
                "reset_count": reset_count,
            },
        )
        await interaction.followup.send(
            (
                f"Fechamento concluido. Exportei **{len(xp_rows)}** jogador(es), "
                f"backup salvo em `{backup_path.name}` e zerei o XP bank atual."
            ),
            ephemeral=True,
        )

    async def _audit(
        self,
        interaction: discord.Interaction,
        action: str,
        target_id: int,
        payload: dict,
    ) -> None:
        await self.audit_service.log(
            guild_id=interaction.guild.id if interaction.guild else None,
            actor_id=interaction.user.id,
            target_id=target_id,
            action=action,
            payload=payload,
        )

    async def _send_closure_files_to_log(
        self,
        guild_id: int,
        commands_text: str,
        backup_text: str,
        commands_filename: str,
        backup_filename: str,
    ) -> None:
        config = await self.admin_repo.get_config(guild_id)
        log_channel_id = config.get("log_channel_id")
        if not log_channel_id:
            return

        channel = self.bot.get_channel(int(log_channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(log_channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        files = [
            discord.File(io.BytesIO(commands_text.encode("utf-8")), filename=commands_filename),
            discord.File(io.BytesIO(backup_text.encode("utf-8")), filename=backup_filename),
        ]
        try:
            await channel.send(
                content="Fechamento do XP bank concluido. Arquivos de exportacao e backup em anexo.",
                files=files,
            )
        except discord.HTTPException:
            return


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
