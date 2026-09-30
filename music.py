import asyncio
import logging
import os
import random
import re
import shlex
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import discord
import yt_dlp
from discord.ext import commands

import historial_canciones
from persona import _persona, comment_later, extract_actions, maybe_song_trivia, say

log = logging.getLogger("music")

FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg").strip() or "ffmpeg"
IDLE_SECONDS = int(float(os.getenv("IDLE_MINUTES", "5")) * 60)
ALONE_SECONDS = 60
DEFAULT_VOLUME = max(0, min(100, int(os.getenv("DEFAULT_VOLUME", "50")))) / 100
MAX_PLAYLIST = 100
SEARCH_RESULTS = 5  # opciones que muestra !buscar
PREFETCH_SECONDS = 30  # se pide el audio de la siguiente canción cuando faltan estos segundos
STREAM_TTL = 45 * 60  # los links de audio de YouTube caducan en unas horas: se reusan como mucho 45 min
RADIO_REQUESTER = "📻 Radio"
QUEUE_PAGE_SIZE = 10
EMBED_COLOR = 0xFF2700

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTIONS = "-vn"

# Búsqueda "plana": rápida, solo título/URL/duración. El stream real se obtiene al reproducir,
# así los links de audio (que caducan a las pocas horas) siempre están frescos.
YDL_SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "extract_flat": "in_playlist",
    "default_search": "ytsearch",
}
YDL_STREAM_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "format": "bestaudio/best",
}


@dataclass
class Track:
    title: str
    url: str
    duration: Optional[int]
    requester: str
    uploader: str = ""
    # Link de audio ya pedido a YouTube (prefetch), y cuándo se pidió.
    stream: Optional[dict] = field(default=None, repr=False)
    stream_at: float = 0.0

    def stream_is_fresh(self) -> bool:
        return self.stream is not None and time.monotonic() - self.stream_at < STREAM_TTL


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "en vivo"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def is_url(query: str) -> bool:
    return query.startswith(("http://", "https://"))


def _extract(query: str, opts: dict) -> Optional[dict]:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(query, download=False)


async def fetch_tracks(query: str, requester: str, limit: int = 1) -> list[Track]:
    """Convierte un link o una búsqueda en una lista de canciones (varias si es playlist).
    limit > 1: devuelve varios resultados de la búsqueda (para elegir con !buscar)."""
    target = query if is_url(query) else f"ytsearch{limit}:{query}"
    info = await asyncio.to_thread(_extract, target, YDL_SEARCH_OPTS)
    if not info:
        return []

    entries = [e for e in info.get("entries") or [] if e] if "entries" in info else [info]
    tracks = []
    for entry in entries[:MAX_PLAYLIST]:
        url = entry.get("webpage_url") or entry.get("url")
        if not url:
            continue
        duration = entry.get("duration")
        tracks.append(
            Track(
                title=entry.get("title") or url,
                url=url,
                duration=int(duration) if duration else None,
                requester=requester,
                uploader=entry.get("channel") or entry.get("uploader") or "",
            )
        )
    return tracks


async def resolve_stream(track: Track) -> dict:
    info = await asyncio.to_thread(_extract, track.url, YDL_STREAM_OPTS)
    if info and "entries" in info:
        info = next(e for e in info["entries"] if e)
    if not info or not info.get("url"):
        raise RuntimeError("yt-dlp no devolvió un stream de audio")
    return info


async def get_stream(track: Track) -> dict:
    """El audio de la canción: el que ya se pidió por adelantado si sigue fresco, o uno nuevo."""
    if track.stream_is_fresh():
        return track.stream
    info = await resolve_stream(track)
    track.stream, track.stream_at = info, time.monotonic()
    return info


