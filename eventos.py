"""Cosas que Lillia hace sola según la fecha y la hora (sin que nadie tenga que configurar nada):

- Calendario (personajes/eventos.json): mientras dura un evento (el Mundial, Navidad, su
  aniversario...) lo tiene presente en la charla; si el evento tiene "anunciar": true, el primer día
  lo anuncia en el canal de eventos (con "buscar": true, busca en internet los datos del día).
- Parche nuevo de League: cuando Data Dragon muestra un parche nuevo, lo anuncia y resume los
  cambios principales (buscando las notas del parche).
- Wrapped mensual: el día 1 publica el Lillia Wrapped del servidor del mes que terminó.
- Copia de seguridad: una vez por día comprime data/ en backups/ (ver respaldo.py).
- Buenas noches: si alguien escribe de madrugada, una vez por noche le dice que se vaya a dormir.
- MSI y finales de ligas: cada 2 semanas busca en internet (Tavily) las fechas confirmadas que
  todavía no están en el calendario y las guarda en data/eventos_esports.json; se anuncian igual que
  las del calendario.

Canal de eventos: PERSONA_EVENTS_CHANNEL en .env; si no está, el último canal donde se usaron
comandos del bot; si no, el canal de bienvenida del servidor.
"""

import asyncio
import logging
import os
import random
import re
import time
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands, tasks

import calendario
import respaldo
import wrapped
from datadragon import dd, patch_label
from jsonio import load_json, save_json
from persona import NO_MENTIONS, _persona, extract_actions

log = logging.getLogger("eventos")

STATE_FILE = Path(__file__).resolve().parent / "data" / "eventos_estado.json"
ESPORTS_EVERY = 14 * 86400  # cada cuánto busca fechas nuevas del MSI y finales de ligas
ESPORTS_RETRY = 86400  # si la búsqueda falló (sin cupo, etc.), se reintenta al día siguiente
ESPORTS_LEAGUES = ("msi", "lck", "lpl", "lec", "lcs", "cblol", "lcp")
ESPORTS_LINE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([^|\n]+?)\s*\|\s*([^\n]+)"
)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _bool(name: str, default: bool) -> bool:
    value = _env(name).lower()
    return default if not value else value in ("1", "true", "si", "sí", "yes")


EVENTS_CHANNELS = [int(x) for x in re.findall(r"\d+", _env("PERSONA_EVENTS_CHANNEL"))]
EVENTS_HOUR = int(_env("PERSONA_EVENTS_HOUR", "12"))  # a partir de qué hora anuncia (hora del PC)
ANNOUNCE_PATCHES = _bool("PERSONA_PATCH_NEWS", True)
ESPORTS_AUTO = _bool("PERSONA_ESPORTS_AUTO", True)
MONTHLY_WRAPPED = _bool("PERSONA_MONTHLY_WRAPPED", True)
NIGHT_GREETING = _bool("PERSONA_NIGHT_GREETING", True)
WELCOME = _bool("PERSONA_WELCOME", True)
WELCOME_FALLBACK = (
    "¡H-hola, {mention}! 🌸 Soy Lillia... bienvenido al bosque. Si quieres música, usa `{prefix}play` y el "
    "nombre de una canción, y si quieres charlar, mencióname. Yo cuido tus sueños 🦌✨"
)
_night = re.findall(r"\d+", _env("PERSONA_NIGHT_HOURS", "1-6"))
NIGHT_START, NIGHT_END = (int(_night[0]), int(_night[1])) if len(_night) == 2 else (1, 6)
NIGHT_CHANNEL_COOLDOWN = 10 * 60  # segundos entre buenas noches en un mismo canal
NIGHT_MAX_PER_GUILD = 6  # buenas noches como mucho por noche y servidor

