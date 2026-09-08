from __future__ import annotations

import discord


def validate_target_user(
    interaction: discord.Interaction,
    target: discord.Member | discord.User | None,
    *,
    require_target: bool,
    i18n=None,
    locale: str = "pt-BR",
) -> str | None:
    def text(key: str) -> str:
        return i18n.t(locale, key) if i18n else key

    if not require_target:
        return None

    if target is None:
        return text("items.needs_target")

    if target.id == interaction.user.id:
        return text("target.self")

    if target.bot:
        return text("target.bot")

    if not interaction.guild:
        return text("target.guild_only")

    if isinstance(target, discord.Member):
        bot_member = interaction.guild.me
        if bot_member is not None and target.top_role >= bot_member.top_role:
            return text("target.hierarchy")

    return None
