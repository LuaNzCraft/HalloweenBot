from __future__ import annotations

import discord


PROFILE_COLOR = discord.Color.from_rgb(245, 122, 39)
SWEET_COINS_EMOJI = "<:sweetcoins:1496365065773781033>"
TRICK_COINS_EMOJI = "<:trickcoins:1496364971398008932>"
BASKET_NORMAL_EMOJI = "🎃"
BASKET_EPIC_EMOJI = "✨"
BASKET_CURSED_EMOJI = "🕷️"


def _section_title(label: str) -> str:
    return f"━━ {label} ━━"


def _decorate_inventory_line(line: str, text) -> str:
    basket_labels = (
        (text("item.cesta_abobora.name"), BASKET_NORMAL_EMOJI),
        (text("item.cesta_doce_epica.name"), BASKET_EPIC_EMOJI),
        (text("item.cesta_doce_amaldicoada.name"), BASKET_CURSED_EMOJI),
    )
    for label, emoji in basket_labels:
        if not label or label not in line:
            continue
        prefix = line.split(label, 1)[0].strip()
        if prefix:
            return line
        if emoji in line:
            return line
        return line.replace(label, f"{emoji} {label}", 1)
    return line


def _inventory_block(profile: dict, text) -> str:
    summary = str(profile.get("inventory_summary") or text("inventory.empty"))
    lines = [_decorate_inventory_line(line.strip(), text) for line in summary.splitlines() if line.strip()]
    if not lines:
        lines = [text("inventory.empty")]
    return f"```md\n{chr(10).join(lines)}\n```"


def build_profile_embed(member: discord.abc.User, profile: dict, i18n=None, locale: str = "pt-BR") -> discord.Embed:
    def text(key: str, **kwargs) -> str:
        return i18n.t(locale, key, **kwargs) if i18n else key

    embed = discord.Embed(
        title=text("profile.title", name=member.display_name),
        description=text("profile.description"),
        color=PROFILE_COLOR,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name=f"\u2728 {text('profile.xp_bank')}", value=f"**{profile['xp_bank']}**", inline=True)
    embed.add_field(name=f"{SWEET_COINS_EMOJI} {text('profile.candies')}", value=f"**{profile['candies']}**", inline=True)
    embed.add_field(name=f"{TRICK_COINS_EMOJI} {text('profile.trick_coins')}", value=f"**{profile['trick_coins']}**", inline=True)
    embed.add_field(name=f"\U0001F3B2 {text('profile.dice')}", value=f"**{profile['dice_count']}**", inline=True)
    embed.add_field(name="\U0001F3E0 Casa", value=f"**{profile.get('board_position', 0)}**", inline=True)
    embed.add_field(name=f"\U0001F501 {text('profile.laps')}", value=f"**{profile['board_laps']}**", inline=True)
    embed.add_field(
        name=_section_title(text("profile.stats")),
        value=(
            f"**{text('profile.candies_total')}**: {profile['candies_total']}\n"
            f"**{text('profile.tricks_total')}**: {profile['tricks_applied_total']}"
        ),
        inline=False,
    )
    embed.add_field(
        name=_section_title(text("profile.inventory")),
        value=_inventory_block(profile, text),
        inline=False,
    )
    embed.set_footer(text=text("profile.footer"))
    return embed


def build_public_profile_embed(member: discord.abc.User, profile: dict, i18n=None, locale: str = "pt-BR") -> discord.Embed:
    def text(key: str, **kwargs) -> str:
        return i18n.t(locale, key, **kwargs) if i18n else key

    embed = discord.Embed(
        title=text("profile.public_title", name=member.display_name),
        description=text("profile.public_description"),
        color=PROFILE_COLOR,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name=f"\u2728 {text('profile.xp_bank')}", value=f"**{profile['xp_bank']}**", inline=True)
    embed.add_field(name=f"{SWEET_COINS_EMOJI} {text('profile.candies')}", value=f"**{profile['candies']}**", inline=True)
    embed.add_field(name=f"{TRICK_COINS_EMOJI} {text('profile.trick_coins')}", value=f"**{profile['trick_coins']}**", inline=True)
    embed.add_field(name=f"\U0001F3B2 {text('profile.dice')}", value=f"**{profile['dice_count']}**", inline=True)
    embed.add_field(name="\U0001F3E0 Casa", value=f"**{profile.get('board_position', 0)}**", inline=True)
    embed.add_field(name=f"\U0001F501 {text('profile.laps')}", value=f"**{profile['board_laps']}**", inline=True)
    embed.add_field(
        name=_section_title(text("profile.stats")),
        value=(
            f"**{text('profile.candies_total')}**: {profile['candies_total']}\n"
            f"**{text('profile.tricks_total')}**: {profile['tricks_applied_total']}"
        ),
        inline=False,
    )
    embed.set_footer(text="Use o botao abaixo para fazer um pedido quando o jogador estiver ativo no evento.")
    return embed
