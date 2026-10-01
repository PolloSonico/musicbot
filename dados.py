"""Dados: Lillia reacciona a las tiradas del bot de D&D y tiene su propio !dado.

Los bots no pueden usar los comandos de barra (/roll) de otros bots, pero sí pueden leer lo que
publican. Cuando el bot de dados manda una tirada, Lillia la lee y:
  - en un 20 natural o un 1 natural (d20) comenta la jugada y reacciona (🎉 / 💀);
  - en el resto de tiradas solo pone un emoji (✨ si salió alto, 🥺 si salió bajo, 🎲 si normal)
    y, de vez en cuando, un comentario.

Configuración (.env, todo opcional):
  DICE_BOT_IDS=123,456        ids de los bots de dados (si no, se reconocen por el nombre)
  DICE_REACTIONS=true         false = no reacciona a las tiradas de otros bots
  DICE_COMMENT_CHANCE=0.15    probabilidad de comentar una tirada normal (0 a 1)
"""

import logging
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord.ext import commands

import persona as persona_mod

log = logging.getLogger("dados")

BOT_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("DICE_BOT_IDS", ""))}
BOT_NAME_RE = re.compile(r"d\s*&\s*d|dnd|dice|dados?|roll|avrae", re.I)
REACTIONS = os.getenv("DICE_REACTIONS", "true").strip().lower() not in ("0", "false", "no", "off")
COMMENT_CHANCE = float(os.getenv("DICE_COMMENT_CHANCE", "0.15") or 0)
COMMENT_COOLDOWN = 20  # segundos entre comentarios en un mismo canal (para no llenar el chat)

MAX_DICE, MAX_SIDES = 50, 1000

DICE_RE = re.compile(r"(?<![\w])(\d{0,3})\s*d\s*(\d{1,4})(?![\w])", re.I)
MOD_RE = re.compile(r"(?<![\w])\d{0,3}\s*d\s*\d{1,4}\s*([+-]\s*\d{1,4})", re.I)
TOTAL_RE = re.compile(r"(?:sum of rolls?(?:\(s\))?|sum|total|result(?:ado)?|suma)\s*[:=]?\s*\**\s*(-?\d+)", re.I)
ROLL_FIELD_RE = re.compile(r"\broll\s*#?\s*\d+|\btirada\s*#?\s*\d+|\bdado\s*#?\s*\d+", re.I)
WHO_RE = re.compile(r"(?:rolls?|rolled|tirada)\s+(?:by|de|para)\s+([^\n(]+?)\s*(?:\(|$)", re.I | re.M)
INT_RE = re.compile(r"-?\d+")


@dataclass
class Roll:
    count: int = 1
    sides: int = 0
    modifier: int = 0
    rolls: list[int] = field(default_factory=list)
    total: Optional[int] = None
    who: str = ""

    @property
    def notation(self) -> str:
        mod = f"{self.modifier:+d}" if self.modifier else ""
        return f"{self.count}d{self.sides}{mod}"

    @property
    def nat20(self) -> bool:
        return self.sides == 20 and 20 in self.rolls

    @property
    def nat1(self) -> bool:
        return self.sides == 20 and 1 in self.rolls and not self.nat20

    def luck(self) -> Optional[float]:
        """0 = lo peor posible, 1 = lo mejor posible (sin contar el modificador)."""
        if self.sides < 2:
            return None
        if self.rolls:
            value, count = sum(self.rolls), len(self.rolls)
        elif self.total is not None:
            value, count = self.total - self.modifier, self.count
        else:
            return None
        lowest, highest = count, count * self.sides
        if not lowest <= value <= highest:
            return None
        return (value - lowest) / (highest - lowest)


# ---------- Leer la tirada de otro bot ----------

def _texts(message: discord.Message) -> list[str]:
    parts = [message.content or ""]
    for embed in message.embeds:
        parts += [embed.title or "", embed.description or "", getattr(embed.author, "name", None) or ""]
        parts += [f"{f.name}: {f.value}" for f in embed.fields]
        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)
    return [p for p in parts if p]