NIGHT_FALLBACK = [
    "💤 ¿T-todavía despierto a esta hora, {name}? Los sueños te están esperando... ve a descansar, ¿sí? 🌙",
    "🌙 {name}... 🥱 ya es muy tarde. El Árbol de los Sueños guarda un lugarcito para ti 🦌💤",
    "🥱 Buenas noches, {name}. Si no duermes, tus sueños se pierden... y después tengo que salir a buscarlos 🌸",
    "✨ ¡Eep! {name}, son horas de dormir... prometo cuidar tus sueños mientras descansas 💤",
]


def _is_night(hour: int) -> bool:
    if NIGHT_START <= NIGHT_END:
        return NIGHT_START <= hour < NIGHT_END
    return hour >= NIGHT_START or hour < NIGHT_END  # por ejemplo 23-5


def _can_send(channel: Optional[discord.abc.GuildChannel]) -> bool:
    if not isinstance(channel, discord.TextChannel):
        return False
    perms = channel.permissions_for(channel.guild.me)
    return perms.send_messages and perms.view_channel and perms.embed_links


class Eventos(commands.Cog, name="Eventos"):
    """Fechas especiales, parches nuevos, Wrapped mensual y buenas noches."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        state = load_json(STATE_FILE, {})
        self.state: dict = state if isinstance(state, dict) else {}
        self.state.setdefault("hechos", {})
        self.state.setdefault("canales", {})
        self._night_greeted: dict[int, str] = {}  # usuario -> "noche" en que ya se lo saludó
        self._night_channel: dict[int, float] = {}
        self._night_count: dict[tuple[int, str], int] = {}
        self.ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()

    # ---------- Estado ----------

    def _done(self, key: str) -> bool:
        return key in self.state["hechos"]

    def _mark(self, key: str) -> None:
        self.state["hechos"][key] = date.today().isoformat()
        # Limpia marcas de hace más de 400 días para que el archivo no crezca para siempre.
        limit = (date.today() - timedelta(days=400)).isoformat()
        self.state["hechos"] = {k: v for k, v in self.state["hechos"].items() if v >= limit}
        save_json(STATE_FILE, self.state)

    def channel_for(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        for channel_id in EVENTS_CHANNELS:
            channel = guild.get_channel(channel_id)
            if _can_send(channel):
                return channel
        saved = self.state["canales"].get(str(guild.id))
        channel = guild.get_channel(int(saved)) if saved else None
        if _can_send(channel):
            return channel
        if _can_send(guild.system_channel):
            return guild.system_channel
        return next((c for c in guild.text_channels if _can_send(c)), None)

    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context) -> None:
        """Recuerda el último canal donde se usó el bot en cada servidor (para los anuncios)."""
        if ctx.guild is None or EVENTS_CHANNELS:
            return
        if self.state["canales"].get(str(ctx.guild.id)) != ctx.channel.id:
            self.state["canales"][str(ctx.guild.id)] = ctx.channel.id
            save_json(STATE_FILE, self.state)

    # ---------- Anunciar ----------

    async def _announce(self, prompt: str, fallback: str, web: str = "") -> None:
        """Manda un anuncio a cada servidor (con la voz del personaje si hay IA)."""
        for guild in self.bot.guilds:
            channel = self.channel_for(guild)
            if channel is None:
                continue
            persona = _persona(self.bot)
            text = None
            if persona:
                text = await persona.ask(channel, f"(({prompt}))", 90 if web else 45, remember=False, web=web)
                text = extract_actions(text)[0] if text else None
            try:
                await channel.send((text or fallback)[:2000], allowed_mentions=NO_MENTIONS)
            except discord.HTTPException as exc:
                log.warning("No se pudo anunciar en %s: %s", guild.name, exc)

    @tasks.loop(minutes=10)
    async def ticker(self) -> None:
        now = datetime.now()
        try:
            await asyncio.to_thread(respaldo.make_backup)  # una vez por día (si ya existe, no hace nada)
        except Exception:
            log.exception("No se pudo hacer la copia de seguridad de data/")
        try:
            await self._check_patch()
            if ESPORTS_AUTO:
                await self._refresh_esports()
            if now.hour >= EVENTS_HOUR:
                await self._check_calendar(now.date())
                if MONTHLY_WRAPPED and now.day == 1:
                    await self._monthly_wrapped(now.date())
        except Exception:
            log.exception("Fallo en la revisión de eventos")

    @ticker.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_patch(self) -> None:
        previous = await dd.refresh()
        if not (previous and ANNOUNCE_PATCHES and dd.version):
            return
        key = f"parche:{dd.version}"
        if self._done(key):
            return
        self._mark(key)
        patch = dd.patch
        await self._announce(
            f"Salió el parche {patch} de League of Legends (antes estaba el {patch_label(previous)}). Con "
            f"las notas del parche {patch} que encontró la búsqueda (abajo), anúncialo al servidor en personaje: cuenta en una lista "
            "corta los 3 a 5 cambios más importantes (campeones mejorados o debilitados, objetos, sistemas). Si no "
            "encuentras las notas todavía, solo anuncia que llegó, sin inventar cambios.",
            f"🌿 ¡Llegó el parche **{patch}** de League of Legends! A ver qué sueños nuevos trae... 🦌✨",
            web=f"League of Legends patch {patch} notes: buffs, nerfs y cambios principales",
        )

    # ---------- MSI y finales de ligas (búsqueda automática) ----------

    @staticmethod
    def _league(name: str) -> Optional[str]:
        low = name.lower()
        if "msi" in low or "mid-season" in low or "mid season" in low:
            return "msi"
        return next((league for league in ESPORTS_LEAGUES if re.search(rf"\b{league}\b", low)), None)

    async def _refresh_esports(self) -> None:
        if time.time() < self.state.get("esports_proxima", 0):
            return
        persona = _persona(self.bot)
        if persona is None:
            return  # sin IA (o sin cupo): se intenta en la próxima vuelta
        today = date.today()
        reply = await persona.ask(
            types.SimpleNamespace(id=0),  # "canal" interno: no se mezcla con ninguna charla
            "((Tarea interna del sistema, no es una charla. Con los resultados de la búsqueda en internet "
            f"(abajo), saca las fechas CONFIRMADAS de los próximos eventos de League of Legends desde hoy "
            f"({today.isoformat()}) y durante los próximos 8 meses: el MSI (Mid-Season Invitational) y las "
            "FINALES de cada split o temporada de las ligas LCK, LPL, LEC, LCS, CBLOL y LCP. No incluyas el "
            "Mundial (Worlds). Responde SOLO con una línea por evento, sin ningún otro texto, con este formato "
            "exacto:\nAAAA-MM-DD | AAAA-MM-DD | nombre del evento | ciudad y estadio\n(la primera fecha es el "
            "día en que empieza y la segunda el último día; si dura un solo día, repite la fecha). Solo fechas "
            "confirmadas oficialmente. Si no encuentras ninguna, responde NINGUNO.))",
            120, remember=False,
            web=f"League of Legends esports {today.year} {today.year + 1} schedule dates: MSI, LCK LPL LEC LCS CBLOL LCP finals",
        )
        if reply is None:
            self.state["esports_proxima"] = time.time() + ESPORTS_RETRY
            save_json(STATE_FILE, self.state)
            return
        found = self._parse_esports(reply, today)
        self._save_esports(found, today)
        self.state["esports_proxima"] = time.time() + ESPORTS_EVERY
        save_json(STATE_FILE, self.state)
        log.info("Calendario de esports actualizado: %d eventos (%s)", len(found), ", ".join(e["nombre"] for e in found))

    def _parse_esports(self, reply: str, today: date) -> list[dict]:
        manual = calendario.manual_events()
        events: list[dict] = []
        for start_text, end_text, name, place in ESPORTS_LINE_RE.findall(reply):
            try:
                start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
            except ValueError:
                continue
            name = re.sub(r"[*_`]", "", name).strip()[:80]
            place = re.sub(r"[*_`]", "", place).strip()[:120]
            league = self._league(name)
            if league is None or re.search(r"worlds|mundial|world championship", name, re.I):
                continue
            if league != "msi" and not re.search(r"final|championship|campeonato", name, re.I):
                continue  # de las ligas solo interesan las finales
            if end < start or (end - start).days > 25 or end < today or start > today + timedelta(days=250):
                continue
            # Si ya está en el calendario manual (misma liga y fechas que se cruzan), manda el manual.
            duplicate = False
            for raw in manual:
                try:
                    m_start = date.fromisoformat(raw["desde"])
                    m_end = date.fromisoformat(raw.get("hasta") or raw["desde"])
                except (KeyError, ValueError):
                    continue  # eventos anuales (MM-DD) o mal escritos
                if self._league(raw.get("nombre", "")) == league and m_start <= end and start <= m_end:
                    duplicate = True
                    break
            if duplicate:
                continue
            events.append({
                "nombre": name,
                "desde": start.isoformat(),
                "hasta": end.isoformat(),
                "contexto": f"{name} en {place} (fechas encontradas automáticamente en internet). Si te "
                            "preguntan resultados, equipos u horarios, búscalos; no los inventes.",
                "anunciar": True,
                "buscar": True,
                "auto": True,
            })
        return events[:12]

    def _save_esports(self, found: list[dict], today: date) -> None:
        """Guarda lo encontrado sin perder eventos automáticos que ya estaban (y siguen vigentes)."""
        old = load_json(calendario.AUTO_FILE, [])
        old = old if isinstance(old, list) else []
        keep_from = (today - timedelta(days=30)).isoformat()
        merged: dict[tuple, dict] = {}
        for event in old + found:  # lo nuevo pisa a lo viejo
            if event.get("hasta", event.get("desde", "")) < keep_from:
                continue
            merged[(self._league(event.get("nombre", "")), event.get("desde"))] = event
        save_json(calendario.AUTO_FILE, sorted(merged.values(), key=lambda e: e["desde"]))

    async def _check_calendar(self, today: date) -> None:
        for event in calendario.events_on(today):
            if not event.announce or event.start != today or self._done(event.key):
                continue
            self._mark(event.key)
            await self._announce(
                f"Hoy, {today:%d/%m}, {'es' if event.start == event.end else 'empieza'}: {event.name}. "
                f"{event.context} Anúncialo al servidor en personaje, con emoción, en 2 a 4 frases"
                + (". Con los resultados de la búsqueda (abajo), cuenta qué pasa hoy (partidos, horarios en hora local, equipos) y "
                   "menciona lo más importante sin inventar" if event.search else "")
                + ".",
                f"📅 ¡Hoy {'es' if event.start == event.end else 'empieza'} **{event.name}**! 🌸",
                web=f"{event.name} League of Legends partidos de hoy {today:%d/%m/%Y} horarios" if event.search else "",
            )

    async def _monthly_wrapped(self, today: date) -> None:
        month = wrapped.previous_month(f"{today:%Y-%m}")
        for guild in self.bot.guilds:
            key = f"wrapped:{guild.id}:{month}"
            if self._done(key):
                continue
            self._mark(key)
            channel = self.channel_for(guild)
            if channel is not None:
                try:
                    await wrapped.post_server_wrapped(self.bot, guild, channel, month)
                except Exception:
                    log.exception("No se pudo publicar el Wrapped mensual en %s", guild.name)

    # ---------- Bienvenida ----------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if getattr(self.bot, "members_intent_missing", False) and not self.state.get("aviso_intent"):
            self.state["aviso_intent"] = True
            save_json(STATE_FILE, self.state)
            import avisos

            avisos.notify(
                self.bot, "members_intent",
                "👋 Para que Lillia salude a los miembros nuevos, activa **Server Members Intent** en "
                "https://discord.com/developers/applications → tu aplicación → **Bot** → *Privileged Gateway "
                "Intents*, guarda, y ejecuta `windows\\reiniciar_bot.bat`. (Si no la quieres, pon "
                "`PERSONA_WELCOME=false` en `.env`.)",
                time.time() + 30 * 86400,
            )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not WELCOME or member.bot:
            return
        guild = member.guild
        channel = guild.system_channel if _can_send(guild.system_channel) else self.channel_for(guild)
        if channel is None:
            return
        text = None
        prefix = getattr(self.bot, "command_prefix_text", "!")
        persona = _persona(self.bot)
        if persona:
            text = await persona.ask(
                channel,
                f"(({member.display_name} acaba de entrar por primera vez al servidor '{guild.name}' (somos un "
                "grupo de amigos). Dale la bienvenida en personaje, tímida pero cálida, en 2 o 3 frases. Cuéntale "
                f"en una frase que pones música (con {prefix}play y el nombre de una canción, "
                "o pidiéndotela al mencionarte) y que puede hablar contigo cuando quiera. No escribas su nombre "
                "con @: el sistema ya lo menciona al principio.))",
                40, remember=False,
            )
            text = extract_actions(text)[0] if text else None
        content = f"{member.mention} {text}" if text else WELCOME_FALLBACK.format(mention=member.mention, prefix=prefix)
        try:
            await channel.send(content[:2000], allowed_mentions=discord.AllowedMentions(users=[member]))
            log.info("Bienvenida a %s en %s", member, guild.name)
        except discord.HTTPException as exc:
            log.warning("No se pudo dar la bienvenida en %s: %s", guild.name, exc)

    # ---------- Buenas noches ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not NIGHT_GREETING or message.author.bot or message.guild is None:
            return
        now = datetime.now()
        if not _is_night(now.hour):
            return
        night = (now - timedelta(hours=12)).date().isoformat()  # la madrugada cuenta como "la noche de ayer"
        user = message.author
        if self._night_greeted.get(user.id) == night:
            return
        if time.monotonic() - self._night_channel.get(message.channel.id, -1e9) < NIGHT_CHANNEL_COOLDOWN:
            return
        if self._night_count.get((message.guild.id, night), 0) >= NIGHT_MAX_PER_GUILD:
            return
        persona_cog = self.bot.get_cog("Personaje")
        if persona_cog is not None and await persona_cog.addressed(message):
            self._night_greeted[user.id] = night  # ya le está hablando: ella misma responde medio dormida
            return
        perms = message.channel.permissions_for(message.guild.me)
        if not perms.send_messages:
            return
        self._night_greeted[user.id] = night
        self._night_channel[message.channel.id] = time.monotonic()
        self._night_count[(message.guild.id, night)] = self._night_count.get((message.guild.id, night), 0) + 1

        text = None
        persona = _persona(self.bot)
        if persona:
            said = re.sub(r"\s+", " ", message.clean_content)[:150]
            text = await persona.ask(
                message.channel,
                f"(({user.display_name} sigue sin dormir a las {now:%H:%M} de la madrugada y escribió en el chat: "
                f"\"{said}\". Dale las buenas noches con ternura, medio dormida, y anímale a ir a descansar pronto. "
                "Una o dos frases cortas, sin sermones.))",
                25, remember=False,
            )
            text = extract_actions(text)[0] if text else None
        text = text or random.choice(NIGHT_FALLBACK).format(name=user.display_name)
        try:
            await message.reply(text[:2000], mention_author=False, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            pass

    # ---------- Comandos ----------

    @commands.command(name="eventos", aliases=["calendario"], help="Fechas especiales de los próximos días.")
    async def eventos(self, ctx: commands.Context) -> None:
        upcoming = calendario.upcoming(45)
        if not upcoming:
            await ctx.send("No hay fechas especiales en los próximos días 🌿")
            return
        today = date.today()
        lines = []
        for start, event in upcoming:
            if event.start <= today <= event.end:
                when = "**ahora**" if event.start != event.end else "**hoy**"
            else:
                when = f"{start:%d/%m}"
            span = f" → {event.end:%d/%m}" if event.end != event.start else ""
            lines.append(f"• {when}{span} · {event.name}")
        embed = discord.Embed(title="📅 Próximas fechas especiales", description="\n".join(lines[:20]), color=0xF5A9D0)
        if dd.patch:
            embed.set_footer(text=f"Parche actual de League: {dd.patch}")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Eventos(bot))
