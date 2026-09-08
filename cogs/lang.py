from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.i18n_service import I18nService


class LangCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.i18n = I18nService()

    @app_commands.command(name="lang", description="Escolhe o idioma das mensagens do bot.")
    @app_commands.choices(
        idioma=[
            app_commands.Choice(name="Português", value="pt-BR"),
            app_commands.Choice(name="English", value="en-US"),
        ]
    )
    async def lang(
        self,
        interaction: discord.Interaction,
        idioma: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if idioma is None:
            locale = await self.i18n.get_user_locale(interaction.user.id)
            await interaction.followup.send(
                self.i18n.t(locale, "lang.current", language=self.i18n.label(locale)),
                ephemeral=True,
            )
            return

        await self.i18n.set_user_locale(interaction.user.id, idioma.value)
        await interaction.followup.send(
            self.i18n.t(idioma.value, "lang.updated", language=self.i18n.label(idioma.value)),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LangCog(bot))