class GuildPlayer:
    """Cola y reproductor de un servidor. Corre en su propia tarea hasta quedar inactivo."""

    def __init__(self, cog: "Music", guild: discord.Guild, channel: discord.abc.Messageable) -> None:
        self.cog = cog
        self.bot = cog.bot
        self.guild = guild
        self.channel = channel
        self.queue: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.loop_mode = False
        self.volume = DEFAULT_VOLUME
        self._wake = asyncio.Event()
        self._next = asyncio.Event()
        self._started_at = 0.0
        self._paused_at: Optional[float] = None
        self.radio = False
        self._radio_failures = 0
        self.played: deque[str] = deque(maxlen=25)  # últimas canciones (para que la radio no repita)
        self._prefetch_task: Optional[asyncio.Task] = None
        self.task = asyncio.create_task(self._run())

    @property
    def voice(self) -> Optional[discord.VoiceClient]:
        return self.guild.voice_client  # type: ignore[return-value]

    def add(self, tracks: list[Track]) -> None:
        self.queue.extend(tracks)
        self._wake.set()

    def elapsed(self) -> float:
        if not self.current:
            return 0.0
        end = self._paused_at if self._paused_at is not None else time.monotonic()
        return end - self._started_at

    def pause(self) -> None:
        self.voice.pause()
        self._paused_at = time.monotonic()

    def resume(self) -> None:
        self.voice.resume()
        if self._paused_at is not None:
            self._started_at += time.monotonic() - self._paused_at
            self._paused_at = None

    def skip(self) -> None:
        # Quitar "current" hace que el bucle pase a la siguiente aunque el loop esté activo.
        self.current = None
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()

    def stop_prefetch(self) -> None:
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = None

    async def _prefetch_loop(self, track: Track) -> None:
        """Mientras suena `track`, pide a YouTube el audio de la siguiente cuando faltan
        PREFETCH_SECONDS, así no hay silencio entre canciones."""
        try:
            while self.current is track:
                remaining = track.duration - self.elapsed() if track.duration else None
                upcoming = track if self.loop_mode else (self.queue[0] if self.queue else None)
                if (remaining is None or remaining <= PREFETCH_SECONDS) and upcoming and not upcoming.stream_is_fresh():
                    try:
                        await get_stream(upcoming)
                        log.info("Audio de '%s' preparado por adelantado", upcoming.title)
                    except Exception as exc:
                        log.info("No se pudo preparar por adelantado '%s': %s", upcoming.title, exc)
                        await asyncio.sleep(20)  # se reintenta más tarde (o al empezar la canción)
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass

    def _after(self, error: Optional[Exception]) -> None:
        if error:
            log.error("Error de reproducción en %s: %s", self.guild.name, error)
        self.bot.loop.call_soon_threadsafe(self._next.set)

    async def _run(self) -> None:
        try:
            while True:
                repeating = self.loop_mode and self.current is not None
                if not repeating:
                    self.current = None
                    if not self.queue and self.radio:
                        if await self._radio_turn():
                            continue
                    if not self.queue:
                        self.cog.refresh_presence()
                        self._wake.clear()
                        try:
                            await asyncio.wait_for(self._wake.wait(), IDLE_SECONDS)
                        except asyncio.TimeoutError:
                            await self._say(
                                "Terminó la música, la cola quedó vacía y te vas del canal de voz.",
                                "No hay nada en la cola, me desconecto 👋",
                            )
                            break
                        continue
                    self.current = self.queue.popleft()
                track = self.current

                vc = self.voice
                if vc is None or not vc.is_connected():
                    break

                try:
                    info = await get_stream(track)
                except Exception as exc:
                    log.warning("No se pudo obtener %s: %s", track.url, exc)
                    await self._say(
                        f"No pudiste reproducir la canción '{track.title}' (falló YouTube) y la saltas.",
                        f"⚠️ No pude reproducir **{track.title}**, la salto.",
                    )
                    self.current = None
                    continue

                before = FFMPEG_BEFORE
                user_agent = (info.get("http_headers") or {}).get("User-Agent")
                if user_agent:
                    before += f" -user_agent {shlex.quote(user_agent)}"
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(
                        info["url"], executable=FFMPEG_PATH, before_options=before, options=FFMPEG_OPTIONS
                    ),
                    volume=self.volume,
                )

                # Si Discord está reconectando la voz, esperamos un poco antes de rendirnos.
                for _ in range(20):
                    if vc.is_connected():
                        break
                    await asyncio.sleep(0.5)

                self._next.clear()
                self._started_at = time.monotonic()
                self._paused_at = None
                try:
                    vc.play(source, after=self._after)
                except discord.ClientException as exc:
                    source.cleanup()
                    log.warning("No se pudo reproducir en %s: %s", self.guild.name, exc)
                    await self._say(
                        "Se cortó tu conexión con el canal de voz y tuviste que parar la música.",
                        "⚠️ Perdí la conexión con el canal de voz. Vuelve a usar play.",
                    )
                    break
                self.played.append(track.title)
                self.cog.refresh_presence()
                self.stop_prefetch()
                self._prefetch_task = asyncio.create_task(self._prefetch_loop(track))
                if not repeating:
                    await self._announce(track, info)
                    maybe_song_trivia(
                        self.bot, self.channel, track.title, track.duration or info.get("duration"),
                        lambda t=track: self.current is t,
                    )
                await self._next.wait()
        except asyncio.CancelledError:
            self.stop_prefetch()
            raise
        except Exception:
            log.exception("El reproductor de %s falló", self.guild.name)
        self.stop_prefetch()
        await self.cog.cleanup(self.guild)

    async def _radio_turn(self) -> bool:
        """Modo radio con la cola vacía: elige y encola una canción. True = seguir con la radio
        (encoló algo o hay que reintentar); False = nada que hacer (nadie escucha o se apagó)."""
        vc = self.voice
        listeners = [m for m in vc.channel.members if not m.bot] if vc and vc.channel else []
        if not listeners:
            return False
        try:
            added = await self.cog.radio_pick(self, listeners)
        except Exception:
            log.exception("La radio no pudo elegir canción")
            added = False
        if added:
            self._radio_failures = 0
            return True
        self._radio_failures += 1
        if self._radio_failures >= 3:
            self.radio = False
            self._radio_failures = 0
            await self._say(
                "No encontraste qué más poner en la radio y la apagaste.",
                "📻 No encontré más canciones para la radio, la apagué.",
            )
            return False
        await asyncio.sleep(5)
        return True  # se vuelve a intentar en la próxima vuelta

    async def _say(self, situation: str, info: str, **kwargs) -> None:
        await say(self.bot, self.channel, situation, info, **kwargs)

    async def _announce(self, track: Track, info: dict) -> None:
        embed = discord.Embed(
            title="🎶 Reproduciendo",
            description=f"[{track.title}]({track.url})",
            color=EMBED_COLOR,
        )
        embed.add_field(name="Duración", value=fmt_duration(track.duration or info.get("duration")))
        embed.add_field(name="Pedido por", value=track.requester)
        if info.get("uploader"):
            embed.add_field(name="Canal", value=info["uploader"])
        if info.get("thumbnail"):
            embed.set_thumbnail(url=info["thumbnail"])
        uploader = f" (del canal de YouTube '{info['uploader']}')" if info.get("uploader") else ""
        await self._say(
            f"Empieza a sonar '{track.title}'{uploader}, que pidió {track.requester}. Preséntala con "
            "algo concreto de la canción o del artista si los conoces (si es música de League of "
            "Legends, habla de los campeones que la cantan o de su historia).",
            "",
            embed=embed,
        )


NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
RADIO_ON = {"on", "si", "sí", "activar", "activa", "encender", "enciende", "prender", "prende", "1", "true"}
RADIO_OFF = {"off", "no", "desactivar", "desactiva", "apagar", "apaga", "0", "false"}


class SearchView(discord.ui.View):
    """Menú desplegable con los resultados de !buscar. Solo quien buscó puede elegir."""

    def __init__(self, cog: "Music", ctx: commands.Context, tracks: list[Track]) -> None:
        super().__init__(timeout=60)
        self.cog, self.ctx, self.tracks = cog, ctx, tracks
        self.message: Optional[discord.Message] = None
        options = []
        for i, track in enumerate(tracks):
            details = f"{track.uploader} · " if track.uploader else ""
            options.append(discord.SelectOption(
                label=track.title[:100],
                description=f"{details}{fmt_duration(track.duration)}"[:100],
                value=str(i),
                emoji=NUMBER_EMOJIS[i],
            ))
        self.select = discord.ui.Select(placeholder="Elige la canción…", options=options)
        self.select.callback = self.chosen
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                f"Solo {self.ctx.author.display_name} puede elegir en esta búsqueda 🌸", ephemeral=True
            )
            return False
        return True

    async def chosen(self, interaction: discord.Interaction) -> None:
        track = self.tracks[int(self.select.values[0])]
        self.stop()
        await interaction.response.edit_message(
            view=None,
            embed=discord.Embed(title="✅ Elegida", description=f"[{track.title}]({track.url})", color=EMBED_COLOR),
        )
        try:
            await self.cog.enqueue(self.ctx, [track])
        except commands.CommandError as exc:
            await self.cog.bot.on_command_error(self.ctx, exc)
        except Exception as exc:
            await self.cog.bot.on_command_error(self.ctx, commands.CommandInvokeError(exc))

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(
                    view=None, embed=discord.Embed(title="⌛ Se acabó el tiempo para elegir", color=EMBED_COLOR)
                )
            except discord.HTTPException:
                pass


