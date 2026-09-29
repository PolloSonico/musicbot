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
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

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

REPLY_TIMEOUT = 45
COMMENT_TIMEOUT = 15
DATA_DIR = Path(__file__).resolve().parent / "data"
PROFILE_FILE = DATA_DIR / "perfil.json"
NO_MENTIONS = discord.AllowedMentions.none()


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

            return GeminiBackend(GEMINI_API_KEY, load_character(), LANGUAGE, GEMINI_MODELS)
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

    def _context(self, channel: discord.abc.Messageable) -> str:
        parts = [f"Hora actual: {datetime.now():%H:%M}."]
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
    ) -> Optional[str]:
        """Manda un mensaje al personaje (una conversación por canal). None si no hay respuesta."""
        if not self.available():
            return None
        try:
            async with asyncio.timeout(timeout):
                return await self.backend.ask(str(channel.id), text, self._context(channel), wait)
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
        return await self.ask(channel, prompt, COMMENT_TIMEOUT, wait=False)

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
            reply = await self.ask(message.channel, f"{message.author.display_name}: {text}")
        if reply is None:
            if not self.backend.available():
                if direct:
                    await self._say_sleeping(message)
                return
            if not direct:
                return
            reply = "*(no me sale responder ahora mismo, prueba otra vez en un rato)*"
        chunks = _split(reply)
        try:
            await message.reply(chunks[0], mention_author=False, allowed_mentions=NO_MENTIONS)
            for chunk in chunks[1:]:
                await message.channel.send(chunk, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as exc:
            log.warning("No se pudo enviar la respuesta: %s", exc)

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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Persona(bot))
