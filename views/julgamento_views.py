from __future__ import annotations

from typing import TYPE_CHECKING

import discord


if TYPE_CHECKING:
    from cogs.julgamento import JulgamentoCog


class JulgamentoSignupView(discord.ui.View):
    def __init__(self, cog: "JulgamentoCog", guild_id: int, allow_join: bool, allow_spectate: bool) -> None:
        # Signup precisa continuar interativa enquanto o evento estiver vivo; restart e cleanup
        # sao tratados no cog_load/force_reset, entao a view aqui permanece intencionalmente sem timeout.
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.join_button.label = cog._jt("pt-BR", "view.join_label")
        self.spectate_button.label = cog._jt("pt-BR", "view.spectate_label")
        self.translate_button.label = cog._jt("en-US", "view.translate_label")
        self.join_button.disabled = not allow_join
        self.spectate_button.disabled = not allow_spectate

    @discord.ui.button(label="🎟️ Entrar no Carrossel", style=discord.ButtonStyle.danger, custom_id="julgamento_participar")
    async def join_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_join(interaction)

    @discord.ui.button(label="👁️ Entrar como Plateia", style=discord.ButtonStyle.secondary, custom_id="julgamento_espectar")
    async def spectate_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_spectate(interaction)

    @discord.ui.button(label="Translate", style=discord.ButtonStyle.secondary, custom_id="julgamento_translate_signup")
    async def translate_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(self.cog._jt("en-US", "view.translate_guild_only"), ephemeral=True)
            return
        runtime = await self.cog.repo.get_runtime(interaction.guild.id)
        signup_ends_at = self.cog._parse_dt(runtime.get("signup_ends_at"))
        embed = await self.cog._build_signup_embed(
            interaction.guild,
            signup_ends_at,
            signup_open=bool(runtime.get("active")) and str(runtime.get("phase")) == "signup",
            locale="en-US",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class CarrosselTranslateView(discord.ui.View):
    def __init__(self, cog: "JulgamentoCog", question: dict, timeout_seconds: int) -> None:
        super().__init__(timeout=max(1, timeout_seconds))
        self.cog = cog
        self.question = question
        self.timeout_seconds = timeout_seconds
        self.translate_en.label = cog._jt("en-US", "view.translate_label")

    @discord.ui.button(label="Translate", style=discord.ButtonStyle.secondary)
    async def translate_en(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        embed = self.cog._build_question_translation_embed(self.question, self.timeout_seconds)
        if embed is None:
            await interaction.response.send_message(self.cog._jt("en-US", "translation.unavailable"), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)


class StaticTranslateView(discord.ui.View):
    def __init__(self, embed: discord.Embed, timeout: int = 900) -> None:
        super().__init__(timeout=timeout)
        self.embed = embed
        self.translate_en.label = "Translate"

    @discord.ui.button(label="Translate", style=discord.ButtonStyle.secondary)
    async def translate_en(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=self.embed, ephemeral=True)