class Music(commands.Cog, name="Música"):
    """Reproduce música y videos de YouTube en el canal de voz."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self._alone_timers: dict[int, asyncio.Task] = {}
        self._presence: Optional[str] = ""  # "" = todavía no se puso ninguno
        self._presence_task: Optional[asyncio.Task] = None

    async def cog_unload(self) -> None:
        for guild_id in list(self.players):
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self.cleanup(guild)

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.CheckFailure("Los comandos de música solo funcionan dentro de un servidor.")
        return True

    async def cleanup(self, guild: discord.Guild) -> None:
        player = self.players.pop(guild.id, None)
        if player:
            player.stop_prefetch()
            player.current = None
        if player and player.task is not asyncio.current_task():
            player.task.cancel()
        self.refresh_presence()
        timer = self._alone_timers.pop(guild.id, None)
        if timer and timer is not asyncio.current_task():
            timer.cancel()
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)

    # ---------- Estado de Discord ("Escuchando ...") ----------

    def refresh_presence(self) -> None:
        """Muestra en el perfil del bot la canción que suena, o "Durmiendo 💤" si no hay música."""
        playing = [p for p in self.players.values() if p.current is not None]
        title = max(playing, key=lambda p: p._started_at).current.title if playing else None
        if title == self._presence:
            return
        self._presence = title
        if title:
            activity = discord.Activity(type=discord.ActivityType.listening, name=title[:128])
        else:
            activity = discord.CustomActivity(name="Durmiendo 💤")

        async def apply() -> None:
            try:
                await self.bot.change_presence(activity=activity)
            except Exception as exc:
                log.debug("No se pudo cambiar el estado: %s", exc)

        if self.bot.is_ready():
            self._presence_task = asyncio.create_task(apply())

    # ---------- Radio ----------

    async def radio_pick(self, player: GuildPlayer, listeners: list[discord.Member]) -> bool:
        """Elige una canción para la radio según los gustos de quienes escuchan y la encola."""
        recent = list(player.played)
        query = None
        persona = _persona(self.bot)
        if persona:
            prompt = (
                "((Modo radio: se terminó la cola y te toca elegir la próxima canción para los que están "
                "escuchando en el canal de voz. Fíjate en sus \"Canciones que pidió\" y en tu estado de "
                "ánimo, y elige UNA canción REAL que les pueda gustar (mismo estilo, artistas parecidos "
                "o de la misma época). No repitas ninguna de las que ya sonaron: "
                f"{'; '.join(recent[-15:]) or 'ninguna'}. Responde SOLO con una línea: Artista - Título))"
            )
            reply = await persona.ask(player.channel, prompt, 30, people=listeners[:5], remember=False)
            if reply:
                lines = [line for line in extract_actions(reply)[0].splitlines() if line.strip()]
                pick = next((line for line in lines if " - " in line), lines[0] if lines else "")
                query = re.sub(r'^[\s\-•*"“«]+|[\s"”»*]+$', "", pick)[:120] or None
        if not query:  # sin IA: una canción al azar de las que ya pidieron los que escuchan
            pool = [t for m in listeners for t in historial_canciones.titles(m) if t not in recent]
            query = random.choice(pool) if pool else None
        if not query:
            return False
        tracks = [t for t in await fetch_tracks(query, RADIO_REQUESTER) if t.title not in recent]
        if not tracks:
            return False
        player.add(tracks[:1])
        log.info("La radio eligió '%s' (búsqueda: %s)", tracks[0].title, query)
        return True

    def status_text(self, guild_id: int) -> str:
        """Resumen de la música para que el personaje sepa qué está sonando."""
        player = self.players.get(guild_id)
        if player is None or (player.current is None and not player.queue):
            return "No está sonando nada ahora mismo y la cola está vacía."
        parts = []
        track = player.current
        if track:
            extra = ""
            if player.voice and player.voice.is_paused():
                extra += " (en pausa)"
            if player.loop_mode:
                extra += " (en repetición)"
            parts.append(
                f"Sonando ahora: '{track.title}' ({track.url}), pedida por {track.requester}, "
                f"va por {fmt_duration(player.elapsed())} de {fmt_duration(track.duration)}{extra}."
            )
        if player.radio:
            parts.append("El modo radio está activado (cuando se vacía la cola, eliges tú la próxima canción).")
        queue = list(player.queue)
        if queue:
            names = ", ".join(f"'{t.title}' (pedida por {t.requester})" for t in queue[:5])
            more = f" y {len(queue) - 5} más" if len(queue) > 5 else ""
            parts.append(f"En cola: {names}{more}.")
        else:
            parts.append("No hay más canciones en cola.")
        return " ".join(parts)

    def get_player(self, ctx: commands.Context) -> GuildPlayer:
        player = self.players.get(ctx.guild.id)
        if player is None:
            player = GuildPlayer(self, ctx.guild, ctx.channel)
            self.players[ctx.guild.id] = player
        else:
            player.channel = ctx.channel
        return player

    def playing_player(self, ctx: commands.Context) -> GuildPlayer:
        player = self.players.get(ctx.guild.id)
        if player is None or player.current is None or ctx.voice_client is None:
            raise commands.CheckFailure("No estoy reproduciendo nada.")
        return player

    async def ensure_voice(self, ctx: commands.Context) -> discord.VoiceClient:
        user_voice = ctx.author.voice
        if user_voice is None or user_voice.channel is None:
            raise commands.CheckFailure("Tienes que estar en un canal de voz.")
        vc = ctx.voice_client
        if vc is None:
            # A veces Discord tarda en abrir la voz: se reintenta una vez antes de rendirse.
            for attempt in (1, 2):
                try:
                    return await user_voice.channel.connect(self_deaf=True, timeout=30)
                except (asyncio.TimeoutError, discord.ClientException) as exc:
                    log.warning("No se pudo conectar a voz en %s (intento %d): %r", ctx.guild.name, attempt, exc)
                    if ctx.voice_client:
                        await ctx.voice_client.disconnect(force=True)
                    if attempt == 2:
                        raise commands.CheckFailure(
                            "No pude conectarme al canal de voz. Prueba otra vez en unos segundos."
                        ) from exc
                    await asyncio.sleep(2)
        if vc.channel != user_voice.channel:
            if vc.is_playing() or vc.is_paused():
                raise commands.CheckFailure(f"Ya estoy reproduciendo en **{vc.channel.name}**.")
            await vc.move_to(user_voice.channel)
        return vc

    # ---------- Comandos ----------

    async def enqueue(self, ctx: commands.Context, tracks: list[Track]) -> None:
        """Pone en la cola canciones ya encontradas (lo usan !play y el menú de !buscar)."""
        await self.ensure_voice(ctx)
        player = self.get_player(ctx)
        historial_canciones.record(ctx.author, [t.title for t in tracks])
        was_busy = player.current is not None or bool(player.queue)
        player.add(tracks)
        if len(tracks) > 1:
            await say(
                self.bot, ctx.channel,
                f"{ctx.author.display_name} añadió una playlist de {len(tracks)} canciones a la cola.",
                f"✅ Añadidas **{len(tracks)}** canciones a la cola.",
            )
        elif was_busy:
            track = tracks[0]
            await say(
                self.bot, ctx.channel,
                f"{ctx.author.display_name} añadió '{track.title}' a la cola; hay otra canción sonando.",
                f"✅ En cola (#{len(player.queue)}): **{track.title}** `{fmt_duration(track.duration)}`",
            )

    @commands.command(name="play", aliases=["p"], help="Reproduce un link de YouTube o busca por nombre.")
    async def play(self, ctx: commands.Context, *, busqueda: str) -> None:
        await self.ensure_voice(ctx)
        async with ctx.typing():
            try:
                tracks = await fetch_tracks(busqueda.strip("<>"), ctx.author.display_name)
            except Exception as exc:
                log.warning("Búsqueda fallida '%s': %s", busqueda, exc)
                tracks = []
        if not tracks:
            await say(
                self.bot, ctx.channel,
                f"{ctx.author.display_name} te pidió poner '{busqueda}' pero no encontraste nada en YouTube.",
                "No encontré nada con eso 😕",
            )
            return
        await self.enqueue(ctx, tracks)

    @commands.command(name="buscar", aliases=["search", "b"], help="Busca en YouTube y te deja elegir entre 5 resultados.")
    async def buscar(self, ctx: commands.Context, *, busqueda: str) -> None:
        busqueda = busqueda.strip().strip("<>")
        if is_url(busqueda):
            await ctx.invoke(self.play, busqueda=busqueda)
            return
        await self.ensure_voice(ctx)
        async with ctx.typing():
            try:
                results = await fetch_tracks(busqueda, ctx.author.display_name, limit=SEARCH_RESULTS)
            except Exception as exc:
                log.warning("Búsqueda fallida '%s': %s", busqueda, exc)
                results = []
        if not results:
            await say(
                self.bot, ctx.channel,
                f"{ctx.author.display_name} te pidió buscar '{busqueda}' pero no encontraste nada en YouTube.",
                "No encontré nada con eso 😕",
            )
            return
        results = results[:SEARCH_RESULTS]
        lines = [
            f"{NUMBER_EMOJIS[i]} **{t.title}**" + (f" · {t.uploader}" if t.uploader else "") + f" `{fmt_duration(t.duration)}`"
            for i, t in enumerate(results)
        ]
        embed = discord.Embed(title=f"🔎 {busqueda[:200]}", description="\n".join(lines), color=EMBED_COLOR)
        embed.set_footer(text=f"{ctx.author.display_name}, elige una en el menú (tienes 60 s)")
        view = SearchView(self, ctx, results)
        view.message = await say(
            self.bot, ctx.channel,
            f"{ctx.author.display_name} te pidió buscar '{busqueda}' y le muestras {len(results)} opciones para que elija.",
            "",
            embed=embed,
            view=view,
        )

    @commands.command(name="radio", help="Modo radio: con la cola vacía, elijo canciones según los gustos de los que escuchan. Uso: radio [on/off]")
    async def radio(self, ctx: commands.Context, modo: Optional[str] = None) -> None:
        player = self.players.get(ctx.guild.id)
        currently_on = bool(player and player.radio)
        choice = (modo or "").strip().lower()
        turn_on = True if choice in RADIO_ON else False if choice in RADIO_OFF else not currently_on
        who = ctx.author.display_name
        if turn_on:
            await self.ensure_voice(ctx)
            player = self.get_player(ctx)
            player.radio = True
            player._wake.set()  # si no sonaba nada, empieza ya
            await say(
                self.bot, ctx.channel,
                f"{who} activó el modo radio: cuando se acabe la cola, elegirás canciones según los gustos "
                "de los que están escuchando.",
                "📻 Radio activada: cuando se vacíe la cola elijo canciones según lo que les gusta a los que escuchan.",
            )
        else:
            if player:
                player.radio = False
            await say(self.bot, ctx.channel, f"{who} apagó el modo radio.", "📻 Radio apagada.")

    @commands.command(name="join", aliases=["j"], help="Entra a tu canal de voz.")
    async def join(self, ctx: commands.Context) -> None:
        vc = await self.ensure_voice(ctx)
        self.get_player(ctx)
        await say(
            self.bot, ctx.channel,
            f"{ctx.author.display_name} te llamó y entraste al canal de voz '{vc.channel.name}'.",
            f"🔊 Conectado a **{vc.channel.name}**",
        )

    @commands.command(name="skip", aliases=["s", "next"], help="Salta la canción actual.")
    async def skip(self, ctx: commands.Context) -> None:
        player = self.playing_player(ctx)
        title = player.current.title
        player.skip()
        await ctx.message.add_reaction("⏭️")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} saltó la canción '{title}'.")

    @commands.command(name="pause", help="Pausa la reproducción.")
    async def pause(self, ctx: commands.Context) -> None:
        player = self.playing_player(ctx)
        if ctx.voice_client.is_paused():
            await ctx.send("Ya está en pausa.")
            return
        player.pause()
        await ctx.message.add_reaction("⏸️")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} pausó la música.")

    @commands.command(name="resume", aliases=["r", "continue"], help="Reanuda la reproducción.")
    async def resume(self, ctx: commands.Context) -> None:
        player = self.playing_player(ctx)
        if not ctx.voice_client.is_paused():
            await ctx.send("No está en pausa.")
            return
        player.resume()
        await ctx.message.add_reaction("▶️")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} quitó la pausa y la música sigue.")

    @commands.command(name="stop", help="Detiene la música y vacía la cola (sigue en el canal).")
    async def stop(self, ctx: commands.Context) -> None:
        player = self.playing_player(ctx)
        player.queue.clear()
        player.loop_mode = False
        player.radio = False  # si no, la radio elegiría otra canción enseguida
        player.skip()
        await ctx.message.add_reaction("⏹️")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} paró la música y vació la cola.")

    @commands.command(name="leave", aliases=["dc", "disconnect"], help="Sale del canal de voz.")
    async def leave(self, ctx: commands.Context) -> None:
        if ctx.voice_client is None:
            await ctx.send("No estoy en ningún canal de voz.")
            return
        await self.cleanup(ctx.guild)
        await ctx.message.add_reaction("👋")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} te pidió que salieras del canal de voz.")

    @commands.command(name="queue", aliases=["q", "cola"], help="Muestra la cola. Uso: queue [página]")
    async def queue(self, ctx: commands.Context, pagina: int = 1) -> None:
        player = self.players.get(ctx.guild.id)
        if player is None or (player.current is None and not player.queue):
            await ctx.send("La cola está vacía.")
            return

        items = list(player.queue)
        pages = max(1, -(-len(items) // QUEUE_PAGE_SIZE))
        pagina = max(1, min(pagina, pages))
        start = (pagina - 1) * QUEUE_PAGE_SIZE

        lines = []
        if player.current:
            loop_tag = " 🔂" if player.loop_mode else ""
            lines.append(f"**Sonando:** {player.current.title} `{fmt_duration(player.current.duration)}`{loop_tag}\n")
        for i, track in enumerate(items[start:start + QUEUE_PAGE_SIZE], start=start + 1):
            lines.append(f"`{i}.` {track.title} `{fmt_duration(track.duration)}` — {track.requester}")
        if not items:
            lines.append("_No hay más canciones en cola._")

        total = sum(t.duration or 0 for t in items)
        embed = discord.Embed(title="📜 Cola", description="\n".join(lines), color=EMBED_COLOR)
        embed.set_footer(text=f"Página {pagina}/{pages} · {len(items)} en cola · {fmt_duration(total)} en total")
        await ctx.send(embed=embed)

    @commands.command(name="nowplaying", aliases=["np"], help="Muestra lo que está sonando.")
    async def nowplaying(self, ctx: commands.Context) -> None:
        player = self.playing_player(ctx)
        track = player.current
        elapsed = player.elapsed()
        bar = ""
        if track.duration:
            filled = int(20 * min(elapsed / track.duration, 1))
            bar = "▬" * filled + "🔘" + "▬" * (20 - filled) + "\n"
        embed = discord.Embed(
            title="🎶 Sonando ahora",
            description=f"[{track.title}]({track.url})\n{bar}`{fmt_duration(elapsed)} / {fmt_duration(track.duration)}`",
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"Pedido por {track.requester}" + (" · 🔂 loop" if player.loop_mode else ""))
        await ctx.send(embed=embed)

    @commands.command(name="loop", aliases=["repeat"], help="Repite la canción actual (activar/desactivar).")
    async def loop(self, ctx: commands.Context) -> None:
        player = self.playing_player(ctx)
        player.loop_mode = not player.loop_mode
        await say(
            self.bot, ctx.channel,
            f"{ctx.author.display_name} "
            + (f"puso '{player.current.title}' en repetición." if player.loop_mode else "quitó la repetición."),
            "🔂 Loop activado" if player.loop_mode else "➡️ Loop desactivado",
        )

    @commands.command(name="shuffle", help="Mezcla la cola.")
    async def shuffle(self, ctx: commands.Context) -> None:
        player = self.players.get(ctx.guild.id)
        if player is None or len(player.queue) < 2:
            await ctx.send("No hay suficientes canciones en la cola para mezclar.")
            return
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await ctx.message.add_reaction("🔀")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} mezcló el orden de la cola.")

    @commands.command(name="remove", aliases=["rm"], help="Quita una canción de la cola. Uso: remove <número>")
    async def remove(self, ctx: commands.Context, numero: int) -> None:
        player = self.players.get(ctx.guild.id)
        if player is None or not 1 <= numero <= len(player.queue):
            await ctx.send("Ese número no está en la cola.")
            return
        track = player.queue[numero - 1]
        del player.queue[numero - 1]
        await say(
            self.bot, ctx.channel,
            f"{ctx.author.display_name} quitó '{track.title}' de la cola.",
            f"🗑️ Quitada: **{track.title}**",
        )

    @commands.command(name="clear", aliases=["cq"], help="Vacía la cola (sin parar la canción actual).")
    async def clear(self, ctx: commands.Context) -> None:
        player = self.players.get(ctx.guild.id)
        if player:
            player.queue.clear()
        await ctx.message.add_reaction("🧹")

    @commands.command(name="volume", aliases=["vol", "v"], help="Cambia el volumen (0-100). Uso: volume <n>")
    async def volume(self, ctx: commands.Context, nivel: Optional[int] = None) -> None:
        player = self.players.get(ctx.guild.id)
        if player is None:
            await ctx.send("No estoy reproduciendo nada.")
            return
        if nivel is None:
            await ctx.send(f"🔊 Volumen actual: **{round(player.volume * 100)}%**")
            return
        nivel = max(0, min(100, nivel))
        player.volume = nivel / 100
        vc = ctx.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = player.volume
        await ctx.send(f"🔊 Volumen: **{nivel}%**")

    # ---------- Eventos ----------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild

        # Si alguien desconecta al bot manualmente, limpiamos su cola.
        if member.id == self.bot.user.id:
            if before.channel and after.channel is None and guild.id in self.players:
                await self.cleanup(guild)
            return

        vc = guild.voice_client
        if vc is None or vc.channel is None:
            return
        humans = [m for m in vc.channel.members if not m.bot]
        timer = self._alone_timers.get(guild.id)
        if not humans and timer is None:
            self._alone_timers[guild.id] = asyncio.create_task(self._leave_if_alone(guild))
        elif humans and timer is not None:
            timer.cancel()
            self._alone_timers.pop(guild.id, None)

    async def _leave_if_alone(self, guild: discord.Guild) -> None:
        await asyncio.sleep(ALONE_SECONDS)
        self._alone_timers.pop(guild.id, None)
        vc = guild.voice_client
        if vc and vc.channel and not [m for m in vc.channel.members if not m.bot]:
            player = self.players.get(guild.id)
            if player:
                await player._say(
                    "Todos se fueron del canal de voz y te quedaste sin nadie, así que te vas.",
                    "Me quedé solo en el canal, me desconecto 👋",
                )
            await self.cleanup(guild)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
