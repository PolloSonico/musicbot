"""Recordatorios: "@Lillia recordame el viernes a las 21 la sesión de D&D".

Se pueden pedir hablando con Lillia (ella entiende la fecha y agrega una orden oculta
[[RECORDATORIO: ...]]) o con el comando:
  !recordatorio [@personas] <cuándo> <qué>    ej: !recordatorio @Lucía viernes 21:00 sesión de D&D
  !recordatorios                              los pendientes tuyos (los que creaste y los que son para ti)
  !borrarrecordatorio <número>                cancela uno

Cuándo: "en 30 minutos", "en 2 horas", "mañana 21:00", "viernes 21hs", "hoy a las 18", "15/10 20:30",
"2026-10-15 20:30". Agregando "todos los días" o "todas las semanas" se repite.

Menciones: al llegar la hora, Lillia escribe en el mismo canal y menciona (pinguea) SOLO a las personas
del recordatorio. Nunca @everyone, @here ni roles, aunque el texto los tenga. Si el canal ya no existe,
les llega por mensaje privado. Los recordatorios se guardan en data/recordatorios.json: si el bot está
apagado a la hora, avisa al volver (marcado como atrasado).

Límites (para que nadie llene el chat): como mucho MAX_PER_AUTHOR pendientes por persona, MAX_TARGETS
personas por recordatorio, hasta un año adelante. Se puede borrar uno si lo creaste tú, si es para
ti (te saca solo a ti) o si puedes administrar mensajes del servidor.
"""

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands, tasks

import persona as persona_mod
from jsonio import load_json, save_json

log = logging.getLogger("recordatorios")

DATA_FILE = Path(__file__).resolve().parent / "data" / "recordatorios.json"
MAX_PER_AUTHOR = 15
MAX_TARGETS = 5
MAX_TEXT = 300
MAX_AHEAD = timedelta(days=366)
LATE_LIMIT = timedelta(days=2)  # atrasados más que esto (bot apagado mucho tiempo) se avisan igual, pero sin pinguear

WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
WEEKDAYS_SHOW = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
REPEATS = {"diario": timedelta(days=1), "semanal": timedelta(weeks=1)}
WEEKLY_DAY_RE = re.compile(r"\btodos los (lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bados|domingos)\b", re.I)
DAILY_RE = re.compile(r"\b(?:todos los d[ií]as|cada d[ií]a|diariamente|a diario)\b", re.I)
WEEKLY_RE = re.compile(r"\b(?:todas las semanas|cada semana|semanalmente)\b", re.I)


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


# ---------- Entender el "cuándo" ----------

UNIT_RE = r"(min(?:uto)?s?|m|h(?:ora)?s?|d[ií]as?|semanas?)"
TIME_RE = r"(?:a\s+las?\s+)?(\d{1,2})(?::(\d{2})|\.(\d{2}))?\s*(?:hs?|horas?)?(?:\s*(de la (?:mañana|manana|tarde|noche)|am|pm))?"


