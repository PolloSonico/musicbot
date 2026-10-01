"""🌸 Lillia Wrapped: resumen musical del mes, por persona o de todo el servidor.

Datos (data/escuchas.jsonl, una línea JSON por evento; ver jsonio.append_jsonl):
- "pedido":  alguien pidió una canción (con !play, !buscar o hablándole a Lillia).
- "escucha": una canción terminó de sonar (o la saltaron): cuántos segundos sonó, quién la pidió,
             quiénes estaban en el canal de voz escuchando y si la eligió la radio.

La primera vez que arranca, importa lo que ya había en data/canciones_por_usuario.json como
pedidos (sin servidor ni minutos, porque eso no se guardaba antes).

Comandos:
  !wrapped                 tu resumen del mes actual
  !wrapped @persona        el de otra persona
  !wrapped server          el del servidor
  !wrapped pasado          el del mes anterior (también: "septiembre", "2026-09", "09/2026")
Y el día 1 de cada mes Lillia publica sola el Wrapped del servidor del mes que terminó
(lo programa eventos.py).
"""

import logging
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

from jsonio import append_jsonl, load_json, read_jsonl
from letras import artist_of, song_name
from persona import _persona, extract_actions, say

log = logging.getLogger("wrapped")

DATA_DIR = Path(__file__).resolve().parent / "data"
EVENTS_FILE = DATA_DIR / "escuchas.jsonl"
LOL_FILE = DATA_DIR / "partidas_lol.jsonl"  # partidas de League de las cuentas vinculadas (lo llena riot.py)
LOL_MIN_GAMES_KDA = 5  # partidas mínimas para competir por "mejor KDA" del servidor
OLD_FILE = DATA_DIR / "canciones_por_usuario.json"
MAX_REQUESTS_PER_PLAYLIST = 25  # de una playlist enorme se cuentan solo las primeras
COLOR = 0xF5A9D0

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábados", "domingos"]
FRANJAS = [(0, 6, "de madrugada 🌙"), (6, 12, "por la mañana ☀️"), (12, 20, "por la tarde 🌿"), (20, 24, "por la noche ✨")]


# ---------- Registrar (lo llama music.py) ----------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record_request(guild: discord.Guild, user: discord.abc.User, tracks: list) -> None:
    """Guarda que `user` pidió estas canciones. Nunca falla (no debe frenar la música)."""
    try:
        _import_old_data()
        for track in tracks[:MAX_REQUESTS_PER_PLAYLIST]:
            append_jsonl(EVENTS_FILE, {
                "tipo": "pedido", "t": _now(), "g": guild.id, "u": user.id, "un": user.name,
                "titulo": track.title, "url": track.url, "canal_yt": track.uploader, "dur": track.duration,
            })
    except Exception:
        log.exception("No se pudo guardar el pedido para el Wrapped")


def record_listen(guild: discord.Guild, track, seconds: float, listeners: list[discord.Member], radio: bool) -> None:
    """Guarda que sonó `track` durante `seconds` con estas personas en el canal de voz."""
    try:
        if seconds < 5:
            return
        skipped = bool(track.duration) and seconds < track.duration - 10
        append_jsonl(EVENTS_FILE, {
            "tipo": "escucha", "t": _now(), "g": guild.id, "titulo": track.title, "url": track.url,
            "canal_yt": track.uploader, "dur": track.duration, "seg": round(seconds),
            "pidio": getattr(track, "requester_id", 0), "oyentes": [m.id for m in listeners],
            "radio": radio, "salto": skipped,
        })
    except Exception:
        log.exception("No se pudo guardar la escucha para el Wrapped")


def record_lol(user_id: int, guild_id: Optional[int], match_id: str, p: dict, info: dict,
               queue_label: str = "", champion_label: str = "") -> None:
    """Guarda una partida de League de una cuenta vinculada (para el Wrapped). Nunca falla."""
    try:
        end = info.get("gameEndTimestamp") or 0
        arena = info.get("gameMode") == "CHERRY" or info.get("queueId") in (1700, 1710)
        place = p.get("placement") or p.get("subteamPlacement") or 0
        append_jsonl(LOL_FILE, {
            "t": datetime.fromtimestamp(end / 1000).isoformat(timespec="seconds") if end else _now(),
            "u": user_id, "g": guild_id or 0, "match": match_id,
            "champ": champion_label or p.get("championName", "?"), "cola": queue_label,
            "k": p.get("kills", 0), "d": p.get("deaths", 0), "a": p.get("assists", 0),
            "win": bool(p.get("win")) if not arena else bool(place and place <= 4),
            "puesto": place if arena else None, "dur": info.get("gameDuration", 0),
            "dmg": p.get("totalDamageDealtToChampions", 0), "penta": p.get("pentaKills", 0),
        })
    except Exception:
        log.exception("No se pudo guardar la partida de League para el Wrapped")


