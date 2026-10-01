"""!estado: panel rápido para el dueño del bot (sin abrir los logs).

Muestra cuánto hace que está prendido, la IA (cupo, modelos), la música (yt-dlp), League (parche,
cuentas vinculadas, Riot API y cliente local para ARAM: Caos), copias de seguridad y los últimos
errores del log de hoy. Solo lo puede usar el dueño (OWNER_ID o el dueño de la app).
"""

import logging
import re
import time
from datetime import date
from pathlib import Path

import discord
from discord.ext import commands

import avisos

log = logging.getLogger("estado")

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "logs" / "bot.log"
LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):[\d,]+ \[(WARNING|ERROR|CRITICAL)\] ([\w.]+): (.*)$")
# Líneas que solo dicen que se cortó internet (Discord reconectando, no se pudo resolver un dominio...).
# No son fallas del bot: se cuentan aparte como "cortes de conexión".
NETWORK_RE = re.compile(r"Attempting a reconnect|getaddrinfo failed|Cannot connect to host|sin conexión|"
                        r"ClientConnectorError|ServerDisconnectedError|TimeoutError|Connection reset", re.I)
OUTAGE_GAP = 5 * 60  # segundos: problemas de red separados por más que esto son cortes distintos


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return (f"{days} d " if days else "") + (f"{hours} h " if hours or days else "") + f"{minutes} min"


def _size(path: Path) -> str:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else path.stat().st_size
    return f"{total / 1024:.0f} KB" if total < 1024 * 1024 else f"{total / 1024 / 1024:.1f} MB"


def _todays_problems(limit: int = 3) -> dict:
    """Lo que pasó hoy en el log: errores y advertencias reales (con los últimos) y, aparte, los cortes
    de conexión (horas en que se cortó internet o Discord)."""
    today = date.today().isoformat()
    result = {"advertencias": 0, "errores": 0, "ultimos": [], "cortes": []}
    last_network = None
    try:
        with open(LOG_FILE, encoding="utf-8", errors="ignore") as f:
            for line in f:
                match = LOG_LINE_RE.match(line.rstrip())
                if not match or match.group(1) != today:
                    continue
                hour, level, source, text = match.group(2), match.group(3), match.group(4), match.group(5)
                if NETWORK_RE.search(text):
                    h, m = map(int, hour.split(":"))
                    minute = h * 60 + m
                    if last_network is None or (minute - last_network) * 60 > OUTAGE_GAP:
                        result["cortes"].append(hour)
                    last_network = minute
                    continue
                result["advertencias" if level == "WARNING" else "errores"] += 1
                result["ultimos"].append(f"{hour} `{source}` {text[:100]}")
    except OSError:
        pass
    result["ultimos"] = result["ultimos"][-limit:]
    return result

