"""Partidas de ARAM: Caos (ARAM Mayhem) leídas desde el cliente de League de ESTA PC.

Riot no comparte esas partidas por su API pública (es intencional), pero el cliente de League
tiene una API local (la "LCU") con el historial de quien tiene la sesión iniciada. Mientras el
cliente esté abierto en la PC del bot, cada pocos minutos:

1. Se leen las últimas partidas del historial del cliente y se quedan las de ARAM: Caos.
2. De cada partida nueva se pide el detalle con los 10 jugadores (nombre#tag de cada uno).
3. Se compara con las cuentas vinculadas (!vincular): así se sabe qué amigos jugaron.
4. Se guarda en data/partidas_mayhem.json en el mismo formato que la API de Riot, para que el
   resumen post-partida, !historial, la charla con Lillia y el Wrapped las usen igual que las demás.

Solo se ven las partidas de la cuenta con sesión en el cliente de esta PC (y las de los amigos
que jugaron EN esas partidas). Si el cliente está cerrado, simplemente no hace nada.

Cómo se conecta: el cliente se lanza con "--app-port=PUERTO --remoting-auth-token=CLAVE"; se
leen de la línea de comandos del proceso LeagueClientUx.exe (o del archivo "lockfile" de la
carpeta de League, si se configura LOL_LOCKFILE). La conexión es solo local (127.0.0.1).
"""

import asyncio
import base64
import logging
import os
import re
from pathlib import Path
from typing import Optional

import aiohttp

from jsonio import load_json, save_json

log = logging.getLogger("lcu")

ENABLED = os.getenv("LCU_MAYHEM", "true").strip().lower() not in ("0", "false", "no") and os.name == "nt"
LOCKFILE = os.getenv("LOL_LOCKFILE", "").strip()
MAYHEM_QUEUES = {int(q) for q in re.findall(r"\d+", os.getenv("LCU_QUEUES", "2400"))}
STORE_FILE = Path(__file__).resolve().parent / "data" / "partidas_mayhem.json"
MAX_STORED = 300
LOCKFILE_GUESSES = [
    r"C:\Riot Games\League of Legends\lockfile",
    r"D:\Riot Games\League of Legends\lockfile",
    r"E:\Riot Games\League of Legends\lockfile",
    r"C:\Program Files\Riot Games\League of Legends\lockfile",
]
PORT_RE = re.compile(r"--app-port=(\d+)")
TOKEN_RE = re.compile(r"--remoting-auth-token=([\w-]+)")


class LCU:
    def __init__(self) -> None:
        self._creds: Optional[tuple[int, str]] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self.connected_as: Optional[str] = None  # "Nombre#TAG" de la sesión del cliente
        self.last_error: Optional[str] = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- Conexión ----------

    async def _from_process(self) -> Optional[tuple[int, str]]:
        """Puerto y clave desde la línea de comandos de LeagueClientUx.exe (no hace falta saber dónde
        está instalado League)."""
        script = ("Get-CimInstance Win32_Process -Filter \"name='LeagueClientUx.exe'\" | "
                  "Select-Object -ExpandProperty CommandLine")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-EncodedCommand", encoded,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                creationflags=0x08000000,  # CREATE_NO_WINDOW: que no aparezca una ventana
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
        except Exception as exc:
            self.last_error = f"no se pudo buscar el proceso del cliente ({exc})"
            return None
        text = out.decode(errors="ignore")
        port, token = PORT_RE.search(text), TOKEN_RE.search(text)
        return (int(port.group(1)), token.group(1)) if port and token else None

    @staticmethod
    def _from_lockfile() -> Optional[tuple[int, str]]:
        """lockfile: 'LeagueClient:pid:puerto:clave:https'."""
        for path in ([LOCKFILE] if LOCKFILE else []) + LOCKFILE_GUESSES:
            try:
                parts = Path(path).read_text(encoding="utf-8").strip().split(":")
                return int(parts[2]), parts[3]
            except (OSError, IndexError, ValueError):
                continue
        return None

    async def _credentials(self) -> Optional[tuple[int, str]]:
        if self._creds is None:
            self._creds = self._from_lockfile() or await self._from_process()
        return self._creds

    async def get(self, path: str):
        creds = await self._credentials()
        if creds is None:
            self.connected_as = None
            self.last_error = "el cliente de League no está abierto en esta PC"
            return None
        port, token = creds
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15), connector=aiohttp.TCPConnector(ssl=False)
            )
        auth = aiohttp.BasicAuth("riot", token)
        try:
            async with self._session.get(f"https://127.0.0.1:{port}{path}", auth=auth) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                self.last_error = None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self._creds = None  # el cliente se cerró o se reinició (cambian puerto y clave)
            self.connected_as = None
            self.last_error = f"no se pudo hablar con el cliente de League ({type(exc).__name__})"
            return None

    # ---------- Datos ----------

    async def whoami(self) -> Optional[str]:
        me = await self.get("/lol-summoner/v1/current-summoner")
        if not me:
            return None
        self.connected_as = f"{me.get('gameName') or me.get('displayName', '?')}#{me.get('tagLine', '')}".rstrip("#")
        return self.connected_as

    async def mayhem_game_ids(self, count: int = 20) -> list[int]:
        """IDs de las últimas partidas de ARAM: Caos del historial del cliente."""
        data = await self.get(f"/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex={count - 1}")
        games = ((data or {}).get("games") or {}).get("games") or []
        return [g["gameId"] for g in games if g.get("queueId") in MAYHEM_QUEUES and g.get("gameId")]

    async def game(self, game_id: int) -> Optional[dict]:
        return await self.get(f"/lol-match-history/v1/games/{game_id}")