def _lol_games(month: str, guild_id: int) -> list[dict]:
    seen, games = set(), []
    for g in read_jsonl(LOL_FILE):
        key = (g.get("u"), g.get("match"))
        if key in seen or not g.get("t", "").startswith(month) or g.get("g") not in (guild_id, 0):
            continue
        seen.add(key)
        games.append(g)
    return games


def _kda_ratio(games: list[dict]) -> float:
    return (sum(g["k"] for g in games) + sum(g["a"] for g in games)) / max(1, sum(g["d"] for g in games))


def lol_user_stats(month: str, guild_id: int, user_id: int) -> dict:
    games = [g for g in _lol_games(month, guild_id) if g.get("u") == user_id]
    if not games:
        return {"partidas": 0}
    best = max(games, key=lambda g: ((g["k"] + g["a"]) / max(1, g["d"]), g["k"]))
    return {
        "partidas": len(games),
        "victorias": sum(1 for g in games if g.get("win")),
        "kda": _kda_ratio(games),
        "campeones": Counter(g["champ"] for g in games).most_common(3),
        "cola": Counter(g.get("cola") or "?" for g in games).most_common(1)[0][0],
        "mejor": f"{best['champ']} {best['k']}/{best['d']}/{best['a']}" + (f" (puesto {best['puesto']})" if best.get("puesto") else ""),
        "pentas": sum(g.get("penta", 0) for g in games),
        "horas": sum(g.get("dur", 0) for g in games),
    }


def lol_server_stats(month: str, guild_id: int) -> dict:
    games = _lol_games(month, guild_id)
    if not games:
        return {"partidas": 0}
    by_user: dict[int, list[dict]] = defaultdict(list)
    for g in games:
        by_user[g["u"]].append(g)
    most = sorted(by_user.items(), key=lambda x: -len(x[1]))[:3]
    eligible = [(uid, _kda_ratio(gs)) for uid, gs in by_user.items() if len(gs) >= LOL_MIN_GAMES_KDA]
    return {
        "partidas": len(games),
        "horas": sum(g.get("dur", 0) for g in games),
        "viciosos": [(uid, len(gs)) for uid, gs in most],
        "mejor_kda": max(eligible, key=lambda x: x[1]) if eligible else None,
        "campeon": Counter(g["champ"] for g in games).most_common(1)[0],
        "pentas": sum(g.get("penta", 0) for g in games),
    }


def _lol_text(lol: dict) -> str:
    rate = round(100 * lol["victorias"] / lol["partidas"])
    champs = ", ".join(f"{c} ({n})" for c, n in lol["campeones"])
    plural = "s" if lol["partidas"] != 1 else ""
    return (f"**{lol['partidas']}** partida{plural} · {rate}% victorias · KDA {lol['kda']:.2f}\n"
            f"Campeones: {champs}\nModo favorito: {lol['cola']} · Mejor partida: {lol['mejor']}"
            + (f"\n🔥 ¡{lol['pentas']} pentakill{'s' if lol['pentas'] > 1 else ''}!" if lol["pentas"] else ""))


def _import_old_data() -> None:
    """Una sola vez: pasa el historial viejo (canciones_por_usuario.json) al formato nuevo."""
    if EVENTS_FILE.exists():
        return
    old = load_json(OLD_FILE, {})
    count = 0
    if isinstance(old, dict):
        for uid, entry in old.items():
            for song in entry.get("canciones", []):
                append_jsonl(EVENTS_FILE, {
                    "tipo": "pedido", "t": f"{song.get('fecha', '2000-01-01')}T12:00:00", "g": 0,
                    "u": int(uid), "un": entry.get("usuario", ""), "titulo": song.get("titulo", "?"),
                    "url": "", "canal_yt": "", "dur": None, "importado": True,
                })
                count += 1
    if not EVENTS_FILE.exists():
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        EVENTS_FILE.touch()
    log.info("Wrapped: importados %d pedidos del historial viejo", count)


# ---------- Cálculos ----------

def _top(counter: Counter, n: int) -> list[tuple[str, int]]:
    return [(k, v) for k, v in counter.most_common(n) if k]


def _month_events(month: str, guild_id: int) -> list[dict]:
    _import_old_data()
    return [e for e in read_jsonl(EVENTS_FILE)
            if e.get("t", "").startswith(month) and e.get("g") in (guild_id, 0)]


