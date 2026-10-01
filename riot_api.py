"""Cliente mínimo de la API oficial de Riot Games (League of Legends).

La clave va en .env como RIOT_API_KEY (developer.riotgames.com → tu app → API key). Nunca se
muestra en Discord ni en los logs.

Conceptos:
- Riot ID: el nombre de la cuenta, "Nombre#TAG". Se convierte en un PUUID (identificador fijo de
  la cuenta) con la API "account".
- Servidor ("plataforma"): donde juega la cuenta. Argentina es LAS = la2. Las APIs de perfil,
  rango y partida en vivo se piden al servidor; el historial de partidas se pide a la "región"
  (americas para LAS, LAN, BR y NA).
- Límites de la clave personal: 20 pedidos por segundo y 100 cada 2 minutos. El cliente espera
  solo cuando se acerca al límite, así nunca se pasa (y si Riot igual responde 429, espera lo que
  indique y reintenta una vez).
"""

import asyncio
import logging
import os
import time
from collections import deque
from typing import Optional
from urllib.parse import quote

import aiohttp

log = logging.getLogger("riot")

API_KEY = os.getenv("RIOT_API_KEY", "").strip()
_platform_env = os.getenv("RIOT_PLATFORM", "las").strip().lower() or "las"

# Nombres que usa la gente -> código de plataforma de Riot.
PLATFORMS = {
    "las": "la2", "la2": "la2", "lan": "la1", "la1": "la1", "br": "br1", "br1": "br1",
    "na": "na1", "na1": "na1", "euw": "euw1", "euw1": "euw1", "eune": "eun1", "eun1": "eun1",
    "tr": "tr1", "tr1": "tr1", "ru": "ru", "me": "me1", "me1": "me1", "kr": "kr", "jp": "jp1",
    "jp1": "jp1", "oce": "oc1", "oc1": "oc1", "sg": "sg2", "sg2": "sg2", "tw": "tw2", "tw2": "tw2",
    "vn": "vn2", "vn2": "vn2",
}
DEFAULT_PLATFORM = PLATFORMS.get(_platform_env, "la2")
PLATFORM_NAMES = {"la2": "LAS", "la1": "LAN", "br1": "BR", "na1": "NA", "euw1": "EUW", "eun1": "EUNE",
                  "tr1": "TR", "ru": "RU", "me1": "ME", "kr": "KR", "jp1": "JP", "oc1": "OCE", "sg2": "SG",
                  "tw2": "TW", "vn2": "VN"}
REGIONS = {"la2": "americas", "la1": "americas", "br1": "americas", "na1": "americas",
           "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
           "kr": "asia", "jp1": "asia", "oc1": "sea", "sg2": "sea", "tw2": "sea", "vn2": "sea"}

TIERS = {"IRON": "Hierro", "BRONZE": "Bronce", "SILVER": "Plata", "GOLD": "Oro", "PLATINUM": "Platino",
         "EMERALD": "Esmeralda", "DIAMOND": "Diamante", "MASTER": "Maestro", "GRANDMASTER": "Gran Maestro",
         "CHALLENGER": "Retador"}
QUEUES = {400: "Normal (reclutamiento)", 420: "Clasificatoria Solo/Dúo", 430: "Normal (a ciegas)",
          440: "Clasificatoria Flexible", 450: "ARAM", 480: "Partida rápida", 490: "Partida rápida",
          700: "Clash", 720: "ARAM Clash", 830: "Contra la IA", 840: "Contra la IA", 850: "Contra la IA",
          870: "Contra la IA", 880: "Contra la IA", 890: "Contra la IA", 900: "URF", 1020: "Uno para todos",
          1300: "Nexus Blitz", 1400: "Libro de hechizos definitivo", 1700: "Arena", 1710: "Arena",
          1810: "Enjambre", 1820: "Enjambre", 1830: "Enjambre", 1840: "Enjambre", 1900: "URF",
          2300: "Brawl", 2400: "ARAM: Caos", 0: "Personalizada"}
POSITIONS = {"TOP": "top", "JUNGLE": "jungla", "MIDDLE": "mid", "BOTTOM": "ADC", "UTILITY": "support"}


class RiotError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class NotFound(RiotError):
    pass


def enabled() -> bool:
    return bool(API_KEY)


def platform_of(text: str) -> Optional[str]:
    return PLATFORMS.get(text.strip().lower())


