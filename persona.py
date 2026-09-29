"""Personaje de Character.AI: le da voz propia al bot.

- Responde cuando le hablan: mencionándolo, respondiendo a sus mensajes, con el prefijo
  seguido de algo que no es un comando (ej: "!hola Lillia"), por DM, o en los canales
  de CAI_CHANNELS (ahí responde a todo).
- El resto del bot usa say() / comment_later() para que los avisos (canción en cola,
  desconexión, errores...) los diga el personaje en vez de textos fijos. Si Character.AI
  no está configurado o no responde a tiempo, se usa el texto fijo de siempre.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

try:
    from PyCharacterAI import get_client
    from PyCharacterAI.exceptions import ActionError
except ImportError:  # librería no instalada: el bot funciona igual, con textos fijos
    get_client = None
    ActionError = Exception

log = logging.getLogger("persona")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "si", "sí", "yes", "y")


CAI_TOKEN = os.getenv("CAI_TOKEN", "").strip()
CAI_CHARACTER_ID = os.getenv("CAI_CHARACTER_ID", "").strip()
CAI_CHANNELS = {int(x) for x in re.findall(r"\d+", os.getenv("CAI_CHANNELS", ""))}
CAI_LANGUAGE = os.getenv("CAI_LANGUAGE", "español").strip() or "español"
CAI_MUSIC_COMMENTS = _env_bool("CAI_MUSIC_COMMENTS", True)
CAI_USE_PROFILE = _env_bool("CAI_USE_PROFILE", True)
CAI_ALLOW_DM = _env_bool("CAI_ALLOW_DM", True)

REPLY_TIMEOUT = 45
COMMENT_TIMEOUT = 15
DATA_DIR = Path(__file__).resolve().parent / "data"
CHATS_FILE = DATA_DIR / "cai_chats.json"
PROFILE_FILE = DATA_DIR / "cai_profile.json"
NO_MENTIONS = discord.AllowedMentions.none()


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _split(text: str, size: int = 2000) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]


class Persona(commands.Cog, name="Personaje"):
    """Charla con el personaje de Character.AI."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.enabled = bool(CAI_TOKEN and CAI_CHARACTER_ID and get_client)
        self.client = None
        self.character = None
        self.chats: dict[str, str] = _load_json(CHATS_FILE)
        self._lock = asyncio.Lock()
        self._broken = False
        self._started = False
        if not self.enabled:
            reason = "falta la librería PyCharacterAI" if get_client is None else "faltan CAI_TOKEN o CAI_CHARACTER_ID en .env"
            log.warning("Personaje desactivado (%s). Se usarán textos fijos.", reason)

    @property
    def name(self) -> str:
        if self.character and self.character.name:
            return self.character.name
        return self.bot.user.name if self.bot.user else "Bot"

    async def cog_unload(self) -> None:
        await self._close_client()

    # ---------- Conexión con Character.AI ----------

    async def _close_client(self) -> None:
        client, self.client = self.client, None
        if client:
            try:
                await client.close_session()
            except Exception:
                pass

    async def _ensure_client(self) -> None:
        if self._broken:
            await self._close_client()
            self._broken = False
        if self.client is None:
            self.client = await get_client(token=CAI_TOKEN)
            if self.character is None:
                self.character = await self.client.character.fetch_character_info(CAI_CHARACTER_ID)
                log.info("Personaje cargado: %s", self.character.name)

    async def _chat_for(self, key: str) -> str:
        chat_id = self.chats.get(key)
        if chat_id is None:
            chat, _ = await self.client.chat.create_chat(CAI_CHARACTER_ID, greeting=False)
            chat_id = chat.chat_id
            self.chats[key] = chat_id
            _save_json(CHATS_FILE, self.chats)
        return chat_id

    async def _send(self, key: str, text: str) -> Optional[str]:
        for attempt in (1, 2):
            try:
                await self._ensure_client()
                chat_id = await self._chat_for(key)
                turn = await self.client.chat.send_message(CAI_CHARACTER_ID, chat_id, text)
                candidate = turn.get_primary_candidate()
                if candidate is None or candidate.is_filtered or not candidate.text.strip():
                    return None
                return candidate.text.strip()
            except Exception as exc:
                log.warning("Error con Character.AI (intento %d): %s", attempt, exc)
                self._broken = True
                if attempt == 2 and isinstance(exc, ActionError):
                    # El chat pudo haberse borrado en Character.AI: la próxima vez se crea otro.
                    self.chats.pop(key, None)
                    _save_json(CHATS_FILE, self.chats)
        return None

    async def ask(self, key: str, text: str, timeout: float = REPLY_TIMEOUT) -> Optional[str]:
        """Manda un mensaje al personaje (una conversación por canal de Discord)."""
        if not self.enabled:
            return None
        holding = False
        try:
            async with asyncio.timeout(timeout):
                async with self._lock:
                    holding = True
                    return await self._send(key, text)
        except TimeoutError:
            log.warning("Character.AI tardó más de %ss en responder", timeout)
            if holding:
                self._broken = True  # la conexión quedó a medias, se rehace en el próximo mensaje
            return None

    async def comment(self, channel: discord.abc.Messageable, situation: str) -> Optional[str]:
        """Pide al personaje un comentario corto sobre algo que pasó (canción, error, etc.)."""
        if not (self.enabled and CAI_MUSIC_COMMENTS):
            return None
        prompt = (
            f"(({situation} Reacciona en personaje, en {CAI_LANGUAGE}, "
            f"con una o dos frases cortas.))"
        )
        return await self.ask(str(channel.id), prompt, COMMENT_TIMEOUT)

    # ---------- Perfil (nombre y avatar del personaje) ----------

    async def _apply_profile(self) -> None:
        if not (CAI_USE_PROFILE and self.character):
            return
        for guild in self.bot.guilds:
            await self._apply_nick(guild)

        avatar = getattr(self.character, "avatar", None)
        if not avatar or not avatar.get_file_name():
            return
        url = avatar.get_url().replace("webp=true", "webp=false")
        saved = _load_json(PROFILE_FILE)
        if saved.get("avatar_url") == url:
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    image = await resp.read()
            await self.bot.user.edit(avatar=image)
            _save_json(PROFILE_FILE, {"avatar_url": url, "sha1": hashlib.sha1(image).hexdigest()})
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
    async def on_ready(self) -> None:
        if self._started or not self.enabled:
            return
        self._started = True
        try:
            async with asyncio.timeout(60):
                async with self._lock:
                    await self._ensure_client()
        except Exception as exc:
            self._broken = True
            log.error("No se pudo conectar con Character.AI (¿token o ID del personaje incorrectos?): %s", exc)
            return
        await self._apply_profile()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        if CAI_USE_PROFILE and self.character:
            await self._apply_nick(guild)

    # ---------- Conversación ----------

    def _extract_text(self, message: discord.Message, ctx: commands.Context) -> Optional[str]:
        """Devuelve el texto para el personaje, o None si el mensaje no va dirigido al bot."""
        me = self.bot.user
        content = message.content
        addressed = False

        if ctx.prefix and content.startswith(ctx.prefix):
            content = content[len(ctx.prefix):]
            addressed = True
        if message.guild is None:
            addressed = addressed or CAI_ALLOW_DM
        if me in message.mentions:
            addressed = True
        ref = message.reference
        if ref and isinstance(ref.resolved, discord.Message) and ref.resolved.author == me:
            addressed = True
        if message.channel.id in CAI_CHANNELS:
            addressed = True
        if not addressed:
            return None

        for user in message.mentions:
            replacement = "" if user == me else user.display_name
            content = content.replace(f"<@{user.id}>", replacement).replace(f"<@!{user.id}>", replacement)
        content = content.strip()
        if message.attachments and not content:
            content = "(te manda un archivo)"
        return content or "(te saluda)"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not self.enabled:
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return  # es un comando de verdad, lo maneja su propio código
        text = self._extract_text(message, ctx)
        if text is None:
            return

        async with message.channel.typing():
            reply = await self.ask(str(message.channel.id), f"{message.author.display_name}: {text}")
        if reply is None:
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
        self.chats.pop(str(ctx.channel.id), None)
        _save_json(CHATS_FILE, self.chats)
        await ctx.send("🧠 Memoria de este canal borrada: la próxima charla empieza de cero.")

    @commands.command(name="personaje", help="Muestra qué personaje está usando el bot.")
    async def personaje(self, ctx: commands.Context) -> None:
        if not self.enabled:
            await ctx.send("El personaje está desactivado (falta configurar CAI_TOKEN y CAI_CHARACTER_ID en .env).")
            return
        if self.character is None:
            await ctx.send("No pude conectar con Character.AI. Revisa `logs/bot.log`.")
            return
        embed = discord.Embed(title=self.character.name, description=(self.character.title or "")[:4000], color=0x9B59B6)
        embed.set_footer(text=f"ID: {CAI_CHARACTER_ID}")
        await ctx.send(embed=embed)


# ---------- Ayudas para el resto del bot ----------

_background: set[asyncio.Task] = set()


def _persona(bot: commands.Bot) -> Optional[Persona]:
    cog = bot.get_cog("Personaje")
    return cog if isinstance(cog, Persona) and cog.enabled else None


async def say(
    bot: commands.Bot,
    channel: discord.abc.Messageable,
    situation: str,
    info: str,
    **kwargs,
) -> None:
    """Envía `info` (texto fijo) acompañado del comentario del personaje sobre `situation`."""
    persona = _persona(bot)
    line = None
    if persona:
        async with channel.typing():
            line = await persona.comment(channel, situation)
    content = f"{line}\n-# {info}" if line and info else (line or info or None)
    try:
        await channel.send(content, allowed_mentions=NO_MENTIONS, **kwargs)
    except discord.HTTPException as exc:
        log.warning("No se pudo enviar el mensaje: %s", exc)


def comment_later(bot: commands.Bot, channel: discord.abc.Messageable, situation: str) -> None:
    """Comentario opcional en segundo plano (para comandos que ya respondieron con una reacción)."""
    persona = _persona(bot)
    if not (persona and CAI_MUSIC_COMMENTS):
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