def _hours(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def _favorite_moment(events: list[dict]) -> Optional[str]:
    if not events:
        return None
    days = Counter(datetime.fromisoformat(e["t"]).weekday() for e in events)
    hours = Counter(datetime.fromisoformat(e["t"]).hour for e in events if not e.get("importado"))
    day = DIAS[days.most_common(1)[0][0]]
    if not hours:
        return f"los {day}"
    hour = hours.most_common(1)[0][0]
    franja = next(label for start, end, label in FRANJAS if start <= hour < end)
    return f"los {day}, {franja}"


def user_stats(month: str, guild_id: int, user_id: int) -> dict:
    events = _month_events(month, guild_id)
    requests = [e for e in events if e["tipo"] == "pedido" and e.get("u") == user_id]
    listens = [e for e in events if e["tipo"] == "escucha" and user_id in e.get("oyentes", [])]
    songs = Counter(song_name(e["titulo"]) for e in requests)
    artists = Counter(artist_of(e["titulo"], e.get("canal_yt", "")) for e in requests)
    ranking = Counter(e["u"] for e in events if e["tipo"] == "pedido")
    position = sorted(ranking, key=lambda u: -ranking[u]).index(user_id) + 1 if user_id in ranking else None
    their_plays = [e for e in events if e["tipo"] == "escucha" and e.get("pidio") == user_id]
    return {
        "pedidos": len(requests),
        "distintas": len(songs),
        "top_canciones": _top(songs, 5),
        "top_artistas": _top(artists, 3),
        "segundos": sum(e.get("seg", 0) for e in listens),
        "escuchadas": len(listens),
        "saltadas": sum(1 for e in their_plays if e.get("salto")),
        "sonaron": len(their_plays),
        "momento": _favorite_moment(requests),
        "primera": song_name(requests[0]["titulo"]) if requests else None,
        "puesto": position,
        "djs": len(ranking),
    }


def server_stats(month: str, guild_id: int) -> dict:
    events = _month_events(month, guild_id)
    requests = [e for e in events if e["tipo"] == "pedido"]
    listens = [e for e in events if e["tipo"] == "escucha"]
    djs = Counter(e["u"] for e in requests)
    names = {e["u"]: e.get("un", "?") for e in requests}
    listeners = defaultdict(float)
    for e in listens:
        for uid in e.get("oyentes", []):
            listeners[uid] += e.get("seg", 0)
    return {
        "pedidos": len(requests),
        "sonaron": len(listens),
        "segundos": sum(e.get("seg", 0) for e in listens),
        "top_canciones": _top(Counter(song_name(e["titulo"]) for e in listens or requests), 5),
        "top_artistas": _top(Counter(artist_of(e["titulo"], e.get("canal_yt", "")) for e in listens or requests), 5),
        "top_djs": [(uid, names.get(uid, "?"), n) for uid, n in djs.most_common(3)],
        "top_oyentes": sorted(listeners.items(), key=lambda x: -x[1])[:3],
        "radio": sum(1 for e in listens if e.get("radio")),
        "saltadas": sum(1 for e in listens if e.get("salto")),
        "momento": _favorite_moment(requests),
    }


# ---------- Mes pedido ----------

def parse_month(text: str, today: Optional[date] = None) -> Optional[str]:
    """'' -> mes actual; 'pasado'/'anterior' -> mes anterior; 'septiembre', '2026-09', '09/2026'."""
    today = today or date.today()
    text = text.strip().lower()
    if not text:
        return f"{today:%Y-%m}"
    if text in ("pasado", "anterior", "ultimo", "último"):
        year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        return f"{year}-{month:02d}"
    if m := re.fullmatch(r"(\d{4})[-/](\d{1,2})", text):
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    if m := re.fullmatch(r"(\d{1,2})[-/](\d{4})", text):
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    for i, name in enumerate(MESES, start=1):
        if text.startswith(name[:3]):
            year = today.year if i <= today.month else today.year - 1  # "diciembre" en enero = el pasado
            return f"{year}-{i:02d}"
    return None


def month_label(month: str) -> str:
    year, mon = month.split("-")
    return f"{MESES[int(mon) - 1]} de {year}"


def previous_month(month: str) -> str:
    year, mon = map(int, month.split("-"))
    return f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"


# ---------- Embeds ----------

def _lines(items: list[tuple[str, int]], unit: str = "") -> str:
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    return "\n".join(f"{medals[i]} {name[:60]}" + (f" · {n}{unit}" if n > 1 else "") for i, (name, n) in enumerate(items)) or "-"


def user_embed(member: discord.abc.User, month: str, st: dict, lol: Optional[dict] = None) -> discord.Embed:
    embed = discord.Embed(title=f"🌸 Lillia Wrapped · {member.display_name}", description=f"*{month_label(month)}*", color=COLOR)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🎶 Canciones pedidas", value=f"**{st['pedidos']}** ({st['distintas']} distintas)")
    embed.add_field(name="🎧 Tiempo escuchando", value=f"**{_hours(st['segundos'])}**")
    if st["puesto"]:
        embed.add_field(name="🏆 Puesto de DJ", value=f"**#{st['puesto']}** de {st['djs']}")
    embed.add_field(name="💖 Sus canciones", value=_lines(st["top_canciones"]), inline=False)
    if st["top_artistas"]:
        embed.add_field(name="🎤 Sus artistas", value=_lines(st["top_artistas"]), inline=False)
    extras = []
    if st["momento"]:
        extras.append(f"Pide música sobre todo {st['momento']}")
    if st["primera"]:
        extras.append(f"Abrió el mes con *{st['primera'][:60]}*")
    if st["sonaron"]:
        extras.append(f"De sus canciones, saltaron {st['saltadas']} de {st['sonaron']}")
    if extras:
        embed.add_field(name="✨ Curiosidades", value="\n".join(extras), inline=False)
    if lol and lol.get("partidas"):
        embed.add_field(name="🎮 League of Legends", value=_lol_text(lol), inline=False)
    return embed


def server_embed(guild: discord.Guild, month: str, st: dict, lol: Optional[dict] = None) -> discord.Embed:
    embed = discord.Embed(title=f"🌸 Lillia Wrapped · {guild.name}", description=f"*{month_label(month)}*", color=COLOR)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="🎶 Canciones que sonaron", value=f"**{st['sonaron']}** ({st['pedidos']} pedidas)")
    embed.add_field(name="⏳ Música en total", value=f"**{_hours(st['segundos'])}**")
    if st["radio"]:
        embed.add_field(name="📻 Elegidas por la radio", value=f"**{st['radio']}**")
    embed.add_field(name="💖 Las más escuchadas", value=_lines(st["top_canciones"]), inline=False)
    embed.add_field(name="🎤 Artistas del mes", value=_lines(st["top_artistas"]), inline=False)
    if st["top_djs"]:
        djs = [(f"<@{uid}>", n) for uid, _name, n in st["top_djs"]]
        embed.add_field(name="🎧 DJs del mes", value=_lines(djs, " pedidas"), inline=True)
    if st["top_oyentes"]:
        medals = ["🥇", "🥈", "🥉"]
        embed.add_field(
            name="👂 Más horas escuchando",
            value="\n".join(f"{medals[i]} <@{uid}> · {_hours(sec)}" for i, (uid, sec) in enumerate(st["top_oyentes"])),
            inline=True,
        )
    if lol and lol.get("partidas"):
        lines = [f"**{lol['partidas']}** partidas · {_hours(lol['horas'])} jugando"]
        if lol["viciosos"]:
            lines.append("Más partidas: " + ", ".join(f"<@{uid}> ({n})" for uid, n in lol["viciosos"]))
        if lol["mejor_kda"]:
            lines.append(f"Mejor KDA: <@{lol['mejor_kda'][0]}> ({lol['mejor_kda'][1]:.2f})")
        lines.append(f"Campeón del server: {lol['campeon'][0]} ({lol['campeon'][1]} partidas)")
        if lol["pentas"]:
            lines.append(f"🔥 Pentakills: {lol['pentas']}")
        embed.add_field(name="🎮 League del servidor", value="\n".join(lines), inline=False)
    if st["momento"]:
        embed.set_footer(text=f"El servidor pide música sobre todo {st['momento']}")
    return embed


