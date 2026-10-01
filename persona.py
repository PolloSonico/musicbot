"""Personaje con IA: le da voz propia al bot.

- Responde cuando le hablan: mencionándolo, respondiendo a sus mensajes, con el prefijo
  seguido de algo que no es un comando (ej: "!hola Lillia"), por DM, o en los canales
  de PERSONA_CHANNELS (ahí responde a todo).
- El resto del bot usa say() / comment_later() para que los avisos (canción en cola,
  desconexión, errores...) los diga el personaje en vez de textos fijos.
- La IA NUNCA bloquea la música: los avisos se mandan al instante con el texto fijo y la frase
  del personaje se añade después (editando el mensaje) cuando llega. Si la IA no está configurada,
  no responde a tiempo o se acabó el cupo diario, queda el texto fijo.

La IA es Google Gemini (GEMINI_API_KEY en .env).
"""

import asyncio
import contextvars
import hashlib
import logging
import os
import random
import re
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

import discord
from discord.ext import commands

import avisos
import historial_canciones
import letras
import memoria_personas
from datadragon import dd
from datadragon import normalize as normalize_name
from jsonio import load_json, save_json

log = logging.getLogger("persona")


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value is not None and value.strip() else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name).lower()
    return default if not value else value in ("1", "true", "si", "sí", "yes", "y")


GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODELS = [m.strip() for m in _env("GEMINI_MODEL").split(",") if m.strip()]

CHANNELS = {int(x) for x in re.findall(r"\d+", _env("PERSONA_CHANNELS"))}
LANGUAGE = _env("PERSONA_LANGUAGE", default="español")
MUSIC_COMMENTS = _env_bool("PERSONA_MUSIC_COMMENTS", default=True)
USE_PROFILE = _env_bool("PERSONA_USE_PROFILE", default=True)
ALLOW_DM = _env_bool("PERSONA_ALLOW_DM", default=True)
# true = cuando preguntan por builds / picks / runas de League, la IA busca en Google datos del
# parche actual (u.gg, op.gg, lolalytics, leagueofgraphs...) antes de responder.
LOL_SEARCH = _env_bool("PERSONA_LOL_SEARCH", default=True)
# true = cuando le hablan directamente, Lillia PUEDE buscar en Google si hace falta un dato actual (clima,
# noticias, horarios, precios...). Ella decide: en la charla normal no busca (y no gasta cupo de búsquedas).
AUTO_SEARCH = _env_bool("PERSONA_AUTO_SEARCH", default=True)
# true = se le puede pedir música con palabras normales ("@Lillia poneme Tik Tok de Kesha")
MUSIC_CONTROL = _env_bool("PERSONA_MUSIC_CONTROL", default=True)
# Curiosidades sobre la canción que suena: probabilidad por canción y máximo por día.
TRIVIA_CHANCE = float(_env("PERSONA_TRIVIA_CHANCE", default="0.15"))
TRIVIA_PER_DAY = int(_env("PERSONA_TRIVIA_PER_DAY", default="1"))

# Probabilidad (0 a 1) de reaccionar con un emoji cuando alguien nombra al personaje sin hablarle.
NAME_REACT_CHANCE = float(_env("PERSONA_NAME_REACT", default="0.5"))
NAME_REACT_COOLDOWN = 90  # segundos mínimos entre reacciones de este tipo en un mismo canal
NAME_REACT_DEFAULT = ["🌸", "🦌", "✨", "😳", "🥺", "💤", "🌿"]
NAME_REACT_KEYWORDS = ("lillia", "flor", "flower", "heart", "love", "corazon", "sleep", "dream", "deer",
                       "ciervo", "uwu", "cute", "blush", "shy", "hug", "abrazo")
MAX_SERVER_EMOJIS = 60  # cuántos emojis del servidor se le muestran a la IA
MAX_EMOJIS_PER_MESSAGE = 2  # si la IA pone más, se dejan solo los primeros
AY_EVERY = 4  # el "¡Ay!" al empezar un mensaje se permite como mucho una vez cada tantos mensajes
AY_START_RE = re.compile(r"^(\s*)(¡\s*)?ay+\s*([,!…]+)\s*", re.I)
# Emojis unicode (con sus variantes y combinaciones) y emojis propios del servidor <:nombre:id>.
EMOJI_RE = re.compile(
    r"<a?:\w+:\d+>|(?:[\U0001F1E6-\U0001F1FF]{2})|"
    r"(?:[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u2190-\u21FF\u2934\u2935\u3030\u303D\u3297\u3299]"
    r"[\uFE0F\U0001F3FB-\U0001F3FF]?(?:\u200D[\U0001F300-\U0001FAFF\u2600-\u27BF][\uFE0F]?)*)"
)

# Imágenes que la IA puede ver (Gemini no acepta GIF).
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
MAX_IMAGES = 3
MAX_IMAGE_BYTES = 7 * 1024 * 1024

REPLY_TIMEOUT = 45
SEARCH_TIMEOUT = 75
COMMENT_TIMEOUT = 15
DATA_DIR = Path(__file__).resolve().parent / "data"
PROFILE_FILE = DATA_DIR / "perfil.json"
TRIVIA_FILE = DATA_DIR / "curiosidades.json"
NO_MENTIONS = discord.AllowedMentions.none()


