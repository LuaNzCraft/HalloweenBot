from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands

from repositories.users_repo import UsersRepo


def _resolve_daily_dice_timezone() -> timezone | ZoneInfo:
    try:
        return ZoneInfo("America/Sao_Paulo")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=-3))


DAILY_DICE_TIMEZONE = _resolve_daily_dice_timezone()


class DailyDiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.users_repo = UsersRepo()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        if not isinstance(message.author, discord.Member):
            return

        date_key = datetime.now(DAILY_DICE_TIMEZONE).date().isoformat()
        await self.users_repo.grant_daily_dice_if_needed(message.author.id, date_key, amount=1)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DailyDiceCog(bot))