def _summary_for_ai(st: dict, who: str, lol: Optional[dict] = None) -> str:
    songs = ", ".join(f"'{s}' ({n} veces)" for s, n in st["top_canciones"][:3]) or "ninguna"
    artists = ", ".join(a for a, _ in st["top_artistas"][:3]) or "no se sabe"
    text = (f"{who}: {st['pedidos']} canciones pedidas, {_hours(st['segundos'])} de música, canciones más "
            f"repetidas: {songs}; artistas: {artists}; momento favorito: {st.get('momento') or '?'}.")
    if lol and lol.get("partidas"):
        if "victorias" in lol:
            text += (f" En League: {lol['partidas']} partidas, {lol['victorias']} victorias, KDA {lol['kda']:.2f}, "
                     f"campeones más jugados: {', '.join(c for c, _ in lol['campeones'])}, mejor partida: {lol['mejor']}.")
        else:
            text += (f" En League el servidor jugó {lol['partidas']} partidas; campeón más jugado: {lol['campeon'][0]}"
                     + (f"; hubo {lol['pentas']} pentakills" if lol["pentas"] else "") + ".")
    return text


async def _send(bot: commands.Bot, channel, embed: discord.Embed, situation: str, fallback: str) -> None:
    """Manda el embed al instante y le agrega el comentario de Lillia cuando llega (sin texto fijo)."""
    message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    persona = _persona(bot)
    line = None
    if persona:
        line = await persona.ask(channel, f"(({situation}))", 40, remember=False)
        line = extract_actions(line)[0] if line else None
    try:
        await message.edit(content=(line or fallback)[:2000])
    except discord.HTTPException:
        pass


