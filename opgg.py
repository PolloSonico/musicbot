"""Builds, runas, counters y tier list de League desde OP.GG, por su servidor oficial para IAs (MCP).

OP.GG ofrece gratis y sin clave un servidor "MCP" (https://mcp-api.op.gg/mcp): se le piden herramientas
por nombre con parámetros y devuelve los datos del parche actual. Acá se usan:
  - lol_get_champion_analysis      build de un campeón: runas, hechizos, objetos, orden de habilidades, counters
  - lol_get_lane_matchup_guide     guía de un enfrentamiento de línea (mi campeón contra otro)
  - lol_list_lane_meta_champions   tier list por línea (los campeones más fuertes del parche)
  - lol_list_aram_augments         aumentos de ARAM: Caos para un campeón
  - lol_esports_list_schedules     calendario y resultados de las ligas profesionales

Lo que devuelve se le pasa a Lillia como "información real" para que responda con datos exactos.
Si OP.GG no responde, no se agrega nada (y se usa la búsqueda en internet).

Cómo se habla con un servidor MCP (JSON-RPC por HTTP): primero "initialize" (da un id de sesión), después
"tools/call" con el nombre de la herramienta y sus argumentos. La respuesta puede venir como JSON o como
eventos (SSE: líneas "data: {...}").
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import aiohttp

from datadragon import dd
from jsonio import load_json, save_json

log = logging.getLogger("opgg")

URL = os.getenv("OPGG_MCP_URL", "").strip() or "https://mcp-api.op.gg/mcp"
ENABLED = os.getenv("OPGG", "true").strip().lower() not in ("0", "false", "no", "off")
LANG = os.getenv("OPGG_LANG", "").strip() or "es_ES"
NAMES_FILE = Path(__file__).resolve().parent / "data" / "opgg_campeones.json"
DEBUG_FILE = Path(__file__).resolve().parent / "data" / "opgg_debug.json"
CACHE_SECONDS = 1800
MAX_TEXT = 6000  # caracteres de cada respuesta que se le pasan a la IA
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

POSITIONS = [
    ("top", r"top|superior"), ("jungle", r"jungla|jungle|jg|jungler|junglero"), ("mid", r"mid|medio|central"),
    ("adc", r"adc|bot|tirador|carry|inferior"), ("support", r"support|supp|sup|soporte|apoyo"),
]
LEAGUES = ["lck", "lpl", "lec", "lcs", "cblol", "lla", "lcp", "lta", "ljl", "vcs", "pcs", "msi", "worlds", "first stand"]

ANALYSIS_FIELDS = [
    "champion", "position",
    "data.summary.average_stats.{win_rate,pick_rate,ban_rate,tier,rank}",
    "data.summoner_spells.{ids_names[],pick_rate}",
    "data.runes.{primary_page_name,primary_rune_names[],secondary_page_name,secondary_rune_names[],stat_mod_names[],pick_rate}",
    "data.starter_items.{ids_names[],pick_rate}",
    "data.boots.{ids_names[],pick_rate}",
    "data.core_items.{ids_names[],pick_rate}",
    "data.fourth_items[].{ids_names[],pick_rate}",
    "data.fifth_items[].{ids_names[],pick_rate}",
    "data.sixth_items[].{ids_names[],pick_rate}",
    "data.skills.{order[]}",
    "data.skill_masteries.{ids[]}",
    "data.strong_counters[].{champion_name,win_rate}",
    "data.weak_counters[].{champion_name,win_rate}",
]

_cache: dict[str, tuple[float, str]] = {}
_session: Optional[aiohttp.ClientSession] = None
_headers: dict = {}
_ready = False
_next_id = 0
_lock = asyncio.Lock()
_down_until = 0.0


async def close() -> None:
    if _session and not _session.closed:
        await _session.close()


# ---------- Cliente MCP ----------

async def _post(payload: dict) -> Optional[dict]:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
    async with _session.post(URL, json=payload, headers={**HEADERS, **_headers}) as resp:
        session_id = resp.headers.get("Mcp-Session-Id")
        if session_id:
            _headers["Mcp-Session-Id"] = session_id
        text = await resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
        if not text.strip():
            return None
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            for line in text.splitlines():
                if line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    if "result" in data or "error" in data:
                        return data
            return None
        return json.loads(text)


async def _initialize() -> None:
    global _ready
    _headers.clear()
    await _post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "lillia-bot", "version": "1.0"}}})
    await _post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    _ready = True


class ToolError(Exception):
    """La herramienta respondió con un error (parámetro inválido, campeón desconocido...)."""


async def call_tool(name: str, arguments: dict) -> str:
    """Llama a una herramienta de OP.GG y devuelve su respuesta como texto. ToolError si la rechaza."""
    global _ready, _next_id, _down_until
    key = name + json.dumps(arguments, sort_keys=True)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    if not ENABLED or time.time() < _down_until:
        raise RuntimeError("OP.GG no disponible")
    async with _lock:
        try:
            for attempt in (1, 2):
                if not _ready:
                    await _initialize()
                _next_id += 1
                try:
                    data = await _post({"jsonrpc": "2.0", "id": _next_id, "method": "tools/call",
                                        "params": {"name": name, "arguments": arguments}})
                    break
                except RuntimeError:
                    _ready = False  # la sesión venció: se abre otra y se reintenta una vez
                    if attempt == 2:
                        raise
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            _down_until = time.time() + 120
            raise RuntimeError(f"OP.GG no responde ({exc or type(exc).__name__})") from exc
    if not data or "error" in data:
        raise ToolError(str((data or {}).get("error", "sin respuesta"))[:300])
    result = data.get("result", {})
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text").strip()
    if result.get("isError") or not text:
        raise ToolError(text[:300] or "respuesta vacía")
    _cache[key] = (time.time(), text)
    _remember_raw(name, arguments, text)
    return text


def _remember_raw(name: str, arguments: dict, text: str) -> None:
    """Guarda las últimas respuestas de OP.GG tal cual llegan (data/opgg_debug.json), para revisar qué trae."""
    try:
        debug = load_json(DEBUG_FILE, [])
        debug = debug if isinstance(debug, list) else []
        args = {k: v for k, v in arguments.items() if k != "desired_output_fields"}
        debug.append({"cuando": time.strftime("%Y-%m-%d %H:%M:%S"), "herramienta": name, "argumentos": args,
                      "respuesta": text[:4000]})
        save_json(DEBUG_FILE, debug[-15:])
    except Exception:
        pass
    log.info("OP.GG %s %s: %d caracteres", name, {k: v for k, v in arguments.items() if k != "desired_output_fields"}, len(text))


# ---------- Traducir la pregunta a parámetros ----------

def champion_keys(cid: str) -> list[str]:
    """Formas posibles del nombre para OP.GG (UPPER_SNAKE_CASE) a partir del id de Data Dragon.
    "KogMaw" -> ["KOG_MAW", "KOGMAW", ...]. La que funciona se guarda en data/opgg_campeones.json."""
    special = {"MonkeyKing": ["WUKONG", "MONKEY_KING"], "Nunu": ["NUNU_WILLUMP", "NUNU"], "Renata": ["RENATA_GLASC", "RENATA"]}
    snake = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", cid).upper()
    english = dd.champions.get(cid, {}).get("name", cid)
    by_name = re.sub(r"[^A-Z0-9]+", "_", english.upper()).strip("_")
    out = []
    for k in special.get(cid, []) + [snake, cid.upper(), by_name]:
        if k and k not in out:
            out.append(k)
    return out


def position_in(text: str) -> str:
    low = f" {text.lower()} "
    for position, pattern in POSITIONS:
        if re.search(rf"(?<![\wáéíóú])(?:{pattern})(?![\wáéíóú])", low):
            return position
    return "none"


TAG_LANES = {"Marksman": ["adc", "mid"], "Support": ["support", "mid"], "Mage": ["mid", "support"],
             "Assassin": ["mid", "jungle"], "Fighter": ["top", "jungle"], "Tank": ["top", "support", "jungle"]}


def positions_for(cid: str, asked: str) -> list[str]:
    """Líneas a probar en OP.GG: la que nombró la persona o, si no dijo, las habituales del campeón
    según su clase (OP.GG exige una línea concreta: no acepta "ninguna")."""
    if asked not in ("none", "all", ""):
        return [asked]
    lanes: list[str] = []
    for tag in dd.champions.get(cid, {}).get("tags", []):
        for lane in TAG_LANES.get(tag, []):
            if lane not in lanes:
                lanes.append(lane)
    return (lanes or ["mid", "top", "adc"])[:3]


def game_mode_in(text: str) -> str:
    low = text.lower()
    return "aram" if "aram" in low else "urf" if "urf" in low else "flex" if "flex" in low else "ranked"


class PositionError(ToolError):
    """OP.GG no tiene datos de ese campeón en esa línea."""


def _load_known() -> dict:
    known = load_json(NAMES_FILE, {})
    return known if isinstance(known, dict) else {}


async def _with_champion(cid: str, make_call) -> Optional[str]:
    """Prueba las formas del nombre del campeón hasta que OP.GG acepte una (y la recuerda).
    Si el problema es la línea (PositionError), no sigue probando nombres: lo deja pasar."""
    known = _load_known()
    keys = ([known[cid]] if cid in known else []) + [k for k in champion_keys(cid) if k != known.get(cid)]
    for key in keys:
        try:
            text = await make_call(key)
        except ToolError as exc:
            if "position" in str(exc).lower():
                raise PositionError(str(exc)) from exc
            log.info("OP.GG no aceptó %s como %s: %s", cid, key, exc)
            continue
        if known.get(cid) != key:
            known = _load_known()
            known[cid] = key
            save_json(NAMES_FILE, known)
        return text
    return None


async def _lang_call(name: str, arguments: dict) -> str:
    """Llama en español; si OP.GG no tiene ese idioma, en inglés."""
    try:
        return await call_tool(name, {**arguments, "lang": LANG})
    except ToolError as exc:
        if LANG == "en_US" or "lang" not in str(exc).lower():
            raise  # el problema no es el idioma (campeón o línea inválidos): no tiene sentido repetir en inglés
        return await call_tool(name, {**arguments, "lang": "en_US"})


# ---------- Datos para la IA ----------

async def champion_build(cid: str, position: str = "none", game_mode: str = "ranked") -> str:
    """Build del campeón. En ARAM/URF no hay líneas; en la Grieta se prueba la línea pedida o las habituales."""
    known = _load_known()
    remembered = known.get(f"{cid}:linea")
    lanes = ["none"] if game_mode in ("aram", "urf", "nexus_blitz") else positions_for(cid, position)
    if remembered in lanes:  # la que funcionó la última vez, primero
        lanes = [remembered] + [l for l in lanes if l != remembered]
    for lane in lanes:
        try:
            text = await _with_champion(cid, lambda key: _lang_call("lol_get_champion_analysis", {
                "game_mode": game_mode, "champion": key, "position": lane, "desired_output_fields": ANALYSIS_FIELDS}))
        except PositionError as exc:
            log.info("OP.GG no tiene a %s en %s: %s", cid, lane, exc)
            continue
        if not text:
            return ""
        if lane != "none" and position in ("none", "all", "") and remembered != lane:
            known = _load_known()
            known[f"{cid}:linea"] = lane
            save_json(NAMES_FILE, known)
        where = "" if lane == "none" else f" de {lane}"
        return (f"Datos de OP.GG del parche actual para {dd.champion_name(cid)}{where} ({game_mode}): build más usada "
                f"(runas, hechizos, objetos por orden, orden de habilidades) y counters:\n{text[:MAX_TEXT]}")
    return ""


async def matchup(my_cid: str, enemy_cid: str, position: str) -> str:
    async def with_enemy(my_key: str) -> str:
        found = await _with_champion(enemy_cid, lambda enemy_key: _lang_call("lol_get_lane_matchup_guide", {
            "position": position, "my_champion": my_key, "opponent_champion": enemy_key}))
        if found is None:
            raise ToolError("rival desconocido")
        return found

    text = await _with_champion(my_cid, with_enemy)
    if not text:
        return ""
    return (f"Guía de OP.GG del enfrentamiento {dd.champion_name(my_cid)} contra {dd.champion_name(enemy_cid)} "
            f"({position}):\n{text[:MAX_TEXT]}")


async def tier_list(position: str) -> str:
    lanes = [position] if position not in ("none", "all") else ["top", "jungle", "mid", "adc", "support"]
    fields = [f"data.positions.{lane}[].{{champion,tier,rank,win_rate,pick_rate,ban_rate}}" for lane in lanes]
    text = await _lang_call("lol_list_lane_meta_champions", {
        "position": position if position not in ("none", "") else "all", "desired_output_fields": fields})
    return ("Tier list de OP.GG del parche actual (tier 1 = los más fuertes; ordenados por puesto):\n"
            + text[:MAX_TEXT if len(lanes) == 1 else MAX_TEXT * 2])


async def aram_augments(cid: str) -> str:
    key = next((k for k, v in dd.by_key.items() if v == cid), None)
    if key is None:
        return ""
    text = await _lang_call("lol_list_aram_augments", {
        "champion_id": int(key), "desired_output_fields": ["data.augments[].{name,tier,performance,popular}"]})
    return f"Aumentos de ARAM: Caos para {dd.champion_name(cid)} según OP.GG (mejor tier = mejor):\n{text[:MAX_TEXT]}"


async def esports(text: str, team: str = "") -> str:
    """Calendario y últimos resultados de la liga y/o el equipo nombrados."""
    low = text.lower()
    league = next((l for l in LEAGUES if re.search(rf"(?<!\w){re.escape(l)}(?!\w)", low)), None)
    if not league and not team:
        return ""
    args = {**({"league": league} if league else {}), **({"team_name": team} if team else {})}
    parts = []
    for mode, title in (("schedule", "Próximos partidos"), ("result", "Últimos resultados")):
        try:
            found = await call_tool("lol_esports_list_schedules", {**args, "mode": mode, **({"limit": 5} if mode == "result" else {})})
        except ToolError as exc:
            log.info("OP.GG no dio el calendario (%s, %s): %s", mode, args, exc)
            continue
        parts.append(f"{title} ({', '.join(str(v) for v in args.values())}) según OP.GG; las horas están en UTC, "
                     f"conviértelas a la hora local:\n{found[:MAX_TEXT // 2]}")
    return "\n\n".join(parts)


LEAGUE_REGION = {"lcs": "NA", "lta": "NA", "lck": "KR", "lec": "EUW", "cblol": "BR", "lla": "LAN", "lpl": "CN",
                 "ljl": "JP", "vcs": "VN", "pcs": "TW"}


async def pro_player(name: str, text: str) -> str:
    """En qué equipo y región juega un profesional (por su apodo), según OP.GG."""
    low = text.lower()
    league = next((l for l in LEAGUES if re.search(rf"(?<!\w){re.escape(l)}(?!\w)", low)), None)
    regions = ([LEAGUE_REGION[league]] if league in LEAGUE_REGION else []) + ["NA", "KR", "EUW", "BR"]
    for region in list(dict.fromkeys(regions))[:3]:
        try:
            found = await call_tool("lol_get_pro_player_riot_id",
                                    {"player_name": name, "region": region, "return_suggestions": True})
        except ToolError as exc:
            log.info("OP.GG no encontró al jugador %s en %s: %s", name, region, exc)
            continue
        if found and not re.search(r"not found|no (?:pro )?player|\[\]|null", found[:200], re.I):
            return f"Jugador profesional \"{name}\" según OP.GG (equipo y región actuales):\n{found[:1500]}"
    return ""


def league_in(text: str) -> Optional[str]:
    low = text.lower()
    return next((l for l in LEAGUES if re.search(rf"(?<!\w){re.escape(l)}(?!\w)", low)), None)


def is_team_or_league(word: str) -> bool:
    import esports_datos
    low = word.lower()
    return low in LEAGUES or any(low in name.split() or low == name for name in esports_datos.TEAMS)


TIER_RE = re.compile(r"tier ?list|meta|qu[eé] (?:juego|pickeo|me conviene)|mejores? (?:campe|pick)|m[aá]s (?:fuertes?|rotos?)|"
                     r"campe[oó]n(?:es)? (?:fuertes?|rotos?|op)|est[aá] roto", re.I)
VERSUS_RE = re.compile(r"\b(contra|vs\.?|versus|counter\w*|le gana|matchup)\b", re.I)
AUGMENT_RE = re.compile(r"aumento|augment|caos|mayhem", re.I)


async def context_for(text: str, own_name: frozenset = frozenset()) -> str:
    """Datos de OP.GG para una pregunta de League (build, counter, tier list...), o '' si no aplica o no responde."""
    if not ENABLED or re.search(r"\barena\b", text, re.I):  # OP.GG no ofrece datos de Arena por este medio
        return ""
    champions = dd.find_champions(text, own_name)
    position = position_in(text)
    try:
        if len(champions) >= 2 and VERSUS_RE.search(text):
            guide = ""
            for lane in positions_for(champions[0], position):
                try:
                    guide = await matchup(champions[0], champions[1], lane)
                except PositionError:
                    continue
                if guide:
                    break
            parts = [guide, await champion_build(champions[0], position)]
            return "\n\n".join(p for p in parts if p)
        if champions:
            if AUGMENT_RE.search(text):
                parts = [await aram_augments(champions[0]), await champion_build(champions[0], "none", "aram")]
                return "\n\n".join(p for p in parts if p)
            return await champion_build(champions[0], position, game_mode_in(text))
        if TIER_RE.search(text):
            return await tier_list(position)
    except Exception as exc:
        log.info("No se pudo consultar OP.GG: %s", exc)
    return ""