def _apply_time(day: datetime, hour: int, minute: int, part: Optional[str]) -> Optional[datetime]:
    part = _plain(part or "")
    if part in ("pm", "de la tarde", "de la noche") and hour < 12:
        hour += 12
    if part in ("am", "de la manana") and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_when(text: str, now: Optional[datetime] = None) -> Optional[tuple[datetime, str, Optional[str]]]:
    """Lee el "cuándo" del principio del texto. Devuelve (fecha y hora, el resto del texto, repetición)
    o None si no lo entiende. Las horas son las de la PC del bot."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    repeat = None
    m = WEEKLY_DAY_RE.search(text)
    if m:  # "todos los viernes 21hs" = cada semana, el viernes
        day_word = m.group(1)
        day_word = day_word[:-1] if _plain(day_word) in ("sabados", "domingos") else day_word
        text, repeat = text[:m.start()] + day_word + text[m.end():], "semanal"
    for regex, kind in ((DAILY_RE, "diario"), (WEEKLY_RE, "semanal")):
        m = regex.search(text)
        if m:
            text, repeat = text[:m.start()] + text[m.end():], kind
    text = re.sub(r"\s{2,}", " ", text)
    t = text.strip()
    low = _plain(t)

    def rest(match_len: int) -> str:
        return re.sub(r"^[\s,:;.-]+", "", t[match_len:]).strip()

    # "en 30 minutos", "dentro de 2 horas"
    m = re.match(rf"(?:en|dentro de)\s+(\d{{1,4}}|un|una|media)\s*{UNIT_RE}\b", low)
    if m:
        amount = {"un": 1, "una": 1, "media": 0.5}.get(m.group(1)) or int(m.group(1))
        unit = m.group(2)
        delta = (timedelta(minutes=amount) if unit.startswith("m") and not unit.startswith("me") else
                 timedelta(hours=amount) if unit.startswith("h") else
                 timedelta(weeks=amount) if unit.startswith("s") else timedelta(days=amount))
        return now + delta, rest(m.end()), repeat

    day: Optional[datetime] = None
    used = 0
    weekday = False
    m = re.match(r"(pasado manana|manana|hoy)\b", low)
    if m:
        day = now + timedelta(days={"hoy": 0, "manana": 1, "pasado manana": 2}[m.group(1)])
        used = m.end()
    if day is None:
        m = re.match(r"(?:el\s+)?(?:proximo\s+)?(lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b", low)
        if m:
            ahead = (WEEKDAYS.index(m.group(1)) - now.weekday()) % 7
            day, used, weekday = now + timedelta(days=ahead), m.end(), True
    if day is None:
        m = re.match(r"(?:el\s+)?(\d{4})-(\d{1,2})-(\d{1,2})\b", low) or re.match(r"(?:el\s+)?(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", low)
        if m:
            try:
                if "-" in m.group(0):
                    day = now.replace(year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3)))
                else:
                    year = int(m.group(3)) if m.group(3) else now.year
                    year += 2000 if year < 100 else 0
                    day = now.replace(year=year, month=int(m.group(2)), day=int(m.group(1)))
                    if not m.group(3) and day.date() < now.date():
                        day = day.replace(year=now.year + 1)
            except ValueError:
                return None
            used = m.end()

    tail = low[used:]
    tm = re.match(rf"\s*,?\s*{TIME_RE}(?![\d/])", tail)
    has_time = bool(tm) and (used > 0 or re.match(r"\s*(?:a\s+las?\s+|\d{1,2}(?::|\.|\s*hs?\b|\s*horas?\b|\s*(?:am|pm)\b|\s+de la))", tail))
    if has_time:
        when = _apply_time(day or now, int(tm.group(1)), int(tm.group(2) or tm.group(3) or 0), tm.group(4))
        if when is None:
            return None
        used += tm.end()
        if day is None and when <= now:  # "a las 9" ya pasó hoy: mañana
            when += timedelta(days=1)
    elif day is not None:
        when = day.replace(hour=10, minute=0)  # solo el día: a las 10 de la mañana
    else:
        return None
    if weekday and when <= now:  # "el viernes" a una hora que ya pasó hoy: el de la semana que viene
        when += timedelta(weeks=1)
    return when, rest(used), repeat


def show_when(when: datetime, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    hour = f"{when:%H:%M}"
    if when.date() == now.date():
        return f"hoy a las {hour}"
    if when.date() == (now + timedelta(days=1)).date():
        return f"mañana a las {hour}"
    return f"el {WEEKDAYS_SHOW[when.weekday()]} {when:%d/%m} a las {hour}" + (f" de {when.year}" if when.year != now.year else "")


# ---------- Guardado ----------

def load() -> dict:
    data = load_json(DATA_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("siguiente", 1)
    data.setdefault("pendientes", [])
    return data


def save(data: dict) -> None:
    save_json(DATA_FILE, data)


def add(author_id: int, guild_id: Optional[int], channel_id: int, targets: list[int], when: datetime,
        text: str, repeat: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """Guarda un recordatorio. ValueError con el motivo (en español) si no se puede."""
    now = now or datetime.now()
    text = re.sub(r"\s+", " ", text or "").strip()[:MAX_TEXT]
    if not text:
        raise ValueError("¿De qué te tengo que avisar? Me falta el texto del recordatorio.")
    if when <= now:
        raise ValueError("Esa hora ya pasó 🥺")
    if when - now > MAX_AHEAD:
        raise ValueError("Es demasiado lejos: como mucho un año adelante.")
    targets = list(dict.fromkeys(targets))[:MAX_TARGETS] or [author_id]
    data = load()
    mine = [r for r in data["pendientes"] if r["autor"] == author_id]
    if len(mine) >= MAX_PER_AUTHOR:
        raise ValueError(f"Ya tienes {MAX_PER_AUTHOR} recordatorios pendientes. Borra alguno con !borrarrecordatorio.")
    reminder = {"id": data["siguiente"], "autor": author_id, "para": targets, "servidor": guild_id,
                "canal": channel_id, "cuando": when.isoformat(timespec="minutes"), "texto": text,
                "repetir": repeat if repeat in REPEATS else None, "creado": now.isoformat(timespec="minutes")}
    data["siguiente"] += 1
    data["pendientes"].append(reminder)
    save(data)
    return reminder


def pending_for(user_id: int) -> list[dict]:
    return sorted((r for r in load()["pendientes"] if r["autor"] == user_id or user_id in r["para"]),
                  key=lambda r: r["cuando"])


def remove(number: int, user_id: int, is_admin: bool = False) -> str:
    """Borra el recordatorio (o saca a esa persona si solo es destinataria). Devuelve qué pasó."""
    data = load()
    reminder = next((r for r in data["pendientes"] if r["id"] == number), None)
    if reminder is None:
        return "no_existe"
    if reminder["autor"] == user_id or is_admin:
        data["pendientes"].remove(reminder)
        result = "borrado"
    elif user_id in reminder["para"]:
        reminder["para"].remove(user_id)
        if not reminder["para"]:
            data["pendientes"].remove(reminder)
        result = "salido"
    else:
        return "ajeno"
    save(data)
    return result


def take_due(now: Optional[datetime] = None) -> list[dict]:
    """Saca los que ya tocan (y reprograma los que se repiten)."""
    now = now or datetime.now()
    data = load()
    due = [r for r in data["pendientes"] if datetime.fromisoformat(r["cuando"]) <= now]
    if not due:
        return []
    for r in due:
        data["pendientes"].remove(r)
        if r.get("repetir") in REPEATS:
            nxt = dict(r)
            when = datetime.fromisoformat(r["cuando"])
            while when <= now:
                when += REPEATS[r["repetir"]]
            nxt["cuando"] = when.isoformat(timespec="minutes")
            data["pendientes"].append(nxt)
    save(data)
    return due


# ---------- Cog ----------

class Recordatorios(commands.Cog, name="Recordatorios"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()

    # --- Crear (lo usan el comando y la charla) ---

    def resolve_people(self, message: discord.Message, names: str) -> tuple[list[int], list[str]]:
        """Destinatarios: "yo", @menciones del mensaje, o nombres de miembros del servidor.
        Devuelve (ids, nombres que no encontró)."""
        ids: list[int] = []
        missing: list[str] = []
        bot_id = self.bot.user.id if self.bot.user else 0
        mentioned = [m for m in message.mentions if not m.bot and m.id != bot_id]
        for part in [p.strip(" @") for p in re.split(r",| y ", names or "") if p.strip(" @")]:
            low = _plain(part)
            if low in ("yo", "mi", "me", "a mi", "autor"):
                ids.append(message.author.id)
                continue
            member = next((m for m in mentioned if _plain(part) in (_plain(m.display_name), _plain(m.name))), None)
            if member is None and message.guild is not None:
                member = next((m for m in message.guild.members if not m.bot and
                               low in (_plain(m.display_name), _plain(m.name), _plain(getattr(m, "global_name", "") or ""))), None)
            if member is not None:
                ids.append(member.id)
            elif re.fullmatch(r"<@!?\d+>", part):
                continue
            else:
                missing.append(part)
        if not ids and not missing:
            ids = [m.id for m in mentioned] or [message.author.id]
        return ids, missing

    def create(self, message: discord.Message, when: datetime, text: str, targets: list[int],
               repeat: Optional[str]) -> dict:
        return add(message.author.id, message.guild.id if message.guild else None, message.channel.id,
                   targets, when, text, repeat)

    def confirmation(self, reminder: dict, guild: Optional[discord.Guild]) -> str:
        when = datetime.fromisoformat(reminder["cuando"])
        who = ", ".join(self._name(uid, guild) for uid in reminder["para"])
        repeat = {"diario": " (todos los días)", "semanal": " (todas las semanas)"}.get(reminder.get("repetir") or "", "")
        return f"⏰ Recordatorio **#{reminder['id']}** para {who}: {show_when(when)}{repeat} — {reminder['texto']}"

    def _name(self, uid: int, guild: Optional[discord.Guild]) -> str:
        member = guild.get_member(uid) if guild else None
        user = member or self.bot.get_user(uid)
        return f"**{user.display_name}**" if user else f"<@{uid}>"

    async def from_chat(self, message: discord.Message, arg: str) -> None:
        """Orden [[RECORDATORIO: cuándo | para quién | qué | repetir]] que agrega Lillia al responder."""
        parts = [p.strip() for p in arg.split("|")]
        while len(parts) < 4:
            parts.append("")
        when_text, people, text, repeat_text = parts[:4]
        try:
            when = datetime.fromisoformat(when_text.replace("T", " ")[:16])
            repeat = None
        except ValueError:
            parsed = parse_when(when_text)
            if parsed is None:
                await message.channel.send("⏰ No entendí la fecha del recordatorio 🥺 Prueba con "
                                           "`!recordatorio viernes 21:00 sesión de D&D`.", allowed_mentions=persona_mod.NO_MENTIONS)
                return
            when, _, repeat = parsed
        repeat_plain = _plain(repeat_text)
        if "dia" in repeat_plain or "diar" in repeat_plain:
            repeat = "diario"
        elif "seman" in repeat_plain:
            repeat = "semanal"
        targets, missing = self.resolve_people(message, people)
        if missing:
            await message.channel.send(f"⏰ No encontré a {', '.join(missing)} en el servidor: menciónalos con @ y "
                                       "pídemelo de nuevo.", allowed_mentions=persona_mod.NO_MENTIONS)
            return
        try:
            reminder = self.create(message, when, text, targets, repeat)
        except ValueError as exc:
            await message.channel.send(f"⏰ {exc}", allowed_mentions=persona_mod.NO_MENTIONS)
            return
        await message.channel.send(f"-# {self.confirmation(reminder, message.guild)}", allowed_mentions=persona_mod.NO_MENTIONS)

    # --- Comandos ---

    @commands.command(name="recordatorio", aliases=["recordame", "avisame"],
                      help="Te aviso (o a quien menciones) a una hora: !recordatorio [@personas] viernes 21:00 sesión de D&D")
    async def recordatorio(self, ctx: commands.Context, *, texto: str = "") -> None:
        clean = re.sub(r"<@!?\d+>", " ", texto).strip()
        parsed = parse_when(clean) if clean else None
        if parsed is None:
            await ctx.send(f"⏰ Dime cuándo y qué. Por ejemplo: `{ctx.prefix}recordatorio mañana 21:00 sesión de D&D`, "
                           f"`{ctx.prefix}recordatorio @Lucía en 30 minutos sacar la pizza` o "
                           f"`{ctx.prefix}recordatorio todos los viernes 21hs D&D`.")
            return
        when, text, repeat = parsed
        bot_id = self.bot.user.id if self.bot.user else 0
        targets = [m.id for m in ctx.message.mentions if not m.bot and m.id != bot_id] or [ctx.author.id]
        try:
            reminder = self.create(ctx.message, when, text, targets, repeat)
        except ValueError as exc:
            await ctx.send(f"⏰ {exc}")
            return
        await persona_mod.say(self.bot, ctx.channel, f"{ctx.author.display_name} te pidió un recordatorio: "
                              f"{reminder['texto']} ({show_when(when)}).", self.confirmation(reminder, ctx.guild))

    @commands.command(name="recordatorios", aliases=["misrecordatorios"], help="Tus recordatorios pendientes.")
    async def recordatorios(self, ctx: commands.Context) -> None:
        mine = pending_for(ctx.author.id)
        if not mine:
            await ctx.send("⏰ No tienes recordatorios pendientes.")
            return
        lines = []
        for r in mine[:20]:
            when = datetime.fromisoformat(r["cuando"])
            who = ", ".join(self._name(uid, ctx.guild) for uid in r["para"])
            extra = f" · de {self._name(r['autor'], ctx.guild)}" if r["autor"] != ctx.author.id else ""
            repeat = " 🔁" if r.get("repetir") else ""
            lines.append(f"**#{r['id']}** {show_when(when)}{repeat} — {r['texto'][:80]} · para {who}{extra}")
        embed = discord.Embed(title="⏰ Recordatorios pendientes", description="\n".join(lines), color=0xE8A0BF)
        embed.set_footer(text=f"Para cancelar uno: {ctx.prefix}borrarrecordatorio <número>")
        await ctx.send(embed=embed, allowed_mentions=persona_mod.NO_MENTIONS)

    @commands.command(name="borrarrecordatorio", aliases=["cancelarrecordatorio"], help="Cancela un recordatorio por su número.")
    async def borrarrecordatorio(self, ctx: commands.Context, numero: int) -> None:
        perms = getattr(ctx.author, "guild_permissions", None)
        result = remove(numero, ctx.author.id, bool(perms and perms.manage_messages))
        await ctx.send({
            "borrado": f"🗑️ Recordatorio #{numero} cancelado.",
            "salido": f"👋 Listo, ya no te aviso del recordatorio #{numero} (a los demás sí).",
            "no_existe": f"No hay ningún recordatorio #{numero} pendiente.",
            "ajeno": "Ese recordatorio no es tuyo ni es para ti 🥺",
        }[result])

    # --- Avisar ---

    @tasks.loop(seconds=20)
    async def ticker(self) -> None:
        try:
            for reminder in take_due():
                await self._deliver(reminder)
        except Exception:
            log.exception("Error revisando los recordatorios")

    @ticker.before_loop
    async def _ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _deliver(self, reminder: dict) -> None:
        when = datetime.fromisoformat(reminder["cuando"])
        late = datetime.now() - when
        guild = self.bot.get_guild(reminder["servidor"]) if reminder.get("servidor") else None
        channel = self.bot.get_channel(reminder["canal"])
        author = self._name(reminder["autor"], guild)
        targets = reminder["para"]
        mentions = " ".join(f"<@{uid}>" for uid in targets)
        own = targets == [reminder["autor"]]
        note = f" (atrasado: estaba dormida a las {when:%H:%M} del {when:%d/%m})" if late > timedelta(minutes=5) else ""
        text = f"⏰ {mentions} " + ("¡Recordatorio!" if own else f"Recordatorio de {author}:") + f" **{reminder['texto']}**{note}"
        # Solo se pinguea a los destinatarios; nunca @everyone, @here ni roles.
        allowed = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(uid) for uid in targets],
                                          replied_user=False)
        if late > LATE_LIMIT:
            allowed = persona_mod.NO_MENTIONS
        sent = None
        if channel is not None:
            try:
                sent = await channel.send(text[:2000], allowed_mentions=allowed)
            except discord.HTTPException as exc:
                log.warning("No se pudo mandar el recordatorio #%s al canal: %s", reminder["id"], exc)
        if sent is None:  # el canal ya no existe o no hay permiso: por privado
            for uid in targets:
                try:
                    user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                    await user.send(text.replace(mentions, "").strip()[:2000])
                except discord.HTTPException:
                    pass
            return
        persona = persona_mod._persona(self.bot)
        if persona is None or not persona_mod.MUSIC_COMMENTS:
            return

        async def add_line() -> None:
            who = ", ".join(self._name(uid, guild).strip("*") for uid in targets)
            line = await persona.comment(channel, f"Llegó la hora de un recordatorio para {who}: "
                                                  f"\"{reminder['texto']}\". Avísales con cariño (no repitas la hora).")
            if line:
                try:
                    await sent.edit(content=f"{text}\n{line}"[:2000], allowed_mentions=persona_mod.NO_MENTIONS)
                except discord.HTTPException:
                    pass

        persona_mod._spawn(add_line())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Recordatorios(bot))
