"""League of Legends con la API de Riot: cuentas vinculadas, perfil, partida en vivo, historial y
resumen automático al terminar cada partida.

Comandos:
  !vincular Nombre#TAG [servidor]   vincula TU cuenta (servidor por defecto: RIOT_PLATFORM, LAS)
  !desvincular                      la desvincula
  !perfil [@persona | Nombre#TAG]   nivel, rango Solo/Dúo y Flex, campeones con más maestría
  !partida [@persona | Nombre#TAG]  la partida en vivo: campeones, jugadores y rangos de los dos equipos
  !historial [@persona | Nombre#TAG] las últimas 5 partidas

Resumen post-partida (RIOT_POST_GAME): cada pocos minutos mira si las cuentas vinculadas
terminaron una partida nueva y la publica en el canal de eventos con un comentario de Lillia. Si
varios amigos jugaron juntos, sale un solo mensaje para todos.
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks

import avisos
import calendario
import lcu as lcu_mod
import riot_cuentas as cuentas
import wrapped
from datadragon import dd
from jsonio import save_json
from riot_api import (DEFAULT_PLATFORM, PLATFORM_NAMES, POSITIONS, QUEUES, NotFound, RiotError, client, enabled,
                      platform_of, tier_text)
from persona import NO_MENTIONS, _persona, extract_actions

log = logging.getLogger("riot")

POST_GAME = os.getenv("RIOT_POST_GAME", "true").strip().lower() not in ("0", "false", "no")
# false (por defecto) = al terminar una partida solo sale el comentario de Lillia; true = también el cuadro con
# el resultado (KDA, súbditos, daño...).
POST_GAME_DETAILS = os.getenv("RIOT_POST_GAME_DETAILS", "false").strip().lower() in ("1", "true", "si", "sí", "yes")
POLL_MINUTES = max(2, int(os.getenv("RIOT_POLL_MINUTES", "4") or 4))
CACHE_HOURS = 6  # cada cuánto se refresca el rango/mains guardados para la charla
MAX_GAME_AGE = 3 * 3600  # no se anuncian partidas que terminaron hace más de esto (bot apagado, etc.)
MIN_GAME_SECONDS = 300  # menos que esto es un remake: no se anuncia
SKIP_QUEUES = {0}  # personalizadas / herramienta de práctica
COLOR = 0x0AC8B9
NETWORK_ERRORS = (aiohttp.ClientConnectionError, asyncio.TimeoutError)
MENTION_RE = re.compile(r"^\s*<@!?(\d+)>\s*")
RIOT_ID_RE = re.compile(r"^\s*(.{3,16}?)\s*#\s*([A-Za-z0-9]{2,5})\s*(?:\s+([A-Za-z]{2,4}\d?))?\s*$")


def _ago(timestamp_ms: Optional[int]) -> str:
    if not timestamp_ms:
        return ""
    minutes = int((time.time() - timestamp_ms / 1000) // 60)
    if minutes < 60:
        return f"hace {max(minutes, 1)} min"
    if minutes < 48 * 60:
        return f"hace {minutes // 60} h"
    return f"hace {minutes // 1440} días"


def _duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


# Si Riot agrega una cola nueva (otro número), al menos se reconoce el modo de juego.
MODES = {"CHERRY": "Arena", "ARAM": "ARAM", "URF": "URF", "ARURF": "URF", "ONEFORALL": "Uno para todos",
         "NEXUSBLITZ": "Nexus Blitz", "ULTBOOK": "Libro de hechizos definitivo", "STRAWBERRY": "Enjambre",
         "CLASSIC": "Grieta del Invocador"}
MAYHEM_NOTE = (
    "Sobre ARAM: Caos (ARAM Mayhem): Riot no comparte esas partidas por su API; solo se conocen las que se "
    "jugaron desde la PC del bot con el cliente de League abierto (y las de los amigos que estaban en esas "
    "partidas). Si preguntan por una que no está acá, explícalo con dulzura y pide que te cuenten cómo les fue."
)
CHAT_CACHE_SECONDS = 180
LIVE_CACHE_SECONDS = 60  # "¿alguien está jugando?": cada cuánto se vuelve a preguntar a Riot por la misma cuenta
MAX_LIVE_CHECKS = 12
LCU_PLAYING = {"ChampSelect": "en la selección de campeones", "GameStart": "cargando la partida",
               "InProgress": "en partida", "Reconnect": "reconectándose a una partida"}
CLASH_EVERY = 6 * 3600  # cada cuánto se consultan los torneos de Clash
CLASH_REMINDER_DAYS = 2  # cuántos días antes se avisa
CLASH_WEEK_DAYS = 7  # además, un aviso una semana antes (para ir armando equipo)


def _queue(game) -> str:
    """Nombre de la cola a partir del número o del 'info' de la partida (usa también el modo)."""
    if isinstance(game, dict):
        qid = game.get("queueId", game.get("gameQueueConfigId", 0))
        return QUEUES.get(qid) or MODES.get(game.get("gameMode", ""), "Partida")
    return QUEUES.get(game, "Partida")


def _is_arena(info: dict) -> bool:
    return info.get("gameMode") == "CHERRY" or info.get("queueId") in (1700, 1710)


def _result(p: dict, info: dict) -> tuple[str, str]:
    """(emoji, texto) del resultado. En Arena cuenta el puesto, no ganar/perder."""
    if _is_arena(info):
        place = p.get("placement") or p.get("subteamPlacement") or 0
        emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(place, "✅" if place and place <= 4 else "❌")
        return emoji, f"puesto {place}" if place else "Arena"
    return ("✅", "victoria") if p.get("win") else ("❌", "derrota")


def _stats(p: dict, info: dict) -> str:
    """'5/2/7 · 190 CS' (o '12/8/9 · puesto 5' en Arena, donde no hay súbditos)."""
    if _is_arena(info):
        return f"{_kda(p)} · {_result(p, info)[1]}"
    return f"{_kda(p)} · {_cs(p)} CS"


def _kda(p: dict) -> str:
    return f"{p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)}"


def _cs(p: dict) -> int:
    return p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)


class Riot(commands.Cog, name="League"):
    """Tu cuenta de League: perfil, partida en vivo, historial y resúmenes."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._chat_cache: dict[int, tuple[float, str]] = {}
        self._live_cache: dict[str, tuple[float, Optional[dict]]] = {}  # puuid -> (cuándo, partida en vivo o None)
        self._clash_checked = 0.0
        client.on_key_error = lambda text: avisos.notify(
            bot, "riot_key", f"🔑 {text} Revisa `RIOT_API_KEY` en `.env` y reinicia el bot.", time.time() + 6 * 3600
        )
        if enabled() and POST_GAME:
            self.poller.change_interval(minutes=POLL_MINUTES)
            self.poller.start()
        elif not enabled():
            log.warning("Riot API desactivada: falta RIOT_API_KEY en .env")

    async def cog_unload(self) -> None:
        self.poller.cancel()
        await client.close()
        await lcu_mod.lcu.close()

    async def cog_check(self, ctx: commands.Context) -> bool:
        if not enabled():
            raise commands.CheckFailure("La conexión con League no está configurada (falta RIOT_API_KEY en .env).")
        return True

    # ---------- Utilidades ----------

    async def _ensure_dd(self) -> None:
        if dd.version is None or not dd.champions:
            try:
                await asyncio.wait_for(dd.refresh(force=dd.version is None), 10)
            except Exception:
                pass

    async def _target(self, ctx: commands.Context, text: str) -> tuple[dict, str]:
        """La cuenta a consultar: @mención (vinculada), Nombre#TAG [servidor], o la propia."""
        text = re.sub(r"<@!?\d+>", "", text or "").strip()
        if ctx.message.mentions:
            member = ctx.message.mentions[0]
            account = cuentas.get(member.id)
            if not account:
                raise commands.CheckFailure(f"{member.display_name} no vinculó su cuenta todavía (`!vincular Nombre#TAG`).")
            return account, member.display_name
        if text:
            match = RIOT_ID_RE.match(text)
            if not match:
                raise commands.CheckFailure("Escribe el Riot ID así: `Nombre#TAG` (y opcional el servidor: `las`, `lan`, `br`...).")
            platform = platform_of(match.group(3)) if match.group(3) else DEFAULT_PLATFORM
            if platform is None:
                raise commands.CheckFailure(f"No conozco el servidor `{match.group(3)}`. Usa `las`, `lan`, `br`, `na`, `euw`...")
            try:
                acc = await client.account_by_riot_id(match.group(1), match.group(2), platform)
            except NotFound:
                raise commands.CheckFailure(f"No encontré la cuenta **{match.group(1)}#{match.group(2)}** 😳")
            account = {"puuid": acc["puuid"], "nombre": acc.get("gameName", match.group(1)),
                       "tag": acc.get("tagLine", match.group(2)), "plataforma": platform,
                       "servidor_lol": PLATFORM_NAMES.get(platform, platform.upper())}
            return account, cuentas.riot_id(account)
        account = cuentas.get(ctx.author.id)
        if not account:
            raise commands.CheckFailure("Primero vincula tu cuenta: `!vincular Nombre#TAG` (por ejemplo `!vincular Lillia#LAS`).")
        return account, ctx.author.display_name

    async def _comment(self, channel, situation: str, timeout: float = 30) -> Optional[str]:
        persona = _persona(self.bot)
        if not persona:
            return None
        text = await persona.ask(channel, f"(({situation}))", timeout, remember=False)
        return extract_actions(text)[0] if text else None

    async def _send_with_comment(self, channel, embed: discord.Embed, situation: str) -> None:
        """Manda el embed al instante y le suma el comentario de Lillia cuando llega."""
        message = await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        line = await self._comment(channel, situation)
        if line:
            try:
                await message.edit(content=line[:2000])
            except discord.HTTPException:
                pass

    async def refresh_cache(self, user_id: int, account: dict) -> dict:
        """Actualiza rango y mains guardados (lo que Lillia sabe en la charla)."""
        await self._ensure_dd()
        platform = account["plataforma"]
        ranks = await client.ranks(account["puuid"], platform)
        masteries = await client.top_masteries(account["puuid"], platform, 3)
        solo, flex = ranks.get("RANKED_SOLO_5x5"), ranks.get("RANKED_FLEX_SR")
        texto = f"Solo/Dúo {tier_text(solo)}" + (f", Flex {tier_text(flex)}" if flex else "")
        mains = [dd.champion_name(m["championId"]) for m in masteries]
        cuentas.update(user_id, rango_texto=texto, mains=mains, actualizado=time.time())
        account.update(rango_texto=texto, mains=mains, actualizado=time.time())
        return {"ranks": ranks, "masteries": masteries}

    async def recent_matches(self, account: dict, count: int = 5) -> list[dict]:
        """Últimas partidas de la cuenta: las de la API de Riot más las de ARAM: Caos leídas del
        cliente local, ordenadas de la más nueva a la más vieja."""
        ids = await client.match_ids(account["puuid"], account["plataforma"], count=count)
        fetched = await asyncio.gather(*(client.match(mid) for mid in ids), return_exceptions=True)
        matches = [m for m in fetched if not isinstance(m, Exception)]
        matches += lcu_mod.stored_matches_for(account["puuid"])[:count]
        matches.sort(key=lambda m: m.get("info", {}).get("gameEndTimestamp", 0), reverse=True)
        return matches[:count]

    async def _live(self, account: dict) -> Optional[dict]:
        cached = self._live_cache.get(account["puuid"])
        if cached and time.time() - cached[0] < LIVE_CACHE_SECONDS:
            return cached[1]
        game = await client.live_game(account["puuid"], account["plataforma"])
        self._live_cache[account["puuid"]] = (time.time(), game)
        return game

    async def _local_client_state(self) -> tuple[Optional[str], str, str]:
        """(Riot ID con sesión en el cliente de esta PC, en qué está, cola). Sirve para ARAM: Caos, que
        Riot no muestra por su API pública."""
        if not lcu_mod.ENABLED:
            return None, "", ""
        lcu = lcu_mod.lcu
        phase = await lcu.get("/lol-gameflow/v1/gameflow-phase")
        if phase not in LCU_PLAYING:
            return None, "", ""
        riot_id = await lcu.whoami()
        session = await lcu.get("/lol-gameflow/v1/session") or {}
        queue = ((session.get("gameData") or {}).get("queue") or {})
        return riot_id, LCU_PLAYING[phase], QUEUES.get(queue.get("id")) or queue.get("description") or ""

    async def live_context(self, guild: Optional[discord.Guild], people: list, everyone: bool) -> str:
        """Quién está jugando AHORA, para la charla ("¿alguien está en partida?").
        everyone=True: todas las cuentas vinculadas del servidor; si no, las de `people`."""
        if not enabled():
            return ""
        accounts = cuentas.load()
        if everyone:
            ids = [int(uid) for uid in accounts
                   if guild is None or guild.get_member(int(uid)) is not None or accounts[uid].get("servidor") == guild.id]
        else:
            ids = [p.id for p in people if str(p.id) in accounts]
        not_linked = [p.display_name for p in people if str(p.id) not in accounts] if not everyone else []
        ids = ids[:MAX_LIVE_CHECKS]
        if not ids:
            return ("Nadie de los que preguntan tiene la cuenta de League vinculada (!vincular), así que NO sabes si "
                    "están jugando: dilo así, sin inventar.") if not_linked or not everyone else \
                   "Nadie del servidor tiene la cuenta de League vinculada (!vincular): no sabes quién está jugando."
        await self._ensure_dd()
        games = await asyncio.gather(*(self._live(accounts[str(uid)]) for uid in ids), return_exceptions=True)
        try:
            local_id, local_state, local_queue = await self._local_client_state()
        except Exception:
            local_id, local_state, local_queue = None, "", ""

        def name_of(uid: int) -> str:
            member = guild.get_member(uid) if guild else None
            user = member or self.bot.get_user(uid)
            return user.display_name if user else accounts[str(uid)].get("discord", "?")

        by_puuid = {accounts[str(uid)]["puuid"]: uid for uid in ids}
        lines = []
        for uid, game in zip(ids, games):
            account = accounts[str(uid)]
            who = f"{name_of(uid)} ({cuentas.riot_id(account)})"
            if isinstance(game, Exception):
                lines.append(f"- {who}: no se pudo consultar ahora")
                continue
            if game:
                participants = game.get("participants", [])
                me = next((p for p in participants if p.get("puuid") == account["puuid"]), {})
                length = game.get("gameLength", 0) or (int(time.time() - game["gameStartTime"] / 1000)
                                                       if game.get("gameStartTime") else 0)
                friends = [name_of(by_puuid[p["puuid"]]) for p in participants
                           if p.get("puuid") in by_puuid and p.get("puuid") != account["puuid"]]
                lines.append(f"- {who}: EN PARTIDA de {_queue(game)} con {dd.champion_name(me.get('championId', 0))}, "
                             f"{max(length, 0) // 60} min de juego" + (f", junto con {', '.join(friends)}" if friends else ""))
                continue
            local = local_id and lcu_mod._norm(local_id) == lcu_mod._norm(cuentas.riot_id(account))
            if local:
                lines.append(f"- {who}: {local_state.upper()}" + (f" de {local_queue}" if local_queue else "")
                             + " (visto en el cliente de League de la PC del bot)")
            else:
                lines.append(f"- {who}: no está en partida")
        text = ("Quién está jugando League AHORA MISMO (consultado recién a Riot):\n" + "\n".join(lines)
                + "\nSolo se ven las cuentas vinculadas con !vincular" + (f" ({', '.join(not_linked)} no la tiene)" if not_linked else "")
                + "; de los demás no sabes nada. Las partidas de ARAM: Caos solo se ven si se juegan desde la PC del bot. "
                "Para ver los equipos completos de alguien: !partida @persona. Responde con estos datos, sin inventar.")
        return text

    async def chat_context(self, user: discord.abc.User) -> str:
        """Datos reales y frescos de Riot para la charla ("¿cómo me fue en la última?"): las últimas
        5 partidas de la cuenta vinculada. Se guardan 3 minutos para no repetir pedidos."""
        account = cuentas.get(user.id)
        if not account or not enabled():
            return ""
        cached = self._chat_cache.get(user.id)
        if cached and time.time() - cached[0] < CHAT_CACHE_SECONDS:
            return cached[1]
        await self._ensure_dd()
        matches = await self.recent_matches(account, 5)
        lines = []
        for i, match in enumerate(matches):
            info = match.get("info", {})
            p = next((x for x in info.get("participants", []) if x.get("puuid") == account["puuid"]), None)
            if not p:
                continue
            position = "" if _is_arena(info) else POSITIONS.get(p.get("teamPosition", ""), "")
            lines.append(
                f"{i + 1}) {_ago(info.get('gameEndTimestamp'))}, {_queue(info)}"
                + (" (leída del cliente de la PC del bot)" if info.get("desdeCliente") else "") + ", "
                f"{dd.champion_name(p.get('championName', '?'))}{' de ' + position if position else ''}: "
                f"{_kda(p)} (asesinatos/muertes/asistencias), "
                + (_result(p, info)[1] if _is_arena(info) else f"{_cs(p)} súbditos, {_result(p, info)[1]}")
                + f", {p.get('totalDamageDealtToChampions', 0) / 1000:.1f}k de daño a campeones"
            )
        text = (
            f"Datos REALES de Riot sobre {user.display_name} ({cuentas.riot_id(account)}). "
            + (f"Rango: {account['rango_texto']}. " if account.get("rango_texto") else "")
            + ("Sus últimas partidas, de la más reciente a la más vieja:\n" + "\n".join(lines) if lines
               else "No se encontraron partidas recientes.")
            + f"\n{MAYHEM_NOTE} Usa estos datos para responder con precisión (sin inventar nada que no esté acá)."
        )
        self._chat_cache[user.id] = (time.time(), text)
        return text

    # ---------- Comandos ----------

    async def _other_member(self, ctx: commands.Context, text: str) -> tuple[Optional[discord.abc.User], str, bool]:
        """Si el texto empieza con @alguien: (esa persona, el resto, True). Solo el dueño del bot y quien
        puede administrar el servidor pueden vincular o desvincular cuentas de otros.
        Devuelve (None, ..., True) si no tiene permiso (ya avisó)."""
        m = MENTION_RE.match(text or "")
        if not m:
            return ctx.author, text, False
        uid = int(m.group(1))
        rest = text[m.end():].strip()
        if uid == ctx.author.id:
            return ctx.author, rest, False
        perms = getattr(ctx.author, "guild_permissions", None)
        if not (await self.bot.is_owner(ctx.author) or (perms and perms.manage_guild)):
            await ctx.send("Solo el dueño del bot o quien administra el servidor puede vincular o desvincular la cuenta "
                           "de otra persona 🌸 Pídele que lo haga con `!vincular Nombre#TAG`.")
            return None, rest, True
        member = next((u for u in ctx.message.mentions if u.id == uid), None)
        if member is None and ctx.guild:
            member = ctx.guild.get_member(uid)
        if member is None:
            try:
                member = await self.bot.fetch_user(uid)
            except discord.HTTPException:
                await ctx.send("No encontré a esa persona 🥺")
                return None, rest, True
        if member.bot:
            await ctx.send("Los bots no juegan League... creo 😳")
            return None, rest, True
        return member, rest, True

    @commands.command(name="vincular", aliases=["link"],
                      help="Vincula tu cuenta de League. Uso: vincular Nombre#TAG [las/lan/br/...]. "
                           "El dueño o los admins: vincular @amigo Nombre#TAG")
    async def vincular(self, ctx: commands.Context, *, riot_id: str) -> None:
        target, riot_id, for_other = await self._other_member(ctx, riot_id)
        if target is None:
            return
        match = RIOT_ID_RE.match(riot_id)
        if not match:
            await ctx.send("Escribe el Riot ID así: `!vincular Nombre#TAG` (si no es de LAS, agrega el servidor: "
                           "`!vincular Nombre#TAG lan`). Para otra persona: `!vincular @amigo Nombre#TAG`.")
            return
        platform = platform_of(match.group(3)) if match.group(3) else DEFAULT_PLATFORM
        if platform is None:
            await ctx.send(f"No conozco el servidor `{match.group(3)}`. Usa `las`, `lan`, `br`, `na`, `euw`...")
            return
        async with ctx.typing():
            try:
                acc = await client.account_by_riot_id(match.group(1), match.group(2), platform)
                await client.summoner(acc["puuid"], platform)  # comprueba que juega LoL en ese servidor
            except NotFound:
                await ctx.send(f"No encontré **{match.group(1)}#{match.group(2)}** en {PLATFORM_NAMES.get(platform, platform)} 😳 ¿Está bien escrito?")
                return
            account = {
                "puuid": acc["puuid"], "nombre": acc.get("gameName", match.group(1)), "tag": acc.get("tagLine", match.group(2)),
                "plataforma": platform, "servidor_lol": PLATFORM_NAMES.get(platform, platform.upper()),
                "discord": target.name, "servidor": ctx.guild.id if ctx.guild else None,
                "vinculado": datetime.now().date().isoformat(),
            }
            try:  # la última partida actual: así el resumen automático empieza con la próxima
                ids = await client.match_ids(acc["puuid"], platform, count=1)
                account["ultima_partida"] = ids[0] if ids else None
            except RiotError:
                account["ultima_partida"] = None
            if for_other:
                account["vinculado_por"] = ctx.author.id
            cuentas.put(target.id, account)
            self._chat_cache.pop(target.id, None)  # que Lillia vea la cuenta nueva en la charla
            try:
                await self.refresh_cache(target.id, account)
            except RiotError:
                pass
        extra = f" · {account['rango_texto']}" if account.get("rango_texto") else ""
        if for_other:
            await ctx.send(
                f"🌸 ¡Listo! Vinculé **{cuentas.riot_id(account)}** ({account['servidor_lol']}){extra} a "
                f"**{target.display_name}**.\n-# Cuando termine una partida le haré un resumen. "
                f"`!desvincular @{target.display_name}` para quitarla.", allowed_mentions=NO_MENTIONS)
            return
        await ctx.send(
            f"🌸 ¡Listo, {ctx.author.display_name}! Vinculé **{cuentas.riot_id(account)}** ({account['servidor_lol']}){extra}.\n"
            "-# Cuando termines una partida te haré un resumen. `!desvincular` para dejar de hacerlo."
        )

    @commands.command(name="desvincular", aliases=["unlink"],
                      help="Desvincula tu cuenta de League. El dueño o los admins: desvincular @amigo")
    async def desvincular(self, ctx: commands.Context, *, quien: str = "") -> None:
        target, _, for_other = await self._other_member(ctx, quien)
        if target is None:
            return
        removed = cuentas.remove(target.id)
        if for_other:
            await ctx.send(f"🍃 Listo, desvinculé la cuenta de League de **{target.display_name}**." if removed
                           else f"**{target.display_name}** no tenía ninguna cuenta vinculada 🌸", allowed_mentions=NO_MENTIONS)
        elif removed:
            await ctx.send("🍃 Listo, desvinculé tu cuenta de League.")
        else:
            await ctx.send("No tenías ninguna cuenta vinculada 🌸")

    @commands.command(name="perfil", aliases=["rango", "lol"], help="Perfil de League. Uso: perfil [@persona | Nombre#TAG]")
    async def perfil(self, ctx: commands.Context, *, quien: str = "") -> None:
        async with ctx.typing():
            account, who = await self._target(ctx, quien)
            await self._ensure_dd()
            platform = account["plataforma"]
            summoner = await client.summoner(account["puuid"], platform)
            ranks = await client.ranks(account["puuid"], platform)
            masteries = await client.top_masteries(account["puuid"], platform, 3)
        linked_id = ctx.message.mentions[0].id if ctx.message.mentions else (ctx.author.id if not quien.strip() else None)
        if linked_id:
            solo, flex = ranks.get("RANKED_SOLO_5x5"), ranks.get("RANKED_FLEX_SR")
            cuentas.update(linked_id, rango_texto=f"Solo/Dúo {tier_text(solo)}" + (f", Flex {tier_text(flex)}" if flex else ""),
                           mains=[dd.champion_name(m["championId"]) for m in masteries], actualizado=time.time())

        embed = discord.Embed(title=f"{cuentas.riot_id(account)} · {account.get('servidor_lol', '')}",
                              description=f"Nivel **{summoner.get('summonerLevel', '?')}**", color=COLOR)
        icon = dd.profile_icon_url(summoner.get("profileIconId"))
        if icon:
            embed.set_thumbnail(url=icon)
        for queue, label in (("RANKED_SOLO_5x5", "🏆 Solo/Dúo"), ("RANKED_FLEX_SR", "👥 Flexible")):
            entry = ranks.get(queue)
            if entry:
                wins, losses = entry.get("wins", 0), entry.get("losses", 0)
                rate = round(100 * wins / (wins + losses)) if wins + losses else 0
                embed.add_field(name=label, value=f"**{tier_text(entry)}**\n{wins}V {losses}D · {rate}%")
            else:
                embed.add_field(name=label, value="Sin clasificar")
        if masteries:
            lines = [f"**{dd.champion_name(m['championId'])}** · nivel {m.get('championLevel', '?')} · "
                     f"{m.get('championPoints', 0):,} pts".replace(",", ".") for m in masteries]
            embed.add_field(name="💖 Más jugados", value="\n".join(lines), inline=False)
        mains = ", ".join(dd.champion_name(m["championId"]) for m in masteries) or "ninguno"
        await self._send_with_comment(
            ctx.channel, embed,
            f"{ctx.author.display_name} te pidió ver el perfil de League de {who}: "
            f"Solo/Dúo {tier_text(ranks.get('RANKED_SOLO_5x5'))}, campeones con más maestría: {mains}. "
            "Coméntalo en personaje en 1 o 2 frases (algo tierno o gracioso sobre sus mains o su rango).",
        )

    @commands.command(name="partida", aliases=["envivo", "live"], help="La partida en vivo. Uso: partida [@persona | Nombre#TAG]")
    async def partida(self, ctx: commands.Context, *, quien: str = "") -> None:
        async with ctx.typing():
            account, who = await self._target(ctx, quien)
            await self._ensure_dd()
            game = await client.live_game(account["puuid"], account["plataforma"])
            if game is None:
                await ctx.send(f"💤 {who} no está jugando ahora mismo.")
                return
            participants = game.get("participants", [])
            linked = {acc["puuid"] for acc in cuentas.load().values()}

            async def solo_rank(p: dict) -> str:
                try:
                    ranks = await client.ranks(p["puuid"], account["plataforma"])
                    return tier_text(ranks.get("RANKED_SOLO_5x5")).replace(" (", " · ").replace(")", "")
                except (RiotError, KeyError):
                    return "?"

            ranks = await asyncio.gather(*(solo_rank(p) for p in participants))

        teams: dict[int, list[str]] = {100: [], 200: []}
        for p, rank in zip(participants, ranks):
            star = "⭐ " if p.get("puuid") in linked else ""
            name = p.get("riotId") or p.get("summonerName") or "?"
            teams.setdefault(p.get("teamId", 100), []).append(f"{star}**{dd.champion_name(p.get('championId', 0))}** — {name} · {rank}")
        length = game.get("gameLength", 0)
        if not length and game.get("gameStartTime"):
            length = int(time.time() - game["gameStartTime"] / 1000)
        embed = discord.Embed(
            title=f"🎮 {who} está en partida",
            description=f"{_queue(game)} · {_duration(max(length, 0))} de juego",
            color=COLOR,
        )
        embed.add_field(name="🔵 Equipo azul", value="\n".join(teams.get(100, [])) or "-", inline=False)
        embed.add_field(name="🔴 Equipo rojo", value="\n".join(teams.get(200, [])) or "-", inline=False)
        embed.set_footer(text="⭐ = cuenta vinculada en este servidor")
        blue = ", ".join(dd.champion_name(p.get("championId", 0)) for p in participants if p.get("teamId") == 100)
        red = ", ".join(dd.champion_name(p.get("championId", 0)) for p in participants if p.get("teamId") == 200)
        me = next((p for p in participants if p.get("puuid") == account["puuid"]), {})
        side = "azul" if me.get("teamId") == 100 else "rojo"
        await self._send_with_comment(
            ctx.channel, embed,
            f"{who} está jugando una partida de League ({_queue(game)}) con "
            f"{dd.champion_name(me.get('championId', 0))}, en el equipo {side}. Equipo azul: {blue}. Equipo rojo: {red}. "
            "Opina en personaje en 2 frases sobre las composiciones (qué equipo te parece más fuerte o qué "
            "campeón da miedo) y dale ánimo.",
        )

    @commands.command(name="historial", aliases=["partidas"], help="Las últimas 5 partidas. Uso: historial [@persona | Nombre#TAG]")
    async def historial(self, ctx: commands.Context, *, quien: str = "") -> None:
        async with ctx.typing():
            account, who = await self._target(ctx, quien)
            await self._ensure_dd()
            matches = await self.recent_matches(account, 5)
        lines, wins, played = [], 0, 0
        for match in matches:
            info = match.get("info", {})
            p = next((x for x in info.get("participants", []) if x.get("puuid") == account["puuid"]), None)
            if not p:
                continue
            played += 1
            emoji, _text = _result(p, info)
            wins += emoji in ("✅", "🥇", "🥈", "🥉")
            position = "" if _is_arena(info) else POSITIONS.get(p.get("teamPosition", ""), "")
            lines.append(
                f"{emoji} **{dd.champion_name(p.get('championName', '?'))}**"
                + (f" ({position})" if position else "")
                + f" · {_stats(p, info)} · {_queue(info)} · {_ago(info.get('gameEndTimestamp'))}"
            )
        if not lines:
            await ctx.send(f"No encontré partidas recientes de {who} 💤")
            return
        embed = discord.Embed(title=f"📜 Últimas partidas de {who}", description="\n".join(lines), color=COLOR)
        embed.set_footer(text=f"{wins} victorias (o top 4 en Arena) de {played} · ARAM: Caos solo si se jugó desde la PC del bot")
        await self._send_with_comment(
            ctx.channel, embed,
            f"{ctx.author.display_name} te pidió ver las últimas partidas de {who}: le fue bien en {wins} de {played}. "
            f"Detalle: {' | '.join(re.sub(r'[*✅❌🥇🥈🥉]', '', l) for l in lines)}. Coméntalo en personaje en 1 o 2 "
            "frases (si viene en racha, felicítalo; si viene mal, consuélalo con ternura).",
        )

    # ---------- Resumen automático post-partida ----------

    @tasks.loop(minutes=4)
    async def poller(self) -> None:
        async def run(what: str, step) -> None:
            try:
                await step()
            except NETWORK_ERRORS as exc:  # sin internet un rato (la PC despertando, el router...): se reintenta solo
                log.warning("%s: sin conexión (%s)", what, exc or type(exc).__name__)
            except Exception:
                log.exception("Fallo %s", what.lower())

        async def new_games() -> None:
            await self._ensure_dd()
            await self._check_new_games()

        await run("Revisando partidas nuevas", new_games)
        await run("Revisando los torneos de Clash", self._check_clash)
        if lcu_mod.ENABLED:
            await run("Leyendo las partidas de ARAM: Caos del cliente", self._check_lcu)

    @poller.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_new_games(self) -> None:
        data = cuentas.load()
        new_games: dict[str, list[tuple[int, dict]]] = {}  # partida -> [(id de Discord, cuenta)]
        for uid, account in data.items():
            try:
                ids = await client.match_ids(account["puuid"], account["plataforma"], count=5)
            except NotFound:
                continue
            except RiotError as exc:
                log.warning("No se pudo revisar las partidas de %s: %s", cuentas.riot_id(account), exc)
                if exc.status in (401, 403):
                    return
                continue
            if not ids:
                continue
            last = account.get("ultima_partida")
            fresh = ids[: ids.index(last)] if last in ids else ids[:1] if last else []
            if ids[0] != last:
                cuentas.update(int(uid), ultima_partida=ids[0])
            for match_id in fresh:
                new_games.setdefault(match_id, []).append((int(uid), account))
            # Refresca de vez en cuando el rango y los mains guardados.
            if time.time() - account.get("actualizado", 0) > CACHE_HOURS * 3600:
                try:
                    await self.refresh_cache(int(uid), account)
                except RiotError:
                    pass
        for match_id, players in new_games.items():
            try:
                await self._announce_game(match_id, players)
            except Exception:
                log.exception("No se pudo anunciar la partida %s", match_id)

    # ---------- Clash ----------

    async def _check_clash(self) -> None:
        """Pone los torneos de Clash en el calendario (data/eventos_clash.json): Lillia los anuncia
        una semana antes, CLASH_REMINDER_DAYS días antes y el mismo día, y los tiene presentes en la charla."""
        if time.time() - self._clash_checked < CLASH_EVERY:
            return
        self._clash_checked = time.time()
        tournaments = await client.clash_tournaments()
        events, dates = [], 0
        for t in tournaments:
            name = (t.get("nameKey") or "Clash").replace("_", " ").title()
            day = (t.get("nameKeySecondary") or "").replace("day_", "día ").replace("_", " ")
            label = f"Clash: copa de {name}" + (f" ({day})" if day else "")
            for phase in t.get("schedule", []):
                if phase.get("cancelled") or not phase.get("startTime"):
                    continue
                start = datetime.fromtimestamp(phase["startTime"] / 1000)
                registration = datetime.fromtimestamp(phase.get("registrationTime", phase["startTime"]) / 1000)
                if start.date() < datetime.now().date():
                    continue
                when = (f"el {start:%d/%m}: las inscripciones abren a las {registration:%H:%M} y el torneo empieza "
                        f"a las {start:%H:%M} (hora local)")
                week_before = (start - timedelta(days=CLASH_WEEK_DAYS)).date()
                if week_before >= datetime.now().date():  # si Riot lo publicó tarde, este aviso no va
                    events.append({
                        "nombre": f"Falta una semana para el {label}", "desde": week_before.isoformat(),
                        "contexto": f"En una semana hay torneo de Clash ({label}) {when}. Avisa con tiempo para que tus "
                                    "amigos vayan armando un equipo de 5.",
                        "anunciar": True, "buscar": False, "auto": True,
                    })
                dates += 1
                events.append({
                    "nombre": f"Se viene el {label}", "desde": (start - timedelta(days=CLASH_REMINDER_DAYS)).date().isoformat(),
                    "hasta": (start - timedelta(days=1)).date().isoformat(),
                    "contexto": f"Se viene un torneo de Clash ({label}) {when}. Anima a tus amigos a armar un equipo de 5 "
                                "(y a anotarse en el cliente antes de que cierren las inscripciones).",
                    "anunciar": True, "buscar": False, "auto": True,
                })
                events.append({
                    "nombre": label, "desde": start.date().isoformat(),
                    "contexto": f"Hoy hay torneo de Clash ({label}): {when}. Dales ánimo a los que juegan.",
                    "anunciar": True, "buscar": False, "auto": True,
                })
        save_json(calendario.CLASH_FILE, events)
        log.info("Clash: %d fechas en el calendario", dates)

    async def _check_lcu(self) -> None:
        """ARAM: Caos desde el cliente de League de esta PC: partidas nuevas, quiénes de los amigos
        vinculados jugaron, y se guardan/anuncian como las demás."""
        lcu = lcu_mod.lcu
        if await lcu.whoami() is None:
            return
        game_ids = await lcu.mayhem_game_ids(20)
        store = lcu_mod.load_store()
        seen = set(store["vistos"])
        if not store.get("inicializado"):
            # Primera vez: las partidas viejas se guardan (para !historial y la charla) pero no se anuncian.
            store["inicializado"] = True
            announce = False
        else:
            announce = True
        accounts = cuentas.load()
        for game_id in reversed(game_ids):  # de la más vieja a la más nueva
            if game_id in seen:
                continue
            raw = await lcu.game(game_id)
            store["vistos"].append(game_id)
            if not raw:
                continue
            match = lcu_mod.to_match(raw, lambda key: dd.by_key.get(int(key or 0), str(key)))
            players = lcu_mod.link_participants(match, accounts)
            store["partidas"][match["metadata"]["matchId"]] = match
            log.info("ARAM: Caos %s leída del cliente (%d amigos vinculados)", game_id, len(players))
            if announce and players:
                lcu_mod.save_store(store)
                self._chat_cache.clear()
                await self._announce_match(match, players)
        lcu_mod.save_store(store)

    async def _announce_game(self, match_id: str, players: list[tuple[int, dict]]) -> None:
        await self._announce_match(await client.match(match_id), players)

    async def _announce_match(self, match: dict, players: list[tuple[int, dict]]) -> None:
        match_id = match.get("metadata", {}).get("matchId", "?")
        info = match.get("info", {})
        duration = info.get("gameDuration", 0)
        end = info.get("gameEndTimestamp", 0) / 1000
        if duration >= MIN_GAME_SECONDS:  # para el Wrapped de League (los remakes no cuentan)
            by_puuid_all = {p.get("puuid"): p for p in info.get("participants", [])}
            for uid, account in players:
                if account["puuid"] in by_puuid_all:
                    p = by_puuid_all[account["puuid"]]
                    wrapped.record_lol(uid, account.get("servidor"), match_id, p, info,
                                       _queue(info), dd.champion_name(p.get("championName", "?")))
        if info.get("queueId", 0) in SKIP_QUEUES or duration < MIN_GAME_SECONDS or time.time() - end > MAX_GAME_AGE:
            return
        by_puuid = {p.get("puuid"): p for p in info.get("participants", [])}
        rows, ai_lines, who_played = [], [], []
        guild_ids = set()
        win = None
        for uid, account in players:
            p = by_puuid.get(account["puuid"])
            if not p:
                continue
            win = p.get("win") if win is None else win
            minutes = max(duration / 60, 1)
            arena = _is_arena(info)
            emoji, result = _result(p, info)
            position = "" if arena else POSITIONS.get(p.get("teamPosition", ""), "")
            champ = dd.champion_name(p.get("championName", "?"))
            guild = self.bot.get_guild(account.get("servidor") or 0)
            member = guild.get_member(uid) if guild else None
            shown = member.display_name if member else (account.get("discord") or cuentas.riot_id(account))
            details = (
                f"**{_kda(p)}** · {result} · {p.get('totalDamageDealtToChampions', 0) / 1000:.1f}k daño" if arena else
                f"**{_kda(p)}** · {_cs(p)} CS ({_cs(p) / minutes:.1f}/min) · "
                f"{p.get('totalDamageDealtToChampions', 0) / 1000:.1f}k daño · visión {p.get('visionScore', 0)}"
            )
            rows.append((f"{emoji} {shown} · {champ}" + (f" ({position})" if position else ""), details))
            who_played.append(f"{shown} ({champ})")
            ai_lines.append(
                f"{shown} jugó {champ}{' de ' + position if position else ''}: {_kda(p)} (asesinatos/muertes/"
                f"asistencias), " + (f"terminó en el {result}" if arena else f"{_cs(p)} súbditos, {result}")
                + (", fue pentakill" if p.get("pentaKills") else "")
            )
            summary = f"{champ} {_kda(p)}, {result} ({_queue(info)})"
            cuentas.update(uid, ultima_resumen=summary)
            if account.get("servidor"):
                guild_ids.add(account["servidor"])
        if not rows:
            return
        good = ("✅", "🥇", "🥈", "🥉")
        title = ("🏆 ¡Victoria!" if all(r[0].startswith(good) for r in rows)
                 else "💔 Derrota" if all(r[0].startswith("❌") for r in rows) else "🏁 Fin de partida")
        embed = discord.Embed(
            title=title,
            description=f"{_queue(info)} · {_duration(duration)}" + (" · leída del cliente" if info.get("desdeCliente") else ""),
            color=0x2ECC71 if title.startswith("🏆") else 0xE74C3C if title.startswith("💔") else COLOR,
        )
        for name, value in rows:
            embed.add_field(name=name, value=value, inline=False)
        if POST_GAME_DETAILS:
            situation = (
                "Terminó una partida de League de tus amigos del servidor. " + "; ".join(ai_lines)
                + ". Coméntala en personaje en 2 o 3 frases: si ganaron, celébralo; si perdieron, consuélalos con "
                "ternura (sin ser cruel), y menciona algún dato concreto (un buen KDA, muchas muertes...)."
            )
        else:
            outcome = ("ganaron" if title.startswith("🏆") else "perdieron" if title.startswith("💔")
                       else "terminaron (con resultados distintos)")
            situation = (
                f"Terminó una partida de League ({_queue(info)}) de tus amigos del servidor y {outcome}. Datos solo "
                "para que sepas cómo les fue: " + "; ".join(ai_lines)
                + ". Escríbeles un mensaje en personaje de 2 o 3 frases, nombrando a quién jugó, el modo y el "
                "campeón: si ganaron, celébralo; si perdieron, consuélalos con ternura (sin ser cruel). NO menciones "
                "números (ni KDA, ni puesto, ni súbditos, ni daño, ni minutos): solo cómo les fue en general."
            )
            people = " y ".join([", ".join(who_played[:-1]), who_played[-1]]) if len(who_played) > 1 else who_played[0]
            fallback = f"🌸 ¡{people} {'terminaron' if len(who_played) > 1 else 'terminó'} una partida de {_queue(info)}!"
        eventos = self.bot.get_cog("Eventos")
        guilds = [self.bot.get_guild(g) for g in guild_ids] or list(self.bot.guilds)
        for guild in filter(None, guilds):
            channel = eventos.channel_for(guild) if eventos else guild.system_channel
            if channel is None:
                continue
            try:
                if POST_GAME_DETAILS:
                    await self._send_with_comment(channel, embed, situation)
                else:  # solo el comentario (sin la IA: una línea corta, sin el resultado)
                    line = await self._comment(channel, situation)
                    await channel.send((line or fallback)[:2000], allowed_mentions=NO_MENTIONS)
                log.info("Resumen de la partida %s publicado en %s", match_id, guild.name)
            except discord.HTTPException as exc:
                log.warning("No se pudo publicar el resumen en %s: %s", guild.name, exc)

    # ---------- Errores ----------

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, RiotError):
            ctx.command_failed = True
            await ctx.send("😳 Riot no me respondió bien ahora mismo. Prueba en un ratito.")
            log.warning("Riot API en %s: %s", ctx.command, original)
            error.handled = True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Riot(bot))
