import asyncio
import logging
import os
import random
import shlex
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
import yt_dlp
from discord.ext import commands

from persona import comment_later, say

log = logging.getLogger("music")

FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg").strip() or "ffmpeg"
IDLE_SECONDS = int(float(os.getenv("IDLE_MINUTES", "5")) * 60)
ALONE_SECONDS = 60
DEFAULT_VOLUME = max(0, min(100, int(os.getenv("DEFAULT_VOLUME", "50")))) / 100
MAX_PLAYLIST = 100
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


async def fetch_tracks(query: str, requester: str) -> list[Track]:
    """Convierte un link o una búsqueda en una lista de canciones (varias si es playlist)."""
    target = query if is_url(query) else f"ytsearch1:{query}"
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
                    if not self.queue:
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
                    info = await resolve_stream(track)
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
                if not repeating:
                    await self._announce(track, info)
                await self._next.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("El reproductor de %s falló", self.guild.name)
        await self.cog.cleanup(self.guild)

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
        await self._say(
            f"Empieza a sonar '{track.title}', que pidió {track.requester}. Preséntala.",
            "",
            embed=embed,
        )


class Music(commands.Cog, name="Música"):
    """Reproduce música y videos de YouTube en el canal de voz."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self._alone_timers: dict[int, asyncio.Task] = {}

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
        if player and player.task is not asyncio.current_task():
            player.task.cancel()
        timer = self._alone_timers.pop(guild.id, None)
        if timer and timer is not asyncio.current_task():
            timer.cancel()
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)

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

    @commands.command(name="play", aliases=["p"], help="Reproduce un link de YouTube o busca por nombre.")
    async def play(self, ctx: commands.Context, *, busqueda: str) -> None:
        await self.ensure_voice(ctx)
        player = self.get_player(ctx)
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