def tier_text(entry: Optional[dict]) -> str:
    """{'tier': 'GOLD', 'rank': 'II', 'leaguePoints': 40, ...} -> 'Oro II (40 LP)'."""
    if not entry:
        return "Sin clasificar"
    tier = TIERS.get(entry.get("tier", ""), entry.get("tier", "?").title())
    division = "" if entry.get("tier") in ("MASTER", "GRANDMASTER", "CHALLENGER") else f" {entry.get('rank', '')}"
    return f"{tier}{division} ({entry.get('leaguePoints', 0)} LP)"


class _RateLimiter:
    """Ventanas deslizantes: como mucho `n` pedidos cada `seconds` segundos (para cada ventana)."""

    def __init__(self, windows: list[tuple[int, float]]) -> None:
        self.windows = [(n, seconds, deque()) for n, seconds in windows]
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                delay = 0.0
                for n, seconds, stamps in self.windows:
                    while stamps and now - stamps[0] >= seconds:
                        stamps.popleft()
                    if len(stamps) >= n:
                        delay = max(delay, seconds - (now - stamps[0]) + 0.05)
                if delay <= 0:
                    for _n, _s, stamps in self.windows:
                        stamps.append(now)
                    return
                await asyncio.sleep(delay)


class RiotClient:
    def __init__(self, api_key: str = API_KEY) -> None:
        self.api_key = api_key
        # Un poco por debajo de los límites de la clave personal (20/s y 100/2min).
        self.limiter = _RateLimiter([(18, 1.0), (95, 120.0)])
        self._session: Optional[aiohttp.ClientSession] = None
        self.on_key_error = None  # función(texto) para avisar al dueño si la clave deja de servir

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, host: str, path: str, params: Optional[dict] = None, retry: bool = True):
        if not self.api_key:
            raise RiotError(0, "falta RIOT_API_KEY en .env")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        await self.limiter.wait()
        url = f"https://{host}.api.riotgames.com{path}"
        async with self._session.get(url, params=params, headers={"X-Riot-Token": self.api_key}) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 404:
                raise NotFound(404, "no encontrado")
            if resp.status == 429 and retry:
                wait = float(resp.headers.get("Retry-After", "5"))
                log.warning("Riot API: límite alcanzado, esperando %.0fs", wait)
                await asyncio.sleep(min(wait, 60))
                return await self._get(host, path, params, retry=False)
            if resp.status in (401, 403):
                log.error("Riot API: la clave no es válida o no tiene acceso (%s)", resp.status)
                if self.on_key_error:
                    self.on_key_error(f"La clave de la Riot API no funciona (error {resp.status}).")
            raise RiotError(resp.status, f"Riot respondió {resp.status}")

    # ---------- Endpoints ----------

    async def account_by_riot_id(self, name: str, tag: str, platform: str) -> dict:
        region = REGIONS.get(platform, "americas")
        return await self._get(region, f"/riot/account/v1/accounts/by-riot-id/{quote(name)}/{quote(tag)}")

    async def account_by_puuid(self, puuid: str, platform: str) -> dict:
        region = REGIONS.get(platform, "americas")
        return await self._get(region, f"/riot/account/v1/accounts/by-puuid/{puuid}")

    async def summoner(self, puuid: str, platform: str) -> dict:
        return await self._get(platform, f"/lol/summoner/v4/summoners/by-puuid/{puuid}")

    async def ranks(self, puuid: str, platform: str) -> dict[str, dict]:
        """{'RANKED_SOLO_5x5': {...}, 'RANKED_FLEX_SR': {...}}"""
        entries = await self._get(platform, f"/lol/league/v4/entries/by-puuid/{puuid}")
        return {e.get("queueType", "?"): e for e in entries}

    async def top_masteries(self, puuid: str, platform: str, count: int = 3) -> list[dict]:
        return await self._get(
            platform, f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top", {"count": count}
        )

    async def live_game(self, puuid: str, platform: str) -> Optional[dict]:
        try:
            return await self._get(platform, f"/lol/spectator/v5/active-games/by-summoner/{puuid}")
        except NotFound:
            return None

    async def match_ids(self, puuid: str, platform: str, count: int = 5, start_time: Optional[int] = None) -> list[str]:
        params = {"start": 0, "count": count}
        if start_time:
            params["startTime"] = start_time
        region = REGIONS.get(platform, "americas")
        return await self._get(region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params)

    async def clash_tournaments(self, platform: str = DEFAULT_PLATFORM) -> list[dict]:
        """Torneos de Clash próximos o en curso del servidor."""
        return await self._get(platform, "/lol/clash/v1/tournaments")

    async def match(self, match_id: str) -> dict:
        platform = match_id.split("_", 1)[0].lower()
        region = REGIONS.get(platform, "americas")
        return await self._get(region, f"/lol/match/v5/matches/{match_id}")


client = RiotClient()