def to_match(raw: dict, champion_id_of) -> dict:
    """Convierte una partida del cliente al formato de la API de Riot (match-v5), así el resto del
    bot la trata igual. champion_id_of(número) -> id de Data Dragon ("MonkeyKing")."""
    identities = {i.get("participantId"): i.get("player", {}) for i in raw.get("participantIdentities", [])}
    participants = []
    for p in raw.get("participants", []):
        stats = p.get("stats", {})
        player = identities.get(p.get("participantId"), {})
        participants.append({
            "puuid": None,  # el PUUID del cliente no sirve para la API: se cruza por nombre#tag
            "riotIdGameName": player.get("gameName") or player.get("summonerName", ""),
            "riotIdTagline": player.get("tagLine", ""),
            "championId": p.get("championId"),
            "championName": champion_id_of(p.get("championId")),
            "teamId": p.get("teamId"),
            "teamPosition": "",
            "win": bool(stats.get("win")),
            "kills": stats.get("kills", 0), "deaths": stats.get("deaths", 0), "assists": stats.get("assists", 0),
            "totalMinionsKilled": stats.get("totalMinionsKilled", 0),
            "neutralMinionsKilled": stats.get("neutralMinionsKilled", 0),
            "totalDamageDealtToChampions": stats.get("totalDamageDealtToChampions", 0),
            "visionScore": stats.get("visionScore", 0),
            "pentaKills": stats.get("pentaKills", 0),
            "champLevel": stats.get("champLevel", 0),
        })
    duration = raw.get("gameDuration", 0)
    if duration > 20000:  # algunas versiones lo dan en milisegundos
        duration //= 1000
    created = raw.get("gameCreation", 0)
    return {
        "metadata": {"matchId": f"LCU_{raw.get('gameId')}"},
        "info": {
            "queueId": raw.get("queueId", 2400),
            "gameMode": raw.get("gameMode", ""),
            "gameDuration": duration,
            "gameCreation": created,
            "gameEndTimestamp": created + duration * 1000,
            "participants": participants,
            "desdeCliente": True,
        },
    }


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def link_participants(match: dict, accounts: dict) -> list[tuple[int, dict]]:
    """Marca con su PUUID a los jugadores de la partida que tienen cuenta vinculada (comparando
    nombre#tag) y devuelve [(id de Discord, cuenta)]."""
    by_riot_id = {(_norm(a.get("nombre")), _norm(a.get("tag"))): (int(uid), a) for uid, a in accounts.items()}
    found = []
    for p in match["info"]["participants"]:
        hit = by_riot_id.get((_norm(p.get("riotIdGameName")), _norm(p.get("riotIdTagline"))))
        if hit:
            p["puuid"] = hit[1]["puuid"]
            found.append(hit)
    return found


# ---------- Guardado ----------

def load_store() -> dict:
    data = load_json(STORE_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("partidas", {})
    data.setdefault("vistos", [])
    return data


def save_store(data: dict) -> None:
    matches = data["partidas"]
    if len(matches) > MAX_STORED:  # se quedan las más nuevas
        newest = sorted(matches, key=lambda k: matches[k]["info"].get("gameEndTimestamp", 0))[-MAX_STORED:]
        data["partidas"] = {k: matches[k] for k in newest}
    data["vistos"] = data["vistos"][-500:]
    save_json(STORE_FILE, data)


def stored_matches_for(puuid: str) -> list[dict]:
    """Partidas de ARAM: Caos guardadas donde jugó esa cuenta (de la más nueva a la más vieja)."""
    matches = [m for m in load_store()["partidas"].values()
               if any(p.get("puuid") == puuid for p in m["info"]["participants"])]
    return sorted(matches, key=lambda m: m["info"].get("gameEndTimestamp", 0), reverse=True)


lcu = LCU()
