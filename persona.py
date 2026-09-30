"""Personaje con IA: le da voz propia al bot.

- Responde cuando le hablan: mencionándolo, respondiendo a sus mensajes, con el prefijo
  seguido de algo que no es un comando (ej: "!hola Lillia"), por DM, o en los canales
  de PERSONA_CHANNELS (ahí responde a todo).
- El resto del bot usa say() / comment_later() para que los avisos (canción en cola,
  desconexión, errores...) los diga el personaje en vez de textos fijos.
- La IA NUNCA bloquea la música: si no está configurada, no responde a tiempo o se acabó el
  cupo diario, se usan al instante los textos fijos de siempre.

Proveedores (AI_PROVIDER en .env): "gemini" (recomendado) o "characterai".
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

import discord
from discord.ext import commands

import historial_canciones

log = logging.getLogger("persona")


def _env(*names: str, default: str = "") -> str:
    """Primer valor definido entre varios nombres (los CAI_* viejos siguen funcionando)."""
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_bool(*names: str, default: bool) -> bool:
    value = _env(*names).lower()
    return default if not value else value in ("1", "true", "si", "sí", "yes", "y")


AI_PROVIDER = _env("AI_PROVIDER").lower()
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODELS = [m.strip() for m in _env("GEMINI_MODEL").split(",") if m.strip()]
CAI_TOKEN = _env("CAI_TOKEN")
CAI_CHARACTER_ID = _env("CAI_CHARACTER_ID")

CHANNELS = {int(x) for x in re.findall(r"\d+", _env("PERSONA_CHANNELS", "CAI_CHANNELS"))}
LANGUAGE = _env("PERSONA_LANGUAGE", "CAI_LANGUAGE", default="español")
MUSIC_COMMENTS = _env_bool("PERSONA_MUSIC_COMMENTS", "CAI_MUSIC_COMMENTS", default=True)
USE_PROFILE = _env_bool("PERSONA_USE_PROFILE", "CAI_USE_PROFILE", default=True)
ALLOW_DM = _env_bool("PERSONA_ALLOW_DM", "CAI_ALLOW_DM", default=True)
# true = se le puede pedir música con palabras normales ("@Lillia poneme Tik Tok de Kesha")
MUSIC_CONTROL = _env_bool("PERSONA_MUSIC_CONTROL", default=True)
# Curiosidades sobre la canción que suena: probabilidad por canción y máximo por día.
TRIVIA_CHANCE = float(_env("PERSONA_TRIVIA_CHANCE", default="0.15"))
TRIVIA_PER_DAY = int(_env("PERSONA_TRIVIA_PER_DAY", default="1"))

REPLY_TIMEOUT = 45
COMMENT_TIMEOUT = 15
DATA_DIR = Path(__file__).resolve().parent / "data"
PROFILE_FILE = DATA_DIR / "perfil.json"
TRIVIA_FILE = DATA_DIR / "curiosidades.json"
NO_MENTIONS = discord.AllowedMentions.none()


# Órdenes que el personaje puede añadir a su respuesta: [[PLAY: búsqueda]], [[SKIP]], ...
ACTION_RE = re.compile(r"\[\[\s*(PLAY|SKIP|STOP|PAUSE|RESUME|LOOP|VOLUME|LEAVE)\s*(?::\s*([^\]\n]*?))?\s*\]\]", re.I)
ACTION_COMMANDS = {
    "play": "play", "skip": "skip", "stop": "stop", "pause": "pause",
    "resume": "resume", "loop": "loop", "volume": "volume", "leave": "leave",
}
MAX_ACTIONS = 5


def extract_actions(reply: str) -> tuple[str, list[tuple[str, str]]]:
    """Separa el texto visible de las órdenes de música que el personaje pidió ejecutar."""
    actions = [(name.lower(), (arg or "").strip()) for name, arg in ACTION_RE.findall(reply)]
    text = ACTION_RE.sub("", reply)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.splitlines())).strip()
    plays = 0
    kept = []
    for name, arg in actions:
        if name == "play":
            if not arg or plays >= 3:
                continue
            plays += 1
        kept.append((name, arg))
    return text, kept[:MAX_ACTIONS]


def _split(text: str, size: int = 2000) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]


def _hhmm(timestamp: Optional[float]) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M") if timestamp else "más tarde"


def create_backend():
    """Crea el proveedor de IA según el .env, o None si no hay ninguno configurado."""
    provider = AI_PROVIDER or ("gemini" if GEMINI_API_KEY else "characterai" if CAI_TOKEN else "")
    try:
        if provider == "gemini":
            if not GEMINI_API_KEY:
                raise RuntimeError("falta GEMINI_API_KEY en .env")
            from ia_gemini import GeminiBackend
            from personaje import load_character

            return GeminiBackend(GEMINI_API_KEY, load_character(), LANGUAGE, GEMINI_MODELS, MUSIC_CONTROL)
        if provider == "characterai":
            if not (CAI_TOKEN and CAI_CHARACTER_ID):
                raise RuntimeError("faltan CAI_TOKEN o CAI_CHARACTER_ID en .env")
            from ia_characterai import CharacterAIBackend

            return CharacterAIBackend(CAI_TOKEN, CAI_CHARACTER_ID)
        if provider not in ("", "none", "ninguno"):
            raise RuntimeError(f"AI_PROVIDER desconocido: {provider}")
    except ImportError as exc:
        log.error("Falta una librería para la IA (%s). Ejecuta windows\\instalar.bat", exc)
    except Exception as exc:
        log.error("No se pudo preparar la IA: %s", exc)
    log.warning("Personaje desactivado: el bot usará textos fijos.")
    return None


class Persona(commands.Cog, name="Personaje"):
    """Charla con el personaje."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.backend = create_backend()
        self._started = False
        self._sleep_notice: dict[int, float] = {}  # canal -> hasta cuándo ya avisamos que no hay cupo
        self._events: dict[int, deque[str]] = {}  # canal -> últimos avisos de la música

    @property
    def enabled(self) -> bool:
        return self.backend is not None

    def available(self) -> bool:
        return self.backend is not None and self.backend.available()

    @property
    def name(self) -> str:
        if self.backend is not None:
            return self.backend.name
        return self.bot.user.name if self.bot.user else "Bot"

    async def cog_unload(self) -> None:
        if self.backend:
            await self.backend.close()

    # ---------- Contexto: qué está pasando con la música ----------

    def note(self, channel: discord.abc.Messageable, situation: str) -> None:
        """Guarda un aviso de la música (aunque la IA no lo comente) para que después lo sepa."""
        events = self._events.setdefault(channel.id, deque(maxlen=6))
        events.append(f"[{datetime.now():%H:%M}] {situation}")

    def _context(self, channel: discord.abc.Messageable, people: Optional[list] = None) -> str:
        parts = [f"Hora actual: {datetime.now():%H:%M}."]
        # Gustos musicales de quien habla y de las personas que menciona.
        for person in people or []:
            try:
                songs = historial_canciones.summary(person)
            except Exception:
                songs = None
            parts.append(songs or f"{person.display_name} todavía no te pidió ninguna canción.")
        guild = getattr(channel, "guild", None)
        music = self.bot.get_cog("Música")
        if guild is not None and music is not None and hasattr(music, "status_text"):
            parts.append(f"Música en el servidor: {music.status_text(guild.id)}")
        events = self._events.get(channel.id)
        if events:
            parts.append("Últimos avisos de la música en este canal:\n" + "\n".join(events))
        return "\n".join(parts)

    # ---------- Hablar con la IA ----------

    async def ask(
        self,
        channel: discord.abc.Messageable,
        text: str,
        timeout: float = REPLY_TIMEOUT,
        wait: bool = True,
        fast: bool = False,
        people: Optional[list] = None,
    ) -> Optional[str]:
        """Manda un mensaje al personaje (una conversación por canal). None si no hay respuesta."""
        if not self.available():
            return None
        try:
            async with asyncio.timeout(timeout):
                context = self._context(channel, people)
                return await self.backend.ask(str(channel.id), text, context, wait, fast)
        except TimeoutError:
            log.warning("%s tardó más de %ss en responder", self.backend.provider, timeout)
        except Exception:
            log.exception("Error inesperado con %s", self.backend.provider)
        return None

    async def comment(self, channel: discord.abc.Messageable, situation: str) -> Optional[str]:
        """Pide al personaje un comentario corto sobre algo que pasó (canción, error, etc.)."""
        if not (MUSIC_COMMENTS and self.available()):
            return None
        prompt = f"(({situation} Reacciona en personaje, en {LANGUAGE}, con una o dos frases cortas.))"
        line = await self.ask(channel, prompt, COMMENT_TIMEOUT, wait=False, fast=True)
        return extract_actions(line)[0] or None if line else None  # en avisos no se obedecen órdenes

    # ---------- Arranque y perfil (nombre y avatar) ----------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._started or self.backend is None:
            return
        self._started = True
        try:
            async with asyncio.timeout(60):
                await self.backend.start()
        except Exception as exc:
            log.error("No se pudo iniciar %s: %s", self.backend.provider, exc)
            if "API key" in str(exc):
                self.backend = None  # clave inválida: no tiene sentido seguir intentando
            return
        log.info("Personaje listo: %s (con %s)", self.backend.name, self.backend.provider)
        await self._apply_profile()

    async def _apply_profile(self) -> None:
        if not (USE_PROFILE and self.backend):
            return
        for guild in self.bot.guilds:
            await self._apply_nick(guild)
        try:
            image = await self.backend.avatar()
            if not image:
                return
            digest = hashlib.sha1(image).hexdigest()
            try:
                saved = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
            if saved.get("sha1") == digest:
                return
            await self.bot.user.edit(avatar=image)
            DATA_DIR.mkdir(exist_ok=True)
            PROFILE_FILE.write_text(json.dumps({"sha1": digest}), encoding="utf-8")
            log.info("Avatar del bot cambiado al del personaje")
        except Exception as exc:
            log.warning("No se pudo poner el avatar del personaje: %s", exc)

    async def _apply_nick(self, guild: discord.Guild) -> None:
        nick = self.name[:32]
        if guild.me and guild.me.nick != nick:
            try:
                await guild.me.edit(nick=nick)
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        if USE_PROFILE and self.backend:
            await self._apply_nick(guild)

    # ---------- Conversación ----------

    def _extract_text(self, message: discord.Message, ctx: commands.Context) -> tuple[Optional[str], bool]:
        """(texto para el personaje, si le hablaron directamente). Texto None = no va con el bot."""
        me = self.bot.user
        content = message.content
        direct = False

        if ctx.prefix and content.startswith(ctx.prefix):
            content = content[len(ctx.prefix):]
            direct = True
        if message.guild is None and ALLOW_DM:
            direct = True
        if me in message.mentions:
            direct = True
        ref = message.reference
        if ref and isinstance(ref.resolved, discord.Message) and ref.resolved.author == me:
            direct = True
        if not direct and message.channel.id not in CHANNELS:
            return None, False

        for user in message.mentions:
            replacement = "" if user == me else user.display_name
            content = content.replace(f"<@{user.id}>", replacement).replace(f"<@!{user.id}>", replacement)
        content = content.strip()
        if message.attachments and not content:
            content = "(te manda un archivo)"
        return content or "(te saluda)", direct

    async def _say_sleeping(self, message: discord.Message) -> None:
        """Sin cupo de IA: avisa una sola vez por canal y después solo reacciona con 😴."""
        until = self.backend.available_again_at()
        channel_id = message.channel.id
        try:
            if self._sleep_notice.get(channel_id, 0) >= (until or 0) and channel_id in self._sleep_notice:
                await message.add_reaction("😴")
                return
            self._sleep_notice[channel_id] = until or 0
            await message.reply(
                f"😴 *{self.name} se quedó dormida: se acabó el límite de la IA por hoy. "
                f"Vuelve a hablar a partir de las {_hhmm(until)}. La música sigue funcionando.*",
                mention_author=False,
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.backend is None:
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return  # es un comando de verdad, lo maneja su propio código
        text, direct = self._extract_text(message, ctx)
        if text is None:
            return
        if not self.backend.available():
            if direct:
                await self._say_sleeping(message)
            return

        async with message.channel.typing():
            people = [message.author] + [
                user for user in message.mentions if user != self.bot.user and not user.bot
            ][:3]
            reply = await self.ask(message.channel, f"{message.author.display_name}: {text}", people=people)
        if reply is None:
            if not self.backend.available():
                if direct:
                    await self._say_sleeping(message)
                return
            if not direct:
                return
            reply = "*(no me sale responder ahora mismo, prueba otra vez en un rato)*"
        reply, actions = extract_actions(reply)
        if reply:
            chunks = _split(reply)
            try:
                await message.reply(chunks[0], mention_author=False, allowed_mentions=NO_MENTIONS)
                for chunk in chunks[1:]:
                    await message.channel.send(chunk, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException as exc:
                log.warning("No se pudo enviar la respuesta: %s", exc)
        # Solo se obedece si le hablaron directamente y dentro de un servidor (la música va por servidor).
        if actions and direct and MUSIC_CONTROL and message.guild is not None:
            await self._run_actions(message, actions)

    async def _run_actions(self, message: discord.Message, actions: list[tuple[str, str]]) -> None:
        """Ejecuta las órdenes de música como si la persona hubiera escrito el comando."""
        ctx = await self.bot.get_context(message)
        for name, arg in actions:
            command = self.bot.get_command(ACTION_COMMANDS[name])
            if command is None:
                continue
            ctx.command = command
            ctx.invoked_with = command.name
            log.info("%s pidió por chat: %s %s", message.author.display_name, name, arg)
            try:
                if name == "play":
                    await ctx.invoke(command, busqueda=arg)
                elif name == "volume":
                    number = re.search(r"\d+", arg)
                    if not number:
                        continue
                    await ctx.invoke(command, nivel=int(number.group()))
                else:
                    await ctx.invoke(command)
            except commands.CommandError as exc:
                await self.bot.on_command_error(ctx, exc)
            except Exception as exc:
                await self.bot.on_command_error(ctx, commands.CommandInvokeError(exc))

    # ---------- Comandos ----------

    @commands.command(name="reset", aliases=["olvidar"], help="Borra la memoria del personaje en este canal.")
    async def reset(self, ctx: commands.Context) -> None:
        if self.backend:
            self.backend.reset(str(ctx.channel.id))
        await ctx.send("🧠 Memoria de este canal borrada: la próxima charla empieza de cero.")

    @commands.command(name="personaje", help="Muestra qué personaje e IA está usando el bot.")
    async def personaje(self, ctx: commands.Context) -> None:
        if self.backend is None:
            await ctx.send("El personaje está desactivado (falta configurar la IA en .env). Revisa `logs/bot.log`.")
            return
        if self.backend.available():
            status = "🟢 Despierta"
        else:
            status = f"😴 Sin cupo de IA hasta las {_hhmm(self.backend.available_again_at())}"
        embed = discord.Embed(title=self.backend.name, description=self.backend.description[:4000], color=0x9B59B6)
        embed.add_field(name="IA", value=self.backend.provider)
        embed.add_field(name="Estado", value=status)
        if hasattr(self.backend, "ranking"):
            lines = []
            for model, seconds, fails in self.backend.ranking()[:6]:
                speed = f"{seconds:.1f}s" if seconds is not None else "sin datos"
                lines.append(f"`{model}` · {speed} · fallos {fails:.0%}")
            embed.add_field(name="Modelos (en orden de preferencia)", value="\n".join(lines) or "-", inline=False)
        await ctx.send(embed=embed)


# ---------- Ayudas para el resto del bot ----------

_background: set[asyncio.Task] = set()


def _note(bot: commands.Bot, channel: discord.abc.Messageable, situation: str) -> None:
    cog = bot.get_cog("Personaje")
    if isinstance(cog, Persona):
        try:
            cog.note(channel, situation)
        except Exception:
            log.exception("No se pudo guardar el aviso para el personaje")  # nunca debe frenar la música


def _persona(bot: commands.Bot) -> Optional[Persona]:
    """El personaje, solo si ahora mismo puede hablar (así no hacemos esperar a nadie)."""
    cog = bot.get_cog("Personaje")
    return cog if isinstance(cog, Persona) and cog.available() else None


async def say(
    bot: commands.Bot,
    channel: discord.abc.Messageable,
    situation: str,
    info: str,
    **kwargs,
) -> None:
    """Envía `info` (texto fijo) acompañado del comentario del personaje sobre `situation`."""
    _note(bot, channel, situation)
    persona = _persona(bot)
    line = None
    if persona and MUSIC_COMMENTS:
        async with channel.typing():
            line = await persona.comment(channel, situation)
    content = f"{line}\n-# {info}" if line and info else (line or info or None)
    try:
        await channel.send(content, allowed_mentions=NO_MENTIONS, **kwargs)
    except discord.HTTPException as exc:
        log.warning("No se pudo enviar el mensaje: %s", exc)


def comment_later(bot: commands.Bot, channel: discord.abc.Messageable, situation: str) -> None:
    """Comentario opcional en segundo plano (para comandos que ya respondieron con una reacción)."""
    _note(bot, channel, situation)
    persona = _persona(bot)
    if not (persona and MUSIC_COMMENTS):
        return

    async def run() -> None:
        line = await persona.comment(channel, situation)
        if line:
            try:
                await channel.send(line, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass

    task = asyncio.create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)


_trivia_pending = False


def _trivia_count_today() -> int:
    try:
        state = json.loads(TRIVIA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return state.get("count", 0) if state.get("date") == date.today().isoformat() else 0


def maybe_song_trivia(
    bot: commands.Bot,
    channel: discord.abc.Messageable,
    title: str,
    duration: Optional[int],
    still_playing: Callable[[], bool],
) -> None:
    """Con poca probabilidad (y como mucho TRIVIA_PER_DAY veces al día), el personaje comenta
    por su cuenta algo sobre la canción que está sonando. Nunca retrasa la música."""
    global _trivia_pending
    if _trivia_pending or TRIVIA_PER_DAY <= 0 or _persona(bot) is None:
        return
    if random.random() >= TRIVIA_CHANCE or _trivia_count_today() >= TRIVIA_PER_DAY:
        return
    _trivia_pending = True

    async def run() -> None:
        global _trivia_pending
        try:
            # Que suene un rato antes de comentar (y no después de la mitad si es corta).
            delay = random.uniform(40, 100)
            if duration:
                delay = min(delay, duration * 0.5)
            await asyncio.sleep(delay)
            persona = _persona(bot)
            if persona is None or not still_playing() or _trivia_count_today() >= TRIVIA_PER_DAY:
                return
            prompt = (
                f"((Mientras suena '{title}', por iniciativa propia se te ocurre comentar algo sobre esa "
                "canción: un dato curioso del artista o la banda, algo de la melodía, de qué trata la "
                "letra o de su historia. Cuenta solo datos que conozcas de verdad; si no conoces la "
                "canción, comenta lo que te hace sentir sin inventar datos. En personaje, en "
                f"{LANGUAGE}, dos o tres frases, con emojis.))"
            )
            line = await persona.ask(channel, prompt, REPLY_TIMEOUT, wait=False)
            line = extract_actions(line)[0] if line else None
            if not line or not still_playing():
                return
            await channel.send(line, allowed_mentions=NO_MENTIONS)
            DATA_DIR.mkdir(exist_ok=True)
            TRIVIA_FILE.write_text(
                json.dumps({"date": date.today().isoformat(), "count": _trivia_count_today() + 1}),
                encoding="utf-8",
            )
            log.info("Curiosidad sobre '%s' enviada", title)
        except Exception:
            log.exception("No se pudo enviar la curiosidad de la canción")
        finally:
            _trivia_pending = False

    task = asyncio.create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Persona(bot))
