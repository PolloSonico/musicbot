"""Consejos en la selección de campeones, leídos del cliente de League de ESTA PC (la LCU).

Mientras el cliente está abierto, cada pocos segundos se mira si la cuenta con sesión iniciada está
en la selección de campeones. Cuando ya tiene campeón, Lillia le manda por mensaje privado (a quien
tenga esa cuenta vinculada con !vincular; si nadie, al dueño del bot) consejos para ESA partida.

Cada modo es distinto, y los consejos se adaptan:
  - Normal / Clasificatoria / Clash (Grieta): elige su campeón; runas, hechizos, build y el
    enfrentamiento de su línea si ya se ve el campeón rival. El consejo sale al confirmar el campeón.
  - ARAM: campeón al azar. Dice si le conviene cambiar por alguno del banco (solo de esos), y runas,
    hechizos y build. Si cambia de campeón, manda un consejo nuevo (como mucho 3 por partida).
  - ARAM: Caos: al azar y SIN runas: aumentos para buscar, build y consejos (nada de runas).
  - Arena: elige su campeón y NO hay runas: aumentos, objetos y cómo jugar con su dupla.
  - Otros modos: consejos generales (sin inventar runas si no sabe si el modo las tiene).

Configuración (.env): LCU_CHAMP_SELECT=true (false = apagado). Usa la misma conexión con el cliente
que las partidas de ARAM: Caos (solo funciona en Windows, en la PC donde se juega).
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands, tasks

import avisos
import lcu as lcu_mod
import persona as persona_mod
import riot_cuentas as cuentas
from datadragon import dd
from riot_api import POSITIONS, QUEUES

log = logging.getLogger("seleccion")

ENABLED = os.getenv("LCU_CHAMP_SELECT", "true").strip().lower() not in ("0", "false", "no", "off") and os.name == "nt"
POLL_SECONDS = 4
RETRY_CLOSED = 45  # con el cliente cerrado, cada cuánto se vuelve a buscar (buscarlo cuesta un poco)
RANDOM_STABLE = 5  # en modos al azar, segundos que tiene que quedarse con un campeón antes del consejo
MAX_TIPS = 3  # consejos por selección de campeones
MIN_GAP = 12  # segundos mínimos entre dos consejos
ADVICE_TIMEOUT = 50


@dataclass
class Mode:
    label: str
    free_pick: bool  # elige entre todos sus campeones (si no: al azar / banco)
    runes: Optional[bool]  # None = no se sabe
    augments: bool = False
    arena: bool = False


def mode_of(queue: dict, session: dict) -> Optional[Mode]:
    """Qué se puede hacer en esta selección de campeones. None = modo sin consejos (Enjambre, práctica)."""
    queue = queue or {}
    qid = queue.get("id")
    game_mode = (queue.get("gameMode") or "").upper()
    label = QUEUES.get(qid) or queue.get("description") or game_mode.title() or "una partida"
    if game_mode in ("STRAWBERRY", "PRACTICETOOL", "TUTORIAL") or game_mode.startswith("TUTORIAL"):
        return None
    my_pick = any(a.get("type") == "pick" and a.get("actorCellId") == session.get("localPlayerCellId")
                  for group in session.get("actions") or [] for a in group)
    random_pick = bool(session.get("benchEnabled")) or not my_pick
    if qid in lcu_mod.MAYHEM_QUEUES or game_mode == "KIWI":
        return Mode("ARAM: Caos", free_pick=False, runes=False, augments=True)
    if game_mode == "CHERRY":
        return Mode(label if "Arena" in label else "Arena", free_pick=True, runes=False, augments=True, arena=True)
    if game_mode == "ARAM":
        return Mode(label, free_pick=False, runes=True)
    if game_mode in ("CLASSIC", "URF", "ONEFORALL", "ULTBOOK", "NEXUSBLITZ"):
        return Mode(label, free_pick=not random_pick, runes=True)
    return Mode(label, free_pick=not random_pick, runes=None)


def _me(session: dict) -> dict:
    cell = session.get("localPlayerCellId")
    return next((p for p in session.get("myTeam") or [] if p.get("cellId") == cell), {})


def my_champion(session: dict, mode: Mode) -> int:
    """Campeón con el que va a jugar (0 = todavía ninguno). Si elige, cuenta recién cuando lo confirma."""
    me = _me(session)
    if mode.free_pick:
        cell = session.get("localPlayerCellId")
        for group in session.get("actions") or []:
            for a in group:
                if a.get("type") == "pick" and a.get("actorCellId") == cell and a.get("completed"):
                    return int(a.get("championId") or me.get("championId") or 0)
        return 0
    return int(me.get("championId") or 0)


def bench_ids(session: dict) -> list[int]:
    bench = session.get("benchChampions") or []
    ids = [b.get("championId") if isinstance(b, dict) else b for b in bench] or session.get("benchChampionIds") or []
    return [int(i) for i in ids if i]


def build_prompt(name: str, mode: Mode, session: dict, champion_id: int) -> str:
    me = _me(session)
    champ = dd.champion_name(champion_id)
    allies = [dd.champion_name(p["championId"]) for p in session.get("myTeam") or []
              if p.get("championId") and p is not me and p.get("cellId") != me.get("cellId")]
    enemies = [dd.champion_name(p["championId"]) for p in session.get("theirTeam") or [] if p.get("championId")]
    position = POSITIONS.get((me.get("assignedPosition") or "").upper())
    lines = [f"{name} está en la selección de campeones de {mode.label} y va a jugar {champ}"
             + (f" de {position}" if position else "") + "."]
    if allies:
        lines.append(("Su dupla: " if mode.arena else "Sus aliados: ") + ", ".join(allies) + ".")
    if enemies:
        lines.append("Rivales que ya se ven: " + ", ".join(enemies) + ".")
    elif mode.free_pick and not mode.arena:
        lines.append("Todavía no se ven los campeones rivales (no inventes contra quién juega).")
    if not mode.free_pick:
        lines.append("En este modo el campeón es al azar: NO le recomiendes elegir otro campeón cualquiera.")
        bench = [dd.champion_name(c) for c in bench_ids(session)]
        if bench:
            lines.append(f"Puede cambiar por alguno del banco: {', '.join(bench)}. Dile en una frase si le conviene "
                         "cambiar (SOLO por uno de esos) o quedarse, pensando en su equipo.")
    if mode.runes is True:
        lines.append("Recomienda runas (rama principal con la piedra angular, rama secundaria y fragmentos), hechizos "
                     "de invocador y la build (objeto inicial, botas y los 3 objetos principales).")
    elif mode.runes is False:
        lines.append(f"En {mode.label} NO hay runas: no las menciones. Recomienda qué aumentos buscar (que existan en "
                     "ese modo) y la build (objetos principales).")
    else:
        lines.append("No se sabe si este modo tiene runas: no las recomiendes; habla de build y de cómo jugarlo.")
    if mode.arena:
        lines.append("Da también un consejo para jugar en dupla con su compañero.")
    elif position and enemies:
        lines.append("Si se ve quién va a su línea, da un consejo concreto para ese enfrentamiento.")
    else:
        lines.append("Termina con un consejo corto para jugarlo.")
    lines.append("Es un mensaje privado justo antes de la partida: breve y fácil de leer (lista corta), "
                 "como mucho 10 líneas, y con tu personalidad.")
    return "((" + " ".join(lines) + "))"


class Seleccion(commands.Cog, name="Selección"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._in_select = False
        self._last_try = 0.0
        self._reset()
        if ENABLED:
            self.poller.start()

    def _reset(self) -> None:
        self._advised: list[int] = []
        self._candidate = (0, 0.0)  # (campeón, desde cuándo)
        self._last_sent = 0.0
        self._busy = False

    async def cog_unload(self) -> None:
        self.poller.cancel()

    @tasks.loop(seconds=POLL_SECONDS)
    async def poller(self) -> None:
        try:
            await self._tick()
        except Exception:
            log.exception("Error mirando la selección de campeones")

    @poller.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick(self) -> None:
        lcu = lcu_mod.lcu
        now = time.monotonic()
        if lcu._creds is None and now - self._last_try < RETRY_CLOSED:
            return
        self._last_try = now
        phase = await lcu.get("/lol-gameflow/v1/gameflow-phase")
        if phase != "ChampSelect":
            if self._in_select:
                self._in_select = False
                self._reset()
            return
        if not self._in_select:
            self._in_select = True
            self._reset()
        session = await lcu.get("/lol-champ-select/v1/session")
        if not session:
            return
        flow = await lcu.get("/lol-gameflow/v1/session") or {}
        mode = mode_of((flow.get("gameData") or {}).get("queue") or {}, session)
        if mode is None:
            return
        champion = my_champion(session, mode)
        if not champion or champion in self._advised or self._busy or len(self._advised) >= MAX_TIPS:
            return
        if self._candidate[0] != champion:
            self._candidate = (champion, now)
        wait = 0 if mode.free_pick else RANDOM_STABLE
        if now - self._candidate[1] < wait or now - self._last_sent < MIN_GAP:
            return
        self._advised.append(champion)
        self._last_sent = now
        self._busy = True
        asyncio.create_task(self._advise(mode, session, champion))

    async def _recipient(self) -> Optional[discord.abc.User]:
        riot_id = await lcu_mod.lcu.whoami()
        if riot_id and "#" in riot_id:
            name, tag = riot_id.rsplit("#", 1)
            for uid, account in cuentas.load().items():
                if lcu_mod._norm(account.get("nombre")) == lcu_mod._norm(name) and \
                        lcu_mod._norm(account.get("tag")) == lcu_mod._norm(tag):
                    try:
                        return self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
                    except discord.HTTPException:
                        break
        return await avisos.get_owner(self.bot)

    async def _advise(self, mode: Mode, session: dict, champion: int) -> None:
        try:
            persona = persona_mod._persona(self.bot)
            user = await self._recipient()
            if persona is None or user is None:
                return
            if dd.version is None:
                await asyncio.wait_for(dd.refresh(), 8)
            name = getattr(user, "display_name", None) or user.name
            prompt = build_prompt(name, mode, session, champion)
            names = " ".join([dd.champion_name(champion)] + [dd.champion_name(c) for c in bench_ids(session)])
            extra = "\n\n".join(p for p in [
                persona_mod.LOL_INSTRUCTIONS, dd.patch_text(),
                await asyncio.wait_for(dd.items_text(), 8),
                await asyncio.wait_for(dd.champions_text(f"campeón {names}"), 8),
            ] if p)
            channel = user.dm_channel or await user.create_dm()
            reply = await persona.ask(channel, prompt, ADVICE_TIMEOUT, people=[user], remember=False,
                                      search=True, extra=extra)
            if not reply:
                return
            text = persona_mod.extract_actions(reply)[0]
            header = f"-# 🌿 Selección de campeones · {mode.label} · **{dd.champion_name(champion)}**\n"
            for chunk in persona_mod._split(header + text):
                await channel.send(chunk, allowed_mentions=persona_mod.NO_MENTIONS)
            log.info("Consejo de selección para %s (%s, %s)", name, mode.label, dd.champion_name(champion))
        except discord.HTTPException as exc:
            log.warning("No se pudo mandar el consejo por privado: %s", exc)
        except Exception:
            log.exception("No se pudo preparar el consejo de selección")
        finally:
            self._busy = False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Seleccion(bot))