async def post_server_wrapped(bot: commands.Bot, guild: discord.Guild, channel, month: str) -> bool:
    """Publica el Wrapped del servidor. False si ese mes no hubo música."""
    st = server_stats(month, guild.id)
    lol = lol_server_stats(month, guild.id)
    if not st["pedidos"] and not st["sonaron"] and not lol["partidas"]:
        return False
    await _send(
        bot, channel, server_embed(guild, month, st, lol),
        f"Presentas el 'Lillia Wrapped' de todo el servidor de {month_label(month)}. "
        f"{_summary_for_ai(st, 'El servidor', lol)} Coméntalo en personaje, con cariño y algo de humor, en 2 a 4 "
        "frases: qué dicen los gustos del grupo y felicita a los DJs del mes. No repitas todos los números "
        "(ya están en la tarjeta).",
        f"🌸 ¡Así sonó **{month_label(month)}** en el servidor! Gracias por compartir sus sueños en forma de canciones 🦌✨",
    )
    return True


class Wrapped(commands.Cog, name="Wrapped"):
    """Resumen musical del mes, al estilo Spotify Wrapped."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(
        name="wrapped", aliases=["resumen"],
        help="Tu resumen del mes (música y League). Uso: wrapped [@persona | server] [pasado | septiembre | 2026-09]",
    )
    @commands.guild_only()
    async def wrapped(self, ctx: commands.Context, *, args: str = "") -> None:
        words = [w for w in re.sub(r"<@!?\d+>", " ", args).split()]
        server = any(w.lower() in ("server", "servidor", "todos") for w in words)
        month_words = " ".join(w for w in words if w.lower() not in ("server", "servidor", "todos"))
        month = parse_month(month_words)
        if month is None:
            await ctx.send("No entendí el mes 😳 Prueba con `pasado`, `septiembre` o `2026-09`.")
            return
        explicit_month = bool(month_words.strip())

        if server:
            if not await post_server_wrapped(self.bot, ctx.guild, ctx.channel, month):
                await ctx.send(f"En {month_label(month)} no sonó música en el servidor todavía 💤")
            return

        member = ctx.message.mentions[0] if ctx.message.mentions else ctx.author
        st = user_stats(month, ctx.guild.id, member.id)
        lol = lol_user_stats(month, ctx.guild.id, member.id)
        if not st["pedidos"] and not st["segundos"] and not lol["partidas"] and not explicit_month:
            # A principio de mes todavía no hay nada: se muestra el mes que terminó.
            month = previous_month(month)
            st = user_stats(month, ctx.guild.id, member.id)
            lol = lol_user_stats(month, ctx.guild.id, member.id)
        if not st["pedidos"] and not st["segundos"] and not lol["partidas"]:
            await say(
                self.bot, ctx.channel,
                f"{ctx.author.display_name} pidió el Wrapped de {member.display_name} de {month_label(month)}, "
                "pero esa persona no pidió ni escuchó música contigo ese mes.",
                f"No tengo canciones de **{member.display_name}** en {month_label(month)} 💤",
            )
            return
        await _send(
            self.bot, ctx.channel, user_embed(member, month, st, lol),
            f"Presentas el 'Lillia Wrapped' de {member.display_name} de {month_label(month)}. "
            f"{_summary_for_ai(st, member.display_name, lol)} Coméntalo en personaje en 2 a 4 frases, con ternura y "
            "algo de humor: qué dicen sus gustos de esa persona (como si fueran sus sueños). No repitas todos "
            "los números (ya están en la tarjeta).",
            f"🌸 Este fue tu mes en canciones, {member.display_name}... ¡qué lindos sueños! 🦌✨",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Wrapped(bot))
