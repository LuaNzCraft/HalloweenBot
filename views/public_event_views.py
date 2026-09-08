from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord


if TYPE_CHECKING:
    from cogs.public_events import PublicEventsCog


CUSTOM_EMOJI_RE = re.compile(r"^<(?P<animated>a)?:(?P<name>[A-Za-z0-9_]+):(?P<id>\d+)>$")
logger = logging.getLogger(__name__)


class ObserverTranslateView(discord.ui.View):
    def __init__(self, cog: "PublicEventsCog", question: dict, deadline_at: datetime) -> None:
        timeout_seconds = max(1, int((deadline_at - discord.utils.utcnow()).total_seconds()))
        super().__init__(timeout=timeout_seconds)
        self.cog = cog
        self.question = question
        self.deadline_at = deadline_at
        self._user_cooldowns: dict[int, datetime] = {}

    @discord.ui.button(label="Translate", style=discord.ButtonStyle.secondary)
    async def translate_en(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        now = discord.utils.utcnow()
        available_at = self._user_cooldowns.get(interaction.user.id)
        if available_at is not None and available_at > now:
            await interaction.response.send_message(
                f"Espere {discord.utils.format_dt(available_at, style='R')} para usar Translate de novo.",
                ephemeral=True,
            )
            return
        embed = self.cog.build_observer_translation_embed(self.question, self.deadline_at)
        if embed is None:
            await interaction.response.send_message(
                "This question does not have an English version yet.",
                ephemeral=True,
            )
            return
        self._user_cooldowns[interaction.user.id] = now + timedelta(seconds=30)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class PublicEventTranslateView(discord.ui.View):
    def __init__(self, embed: discord.Embed, *, cooldown_seconds: int = 30, timeout: float | None = 180) -> None:
        super().__init__(timeout=timeout)
        self.embed = embed
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self._user_cooldowns: dict[int, datetime] = {}

    @discord.ui.button(label="Translate", style=discord.ButtonStyle.secondary)
    async def translate_en(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        now = discord.utils.utcnow()
        available_at = self._user_cooldowns.get(interaction.user.id)
        if available_at is not None and available_at > now:
            await interaction.response.send_message(
                f"Espere {discord.utils.format_dt(available_at, style='R')} para usar Translate de novo.",
                ephemeral=True,
            )
            return
        self._user_cooldowns[interaction.user.id] = now + timedelta(seconds=self.cooldown_seconds)
        await interaction.response.send_message(embed=self.embed, ephemeral=True)


class ChatBonusOptionButton(discord.ui.Button):
    def __init__(self, option_cfg: dict, *, allow_emoji: bool = True) -> None:
        label = str(option_cfg.get("label") or "Opcao")
        emoji = self._parse_emoji(str(option_cfg.get("emoji") or "").strip()) if allow_emoji else None
        super().__init__(label=label, style=discord.ButtonStyle.success, emoji=emoji)
        self.option_cfg = option_cfg

    @staticmethod
    def _parse_emoji(value: str) -> str | discord.PartialEmoji | None:
        if not value:
            return None
        match = CUSTOM_EMOJI_RE.match(value)
        if not match:
            return value
        return discord.PartialEmoji(
            name=match.group("name"),
            id=int(match.group("id")),
            animated=bool(match.group("animated")),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ChatMessageBonusView):
            logger.warning(
                "chat_bonus: button callback without expected view user=%s option=%s view=%s",
                interaction.user.id,
                self.option_cfg.get("id"),
                type(view).__name__ if view is not None else None,
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Falha ao embarcar no Trem Fantasma.",
                    ephemeral=True,
                )
            return
        try:
            await view.handle_option_claim(interaction, self.option_cfg)
        except Exception:
            logger.exception(
                "chat_bonus: button callback crashed guild=%s user=%s option=%s",
                getattr(interaction.guild, "id", None),
                interaction.user.id,
                self.option_cfg.get("id"),
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Falha ao embarcar no Trem Fantasma.",
                    ephemeral=True,
                )


class ChatBonusSeparatorButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="ou", style=discord.ButtonStyle.secondary, disabled=True)


class ChatMessageBonusView(discord.ui.View):
    def __init__(
        self,
        cog: "PublicEventsCog",
        *,
        guild_id: int,
        bonus_cfg: dict,
        options: list[dict],
        locale: str = "pt-BR",
        timeout_seconds: int = 30,
        allow_emoji: bool = True,
    ) -> None:
        super().__init__(timeout=max(1, int(timeout_seconds)))
        self.cog = cog
        self.guild_id = guild_id
        self.bonus_cfg = bonus_cfg
        self.options = options
        self.locale = locale
        self.message: discord.Message | None = None
        self.active = True
        self.claimed_user_ids: set[int] = set()
        self._claim_lock = asyncio.Lock()

        visible_options = options[:2]
        if len(visible_options) == 2:
            self.add_item(ChatBonusOptionButton(visible_options[0], allow_emoji=allow_emoji))
            self.add_item(ChatBonusSeparatorButton())
            self.add_item(ChatBonusOptionButton(visible_options[1], allow_emoji=allow_emoji))
        else:
            for option in visible_options:
                self.add_item(ChatBonusOptionButton(option, allow_emoji=allow_emoji))

    async def on_timeout(self) -> None:
        self.active = False
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def handle_option_claim(self, interaction: discord.Interaction, option_cfg: dict) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
            locale = await self.cog.i18n.get_user_locale(interaction.user.id)
            async with self._claim_lock:
                if not self.active:
                    await interaction.followup.send(
                        self.cog.i18n.t(locale, "event.chat_bonus.expired"),
                        ephemeral=True,
                    )
                    return
                if interaction.user.id in self.claimed_user_ids:
                    await interaction.followup.send(
                        self.cog.i18n.t(locale, "event.chat_bonus.already_claimed"),
                        ephemeral=True,
                    )
                    return
                if self.message is None:
                    raise RuntimeError("chat bonus message is not available")
                summary = await self.cog._grant_chat_bonus_rewards(
                    interaction.user.id,
                    self.guild_id,
                    option_cfg,
                    locale=locale,
                    settlement_key=f"chat_bonus:{self.message.id}:{interaction.user.id}",
                )
                self.claimed_user_ids.add(interaction.user.id)

            await interaction.followup.send(
                self.cog.i18n.t(locale, "event.chat_bonus.claim_success", summary=summary),
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                "chat_bonus: claim failed guild=%s user=%s option=%s",
                self.guild_id,
                interaction.user.id,
                option_cfg.get("id"),
            )
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Falha ao embarcar no Trem Fantasma.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Falha ao embarcar no Trem Fantasma.",
                    ephemeral=True,
                )