def parse_roll(message: discord.Message) -> Optional[Roll]:
    """Saca la tirada de un mensaje de un bot de dados (embed o texto). None si no parece una tirada."""
    texts = _texts(message)
    full = "\n".join(texts)
    dice = DICE_RE.search(full)
    if not dice:
        return None
    roll = Roll(count=int(dice.group(1) or 1), sides=int(dice.group(2)))
    if not (1 <= roll.count <= MAX_DICE and 2 <= roll.sides <= MAX_SIDES):
        return None
    mod = MOD_RE.search(full)
    if mod:
        roll.modifier = int(mod.group(1).replace(" ", ""))
    total = TOTAL_RE.search(full)
    if total:
        roll.total = int(total.group(1))
    # Cada dado suele venir en su propio campo: "Roll #1: 17".
    for embed in message.embeds:
        for f in embed.fields:
            if ROLL_FIELD_RE.search(f.name):
                after = f.name.split(":", 1)[1] if ":" in f.name else ""
                numbers = INT_RE.findall(f"{after} {f.value}")
                if numbers:
                    roll.rolls.append(int(numbers[0]))
    roll.rolls = [r for r in roll.rolls if 1 <= r <= roll.sides]
    if not roll.rolls and roll.count == 1 and roll.total is not None and 1 <= roll.total - roll.modifier <= roll.sides:
        roll.rolls = [roll.total - roll.modifier]
    if roll.total is None and roll.rolls:
        roll.total = sum(roll.rolls) + roll.modifier
    if roll.total is None and not roll.rolls:
        return None
    who = WHO_RE.search(full)
    if who:
        roll.who = who.group(1).strip(" *_`")
    return roll


def is_dice_bot(user: discord.abc.User) -> bool:
    if not user.bot:
        return False
    if BOT_IDS:
        return user.id in BOT_IDS
    return bool(BOT_NAME_RE.search(f"{user.name} {getattr(user, 'display_name', '')}"))


# ---------- Tirar dados ----------

TIRADA_RE = re.compile(r"^(\d{0,3})\s*d\s*(\d{1,4})\s*([+-]\s*\d{1,4})?$", re.I)


def parse_notation(text: str) -> Optional[tuple[int, int, int, str]]:
    """'2d6+3' -> (2, 6, 3, ''). 'ventaja+2' -> (2, 20, 2, 'alta'): tira 2d20 y se queda con el mayor."""
    text = (text or "1d20").strip().lower().replace(" ", "")
    keep = ""
    for word, mode in (("desventaja", "baja"), ("ventaja", "alta")):
        if text.startswith(word):
            keep, text = mode, text[len(word):]
            if text and not re.fullmatch(r"[+-]\d{1,4}", text):
                return None
            text = "2d20" + text
            break
    if text.isdigit():  # "!dado 20" = 1d20
        text = f"1d{text}"
    elif text.startswith(("+", "-")):  # "!dado +5" = 1d20+5
        text = "1d20" + text
    m = TIRADA_RE.match(text)
    if not m:
        return None
    count, sides = int(m.group(1) or 1), int(m.group(2))
    modifier = int(m.group(3)) if m.group(3) else 0
    if not (1 <= count <= MAX_DICE and 2 <= sides <= MAX_SIDES):
        return None
    return count, sides, modifier, keep


def roll_dice(count: int, sides: int, modifier: int = 0, keep: str = "", rng=random) -> tuple[Roll, list[int]]:
    """Devuelve (tirada que cuenta, todos los dados tirados)."""
    thrown = [rng.randint(1, sides) for _ in range(count)]
    used = thrown
    if keep == "alta":
        used = [max(thrown)]
    elif keep == "baja":
        used = [min(thrown)]
    roll = Roll(count=len(used), sides=sides, modifier=modifier, rolls=used, total=sum(used) + modifier)
    return roll, thrown


def _situation(roll: Roll, who: str) -> str:
    detail = f"{roll.notation}: salió {', '.join(map(str, roll.rolls)) or '?'}" + (f" (total {roll.total})" if roll.total is not None else "")
    if roll.nat20:
        return f"{who} sacó un 20 NATURAL en una tirada de D&D ({detail}). ¡Crítico! Celébralo con emoción."
    if roll.nat1:
        return f"{who} sacó un 1 natural en una tirada de D&D ({detail}): pifia. Consuélalo con ternura (y un poquito de gracia)."
    luck = roll.luck()
    tone = "le fue muy bien" if luck is not None and luck >= 0.8 else "le fue bastante mal" if luck is not None and luck <= 0.2 else "una tirada normal"
    return f"{who} tiró dados jugando D&D ({detail}): {tone}."


