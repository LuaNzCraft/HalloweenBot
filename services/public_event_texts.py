from __future__ import annotations

from datetime import datetime, timedelta, timezone

import discord


class PublicEventTextsService:
    def build_chat_candy_spawn_embed(
        self,
        color: discord.Color,
        *,
        target_emoji: str,
        reward_candies: int,
        expires_at: datetime,
        option_count: int,
    ) -> discord.Embed:
        reward_label = "doce" if reward_candies == 1 else "doces"
        embed = discord.Embed(
            title="🍬 Caça aos Doces!",
            description=(
                "Encontre o doce indicado entre as reacoes abaixo.\n\n"
                f"# {target_emoji}\n\n"
                "Escolha com cuidado: voce possui **uma tentativa** nesta aparicao."
            ),
            color=color,
        )
        embed.add_field(name="Opcoes", value=str(option_count), inline=True)
        embed.add_field(name="Premio", value=f"{reward_candies} {reward_label}", inline=True)
        embed.add_field(name="Desaparece", value=discord.utils.format_dt(expires_at, style="R"), inline=True)
        embed.set_footer(text="O primeiro jogador a escolher o doce correto vence.")
        return embed

    def build_chat_candy_winner_embed(
        self,
        color: discord.Color,
        *,
        winner_mention: str,
        reward_summary: str,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="🍬 Doce capturado!",
            description=f"{winner_mention} encontrou o doce correto antes de todo mundo.",
            color=color,
        )
        embed.add_field(name="Premio", value=reward_summary, inline=False)
        embed.set_footer(text="Continue conversando: outro doce pode aparecer a qualquer momento.")
        return embed

    def build_chat_candy_timeout_embed(self, color: discord.Color) -> discord.Embed:
        embed = discord.Embed(
            title="O doce desapareceu...",
            description="Ninguem encontrou o doce correto a tempo.",
            color=color,
        )
        embed.set_footer(text="Continue atento ao chat para a proxima aparicao.")
        return embed

    def build_chat_candy_cancelled_embed(self, color: discord.Color) -> discord.Embed:
        return discord.Embed(
            title="O doce foi recolhido",
            description="Esta aparicao foi encerrada junto com o evento.",
            color=color,
        )

    def build_spider_announcement_embed(self, color: discord.Color, starts_at: datetime) -> discord.Embed:
        embed = discord.Embed(
            title="Uma Chuva de Doces esta chegando...",
            description=(
                "Uma chuva de doces esta prestes a comecar no chat do evento.\n\n"
                "Fique atento: algumas mensagens podem receber doces para capturar por reacao."
            ),
            color=color,
        )
        embed.add_field(name="Horario", value=discord.utils.format_dt(starts_at, style="F"), inline=False)
        embed.add_field(name="Falta", value=discord.utils.format_dt(starts_at, style="R"), inline=False)
        embed.add_field(
            name="Como funciona",
            value="Reaja nos doces antes que eles sumam. Cada captura conta para sua recompensa final.",
            inline=False,
        )
        return embed

    def build_spider_announcement_embed_en(self, color: discord.Color, starts_at: datetime) -> discord.Embed:
        embed = discord.Embed(
            title="A Candy Shower is coming...",
            description=(
                "A candy shower is about to begin in the event chat.\n\n"
                "Stay alert: some messages may receive candy drops for players to capture with reactions."
            ),
            color=color,
        )
        embed.add_field(name="Starts at", value=discord.utils.format_dt(starts_at, style="F"), inline=False)
        embed.add_field(name="In", value=discord.utils.format_dt(starts_at, style="R"), inline=False)
        embed.add_field(
            name="How it works",
            value="React to candy drops before they disappear. Every capture counts toward your final reward.",
            inline=False,
        )
        return embed

    def build_spider_start_embed(self, color: discord.Color, *, ends_at: datetime) -> discord.Embed:
        embed = discord.Embed(
            title="Chuva de Doces iniciada",
            description=(
                "Os doces ja estao caindo pelo chat do evento.\n\n"
                "Reaja rapido quando eles aparecerem para garantir capturas antes que desaparecam."
            ),
            color=color,
        )
        embed.add_field(name="Termina", value=discord.utils.format_dt(ends_at, style="R"), inline=True)
        embed.add_field(name="Dica", value="Quanto mais gente ativa no chat, mais movimento o evento gera.", inline=True)
        return embed

    def build_spider_start_embed_en(self, color: discord.Color, *, ends_at: datetime) -> discord.Embed:
        embed = discord.Embed(
            title="Candy Shower started",
            description=(
                "Candy is already dropping through the event chat.\n\n"
                "React quickly when it appears to secure captures before it vanishes."
            ),
            color=color,
        )
        embed.add_field(name="Ends", value=discord.utils.format_dt(ends_at, style="R"), inline=True)
        embed.add_field(name="Tip", value="The more active the chat is, the more motion the event creates.", inline=True)
        return embed

    def build_spider_finish_embed(self, color: discord.Color, *, body: str, total_spawns: int) -> discord.Embed:
        embed = discord.Embed(
            title="Chuva de Doces encerrada",
            description=body,
            color=color,
        )
        embed.add_field(name="Doces que apareceram", value=str(total_spawns), inline=False)
        return embed

    def build_spider_finish_embed_en(self, color: discord.Color, *, body: str, total_spawns: int) -> discord.Embed:
        embed = discord.Embed(
            title="Candy Shower ended",
            description=body,
            color=color,
        )
        embed.add_field(name="Candy drops that appeared", value=str(total_spawns), inline=False)
        return embed

    def build_chat_bonus_embed(
        self,
        color: discord.Color,
        *,
        message_count: int,
        threshold: int,
        timeout_seconds: int,
        options: list[dict],
    ) -> discord.Embed:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        embed = discord.Embed(
            title="Trem Fantasma em movimento",
            description=(
                f"O chat do evento bateu **{message_count} mensagens** e puxou o **Trem Fantasma**.\n\n"
                "Escolha um vagao abaixo e embarque uma vez antes que ele passe."
            ),
            color=color,
        )
        embed.add_field(name="Meta", value=f"{threshold} mensagens", inline=True)
        embed.add_field(name="Parada", value=discord.utils.format_dt(expires_at, style="R"), inline=True)
        if options:
            lines: list[str] = []
            for option in options:
                emoji = str(option.get("emoji") or "").strip()
                label = str(option.get("label") or "Opcao")
                reward_parts: list[str] = []
                for reward in option.get("rewards") or []:
                    if not isinstance(reward, dict):
                        continue
                    grant_type = str(reward.get("grant_type") or "").strip().lower()
                    grant_id = str(reward.get("grant_id") or "").strip()
                    amount = int(reward.get("amount", 0) or 0)
                    if grant_type != "currency" or amount <= 0:
                        continue
                    if grant_id == "candies":
                        reward_parts.append(f"{amount} doces")
                    elif grant_id == "trick_coins":
                        reward_parts.append(f"{amount} moedas travessas")
                    elif grant_id == "dice_count":
                        reward_parts.append(f"{amount} dados")
                    elif grant_id == "xp_bank":
                        reward_parts.append(f"{amount} XP Bank")
                prefix = f"{emoji} " if emoji else ""
                summary = " | ".join(reward_parts) if reward_parts else "bonus configurado"
                lines.append(f"{prefix}**{label}**: {summary}")
            if lines:
                embed.add_field(name="Vagoes", value="\n".join(lines), inline=False)
        return embed

    def build_observer_announcement_embed(self, color: discord.Color, starts_at: datetime) -> discord.Embed:
        embed = discord.Embed(
            title="📹 *sinal de gravacao detectado...*",
            description=(
                "O Observador vai abrir um novo arquivo.\n\n"
                "Uma pergunta sera registrada. A primeira resposta certa no chat vence.\n"
                "Ele ja esta olhando."
            ),
            color=color,
        )
        embed.add_field(name="Horario", value=discord.utils.format_dt(starts_at, style="F"), inline=False)
        embed.add_field(name="Falta", value=discord.utils.format_dt(starts_at, style="R"), inline=False)
        return embed

    def build_observer_question_embed(
        self,
        color: discord.Color,
        *,
        question_text: str,
        deadline_at: datetime,
        match_mode: str,
        display_text: str | None,
        difficulty: str,
        attachment_filename: str | None = None,
    ) -> discord.Embed:
        exact = match_mode in {"exact", "spaces", "spoiler"}
        validation = "Exato - acentos e maiusculas nao importam." if exact else "Acentos e pontuacao podem variar."
        difficulty_label = {
            "easy": "Easy",
            "medium": "Medium",
            "hard": "Hard",
        }.get(difficulty.lower(), difficulty.capitalize())

        description_lines = [f"> **{question_text}**"]
        if match_mode == "spaces" and display_text:
            description_lines.append(f"\n> Digite exatamente: **'{display_text}'**")
        if match_mode == "spoiler" and display_text:
            description_lines.append(f"\n> Revele e digite: {display_text}")

        embed = discord.Embed(
            title="📹 O Observador esta gravando",
            description="\n".join(description_lines),
            color=color,
        )
        embed.add_field(
            name="Tempo",
            value=discord.utils.format_dt(deadline_at, style="R"),
            inline=True,
        )
        embed.add_field(
            name="Dificuldade",
            value=difficulty_label,
            inline=True,
        )
        embed.add_field(
            name="Validacao",
            value=validation,
            inline=False,
        )
        if attachment_filename:
            embed.set_image(url=f"attachment://{attachment_filename}")
        embed.set_footer(text="Responda antes que a gravacao seja encerrada.")
        return embed

    def build_observer_translation_embed(
        self,
        color: discord.Color,
        *,
        question_en: str,
        deadline_at: datetime,
        match_mode: str,
        display_text: str | None,
        difficulty: str,
    ) -> discord.Embed:
        exact = match_mode in {"exact", "spaces", "spoiler"}
        validation = "Exact text required. Case does not matter." if exact else "Accents and punctuation may vary."
        difficulty_label = {
            "easy": "Easy",
            "medium": "Medium",
            "hard": "Hard",
        }.get(difficulty.lower(), difficulty.capitalize())

        description_lines = [f"> **{question_en}**"]
        if match_mode == "spaces" and display_text:
            description_lines.append(f"\n> Type exactly: **'{display_text}'**")
        if match_mode == "spoiler" and display_text:
            description_lines.append(f"\n> Reveal and type: {display_text}")

        embed = discord.Embed(
            title="📹 The Observer is recording",
            description="\n".join(description_lines),
            color=color,
        )
        embed.add_field(
            name="Time",
            value=discord.utils.format_dt(deadline_at, style="R"),
            inline=True,
        )
        embed.add_field(
            name="Difficulty",
            value=difficulty_label,
            inline=True,
        )
        embed.add_field(
            name="Validation",
            value=validation,
            inline=False,
        )
        embed.set_footer(text="This translation is private to you.")
        return embed

    def build_observer_timeout_embed(
        self,
        color: discord.Color,
        *,
        answer_label: str,
        answer_value: str,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="📼 *gravacao encerrada*",
            description="Ninguem respondeu antes de o arquivo ser fechado.",
            color=color,
        )
        embed.add_field(name=answer_label, value=answer_value, inline=False)
        embed.set_footer(text="O Observador continua atento.")
        return embed

    def build_observer_winner_embed(self, color: discord.Color, winner_mention: str, summary: str) -> discord.Embed:
        embed = discord.Embed(
            title="Resposta certa.",
            description=f"{winner_mention} foi o primeiro a acertar.",
            color=color,
        )
        embed.add_field(name="Recompensas", value=summary, inline=False)
        embed.set_footer(text="Voce escapou do foco desta vez.")
        return embed

    def build_tempo_unavailable_embed(self, title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.orange())
