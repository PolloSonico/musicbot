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

import avisos
import ayudantes
import historial_canciones
import letras
import spotify
import wrapped
from persona import CHAT_ACTION, _persona, comment_later, extract_actions, maybe_song_trivia, say

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
YTDLP_ALERT_AFTER = 3  # fallos seguidos de yt-dlp antes de avisar al dueño por DM
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
QUEUE_PAGE_SIZE = 10
EMBED_COLOR = 0xFF2700

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
# Volumen parejo: "loudnorm" (EBU R128) lleva todas las canciones a la misma sonoridad, así no hay
# una que suene bajito y la siguiente a todo volumen. Se apaga con NORMALIZE_VOLUME=false.
NORMALIZE_VOLUME = os.getenv("NORMALIZE_VOLUME", "true").strip().lower() not in ("0", "false", "no")
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000"
FFMPEG_OPTIONS = f'-vn -af "{LOUDNORM}"' if NORMALIZE_VOLUME else "-vn"
VOLUME_STEP = 10  # cuánto cambian los botones 🔉 / 🔊

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
    requester_id: int = 0  # ID de Discord de quien la pidió (0 = la radio), para el Wrapped
    via_chat: bool = False  # la pidió hablándole a Lillia (ella ya la comentó al responder)
    requested_at: float = 0.0
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
    limit > 1: devuelve varios resultados de la búsqueda (para elegir con !buscar).
    Links de Spotify: cada canción se busca en YouTube recién cuando le toca sonar."""
    if spotify.is_spotify(query):
        _name, found = await spotify.resolve(query)  # lanza spotify.SpotifyError si no pudo
        return [
            Track(title=t.query, url=f"ytsearch1:{t.query}", duration=t.duration, requester=requester, uploader=t.artist)
            for t in found[:MAX_PLAYLIST]
        ]
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
    if track.url.startswith("ytsearch"):  # venía de Spotify: ya sabemos qué video de YouTube es
        track.url = info.get("webpage_url") or track.url
    return info


class GuildPlayer:
    """Cola y reproductor de UN canal de voz. Lo atiende Lillia o un bot ayudante (`client`): el
    audio sale por la conexión de voz de ese bot, pero los mensajes los manda siempre Lillia.
    Corre en su propia tarea hasta quedar inactivo."""

    def __init__(
        self, cog: "Music", client: discord.Client, guild: discord.Guild, voice_channel_id: int,
        channel: discord.abc.Messageable, vc: discord.VoiceClient,
    ) -> None:
        self.cog = cog
        self.bot = cog.bot
        self.client = client  # Lillia (cog.bot) o un ayudante
        self.guild = guild  # el servidor visto por Lillia (para nombres y miembros)
        self.voice_channel_id = voice_channel_id
        self.vc = vc
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
        self.np_message: Optional[discord.Message] = None  # el "Reproduciendo" con los botones
        self.reserved_until = time.monotonic() + 90  # recién pedido: no se lo "roba" otro canal
        self.task = asyncio.create_task(self._run())

    @property
    def voice(self) -> Optional[discord.VoiceClient]:
        return self.vc

    @property
    def is_helper(self) -> bool:
        return self.client is not self.bot

    @property
    def voice_channel(self) -> Optional[discord.VoiceChannel]:
        return self.guild.get_channel(self.voice_channel_id)  # type: ignore[return-value]

    def listeners(self) -> list[discord.Member]:
        channel = self.voice_channel
        return [m for m in channel.members if not m.bot] if channel else []

    @property
    def where(self) -> str:
        """'General' o 'General (con Ayudante1)': para los mensajes."""
        name = self.voice_channel.name if self.voice_channel else "?"
        return f"{name} (con {self.client.user.name})" if self.is_helper and self.client.user else name

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

    def set_volume(self, level: int) -> int:
        level = max(0, min(100, level))
        self.volume = level / 100
        vc = self.voice
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self.volume
        return level

    async def clear_buttons(self) -> None:
        """Quita los botones del "Reproduciendo" anterior (solo el último los tiene)."""
        message, self.np_message = self.np_message, None
        if message is not None:
            try:
                await message.edit(view=None)
            except discord.HTTPException:
                pass

    async def refresh_buttons(self) -> None:
        """Actualiza los botones (pausa/loop) cuando el cambio vino de un comando de texto."""
        if self.np_message is not None and self.current is not None:
            try:
                await self.np_message.edit(view=PlayerControls(self.cog, self))
            except discord.HTTPException:
                pass

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
                    letras.prefetch(upcoming.title, upcoming.uploader, upcoming.duration, upcoming.url)
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
                        await self.clear_buttons()
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
                    self.cog.ytdlp_ok()
                except Exception as exc:
                    log.warning("No se pudo obtener %s: %s", track.url, exc)
                    self.cog.ytdlp_failed(f"reproducir '{track.title}'", exc)
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
                        uploader=track.uploader or info.get("uploader") or "", url=track.url,
                    )
                await self._next.wait()
                self._record_listen(track)
        except asyncio.CancelledError:
            self.stop_prefetch()
            raise
        except Exception:
            log.exception("El reproductor de %s falló", self.guild.name)
        self.stop_prefetch()
        await self.clear_buttons()
        await self.cog.cleanup(self)

    def _record_listen(self, track: Track) -> None:
        """Para el Wrapped: cuánto sonó la canción y quiénes estaban escuchando."""
        end = self._paused_at if self._paused_at is not None else time.monotonic()
        wrapped.record_listen(self.guild, track, end - self._started_at, self.listeners(), track.requester == RADIO_REQUESTER)

    async def _radio_turn(self) -> bool:
        """Modo radio con la cola vacía: elige y encola una canción. True = seguir con la radio
        (encoló algo o hay que reintentar); False = nada que hacer (nadie escucha o se apagó)."""
        listeners = self.listeners()
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

    async def _say(self, situation: str, info: str, **kwargs) -> Optional[discord.Message]:
        return await say(self.bot, self.channel, situation, info, **kwargs)

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
        if len(self.cog.players_in(self.guild.id)) > 1 or self.is_helper:
            embed.add_field(name="🔊 En", value=self.where, inline=False)
        if info.get("thumbnail"):
            embed.set_thumbnail(url=info["thumbnail"])
        uploader = f" (del canal de YouTube '{info['uploader']}')" if info.get("uploader") else ""
        channel_name = track.uploader or info.get("uploader") or ""
        duration = track.duration or info.get("duration")

        async def lyrics_context() -> str:
            lyrics = await letras.get_lyrics(track.title, channel_name, duration, track.url, timeout=5)
            return letras.for_ai(lyrics, max_chars=1500)

        await self.clear_buttons()
        # Si la pidieron hablándole a Lillia y empieza enseguida, ella ya la comentó al responder.
        just_asked = track.via_chat and time.monotonic() - track.requested_at < 90
        self.np_message = await self._say(
            f"Empieza a sonar '{track.title}'{uploader}, que pidió {track.requester}. Preséntala con "
            "algo concreto de la canción o del artista si los conoces (si es música de League of "
            "Legends, habla de los campeones que la cantan o de su historia). Si tienes la letra, "
            "usa de qué trata para presentarla, sin copiarla.",
            "",
            extra_context=lyrics_context,
            comment=not just_asked,
            embed=embed,
            view=PlayerControls(self.cog, self),
        )


class PlayerControls(discord.ui.View):
    """Botones debajo del "🎶 Reproduciendo". Son persistentes (custom_id fijo): siguen
    funcionando aunque el bot se reinicie. Solo los puede usar quien esté en el canal de voz."""

    def __init__(self, cog: "Music", player: Optional["GuildPlayer"] = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        paused = bool(player and player.voice and player.voice.is_paused())
        looping = bool(player and player.loop_mode)
        radio = bool(player and player.radio)
        self.pause_button.emoji = "▶️" if paused else "⏸️"
        self.pause_button.style = discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
        self.loop_button.style = discord.ButtonStyle.success if looping else discord.ButtonStyle.secondary
        self.radio_button.style = discord.ButtonStyle.success if radio else discord.ButtonStyle.secondary

    async def _player(self, interaction: discord.Interaction) -> Optional["GuildPlayer"]:
        """El reproductor del servidor, si la persona puede tocar los botones (si no, le avisa)."""
        player = self.cog.player_for_message(interaction.message) or self.cog.player_for_member(interaction.user)
        vc = player.voice if player else None
        if player is None and interaction.guild and self.cog.players_in(interaction.guild.id):
            await interaction.response.send_message("Tienes que estar en el canal de voz donde suena la música 🦌", ephemeral=True)
            return None
        if player is None or vc is None or player.current is None:
            await interaction.response.send_message("No está sonando nada ahora mismo 🌸", ephemeral=True)
            if interaction.message:
                try:
                    await interaction.message.edit(view=None)
                except discord.HTTPException:
                    pass
            return None
        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None or voice.channel.id != player.voice_channel_id:
            await interaction.response.send_message(
                f"Tienes que estar en **{player.where}** para usar estos botones 🦌", ephemeral=True
            )
            return None
        return player

    async def _refresh(self, interaction: discord.Interaction, player: "GuildPlayer") -> None:
        await interaction.response.edit_message(view=PlayerControls(self.cog, player))

    def _comment(self, interaction: discord.Interaction, situation: str) -> None:
        comment_later(self.cog.bot, interaction.channel, f"{interaction.user.display_name} {situation}")

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary, custom_id="lillia:pausa", row=0)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._player(interaction)
        if player is None:
            return
        if player.voice.is_paused():
            player.resume()
            self._comment(interaction, "quitó la pausa con el botón y la música sigue.")
        else:
            player.pause()
            self._comment(interaction, "pausó la música con el botón.")
        await self._refresh(interaction, player)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="lillia:saltar", row=0)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._player(interaction)
        if player is None:
            return
        title = player.current.title
        await interaction.response.edit_message(view=None)  # la próxima canción trae botones nuevos
        player.np_message = None
        player.skip()
        self._comment(interaction, f"saltó la canción '{title}' con el botón.")

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="lillia:parar", row=0)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._player(interaction)
        if player is None:
            return
        await interaction.response.edit_message(view=None)
        player.np_message = None
        player.queue.clear()
        player.loop_mode = False
        player.radio = False
        player.skip()
        self._comment(interaction, "paró la música y vació la cola con el botón.")

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="lillia:repetir", row=0)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._player(interaction)
        if player is None:
            return
        player.loop_mode = not player.loop_mode
        await self._refresh(interaction, player)
        self._comment(
            interaction,
            f"puso '{player.current.title}' en repetición con el botón." if player.loop_mode
            else "quitó la repetición con el botón.",
        )

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="lillia:mezclar", row=0)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._player(interaction)
        if player is None:
            return
        if len(player.queue) < 2:
            await interaction.response.send_message("No hay suficientes canciones en la cola para mezclar 🌸", ephemeral=True)
            return
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await interaction.response.send_message(f"🔀 {interaction.user.display_name} mezcló la cola.", allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="lillia:bajar", row=1)
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._player(interaction)
        if player is None:
            return
        level = player.set_volume(round(player.volume * 100) - VOLUME_STEP)
        await interaction.response.send_message(f"🔉 Volumen: **{level}%**", ephemeral=True)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="lillia:subir", row=1)
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._player(interaction)
        if player is None:
            return
        level = player.set_volume(round(player.volume * 100) + VOLUME_STEP)
        await interaction.response.send_message(f"🔊 Volumen: **{level}%**", ephemeral=True)

    @discord.ui.button(emoji="📜", label="Cola", style=discord.ButtonStyle.secondary, custom_id="lillia:cola", row=1)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._player(interaction)
        if player is None:
            return
        await interaction.response.send_message(embed=self.cog.queue_embed(player, 1), ephemeral=True)

    @discord.ui.button(emoji="📻", label="Radio", style=discord.ButtonStyle.secondary, custom_id="lillia:radio", row=1)
    async def radio_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._player(interaction)
        if player is None:
            return
        player.radio = not player.radio
        await self._refresh(interaction, player)
        self._comment(
            interaction,
            "activó el modo radio con el botón: cuando se acabe la cola, elegirás canciones según sus gustos."
            if player.radio else "apagó el modo radio con el botón.",
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
        self.players: dict[int, GuildPlayer] = {}  # id del canal de VOZ -> reproductor
        self._alone_timers: dict[int, asyncio.Task] = {}  # id del canal de voz -> temporizador
        self._helper_songs: dict[int, Optional[str]] = {}  # ayudante -> canción que muestra en su estado
        self._presence: Optional[str] = ""  # "" = todavía no se puso ninguno
        self._presence_task: Optional[asyncio.Task] = None
        self._ytdlp_errors: list[str] = []  # fallos seguidos de yt-dlp (se vacía con un éxito)

    async def cog_load(self) -> None:
        # Vista persistente: los botones de mensajes viejos siguen respondiendo tras un reinicio.
        self.bot.add_view(PlayerControls(self))

    async def cog_unload(self) -> None:
        for player in list(self.players.values()):
            await self.cleanup(player)

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.CheckFailure("Los comandos de música solo funcionan dentro de un servidor.")
        return True

    async def cleanup(self, player: "GuildPlayer") -> None:
        """Termina un reproductor: corta la música y saca a su bot del canal de voz."""
        if self.players.get(player.voice_channel_id) is player:
            del self.players[player.voice_channel_id]
        player.stop_prefetch()
        player.current = None
        await player.clear_buttons()
        if player.task is not asyncio.current_task():
            player.task.cancel()
        self.refresh_presence()
        timer = self._alone_timers.pop(player.voice_channel_id, None)
        if timer and timer is not asyncio.current_task():
            timer.cancel()
        if player.vc and player.vc.is_connected():
            await player.vc.disconnect(force=True)

    # ---------- Reproductores (uno por canal de voz) ----------

    def players_in(self, guild_id: int) -> list["GuildPlayer"]:
        return [p for p in self.players.values() if p.guild.id == guild_id]

    def player_for_member(self, member) -> Optional["GuildPlayer"]:
        voice = getattr(member, "voice", None)
        return self.players.get(voice.channel.id) if voice and voice.channel else None

    def player_for_message(self, message: Optional[discord.Message]) -> Optional["GuildPlayer"]:
        if message is None:
            return None
        return next((p for p in self.players.values() if p.np_message and p.np_message.id == message.id), None)

    def player_for_ctx(self, ctx: commands.Context) -> Optional["GuildPlayer"]:
        """El reproductor del canal de voz de quien escribe; si no está en uno y solo hay uno sonando
        en el servidor, ese."""
        player = self.player_for_member(ctx.author)
        if player is None:
            in_guild = self.players_in(ctx.guild.id)
            player = in_guild[0] if len(in_guild) == 1 else None
        return player

    def current_track(self, guild_id: int, member=None) -> Optional["Track"]:
        """Lo que suena para esa persona (o en el servidor). Lo usa el personaje para las letras."""
        player = self.player_for_member(member) if member else None
        playing = [p for p in self.players_in(guild_id) if p.current]
        player = player or (playing[0] if playing else None)
        return player.current if player else None

    def _clients(self) -> list[discord.Client]:
        return [self.bot, *ayudantes.ready()]

    # ---------- Fallos de yt-dlp: aviso al dueño ----------

    def ytdlp_ok(self) -> None:
        self._ytdlp_errors.clear()

    def ytdlp_failed(self, what: str, exc: Exception) -> None:
        """Cuenta un fallo de yt-dlp; al tercero seguido avisa al dueño por DM (como mucho cada 6 h)."""
        error = ANSI_RE.sub("", str(exc)).replace("ERROR: ", "").strip()[:180]
        self._ytdlp_errors.append(f"• {what}: `{error}`")
        if len(self._ytdlp_errors) < YTDLP_ALERT_AFTER:
            return
        version = getattr(getattr(yt_dlp, "version", None), "__version__", "?")
        avisos.notify(
            self.bot,
            "ytdlp",
            f"⚠️ **yt-dlp falló {len(self._ytdlp_errors)} veces seguidas** (versión {version}). "
            "Casi siempre es que YouTube cambió algo y hay que actualizarlo: ejecuta "
            "`windows\\reiniciar_bot.bat` (actualiza yt-dlp al arrancar) o `windows\\instalar.bat`.\n"
            "Últimos errores:\n" + "\n".join(self._ytdlp_errors[-3:]),
        )

    # ---------- Estado de Discord ("Escuchando ...") ----------

    def refresh_presence(self) -> None:
        """Muestra en el perfil del bot la canción que suena, o "Durmiendo 💤" si no hay música.
        Cada ayudante muestra la suya."""
        for helper in ayudantes.ready():
            mine = [p for p in self.players.values() if p.client is helper and p.current is not None]
            song = mine[0].current.title if mine else None
            if self._helper_songs.get(id(helper), "") != song:
                self._helper_songs[id(helper)] = song
                asyncio.create_task(helper.set_song(song))
        playing = [p for p in self.players.values() if p.current is not None and not p.is_helper]
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
        """Resumen de la música para que el personaje sepa qué está sonando (en cada canal de voz)."""
        players = [p for p in self.players_in(guild_id) if p.current is not None or p.queue]
        if not players:
            return "No está sonando nada ahora mismo y la cola está vacía."
        if len(players) == 1:
            return self._status_one(players[0])
        return " | ".join(f"En el canal de voz '{p.where}': {self._status_one(p)}" for p in players)

    def _status_one(self, player: "GuildPlayer") -> str:
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

    def playing_player(self, ctx: commands.Context) -> GuildPlayer:
        player = self.player_for_ctx(ctx)
        if player is None or player.current is None or player.voice is None:
            raise commands.CheckFailure("No estoy reproduciendo nada en tu canal de voz.")
        return player

    async def ensure_player(self, ctx: commands.Context) -> GuildPlayer:
        """El reproductor del canal de voz de quien escribe; si no hay, entra Lillia o, si ella ya
        está ocupada en otro canal, un ayudante libre."""
        user_voice = ctx.author.voice
        if user_voice is None or user_voice.channel is None:
            raise commands.CheckFailure("Tienes que estar en un canal de voz.")
        target = user_voice.channel
        player = self.players.get(target.id)
        if player is not None:
            if player.voice and player.voice.is_connected():
                player.channel = ctx.channel
                player.reserved_until = time.monotonic() + 90
                return player
            await self.cleanup(player)  # quedó colgado (se cortó la voz): se arma de nuevo

        busy = []
        for client in self._clients():
            guild = client.get_guild(ctx.guild.id)
            channel = guild.get_channel(target.id) if guild else None
            if channel is None:
                continue  # ese bot no está en el servidor o no ve el canal
            vc = guild.voice_client
            if vc is not None and vc.is_connected():
                current = next((p for p in self.players_in(ctx.guild.id) if p.client is client), None)
                in_use = current is not None and (
                    current.current is not None or current.queue or current.listeners()
                    or time.monotonic() < current.reserved_until
                )
                if in_use:
                    busy.append(current.where)
                    continue
                # Está en otro canal sin hacer nada: se muda.
                if current is not None:
                    del self.players[current.voice_channel_id]
                    await vc.move_to(channel)
                    current.voice_channel_id = target.id
                    current.channel = ctx.channel
                    current.reserved_until = time.monotonic() + 90
                    self.players[target.id] = current
                    return current
                await vc.disconnect(force=True)
            vc = await self._connect(client, channel, ctx.guild.name)
            player = GuildPlayer(self, client, ctx.guild, target.id, ctx.channel, vc)
            self.players[target.id] = player
            if client is not self.bot:
                log.info("El ayudante %s atiende el canal %s en %s", client.user, target.name, ctx.guild.name)
            return player
        if busy:
            extra = "" if ayudantes.TOKENS else " (con bots ayudantes podría estar en varios canales a la vez)"
            raise commands.CheckFailure(f"Ya estoy poniendo música en **{', '.join(busy)}**{extra}.")
        raise commands.CheckFailure("No pude entrar a tu canal de voz (¿tengo permiso para ver ese canal?).")

    async def _connect(self, client: discord.Client, channel, guild_name: str) -> discord.VoiceClient:
        # A veces Discord tarda en abrir la voz: se reintenta una vez antes de rendirse.
        for attempt in (1, 2):
            try:
                return await channel.connect(self_deaf=True, timeout=30)
            except (asyncio.TimeoutError, discord.ClientException) as exc:
                log.warning("No se pudo conectar a voz en %s (intento %d): %r", guild_name, attempt, exc)
                if channel.guild.voice_client:
                    await channel.guild.voice_client.disconnect(force=True)
                if attempt == 2:
                    raise commands.CheckFailure(
                        "No pude conectarme al canal de voz. Prueba otra vez en unos segundos."
                    ) from exc
                await asyncio.sleep(2)
        raise commands.CheckFailure("No pude conectarme al canal de voz.")

    # ---------- Comandos ----------

    async def enqueue(self, ctx: commands.Context, tracks: list[Track]) -> None:
        """Pone en la cola canciones ya encontradas (lo usan !play y el menú de !buscar)."""
        player = await self.ensure_player(ctx)
        via_chat = CHAT_ACTION.get()
        for track in tracks:
            track.requester_id = ctx.author.id
            track.via_chat, track.requested_at = via_chat, time.monotonic()
        historial_canciones.record(ctx.author, [t.title for t in tracks])
        wrapped.record_request(ctx.guild, ctx.author, tracks)
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

    @commands.command(name="play", aliases=["p", "poner", "pon", "reproducir", "tocar"], help="Reproduce un link de YouTube o busca por nombre.")
    async def play(self, ctx: commands.Context, *, busqueda: str) -> None:
        await self.ensure_player(ctx)
        async with ctx.typing():
            try:
                tracks = await fetch_tracks(busqueda.strip("<>"), ctx.author.display_name)
                self.ytdlp_ok()
            except spotify.SpotifyError as exc:
                log.warning("Spotify: %s (%s)", exc, busqueda)
                await say(
                    self.bot, ctx.channel,
                    f"{ctx.author.display_name} te pasó un link de Spotify pero no pudiste leer qué canciones tiene.",
                    "😕 No pude leer ese link de Spotify. Prueba con el nombre de la canción o un link de YouTube.",
                )
                return
            except Exception as exc:
                log.warning("Búsqueda fallida '%s': %s", busqueda, exc)
                self.ytdlp_failed(f"buscar '{busqueda[:60]}'", exc)
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
        await self.ensure_player(ctx)
        async with ctx.typing():
            try:
                results = await fetch_tracks(busqueda, ctx.author.display_name, limit=SEARCH_RESULTS)
                self.ytdlp_ok()
            except Exception as exc:
                log.warning("Búsqueda fallida '%s': %s", busqueda, exc)
                self.ytdlp_failed(f"buscar '{busqueda[:60]}'", exc)
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
        player = self.player_for_ctx(ctx)
        currently_on = bool(player and player.radio)
        choice = (modo or "").strip().lower()
        turn_on = True if choice in RADIO_ON else False if choice in RADIO_OFF else not currently_on
        who = ctx.author.display_name
        if turn_on:
            player = await self.ensure_player(ctx)
            player.radio = True
            player._wake.set()  # si no sonaba nada, empieza ya
            await player.refresh_buttons()
            await say(
                self.bot, ctx.channel,
                f"{who} activó el modo radio: cuando se acabe la cola, elegirás canciones según los gustos "
                "de los que están escuchando.",
                "📻 Radio activada: cuando se vacíe la cola elijo canciones según lo que les gusta a los que escuchan.",
            )
        else:
            if player:
                player.radio = False
                await player.refresh_buttons()
            await say(self.bot, ctx.channel, f"{who} apagó el modo radio.", "📻 Radio apagada.")

    @commands.command(name="join", aliases=["j", "entrar", "ven", "veni", "vení"], help="Entra a tu canal de voz.")
    async def join(self, ctx: commands.Context) -> None:
        player = await self.ensure_player(ctx)
        helper = f" (lo atiende tu ayudante {player.client.user.name})" if player.is_helper else ""
        await say(
            self.bot, ctx.channel,
            f"{ctx.author.display_name} te llamó y entraste al canal de voz '{player.where}'{helper}.",
            f"🔊 Conectado a **{player.where}**",
        )

    @commands.command(name="skip", aliases=["s", "next", "saltar", "siguiente", "sig"], help="Salta la canción actual.")
    async def skip(self, ctx: commands.Context) -> None:
        player = self.playing_player(ctx)
        title = player.current.title
        player.skip()
        await ctx.message.add_reaction("⏭️")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} saltó la canción '{title}'.")

    @commands.command(name="pause", aliases=["pausa", "pausar"], help="Pausa la reproducción.")
    async def pause(self, ctx: commands.Context) -> None:
        player = self.playing_player(ctx)
        if player.voice.is_paused():
            await ctx.send("Ya está en pausa.")
            return
        player.pause()
        await player.refresh_buttons()
        await ctx.message.add_reaction("⏸️")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} pausó la música.")

    @commands.command(name="resume", aliases=["r", "continue", "seguir", "continuar", "reanudar"], help="Reanuda la reproducción.")
    async def resume(self, ctx: commands.Context) -> None:
        player = self.playing_player(ctx)
        if not player.voice.is_paused():
            await ctx.send("No está en pausa.")
            return
        player.resume()
        await player.refresh_buttons()
        await ctx.message.add_reaction("▶️")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} quitó la pausa y la música sigue.")

    @commands.command(name="stop", aliases=["parar", "detener", "basta"], help="Detiene la música y vacía la cola (sigue en el canal).")
    async def stop(self, ctx: commands.Context) -> None:
        player = self.playing_player(ctx)
        player.queue.clear()
        player.loop_mode = False
        player.radio = False  # si no, la radio elegiría otra canción enseguida
        player.skip()
        await ctx.message.add_reaction("⏹️")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} paró la música y vació la cola.")

    @commands.command(name="leave", aliases=["dc", "disconnect", "salir", "chau", "andate"], help="Sale del canal de voz.")
    async def leave(self, ctx: commands.Context) -> None:
        player = self.player_for_ctx(ctx)
        if player is None:
            await ctx.send("No estoy en tu canal de voz.")
            return
        await self.cleanup(player)
        await ctx.message.add_reaction("👋")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} te pidió que salieras del canal de voz.")

    @commands.command(name="queue", aliases=["q", "cola", "lista"], help="Muestra la cola. Uso: queue [página]")
    async def queue(self, ctx: commands.Context, pagina: int = 1) -> None:
        player = self.player_for_ctx(ctx)
        if player is None or (player.current is None and not player.queue):
            await ctx.send("La cola está vacía.")
            return

        await ctx.send(embed=self.queue_embed(player, pagina))

    def queue_embed(self, player: "GuildPlayer", pagina: int) -> discord.Embed:
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
        return embed

    @commands.command(name="nowplaying", aliases=["np", "sonando", "ahora", "quesuena"], help="Muestra lo que está sonando.")
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

    @commands.command(name="loop", aliases=["repeat", "repetir", "bucle"], help="Repite la canción actual (activar/desactivar).")
    async def loop(self, ctx: commands.Context) -> None:
        player = self.playing_player(ctx)
        player.loop_mode = not player.loop_mode
        await player.refresh_buttons()
        await say(
            self.bot, ctx.channel,
            f"{ctx.author.display_name} "
            + (f"puso '{player.current.title}' en repetición." if player.loop_mode else "quitó la repetición."),
            "🔂 Loop activado" if player.loop_mode else "➡️ Loop desactivado",
        )

    @commands.command(name="shuffle", aliases=["mezclar", "aleatorio"], help="Mezcla la cola.")
    async def shuffle(self, ctx: commands.Context) -> None:
        player = self.player_for_ctx(ctx)
        if player is None or len(player.queue) < 2:
            await ctx.send("No hay suficientes canciones en la cola para mezclar.")
            return
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await ctx.message.add_reaction("🔀")
        comment_later(self.bot, ctx.channel, f"{ctx.author.display_name} mezcló el orden de la cola.")

    @commands.command(name="remove", aliases=["rm", "quitar", "sacar", "borrar"], help="Quita una canción de la cola. Uso: remove <número>")
    async def remove(self, ctx: commands.Context, numero: int) -> None:
        player = self.player_for_ctx(ctx)
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

    @commands.command(name="clear", aliases=["cq", "limpiar", "vaciar"], help="Vacía la cola (sin parar la canción actual).")
    async def clear(self, ctx: commands.Context) -> None:
        player = self.player_for_ctx(ctx)
        if player:
            player.queue.clear()
        await ctx.message.add_reaction("🧹")

    @commands.command(name="volume", aliases=["vol", "v", "volumen"], help="Cambia el volumen (0-100). Uso: volume <n>")
    async def volume(self, ctx: commands.Context, nivel: Optional[int] = None) -> None:
        player = self.player_for_ctx(ctx)
        if player is None:
            await ctx.send("No estoy reproduciendo nada.")
            return
        if nivel is None:
            await ctx.send(f"🔊 Volumen actual: **{round(player.volume * 100)}%**")
            return
        nivel = player.set_volume(nivel)
        await ctx.send(f"🔊 Volumen: **{nivel}%**")

    # ---------- Eventos ----------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild
        ours = {c.user.id: c for c in self._clients() if c.user}

        # Lillia o un ayudante: si alguien lo desconectó a mano se limpia su cola; si lo movieron
        # de canal, el reproductor pasa a ese canal.
        if member.id in ours:
            client = ours[member.id]
            player = next((p for p in self.players_in(guild.id) if p.client is client), None)
            if player is None:
                return
            if before.channel and after.channel is None:
                await self.cleanup(player)
            elif after.channel and after.channel.id != player.voice_channel_id:
                self.players.pop(player.voice_channel_id, None)
                old_timer = self._alone_timers.pop(player.voice_channel_id, None)
                if old_timer:
                    old_timer.cancel()
                player.voice_channel_id = after.channel.id
                self.players[after.channel.id] = player
            return

        # Alguien entró o salió: cada canal con música revisa si se quedó sin gente.
        for player in self.players_in(guild.id):
            key = player.voice_channel_id
            humans = player.listeners()
            timer = self._alone_timers.get(key)
            if not humans and timer is None:
                self._alone_timers[key] = asyncio.create_task(self._leave_if_alone(player))
            elif humans and timer is not None:
                timer.cancel()
                self._alone_timers.pop(key, None)

    async def _leave_if_alone(self, player: "GuildPlayer") -> None:
        await asyncio.sleep(ALONE_SECONDS)
        self._alone_timers.pop(player.voice_channel_id, None)
        if self.players.get(player.voice_channel_id) is player and not player.listeners():
            await player._say(
                "Todos se fueron del canal de voz y te quedaste sin nadie, así que te vas.",
                f"Me quedé sola en **{player.where}**, me desconecto 👋",
            )
            await self.cleanup(player)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