def _reaction(roll: Roll) -> str:
    if roll.nat20:
        return random.choice(["🎉", "✨"])
    if roll.nat1:
        return random.choice(["💀", "🥺"])
    luck = roll.luck()
    if luck is not None and luck >= 0.8:
        return "✨"
    if luck is not None and luck <= 0.2:
        return "🥺"
    return "🎲"


class Dados(commands.Cog, name="Dados"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._seen: deque[int] = deque(maxlen=500)
        self._last_comment: dict[int, float] = {}

    # ---------- Tiradas de otros bots ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._handle(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        # Los comandos de barra suelen mandar primero "pensando..." y después editan con el resultado.
        await self._handle(after)

    async def _handle(self, message: discord.Message) -> None:
        if not REACTIONS or message.guild is None or message.id in self._seen:
            return
        if self.bot.user and message.author.id == self.bot.user.id:
            return
        if not is_dice_bot(message.author):
            return
        roll = parse_roll(message)
        if roll is None:
            return
        self._seen.append(message.id)
        who = roll.who or self._who(message) or "Alguien"
        log.info("Tirada de %s: %s -> %s %s", who, roll.notation, roll.rolls, roll.total)
        try:
            await message.add_reaction(_reaction(roll))
        except discord.HTTPException as exc:
            log.debug("No se pudo reaccionar a la tirada: %s", exc)
        special = roll.nat20 or roll.nat1
        if special or random.random() < COMMENT_CHANCE:
            await self._comment(message, roll, who, force=special)

    @staticmethod
    def _who(message: discord.Message) -> str:
        meta = getattr(message, "interaction_metadata", None) or getattr(message, "interaction", None)
        user = getattr(meta, "user", None)
        return getattr(user, "display_name", "") if user else ""

    async def _comment(self, message: discord.Message, roll: Roll, who: str, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_comment.get(message.channel.id, 0) < COMMENT_COOLDOWN:
            return
        persona = persona_mod._persona(self.bot)
        if persona is None:
            return
        self._last_comment[message.channel.id] = now
        line = await persona.comment(message.channel, _situation(roll, who))
        if not line:
            return
        try:
            await message.reply(line[:2000], mention_author=False, allowed_mentions=persona_mod.NO_MENTIONS)
        except discord.HTTPException as exc:
            log.debug("No se pudo comentar la tirada: %s", exc)

    # ---------- !dado ----------

    @commands.command(name="dado", aliases=["dados", "tirar", "roll"],
                      help="Tira dados al estilo D&D: !dado, !dado 2d6+3, !dado d100, !dado ventaja, !dado desventaja+2.")
    async def dado(self, ctx: commands.Context, *, tirada: str = "1d20") -> None:
        parsed = parse_notation(tirada)
        if parsed is None:
            await ctx.send(f"🎲 No entendí esa tirada. Prueba así: `{ctx.prefix}dado 1d20+3`, `{ctx.prefix}dado 4d6` "
                           f"o `{ctx.prefix}dado ventaja` (máximo {MAX_DICE} dados de {MAX_SIDES} caras).")
            return
        count, sides, modifier, keep = parsed
        roll, thrown = roll_dice(count, sides, modifier, keep)
        name = ctx.author.display_name
        title = {"alta": "con ventaja", "baja": "con desventaja"}.get(keep, roll.notation)
        if keep and modifier:
            title += f" ({modifier:+d})"
        color = 0xF1C40F if roll.nat20 else 0x992D22 if roll.nat1 else 0xE8A0BF
        embed = discord.Embed(title=f"🎲 {name} tira {title}", color=color)
        shown = ", ".join(f"**{r}**" if r in (1, sides) and sides == 20 else str(r) for r in thrown)
        if keep:
            shown += f"  → se queda con **{roll.rolls[0]}**"
        embed.add_field(name="Dados", value=shown[:1024], inline=False)
        if count > 1 or modifier or keep:
            embed.add_field(name="Total", value=f"**{roll.total}**")
        else:
            embed.description = f"# {roll.total}"
        if roll.nat20:
            embed.set_footer(text="¡20 natural! 🎉")
        elif roll.nat1:
            embed.set_footer(text="1 natural... 💀")
        special = roll.nat20 or roll.nat1
        comment = special or random.random() < COMMENT_CHANCE
        await persona_mod.say(self.bot, ctx.channel, _situation(roll, name), "", comment=comment, embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Dados(bot))