class Estado(commands.Cog, name="Estado"):
    """Panel para el dueño del bot."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        owner = await avisos.get_owner(self.bot)
        if owner is None or ctx.author.id != owner.id:
            raise commands.CheckFailure("Ese comando es solo para el dueño del bot 🌸")
        return True

    @commands.command(name="estado", aliases=["status"], help="(Solo el dueño) Cómo está el bot: IA, música, League, respaldos y errores.")
    async def estado(self, ctx: commands.Context) -> None:
        bot = self.bot
        embed = discord.Embed(title="🩺 Estado de Lillia", color=0x9B59B6)
        started = getattr(bot, "started_at", None)
        embed.add_field(
            name="🤖 Bot",
            value=(f"Prendido hace **{_ago(time.time() - started)}**\n" if started else "")
            + f"Ping {bot.latency * 1000:.0f} ms · {len(bot.guilds)} servidor(es)",
        )

        # IA
        persona = bot.get_cog("Personaje")
        backend = getattr(persona, "backend", None)
        if backend is None:
            ai = "❌ Desactivada (revisa GEMINI_API_KEY)"
        elif backend.gemini_available() if hasattr(backend, "gemini_available") else backend.available():
            ranking = backend.ranking(usable_only=True)[:3] if hasattr(backend, "ranking") else []
            models = "\n".join(f"`{m}` {f'{sec:.1f}s' if sec else 'sin datos'}" for m, sec, _f in ranking)
            paused = sum(1 for t in getattr(backend, "cooldowns", {}).values() if t > time.time())
            retired = len(getattr(backend, "retired", {}))
            ai = (f"🟢 Con cupo\n{models}" + (f"\n{paused} modelo(s) en pausa" if paused else "")
                  + (f" · {retired} dado(s) de baja" if retired else ""))
        else:
            until = backend.available_again_at()
            ai = f"😴 Sin cupo, vuelve {avisos.fecha_hora(until)}" if until else "😴 Sin cupo"
        if backend is not None:
            used = sum(u for _m, u, _t, _l in backend.usage_report()) if hasattr(backend, "usage_report") else 0
            ai += f"\nHoy: {used} pedidos (`!cupo` para el detalle)"
            if getattr(backend, "backup", None):
                ai += f"\nRespaldo: {backend.backup.provider} " + ("🟢" if backend.backup.available() else "😴")
            import busqueda
            if busqueda.enabled():
                used, limit = busqueda.usage()
                ai += f"\nBúsqueda web: Tavily {used} de {limit} este mes " + ("🟢" if busqueda.available() else "😴")
            else:
                ai += "\nBúsqueda web: sin configurar (TAVILY_API_KEY)"
        embed.add_field(name="🧠 IA (Gemini)", value=ai)

        # Música
        music = bot.get_cog("Música")
        try:
            import yt_dlp

            ytdlp = yt_dlp.version.__version__
        except Exception:
            ytdlp = "?"
        playing = [p for p in getattr(music, "players", {}).values() if getattr(p, "current", None)]
        fails = len(getattr(music, "_ytdlp_errors", []))
        try:
            import ayudantes

            helpers_text = (f"Ayudantes: {len(ayudantes.ready())} de {len(ayudantes.TOKENS)} conectados\n"
                            if ayudantes.TOKENS else "")
        except Exception:
            helpers_text = ""
        embed.add_field(
            name="🎶 Música",
            value=f"yt-dlp {ytdlp}\n" + (f"Sonando en {len(playing)} canal(es) de voz\n" if playing else "Sin música ahora\n")
            + helpers_text
            + (f"⚠️ {fails} fallo(s) seguidos de yt-dlp" if fails else "✅ yt-dlp sin fallos"),
        )

        # League
        lol_lines = []
        try:
            from datadragon import dd

            lol_lines.append(f"Parche {dd.patch or '?'} · {len(dd.champions)} campeones")
        except Exception:
            pass
        try:
            import riot_api
            import riot_cuentas

            lol_lines.append(("✅ Riot API configurada" if riot_api.enabled() else "❌ Falta RIOT_API_KEY")
                             + f" · {len(riot_cuentas.load())} cuenta(s) vinculada(s)")
        except Exception:
            pass
        try:
            import lcu

            if not lcu.ENABLED:
                lol_lines.append("Cliente local: desactivado")
            elif lcu.lcu.connected_as:
                stored = len(lcu.load_store()["partidas"])
                lol_lines.append(f"🟢 Cliente abierto ({lcu.lcu.connected_as}) · {stored} ARAM: Caos guardadas")
            else:
                lol_lines.append(f"⚪ Cliente cerrado ({lcu.lcu.last_error or 'sin datos todavía'})")
        except Exception:
            pass
        embed.add_field(name="🎮 League", value="\n".join(lol_lines) or "-", inline=False)

        # Respaldos y datos
        try:
            import respaldo

            backups = sorted(respaldo.BACKUP_DIR.glob("data_*.zip"))
            last = backups[-1] if backups else None
            backup_text = (f"Última: `{last.name}` ({_size(last)}) · {len(backups)} guardadas" if last
                           else "⚠️ Todavía no hay copias")
        except Exception:
            backup_text = "?"
        data_dir = BASE_DIR / "data"
        embed.add_field(name="💾 Copias de seguridad", value=f"{backup_text}\nCarpeta data: {_size(data_dir) if data_dir.exists() else '-'}", inline=False)

        # Canal de anuncios
        eventos = bot.get_cog("Eventos")
        if eventos is not None:
            channels = []
            for guild in bot.guilds:
                channel = eventos.channel_for(guild)
                channels.append(f"{guild.name}: {channel.mention if channel else '❌ ninguno'}")
            embed.add_field(name="📣 Canal de anuncios", value="\n".join(channels)[:1024] or "-", inline=False)

        # Errores de hoy
        today = _todays_problems()
        if today["errores"] or today["advertencias"]:
            problems = f"{today['errores']} error(es) · {today['advertencias']} advertencia(s)"
        else:
            problems = "✅ Sin errores"
        if today["cortes"]:
            hours = ", ".join(today["cortes"][-5:])
            problems += f"\n🌐 Cortes de internet: {len(today['cortes'])} (a las {hours}); el bot se reconectó solo"
        if today["ultimos"]:
            problems += "\nÚltimos:\n" + "\n".join(today["ultimos"])
        embed.add_field(name="📋 Log de hoy", value=problems[:1024], inline=False)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Estado(bot))
