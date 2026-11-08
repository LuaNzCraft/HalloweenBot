from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord


@dataclass(frozen=True, slots=True)
class ObserverPanelContent:
    title: str
    description: str
    metadata: str | None = None
    details: str | None = None
    footer: str | None = None
    attachment_description: str | None = None


class PublicEventTextsService:
    def build_chat_candy_spawn_embed(
        self,
        color: discord.Color,
        *,
        target_emoji: str,
        reward_candies: int,
        expires_at: datetime,
        option_count: int,
        locale: str = "pt-BR",
    ) -> discord.Embed:
        if str(locale or "").lower().startswith("en"):
            return self._build_chat_candy_spawn_embed_en(
                color,
                target_emoji=target_emoji,
                reward_candies=reward_candies,
                expires_at=expires_at,
                option_count=option_count,
            )
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
        locale: str = "pt-BR",
    ) -> discord.Embed:
        if str(locale or "").lower().startswith("en"):
            return self._build_chat_candy_winner_embed_en(
                color,
                winner_mention=winner_mention,
                reward_summary=reward_summary,
            )
        embed = discord.Embed(
            title="🍬 Doce capturado!",
            description=f"{winner_mention} encontrou o doce correto antes de todo mundo.",
            color=color,
        )
        embed.add_field(name="Premio", value=reward_summary, inline=False)
        return embed

    def build_chat_candy_timeout_embed(self, color: discord.Color, locale: str = "pt-BR") -> discord.Embed:
        if str(locale or "").lower().startswith("en"):
            embed = discord.Embed(
                title="The candy vanished...",
                description="Nobody found the right candy in time.",
                color=color,
            )
            embed.set_footer(text="Keep an eye on the chat for the next drop.")
            return embed
        embed = discord.Embed(
            title="O doce desapareceu...",
            description="Ninguem encontrou o doce correto a tempo.",
            color=color,
        )
        embed.set_footer(text="Continue atento ao chat para a proxima aparicao.")
        return embed

    def build_chat_candy_cancelled_embed(self, color: discord.Color, locale: str = "pt-BR") -> discord.Embed:
        if str(locale or "").lower().startswith("en"):
            return discord.Embed(
                title="The candy was taken back",
                description="This drop ended along with the event.",
                color=color,
            )
        return discord.Embed(
            title="O doce foi recolhido",
            description="Esta aparicao foi encerrada junto com o evento.",
            color=color,
        )

    def _build_chat_candy_spawn_embed_en(
        self,
        color: discord.Color,
        *,
        target_emoji: str,
        reward_candies: int,
        expires_at: datetime,
        option_count: int,
    ) -> discord.Embed:
        reward_label = "Sweet Coin" if reward_candies == 1 else "Sweet Coins"
        embed = discord.Embed(
            title="🍬 Candy Hunt!",
            description=(
                "Find the indicated candy among the reactions below.\n\n"
                f"# {target_emoji}\n\n"
                "Choose carefully: you get **one attempt** per drop."
            ),
            color=color,
        )
        embed.add_field(name="Options", value=str(option_count), inline=True)
        embed.add_field(name="Reward", value=f"{reward_candies} {reward_label}", inline=True)
        embed.add_field(name="Vanishes", value=discord.utils.format_dt(expires_at, style="R"), inline=True)
        embed.set_footer(text="The first player to pick the right candy wins.")
        return embed

    def _build_chat_candy_winner_embed_en(
        self,
        color: discord.Color,
        *,
        winner_mention: str,
        reward_summary: str,
    ) -> discord.Embed:
        summary = str(reward_summary or "")
        match = re.match(r"^\s*(\d+)\s+doces?\s*$", summary, re.IGNORECASE)
        if match:
            amount = int(match.group(1))
            summary = f"{amount} Sweet Coin" if amount == 1 else f"{amount} Sweet Coins"
        elif "pendente" in summary.lower():
            summary = "Reward pending verification"
        embed = discord.Embed(
            title="🍬 Candy caught!",
            description=f"{winner_mention} found the right candy before everyone else.",
            color=color,
        )
        embed.add_field(name="Reward", value=summary, inline=False)
        return embed

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
        locale: str = "pt-BR",
    ) -> discord.Embed:
        if str(locale or "").lower().startswith("en"):
            return self._build_chat_bonus_embed_en(
                color,
                message_count=message_count,
                threshold=threshold,
                timeout_seconds=timeout_seconds,
                options=options,
            )
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

    def _build_chat_bonus_embed_en(
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
            title="Ghost Train on the move",
            description=(
                f"The event chat hit **{message_count} messages** and pulled the **Ghost Train**.\n\n"
                "Pick a car below and board once before it leaves."
            ),
            color=color,
        )
        embed.add_field(name="Goal", value=f"{threshold} messages", inline=True)
        embed.add_field(name="Departs", value=discord.utils.format_dt(expires_at, style="R"), inline=True)
        if options:
            lines: list[str] = []
            for option in options:
                emoji = str(option.get("emoji") or "").strip()
                label = str(option.get("label") or "Option")
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
                        reward_parts.append(f"{amount} Sweet Coins")
                    elif grant_id == "trick_coins":
                        reward_parts.append(f"{amount} Trick Coins")
                    elif grant_id == "dice_count":
                        reward_parts.append(f"{amount} Dice")
                    elif grant_id == "xp_bank":
                        reward_parts.append(f"{amount} XP Bank")
                prefix = f"{emoji} " if emoji else ""
                summary = " | ".join(reward_parts) if reward_parts else "configured bonus"
                lines.append(f"{prefix}**{label}**: {summary}")
            if lines:
                embed.add_field(name="Cars", value="\n".join(lines), inline=False)
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

    def build_observer_question_panel_content(
        self,
        *,
        question_text: str,
        deadline_at: datetime,
        match_mode: str,
        display_text: str | None,
        difficulty: str,
        question_kind: str,
        has_attachment: bool,
    ) -> ObserverPanelContent:
        exact = match_mode in {"exact", "spaces", "spoiler"}
        if match_mode == "anagram":
            validation = "Anagrama — acentos e pontuação podem variar."
        else:
            validation = (
                "Texto exato — acentos e maiúsculas não importam."
                if exact
                else "Acentos e pontuação podem variar."
            )
        difficulty_label = {
            "easy": "Fácil",
            "medium": "Média",
            "hard": "Difícil",
        }.get(difficulty.lower(), difficulty.capitalize())

        description_lines = [f"> **{question_text}**"]
        if match_mode == "spaces" and display_text:
            description_lines.append(f"\n> Digite exatamente: **'{display_text}'**")
        if match_mode == "spoiler" and display_text:
            description_lines.append(f"\n> Revele e digite: {display_text}")
        if match_mode == "anagram":
            description_lines.append("\n> **Reorganize o sinal interceptado.**")
            if display_text and not has_attachment:
                description_lines.append(f"> Sinal: **{display_text}**")

        title = {
            "anagram": "🔴 REC · SINAIS CORROMPIDOS",
            "chat_quote": "🔴 REC · ARQUIVO DO OBSERVADOR",
            "movie_image": "🔴 REC · IMAGEM INTERCEPTADA",
        }.get(question_kind, "🔴 REC · TRANSMISSÃO DO OBSERVADOR")
        attachment_description = {
            "anagram": "Sinal corrompido interceptado pelo Observador.",
            "chat_quote": "Arquivo com a identidade do autor ocultada.",
            "movie_image": "Imagem interceptada pelo Observador.",
        }.get(question_kind)
        return ObserverPanelContent(
            title=title,
            description="\n".join(description_lines),
            metadata=(
                f"**Tempo:** {discord.utils.format_dt(deadline_at, style='R')}"
                f" • **Dificuldade:** {difficulty_label}"
            ),
            details=f"-# Validação: {validation}",
            footer="Responda no chat antes que a gravação seja encerrada.",
            attachment_description=attachment_description,
        )

    def build_observer_translation_panel_content(
        self,
        *,
        question_en: str,
        deadline_at: datetime,
        match_mode: str,
        display_text: str | None,
        difficulty: str,
        question_kind: str,
    ) -> ObserverPanelContent:
        exact = match_mode in {"exact", "spaces", "spoiler"}
        if match_mode == "anagram":
            validation = "Anagram - accents and punctuation may vary."
        else:
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
        if match_mode == "anagram":
            description_lines.append("\n> **Rearrange the intercepted signal.**")

        title = {
            "anagram": "🔴 REC · CORRUPTED SIGNALS",
            "chat_quote": "🔴 REC · OBSERVER FILE",
            "movie_image": "🔴 REC · INTERCEPTED IMAGE",
        }.get(question_kind, "🔴 REC · OBSERVER TRANSMISSION")
        return ObserverPanelContent(
            title=title,
            description="\n".join(description_lines),
            metadata=(
                f"**Time:** {discord.utils.format_dt(deadline_at, style='R')}"
                f" • **Difficulty:** {difficulty_label}"
            ),
            details=f"-# Validation: {validation}",
            footer="This translation is private to you.",
        )

    def build_observer_timeout_panel_content(
        self,
        *,
        answer_label: str,
        answer_value: str,
        question_kind: str,
    ) -> ObserverPanelContent:
        title = {
            "anagram": "📼 SINAL ENCERRADO",
            "chat_quote": "📼 ARQUIVO ENCERRADO",
        }.get(question_kind, "📼 GRAVAÇÃO ENCERRADA")
        return ObserverPanelContent(
            title=title,
            description="Ninguém respondeu antes de o arquivo ser fechado.",
            details=f"**{answer_label}**\n{answer_value}",
            footer="O Observador continua atento.",
            attachment_description=(
                "Arquivo revelado com a identidade confirmada."
                if question_kind == "chat_quote"
                else None
            ),
        )

    def build_observer_winner_panel_content(
        self,
        *,
        winner_mention: str,
        summary: str,
        answer_label: str,
        answer_value: str,
        question_kind: str,
    ) -> ObserverPanelContent:
        title = {
            "anagram": "📼 SINAL RECONSTRUÍDO",
            "chat_quote": "📼 IDENTIDADE CONFIRMADA",
        }.get(question_kind, "📼 RESPOSTA CONFIRMADA")
        return ObserverPanelContent(
            title=title,
            description=f"{winner_mention} foi o primeiro a acertar.",
            details=(
                f"**{answer_label}**\n{answer_value}\n\n"
                f"**Recompensas**\n{summary}"
            ),
            footer="Você escapou do foco desta vez.",
            attachment_description=(
                "Arquivo revelado com a identidade confirmada."
                if question_kind == "chat_quote"
                else None
            ),
        )

    def build_tempo_unavailable_embed(self, title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.orange())
