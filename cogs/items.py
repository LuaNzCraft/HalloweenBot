from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands, tasks

from repositories.admin_repo import AdminRepo
from repositories.effects_repo import EffectsRepo
from repositories.guild_metrics_repo import GuildMetricsRepo
from repositories.julgamento_repo import JulgamentoRepo
from repositories.public_events_repo import PublicEventsRepo
from repositories.user_guild_metrics_repo import UserGuildMetricsRepo
from services.audit_service import AuditService
from services.inventory_service import InventoryService
from services.i18n_service import I18nService
from services.item_catalog import ItemCatalog
from services.items_runtime import ItemsRuntimeService
from services.items_texts import ItemsTextService
from services.mission_service import MissionService
from services.profile_service import ProfileService
from utils.event_config import get_effects_config
from utils.target_validation import validate_target_user


ITEM_ENGLISH_LABELS = {
    "mute": "Mute",
    "switch": "Switch",
    "interferencia": "Interference",
    "observer_hint": "Observer Hint",
    "ticket_carrossel": "Carousel Ticket",
}


class ItemsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.catalog = ItemCatalog()
        self.i18n = I18nService()
        self.admin_repo = AdminRepo()
        self.effects_repo = EffectsRepo()
        self.public_events_repo = PublicEventsRepo()
        self.julgamento_repo = JulgamentoRepo()
        self.audit_service = AuditService(bot)
        self.inventory_service = InventoryService(
            item_catalog=self.catalog,
            guild_metrics_repo=GuildMetricsRepo(),
            user_guild_metrics_repo=UserGuildMetricsRepo(),
            i18n_service=self.i18n,
        )
        self.profile_service = ProfileService(item_catalog=self.catalog, i18n_service=self.i18n)
        self.mission_service = MissionService(
            users_repo=self.inventory_service.users_repo,
            i18n_service=self.i18n,
        )
        self.effects_config = get_effects_config()
        self.texts = ItemsTextService(self.i18n)
        self.runtime = ItemsRuntimeService(
            bot=bot,
            i18n=self.i18n,
            admin_repo=self.admin_repo,
            effects_repo=self.effects_repo,
            public_events_repo=self.public_events_repo,
            julgamento_repo=self.julgamento_repo,
            audit_service=self.audit_service,
            inventory_service=self.inventory_service,
            mission_service=self.mission_service,
            effects_config=self.effects_config,
            texts=self.texts,
        )
        self._switch_cleanup_loop.start()

    def cog_unload(self) -> None:
        self._switch_cleanup_loop.cancel()

    def reload_config(self) -> None:
        self.effects_config = get_effects_config()
        self.runtime.effects_config = self.effects_config

    async def item_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current_lower = current.lower()
        choices: list[app_commands.Choice[str]] = []
        for item_id, item in self.catalog.usable_items().items():
            english_label = ITEM_ENGLISH_LABELS.get(
                item_id,
                item_id.replace("_", " ").title(),
            )
            label = f"{item.name} ({english_label})"
            if (
                current_lower
                and current_lower not in item_id.lower()
                and current_lower not in item.name.lower()
                and current_lower not in english_label.lower()
            ):
                continue
            choices.append(app_commands.Choice(name=label[:100], value=item_id))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(name="usar", description="Usa um item do seu inventario.")
    @app_commands.describe(item_id="ID interno do item em ingles. Use o autocomplete.")
    @app_commands.autocomplete(item_id=item_autocomplete)
    async def usar(
        self,
        interaction: discord.Interaction,
        item_id: str,
        alvo: discord.Member | None = None,
    ) -> None:
        normalized_item_id = item_id.strip().lower()
        item_def = self.catalog.get(normalized_item_id)
        if item_def is None or not item_def.grantable or not item_def.usable:
            locale = await self.i18n.get_user_locale(interaction.user.id)
            await interaction.response.send_message(self.i18n.t(locale, "items.unknown"), ephemeral=True)
            return

        locale = await self.i18n.get_user_locale(interaction.user.id)
        if normalized_item_id == "mute":
            if alvo is None:
                await interaction.response.send_message(self.i18n.t(locale, "items.needs_target"), ephemeral=True)
                return
            if alvo.id == interaction.user.id:
                await interaction.response.send_message(self.i18n.t(locale, "target.self"), ephemeral=True)
                return
            if not interaction.guild:
                await interaction.response.send_message(self.i18n.t(locale, "target.guild_only"), ephemeral=True)
                return
            mute_guardrail = await self.runtime.get_mute_guardrail_reason(interaction.guild.id, locale)
            if mute_guardrail:
                await interaction.response.send_message(mute_guardrail, ephemeral=True)
                return
            await self.runtime.handle_mute(interaction, alvo, locale)
            return

        if normalized_item_id == "switch":
            target_error = validate_target_user(
                interaction,
                alvo,
                require_target=True,
                i18n=self.i18n,
                locale=locale,
            )
            if target_error:
                await interaction.response.send_message(target_error, ephemeral=True)
                return
            if alvo is None or not interaction.guild:
                await interaction.response.send_message(self.i18n.t(locale, "target.guild_only"), ephemeral=True)
                return
            await self.runtime.handle_switch(interaction, alvo, locale)
            return

        if normalized_item_id == "interferencia":
            target_error = validate_target_user(
                interaction,
                alvo,
                require_target=True,
                i18n=self.i18n,
                locale=locale,
            )
            if target_error:
                await interaction.response.send_message(target_error, ephemeral=True)
                return
            if alvo is None or not interaction.guild:
                await interaction.response.send_message(self.i18n.t(locale, "target.guild_only"), ephemeral=True)
                return
            guardrail = await self.runtime.get_interference_guardrail_reason(interaction.guild.id, locale)
            if guardrail:
                await interaction.response.send_message(guardrail, ephemeral=True)
                return
            await self.runtime.handle_interference(interaction, alvo, locale)
            return

        if normalized_item_id == "observer_hint":
            await self.runtime.handle_observer_hint(interaction, locale)
            return

        target_error = validate_target_user(
            interaction,
            alvo,
            require_target=item_def.requires_target,
            i18n=self.i18n,
            locale=locale,
        )
        if target_error:
            await interaction.response.send_message(target_error, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        result = await self.inventory_service.use_item(
            interaction.user.id,
            normalized_item_id,
            target_id=alvo.id if alvo else None,
            guild_id=interaction.guild.id if interaction.guild else None,
        )
        await interaction.followup.send(result.message, ephemeral=not result.public)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if await self.runtime.handle_admin_mute_message(message):
            return
        await self.runtime.handle_interference_message(message)
        await self.runtime.handle_switch_message(message)

    @tasks.loop(seconds=15)
    async def _switch_cleanup_loop(self) -> None:
        await self.runtime.cleanup_expired_switches()
        await self.runtime.cleanup_expired_interferencias()
        await self.runtime.cleanup_expired_admin_mutes()

    @_switch_cleanup_loop.before_loop
    async def _before_switch_cleanup_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self.runtime.cleanup_expired_switches()
        await self.runtime.cleanup_expired_interferencias()
        await self.runtime.cleanup_expired_admin_mutes()
        await self.runtime.reconcile_interferencia_roles()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ItemsCog(bot))