def _local_tz() -> str:
    """Zona horaria de la PC del bot, para que dé los horarios en hora local (ej: "UTC-3")."""
    offset = datetime.now().astimezone().utcoffset()
    hours = int(offset.total_seconds() // 3600) if offset is not None else 0
    minutes = int(abs(offset.total_seconds()) % 3600 // 60) if offset is not None else 0
    return f"UTC{hours:+d}" + (f":{minutes:02d}" if minutes else "")
# True mientras se ejecuta una orden que el personaje pidió en la charla ([[PLAY]], [[SKIP]]...): como ya
# respondió a la persona, los avisos de esa orden salen sin un segundo comentario de la IA.
CHAT_ACTION: contextvars.ContextVar[bool] = contextvars.ContextVar("chat_action", default=False)


# Órdenes que el personaje puede añadir a su respuesta: [[PLAY: búsqueda]], [[SKIP]], ...
ACTION_RE = re.compile(
    r"\[\[\s*(PLAY|BUSCAR|SKIP|STOP|PAUSE|RESUME|LOOP|VOLUME|LEAVE|RADIO|DADO|RECORDATORIO|RECORDAR|REACCI[OÓ]N)\s*(?::\s*([^\]\n]*?))?\s*\]\]", re.I
)
ACTION_COMMANDS = {
    "play": "play", "buscar": "buscar", "skip": "skip", "stop": "stop", "pause": "pause",
    "resume": "resume", "loop": "loop", "volume": "volume", "leave": "leave", "radio": "radio",
}
# Órdenes que no son de música (se obedecen aunque el control de la música por chat esté apagado).
OTHER_COMMANDS = {"dado": "dado"}

# "¿Alguien está en partida?", "¿@Lucía está jugando?": se consulta a Riot quién juega ahora.
LIVE_RE = re.compile(r"\b(en (?:una |la )?partida|jugando|in ?game|en (?:una )?(?:ranked|aram|arena|normal|flex))\b", re.I)
LIVE_EVERYONE_RE = re.compile(r"\b(alguien|alguno|alguna|qui[eé]n(?:es)?|nadie|los chicos|todos|la gente)\b", re.I)
LIVE_HINT_RE = re.compile(r"\b(lol|league|partida|ranked|aram|arena|flex|est[aá]n?|anda|andan)\b|\?", re.I)

# Preguntas sobre League of Legends que necesitan datos actuales (builds, runas, picks...).
LOL_RE = re.compile(
    r"\b(builds?|runas?|runes?|objetos?|items?|itemiza\w*|counters?|counterea\w*|pick(?:s|eo|ear|eas)?|"
    r"tier ?list|meta|parche|patch|matchups?|win ?rate|orden de (?:habilidades|skills)|skill order|"
    r"campe[oó]n(?:es)? (?:para|contra|fuerte|roto)|qu[eé] (?:juego|pickeo|compro))\b",
    re.I,
)
LOL_INSTRUCTIONS = (
    "La persona pregunta por League of Legends y necesita datos ACTUALES. Busca en Google estadísticas "
    "del parche actual en sitios como u.gg, op.gg, lolalytics, leagueofgraphs o mobalytics, y da "
    "recomendaciones concretas y correctas (campeones, runas, objetos principales, orden de "
    "habilidades o counters, según lo que pregunte), mencionando el parche. Sigue hablando como tú "
    "(con tu personalidad y emojis), pero los datos deben ser exactos: no inventes estadísticas. "
    "Puedes usar una lista corta si ayuda a leerlo. Si la persona tiene su cuenta de League vinculada "
    "(lo verás en \"Lo que está pasando ahora\"), ten en cuenta su rango y sus campeones más jugados."
)

# Preguntas sobre la escena competitiva (Mundial, MSI, ligas, equipos): también buscan en Google.
ESPORTS_RE = re.compile(
    r"\b(worlds|mundial(?:es)? (?:de )?(?:lol|league)|msi|lck(?: ?cl)?|lpl|lec|lcs|lta|lcp|cblol|lla|nacl|ldl|ljl|vcs|"
    r"emea masters|liga (?:latinoamericana|regional)|first stand|esports?|lolesports|fase suiza|swiss stage|play-?ins?|"
    r"t1|faker|gen\.?g|g2|fnatic|hanwha|hle|bilibili|blg|jdg|top esports|kt rolster|fearless|cloud ?9|team liquid|"
    r"100 thieves|flyquest|dignitas|shopify rebellion|lyon gaming|disguised|isurus|leviat[aá]n|estral|movistar koi|"
    r"vivo keyd|pain gaming|loud|furia|red canids|fluxo|dplus|nongshim|drx|fearx|weibo|anyone'?s legend|"
    r"pro ?players?|jugador(?:es)? profesional(?:es)?|equipo profesional|competitivo)\b",
    re.I,
)
# "¿Cuándo vuelve a jugar X?", "¿contra quién juega T1?", "¿quién ganó la final?": datos de HOY, hay que buscar.
SCHEDULE_RE = re.compile(
    r"\b(cu[aá]ndo (?:vuelve a |va a |le toca )?ju[eé]?g\w*|cu[aá]ndo (?:es|son|empieza|arranca|termina) (?:el|la|los|las)|"
    r"pr[oó]xim[oa]s? (?:partid\w*|fecha|serie|match|enfrentamiento|rival)|a qu[eé] hora (?:juega|es|empieza)|"
    r"contra qui[eé]n (?:juega|le toca)|calendario|fixture|cronograma|qui[eé]n gan[oó]|c[oó]mo (?:sali[oó]|qued[oó]|"
    r"le fue a|les fue a)|resultados? de)",
    re.I,
)
# Clima, noticias, precios: también cosas de HOY.
NOW_RE = re.compile(
    r"\b(llov\w*|lluvi\w*|clima|pron[oó]stico|temperatura|cu[aá]nto calor|cu[aá]nto fr[ií]o|tormenta|nieva|nevar|"
    r"granizo|viento|humedad|sensaci[oó]n t[eé]rmica|noticias?|qu[eé] pas[oó] con|cotizaci[oó]n|d[oó]lar (?:hoy|blue|oficial)|"
    r"a cu[aá]nto est[aá]|cu[aá]nto (?:sale|cuesta|vale) (?:hoy|ahora)|estreno|cu[aá]ndo sale)\b",
    re.I,
)
AUTO_SEARCH_NOTE = (
    "Si para responder necesitas un dato actual que no sabes (clima, noticias, horarios, precios, resultados), "
    "búscalo en Google; para la charla normal no busques. Nunca inventes datos actuales."
)
ESPORTS_INSTRUCTIONS = (
    "La persona pregunta por la escena competitiva de League of Legends (Mundial/Worlds, MSI, ligas, "
    "equipos o jugadores). Busca en Google resultados, calendario y noticias ACTUALES (lolesports.com, "
    "Liquipedia, medios de esports) y responde con datos exactos y con fecha. Si pregunta cuándo juega un "
    "jugador, busca en qué equipo está HOY y el próximo partido de ese equipo; da día y hora convertidos a "
    "la hora de ustedes ({tz}). No inventes resultados ni fechas: si un partido todavía no se jugó o no "
    "encuentras la fecha, dilo. Sigue hablando como tú, con tu personalidad."
)
CURRENT_INSTRUCTIONS = (
    "La persona pregunta por algo que pasa AHORA o pronto (un partido, una fecha, un resultado, el clima, una "
    "noticia, un precio). Busca en Google y responde con el dato exacto y concreto: días y horas convertidos a "
    "la hora de ustedes ({tz}); si es el clima, el pronóstico de los días que pregunta (probabilidad de lluvia y "
    "temperaturas) para ese lugar. Si no dice de qué deporte o juego habla y el nombre es de un jugador o equipo "
    "de League of Legends, es de esports de LoL. No inventes nada: si no lo encuentras, dilo. Sigue hablando "
    "como tú, con tu personalidad (sin dejar afuera el dato)."
)

# Preguntas sobre la canción que suena: se le pasa la letra real para que hable con propiedad.
SONG_RE = re.compile(
    r"\b(canci[oó]n|tema|letra|lyrics?|song|suena|sonando|qu[eé] dice|de qu[eé] (?:trata|habla)|"
    r"significa|mensaje|cantante|artista|banda)\b",
    re.I,
)

# Preguntas sobre las partidas/rango de alguien con la cuenta vinculada: se piden datos reales a Riot.
LOL_STATS_RE = re.compile(
    r"\b(partidas?|jugu[eé]|jug[oó]|kda|historial|rango|elo|lp|liga|clasificatorias?|ranked|rankeds?|aram|"
    r"arena|gan[eé]|gan[oó]|perd[ií]|perdi[oó]|racha|mains?|con qu[eé] (?:campe[oó]n|personaje)|me fue|le fue)\b",
    re.I,
)

# Estado de ánimo según la hora del día (en la hora del PC donde corre el bot).
MOODS = [
    (0, 6, "Es de madrugada: estás muy dormida, bostezas (🥱💤), hablas más lento y con frases más "
           "cortas. Si te piden elegir música, sugieres algo tranquilo para dormir."),
    (6, 12, "Es de mañana: te estás despertando, dulce y todavía un poco lenta ☀️🌸."),
    (12, 20, "Es de día: estás despierta, activa y con ganas de jugar y charlar ✨."),
    (20, 24, "Es de noche: estás tranquila y soñadora 🌙; empiezas a pensar en los sueños de todos "
             "y prefieres música más suave."),
]


def current_mood() -> str:
    hour = datetime.now().hour
    return next(text for start, end, text in MOODS if start <= hour < end)
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
    """Crea la IA (Gemini) según el .env, o None si no está configurada."""
    try:
        if not GEMINI_API_KEY:
            raise RuntimeError("falta GEMINI_API_KEY en .env")
        from ia_gemini import GeminiBackend
        from personaje import load_character

        return GeminiBackend(GEMINI_API_KEY, load_character(), LANGUAGE, GEMINI_MODELS, MUSIC_CONTROL)
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
        self._name_reacted: dict[int, float] = {}  # canal -> última reacción por nombrarla
        self._recent_starts: dict[int, deque[bool]] = {}  # canal -> si sus últimos mensajes empezaron con "¡Ay!"
        if self.backend is not None:
            self.backend.alert = lambda key, text, until: avisos.notify(bot, key, text, until)

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
        await dd.close()
        await letras.close()

    # ---------- Contexto: qué está pasando con la música ----------

    def note(self, channel: discord.abc.Messageable, situation: str) -> None:
        """Guarda un aviso de la música (aunque la IA no lo comente) para que después lo sepa."""
        events = self._events.setdefault(channel.id, deque(maxlen=6))
        events.append(f"[{datetime.now():%H:%M}] {situation}")

    def _context(self, channel: discord.abc.Messageable, people: Optional[list] = None, extra: str = "") -> str:
        now = datetime.now()
        weekday = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"][now.weekday()]
        parts = [f"Hoy es {weekday} {now:%Y-%m-%d}. Hora actual: {now:%H:%M}. Tu estado de ánimo: {current_mood()}"]
        try:
            from calendario import active_events_text

            calendar = active_events_text()
            if calendar:
                parts.append(calendar)
        except Exception:
            log.exception("No se pudo leer el calendario de eventos")
        if extra:
            parts.append(extra)
        # Gustos musicales y lo que recuerda de quien habla y de las personas que menciona.
        for person in people or []:
            try:
                songs = historial_canciones.summary(person)
                remembered = memoria_personas.summary(person)
            except Exception:
                songs = remembered = None
            parts.append(songs or f"{person.display_name} todavía no te pidió ninguna canción.")
            if remembered:
                parts.append(remembered)
            try:
                from riot_cuentas import summary as lol_summary  # cuenta de League vinculada (rango, mains)

                lol = lol_summary(person)
            except Exception:
                lol = None
            if lol:
                parts.append(lol)
        guild = getattr(channel, "guild", None)
        if guild is not None:
            emojis = server_emojis(guild)
            if emojis:
                parts.append(
                    "Emojis propios de este servidor (úsalos escribiendo :nombre:): "
                    + " ".join(f":{e.name}:" for e in emojis)
                )
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
        remember: Optional[bool] = None,
        search: bool = False,
        extra: str = "",
        images: Optional[list[tuple[str, bytes]]] = None,
    ) -> Optional[str]:
        """Manda un mensaje al personaje (una conversación por canal). None si no hay respuesta.
        remember=False: no queda en la memoria de la charla (avisos automáticos, radio...).
        images: imágenes que puede ver junto al mensaje."""
        if not self.available():
            return None
        try:
            async with asyncio.timeout(timeout):
                context = self._context(channel, people, extra)
                reply = await self.backend.ask(
                    str(channel.id), text, context, wait, fast, remember, search, images=images
                )
                if not reply:
                    return reply
                limited = self._limit_emojis(channel, reply)
                # En la memoria de la charla queda la versión recortada: si no, se imitaría a sí misma
                # con los emojis de más.
                turns = getattr(self.backend, "history", {}).get(str(channel.id))
                if turns and turns[-1].get("role") == "model":
                    remembered = self._strip_extra_emojis(turns[-1]["parts"][0].get("text", ""))
                    if getattr(self, "_ay_dropped", False):
                        remembered = _drop_ay(remembered)
                    turns[-1]["parts"][0]["text"] = remembered
                guild = getattr(channel, "guild", None)
                return apply_server_emojis(limited, guild) if guild else limited
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
            saved = load_json(PROFILE_FILE, {})
            if isinstance(saved, dict) and saved.get("sha1") == digest:
                return
            await self.bot.user.edit(avatar=image)
            save_json(PROFILE_FILE, {"sha1": digest})
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

    @staticmethod
    def _strip_extra_emojis(text: str) -> str:
        kept: list[str] = []

        def keep(match: re.Match) -> str:
            if match.group(0) in kept or len(kept) >= MAX_EMOJIS_PER_MESSAGE:
                return ""
            kept.append(match.group(0))
            return match.group(0)

        return re.sub(r"[ \t]{2,}", " ", EMOJI_RE.sub(keep, text)).strip()

    def _limit_emojis(self, channel: discord.abc.Messageable, text: str) -> str:
        """Deja como mucho MAX_EMOJIS_PER_MESSAGE emojis (los primeros, sin repetir el mismo en un
        mensaje) y evita que empiece seguido con "¡Ay!". Las órdenes [[...]] no se tocan."""
        kept: list[str] = []

        def keep_or_drop(match: re.Match) -> str:
            emoji = match.group(0)
            if emoji in kept:
                return ""  # repetido en el mismo mensaje (🌸🌸)
            if len(kept) >= MAX_EMOJIS_PER_MESSAGE:
                return ""
            kept.append(emoji)
            return emoji

        body, sep, actions = text.partition("[[")
        body = EMOJI_RE.sub(keep_or_drop, body)
        body = re.sub(r"[ \t]{2,}", " ", body)
        body = re.sub(r"[ \t]+([,.!?…])", r"\1", body)
        body = "\n".join(line.rstrip() for line in body.splitlines()).strip() or (kept[0] if kept else "✨")
        body = self._limit_ay(channel, body)
        return body + (("\n" + sep + actions) if sep else "")

    def _limit_ay(self, channel: discord.abc.Messageable, text: str) -> str:
        """Si ya empezó con "¡Ay!" hace poco, se lo saca a este mensaje:
        "¡Ay, otra canción!" -> "¡Otra canción!"; "¡Ay! Qué lindo" -> "Qué lindo"."""
        self._ay_dropped = False
        starts = self._recent_starts.setdefault(getattr(channel, "id", 0), deque(maxlen=AY_EVERY - 1))
        match = AY_START_RE.match(text)
        if not match:
            starts.append(False)
            return text
        if not any(starts):
            starts.append(True)
            return text  # hace rato que no lo dice: se deja
        starts.append(False)
        self._ay_dropped = True
        return _drop_ay(text)

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
            images = any((a.content_type or "").startswith("image/") for a in message.attachments)
            content = "(te manda una imagen)" if images else "(te manda un archivo)"
        # Que nadie pueda "dictarle" órdenes ocultas escribiéndolas en su mensaje.
        content = content.replace("[[", "[ [").replace("]]", "] ]")
        return content or "(te saluda)", direct

    async def addressed(self, message: discord.Message) -> bool:
        """True si el mensaje es para el bot (comando, mención, respuesta, DM o canal de charla)."""
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return True
        return self._extract_text(message, ctx)[0] is not None

    async def _images(self, message: discord.Message) -> list[tuple[str, bytes]]:
        """Imágenes del mensaje (y del mensaje al que responde) para que la IA las vea."""
        attachments = list(message.attachments)
        ref = message.reference
        if ref and isinstance(ref.resolved, discord.Message):
            attachments += ref.resolved.attachments
        images = []
        for attachment in attachments:
            mime = (attachment.content_type or "").split(";")[0].strip().lower()
            if mime not in IMAGE_TYPES or attachment.size > MAX_IMAGE_BYTES:
                continue
            try:
                images.append((mime, await attachment.read()))
            except discord.HTTPException as exc:
                log.warning("No se pudo descargar la imagen %s: %s", attachment.filename, exc)
            if len(images) >= MAX_IMAGES:
                break
        return images

    async def _maybe_react_to_name(self, message: discord.Message) -> None:
        """Si alguien la nombra sin hablarle ("le pregunté a Lillia..."), a veces reacciona con un
        emoji. No gasta IA."""
        if NAME_REACT_CHANCE <= 0 or message.guild is None:
            return
        names = {self.name.split()[0].lower(), "lillia"} if self.name else {"lillia"}
        if not re.search(r"\b(" + "|".join(map(re.escape, names)) + r")\b", message.content, re.I):
            return
        now = time.monotonic()
        if now - self._name_reacted.get(message.channel.id, -1e9) < NAME_REACT_COOLDOWN:
            return
        if random.random() >= NAME_REACT_CHANCE:
            return
        self._name_reacted[message.channel.id] = now
        themed = [e for e in server_emojis(message.guild) if any(k in e.name.lower() for k in NAME_REACT_KEYWORDS)]
        emoji = random.choice(themed) if themed else random.choice(NAME_REACT_DEFAULT)
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    async def _react(self, message: discord.Message, emoji_text: str) -> None:
        """Orden [[REACCION: emoji]] de la IA."""
        emoji_text = emoji_text.strip()
        if not emoji_text:
            return
        try:
            await message.add_reaction(discord.PartialEmoji.from_str(emoji_text))
        except (discord.HTTPException, ValueError, TypeError):
            log.info("No se pudo reaccionar con %r", emoji_text)

    async def _say_sleeping(self, message: discord.Message) -> None:
        """Sin cupo de IA: avisa una sola vez por canal y después solo reacciona con 😴."""
        until = self.backend.available_again_at()
        channel_id = message.channel.id
        try:
            if self._sleep_notice.get(channel_id, 0) >= (until or 0) and channel_id in self._sleep_notice:
                await message.add_reaction("😴")
                return
            self._sleep_notice[channel_id] = until or 0
            when = avisos.fecha_hora(until) if until else "en un rato"
            await message.reply(
                f"😴 *{self.name} se quedó dormida: se acabó el límite de la IA por hoy. "
                f"Vuelve a hablar {when}. La música sigue funcionando.*",
                mention_author=False,
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return  # es un comando de verdad, lo maneja su propio código
        text, direct = self._extract_text(message, ctx)
        if text is None:
            await self._maybe_react_to_name(message)
            return
        if self.backend is None:
            return
        if not self.backend.available():
            if direct:
                await self._say_sleeping(message)
            return

        lol_question = LOL_SEARCH and bool(LOL_RE.search(text))
        esports_question = LOL_SEARCH and bool(ESPORTS_RE.search(text))
        current_question = LOL_SEARCH and not esports_question and bool(SCHEDULE_RE.search(text) or NOW_RE.search(text))
        explicit_search = lol_question or esports_question or current_question
        # Le hablaron directamente: puede buscar si lo necesita (lo decide ella).
        auto_search = AUTO_SEARCH and direct and not explicit_search
        search = explicit_search or auto_search
        async with message.channel.typing():
            people = [message.author] + [
                user for user in message.mentions if user != self.bot.user and not user.bot
            ][:3]
            images = await self._images(message)
            extra = await self._league_context(text, lol_question, esports_question)
            if current_question:
                extra = f"{CURRENT_INSTRUCTIONS.format(tz=_local_tz())}\n\n{extra}".strip()
            elif auto_search:
                extra = f"{extra}\n\n{AUTO_SEARCH_NOTE}".strip()
            paused = getattr(self.backend, "search_paused_until", 0)
            if explicit_search and paused > time.time():
                extra += (f"\n\nIMPORTANTE: ahora mismo NO puedes buscar en Google (se acabó el cupo de búsquedas "
                          f"hasta las {datetime.fromtimestamp(paused):%H:%M}). Si no sabes el dato exacto, dilo así y "
                          "que te pregunten de nuevo después de esa hora. NO inventes fechas, horarios ni resultados.")
            if SONG_RE.search(text) and message.guild is not None:
                song = await self._current_song_lyrics(message.guild, message.author)
                extra = f"{extra}\n\n{song}".strip() if song else extra
            if LIVE_RE.search(text) and LIVE_HINT_RE.search(text):
                live = await self._live_context(message, people)
                extra = f"{extra}\n\n{live}".strip() if live else extra
            if LOL_STATS_RE.search(text):
                stats = await self._riot_context(people)
                extra = f"{extra}\n\n{stats}".strip() if stats else extra
            reply = await self.ask(
                message.channel,
                f"{message.author.display_name}: {text}",
                SEARCH_TIMEOUT if search else REPLY_TIMEOUT,
                people=people,
                search=search,
                extra=extra,
                images=images,
            )
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
        for name, arg in actions:
            if name == "recordar":
                memoria_personas.add(message.author, arg)  # solo datos de quien habla
            elif name.startswith("reacci"):
                await self._react(message, arg)
            elif name == "recordatorio" and direct and message.guild is not None:
                reminders = self.bot.get_cog("Recordatorios")
                if reminders is not None:
                    await reminders.from_chat(message, arg)
        music_actions = [(name, arg) for name, arg in actions if name in ACTION_COMMANDS]
        # Solo se obedece si le hablaron directamente y dentro de un servidor (la música va por servidor).
        if music_actions and direct and MUSIC_CONTROL and message.guild is not None:
            await self._run_actions(message, music_actions)
        other_actions = [(name, arg) for name, arg in actions if name in OTHER_COMMANDS][:3]
        if other_actions and direct:
            await self._run_actions(message, other_actions)

    async def _live_context(self, message: discord.Message, people: list) -> str:
        """Quién está jugando ahora: las personas mencionadas, todo el servidor ("¿alguien...?") o quien pregunta."""
        riot = self.bot.get_cog("League")
        if riot is None or not hasattr(riot, "live_context"):
            return ""
        others = people[1:]
        everyone = not others and bool(LIVE_EVERYONE_RE.search(message.content or ""))
        try:
            return await asyncio.wait_for(riot.live_context(message.guild, others or people[:1], everyone), 15)
        except Exception as exc:
            log.info("No se pudo ver quién está jugando: %s", exc)
            return ""

    async def _riot_context(self, people: list) -> str:
        """Últimas partidas reales (Riot API) de quien habla y de los que menciona, si están vinculados."""
        riot = self.bot.get_cog("League")
        if riot is None or not hasattr(riot, "chat_context"):
            return ""
        parts = []
        for person in people[:2]:
            try:
                block = await asyncio.wait_for(riot.chat_context(person), 12)
            except Exception as exc:
                log.info("No se pudieron traer las partidas de %s: %s", person, exc)
                continue
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    async def _current_song_lyrics(self, guild: discord.Guild, member=None) -> str:
        """Letra de la canción que suena para esa persona (o en el servidor), o ''."""
        music = self.bot.get_cog("Música")
        track = music.current_track(guild.id, member) if music and hasattr(music, "current_track") else None
        if track is None:
            return ""
        lyrics = await letras.get_lyrics(track.title, track.uploader, track.duration, track.url)
        block = letras.for_ai(lyrics)
        return f"Sobre la canción que suena ahora ('{track.title}'): {block}" if block else ""

    async def _league_context(self, text: str, lol_question: bool, esports_question: bool) -> str:
        """Datos de League para la IA: instrucciones de búsqueda, parche actual (Data Dragon),
        objetos que existen y el kit de los campeones nombrados."""
        parts = []
        try:
            if dd.version is None:
                await asyncio.wait_for(dd.refresh(), 8)
            if lol_question:
                parts += [LOL_INSTRUCTIONS, dd.patch_text(), await asyncio.wait_for(dd.items_text(), 8)]
            if esports_question:
                parts.append(ESPORTS_INSTRUCTIONS.format(tz=_local_tz()))
            own_name = frozenset({normalize_name(self.name)})  # "hola Lillia" no necesita su propio kit
            parts.append(await asyncio.wait_for(dd.champions_text(text, own_name), 8))
        except Exception as exc:
            log.warning("No se pudieron preparar los datos de League: %s", exc)
            if lol_question and LOL_INSTRUCTIONS not in parts:
                parts.insert(0, LOL_INSTRUCTIONS)
        return "\n\n".join(p for p in parts if p)

    async def _run_actions(self, message: discord.Message, actions: list[tuple[str, str]]) -> None:
        """Ejecuta las órdenes de música como si la persona hubiera escrito el comando."""
        ctx = await self.bot.get_context(message)
        for name, arg in actions:
            command = self.bot.get_command(ACTION_COMMANDS.get(name) or OTHER_COMMANDS[name])
            if command is None:
                continue
            ctx.command = command
            ctx.invoked_with = command.name
            log.info("%s pidió por chat: %s %s", message.author.display_name, name, arg)
            error: Optional[commands.CommandError] = None
            token = CHAT_ACTION.set(True)
            try:
                if name in ("play", "buscar"):
                    await ctx.invoke(command, busqueda=arg)
                elif name == "dado":
                    await ctx.invoke(command, tirada=arg or "1d20")
                elif name == "radio":
                    await ctx.invoke(command, modo=arg or None)
                elif name == "volume":
                    number = re.search(r"\d+", arg)
                    if number:
                        await ctx.invoke(command, nivel=int(number.group()))
                else:
                    await ctx.invoke(command)
            except commands.CommandError as exc:
                error = exc
            except Exception as exc:
                error = commands.CommandInvokeError(exc)
            finally:
                CHAT_ACTION.reset(token)
            if error is not None:  # los errores sí los comenta (no había dicho nada de eso)
                await self.bot.on_command_error(ctx, error)

    # ---------- Comandos ----------

    @commands.command(name="reset", aliases=["olvidar"], help="Borra la memoria del personaje en este canal.")
    async def reset(self, ctx: commands.Context) -> None:
        if self.backend:
            self.backend.reset(str(ctx.channel.id))
        await ctx.send("🧠 Memoria de este canal borrada: la próxima charla empieza de cero.")

    @commands.command(name="quesabes", aliases=["memoria", "quesabesdemi"], help="Lo que el personaje recuerda de ti.")
    async def quesabes(self, ctx: commands.Context) -> None:
        known = memoria_personas.facts(ctx.author)
        if not known:
            await ctx.send(f"🌸 Todavía no recuerdo nada especial de ti, {ctx.author.display_name}... ¡cuéntame cosas!")
            return
        embed = discord.Embed(
            title=f"🧠 Lo que {self.name} recuerda de {ctx.author.display_name}",
            description="\n".join(f"• {fact}" for fact in known),
            color=0x9B59B6,
        )
        embed.set_footer(text="Para borrarlo: !olvidame (todo) o !olvidame <palabra> (solo lo que la contenga)")
        await ctx.send(embed=embed)

    @commands.command(name="olvidame", aliases=["olvídame"], help="Borra lo que el personaje recuerda de ti. Uso: olvidame [palabra]")
    async def olvidame(self, ctx: commands.Context, *, que: str = "") -> None:
        removed = memoria_personas.forget(ctx.author, que)
        if removed:
            await ctx.send(f"🍃 Listo, olvidé {removed} cosa{'s' if removed != 1 else ''} sobre ti.")
        else:
            await ctx.send("No tenía nada guardado sobre eso 🌸")

    @commands.command(name="cupo", aliases=["quota"], help="Cuánto cupo de IA se usó hoy y cuánto queda.")
    async def cupo(self, ctx: commands.Context) -> None:
        if self.backend is None:
            await ctx.send("La IA está desactivada.")
            return
        await ctx.send(embed=quota_embed(self.backend))

    @commands.command(name="personaje", help="Muestra qué personaje e IA está usando el bot.")
    async def personaje(self, ctx: commands.Context) -> None:
        if self.backend is None:
            await ctx.send("El personaje está desactivado (falta configurar la IA en .env). Revisa `logs/bot.log`.")
            return
        if self.backend.available():
            status = "🟢 Despierta"
        else:
            until = self.backend.available_again_at()
            status = f"😴 Sin cupo de IA, vuelve {avisos.fecha_hora(until)}" if until else "😴 Sin cupo de IA"
        embed = discord.Embed(title=self.backend.name, description=self.backend.description[:4000], color=0x9B59B6)
        embed.add_field(name="IA", value=self.backend.provider)
        embed.add_field(name="Estado", value=status)
        if hasattr(self.backend, "ranking"):
            lines = []
            for model, seconds, fails in self.backend.ranking()[:6]:
                speed = f"{seconds:.1f}s" if seconds is not None else "sin datos"
                lines.append(f"`{model}` · {speed} · fallos {fails:.0%}")
            embed.add_field(name="Modelos (en orden de preferencia)", value="\n".join(lines) or "-", inline=False)
        if dd.patch:
            embed.add_field(name="Parche de League", value=dd.patch)
        await ctx.send(embed=embed)


# ---------- Ayudas para el resto del bot ----------

def _bar(fraction: float, size: int = 10) -> str:
    filled = max(0, min(size, round(fraction * size)))
    return "▰" * filled + "▱" * (size - filled)


def quota_embed(backend) -> discord.Embed:
    """Uso de la IA de hoy: Gemini (con el límite aprendido) y la IA de respaldo (con lo que informa)."""
    import avisos

    embed = discord.Embed(title="🔋 Cupo de la IA de hoy", color=0x9B59B6)
    lines = []
    for model, used, tokens, limit in backend.usage_report() if hasattr(backend, "usage_report") else []:
        paused = backend.cooldowns.get(model, 0) > time.time()
        state = " · 😴 sin cupo" if paused and limit else " · ⏸️ en pausa" if paused else ""
        if limit:
            pct = min(used / limit, 1)
            lines.append(f"`{model}`\n{_bar(pct)} **{pct:.0%}** ({used}/{limit} pedidos · {tokens / 1000:.0f}k tokens){state}")
        else:
            lines.append(f"`{model}`: {used} pedidos · {tokens / 1000:.0f}k tokens (límite todavía desconocido){state}")
    if not lines:
        lines.append("Todavía no se usó hoy.")
    gemini_text = "\n".join(lines)
    if not backend.gemini_available():
        until = backend.available_again_at()
        gemini_text += f"\n😴 Sin cupo, vuelve {avisos.fecha_hora(until)}" if until else "\n😴 Sin cupo"
    embed.add_field(name="Gemini", value=gemini_text[:1024], inline=False)
    backup = getattr(backend, "backup", None)
    if backup:
        rows = []
        for model, used, tokens, limit, remaining in backup.report():
            if limit and remaining is not None:
                pct = 1 - remaining / limit
                rows.append(f"`{model}`\n{_bar(pct)} **{pct:.0%}** usado (quedan {remaining} de {limit} pedidos hoy)")
            elif used:
                rows.append(f"`{model}`: {used} pedidos hoy")
        embed.add_field(name=f"Respaldo ({backup.provider})", value="\n".join(rows) or "Sin usar hoy ✅", inline=False)
    embed.set_footer(text="Google no informa cuánto cupo queda: el límite de cada modelo se aprende el día que se "
                          "agota. El cupo de Gemini se renueva a medianoche de California.")
    return embed

def _drop_ay(text: str) -> str:
    """'¡Ay, otra canción!' -> '¡Otra canción!'; '¡Ay! Qué lindo' -> 'Qué lindo'."""
    match = AY_START_RE.match(text)
    if not match:
        return text
    rest = text[match.end():]
    if not rest.strip():
        return text
    if match.group(2) and "," in match.group(3):
        return match.group(1) + "¡" + rest[0].upper() + rest[1:]
    return match.group(1) + rest[0].upper() + rest[1:]

EMOJI_NAME_RE = re.compile(r"(?<![<\w]):([A-Za-z0-9_]{2,32}):(?!\d)")


def server_emojis(guild: discord.Guild) -> list[discord.Emoji]:
    """Emojis propios del servidor que el bot puede usar."""
    usable = [e for e in guild.emojis if e.available and e.is_usable()]
    return usable[:MAX_SERVER_EMOJIS]


def apply_server_emojis(text: str, guild: Optional[discord.Guild]) -> str:
    """La IA escribe :nombre: y Discord necesita <:nombre:id>: se convierten los que existen."""
    if not text or guild is None or ":" not in text:
        return text
    by_name = {e.name: e for e in server_emojis(guild)}
    lower = {name.lower(): e for name, e in by_name.items()}

    def swap(match: re.Match) -> str:
        emoji = by_name.get(match.group(1)) or lower.get(match.group(1).lower())
        return str(emoji) if emoji else match.group(0)

    return EMOJI_NAME_RE.sub(swap, text)

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


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def say(
    bot: commands.Bot,
    channel: discord.abc.Messageable,
    situation: str,
    info: str,
    extra_context: Optional[Callable[[], Awaitable[str]]] = None,
    comment: Optional[bool] = None,
    **kwargs,
) -> Optional[discord.Message]:
    """Envía `info` (texto fijo, o un embed) AL INSTANTE y, cuando llega, le añade el comentario
    del personaje sobre `situation` editando el mensaje. Nunca espera a la IA.
    extra_context: función que devuelve más datos para el comentario (por ejemplo la letra de la
    canción); se busca en segundo plano, después de mandar el mensaje."""
    _note(bot, channel, situation)
    try:
        message = await channel.send(info or None, allowed_mentions=NO_MENTIONS, **kwargs)
    except discord.HTTPException as exc:
        log.warning("No se pudo enviar el mensaje: %s", exc)
        return None
    if comment is None:
        comment = not CHAT_ACTION.get()  # si vino de la charla, ya lo comentó al responder
    persona = _persona(bot) if comment else None
    if persona and MUSIC_COMMENTS:

        async def add_line() -> None:
            full = situation
            if extra_context is not None:
                try:
                    extra = await extra_context()
                except Exception:
                    log.exception("No se pudo preparar el contexto extra del comentario")
                    extra = ""
                if extra:
                    full = f"{situation}\n{extra}\n"
            line = await persona.comment(channel, full)
            if not line:
                return
            content = f"{line}\n-# {info}" if info else line
            try:
                await message.edit(content=content[:2000], allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass

        _spawn(add_line())
    return message


def comment_later(bot: commands.Bot, channel: discord.abc.Messageable, situation: str) -> None:
    """Comentario opcional en segundo plano (para comandos que ya respondieron con una reacción)."""
    _note(bot, channel, situation)
    persona = _persona(bot)
    if not (persona and MUSIC_COMMENTS) or CHAT_ACTION.get():
        return

    async def run() -> None:
        line = await persona.comment(channel, situation)
        if line:
            try:
                await channel.send(line, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass

    _spawn(run())


_trivia_pending = False


def _trivia_count_today() -> int:
    state = load_json(TRIVIA_FILE, {})
    if not isinstance(state, dict):
        return 0
    return state.get("count", 0) if state.get("date") == date.today().isoformat() else 0


def maybe_song_trivia(
    bot: commands.Bot,
    channel: discord.abc.Messageable,
    title: str,
    duration: Optional[int],
    still_playing: Callable[[], bool],
    uploader: str = "",
    url: str = "",
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
            lyrics = letras.for_ai(await letras.get_lyrics(title, uploader, duration, url))
            prompt = (
                f"((Mientras suena '{title}', por iniciativa propia se te ocurre comentar algo sobre esa "
                "canción: un dato curioso del artista o la banda, algo de la melodía, de qué trata la "
                "letra o de su historia. Cuenta solo datos que conozcas de verdad; si no conoces la "
                "canción, comenta lo que te hace sentir sin inventar datos. En personaje, en "
                f"{LANGUAGE}, dos o tres frases, con emojis."
                + (f"\n{lyrics}\n" if lyrics else "")
                + "))"
            )
            line = await persona.ask(channel, prompt, REPLY_TIMEOUT, wait=False, remember=False)
            line = extract_actions(line)[0] if line else None
            if not line or not still_playing():
                return
            await channel.send(line, allowed_mentions=NO_MENTIONS)
            save_json(TRIVIA_FILE, {"date": date.today().isoformat(), "count": _trivia_count_today() + 1})
            log.info("Curiosidad sobre '%s' enviada", title)
        except Exception:
            log.exception("No se pudo enviar la curiosidad de la canción")
        finally:
            _trivia_pending = False

    _spawn(run())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Persona(bot))
