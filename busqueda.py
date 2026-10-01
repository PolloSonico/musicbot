"""Búsqueda en internet con Tavily (https://tavily.com): da a Lillia datos actuales
que no sabe (noticias, precios, fechas...).

El plan gratis de Tavily da 1000 búsquedas por mes. Cada búsqueda trae un resumen y los fragmentos de
las páginas encontradas; eso se le pasa a Lillia como "información real" para que responda con datos
actuales (builds del parche, noticias, precios, resultados...).

Configuración (.env):
  TAVILY_API_KEY=tvly-...        la clave (vacío = sin búsqueda en internet)
  TAVILY_MONTHLY_LIMIT=1000      búsquedas por mes que se permite gastar (para no pasarse del plan)

Se cuenta lo gastado en data/busqueda_estado.json. La misma búsqueda no se repite durante 10 minutos.
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp

from jsonio import load_json, save_json

log = logging.getLogger("busqueda")

KEY = os.getenv("TAVILY_API_KEY", "").strip()
MONTHLY_LIMIT = int(os.getenv("TAVILY_MONTHLY_LIMIT", "1000") or 1000)
URL = "https://api.tavily.com/search"
STATE_FILE = Path(__file__).resolve().parent / "data" / "busqueda_estado.json"
CACHE_SECONDS = 600
MAX_RESULTS = 5
SNIPPET = 500  # caracteres de cada resultado que se le pasan a la IA

_cache: dict[str, tuple[float, dict]] = {}
_paused_until = 0.0
_session: Optional[aiohttp.ClientSession] = None
_key_ok = True


def _month() -> str:
    return f"{datetime.now():%Y-%m}"


def usage() -> tuple[int, int]:
    """(búsquedas usadas este mes, límite)."""
    state = load_json(STATE_FILE, {})
    used = state.get("usadas", 0) if isinstance(state, dict) and state.get("mes") == _month() else 0
    return used, MONTHLY_LIMIT


def _count() -> None:
    used, _ = usage()
    save_json(STATE_FILE, {"mes": _month(), "usadas": used + 1})


def enabled() -> bool:
    return bool(KEY) and _key_ok


def available() -> bool:
    used, limit = usage()
    return enabled() and used < limit and time.time() >= _paused_until


async def close() -> None:
    if _session and not _session.closed:
        await _session.close()


async def search(query: str, topic: str = "general", time_range: Optional[str] = None) -> Optional[dict]:
    """Busca en internet. Devuelve {"answer": str, "results": [{"title", "url", "content"}]} o None.
    topic: "general" o "news". time_range: None, "day", "week", "month" o "year"."""
    global _session, _paused_until, _key_ok
    query = " ".join(query.split())[:380]
    if not query or not available():
        return None
    cache_key = f"{topic}|{time_range}|{query.lower()}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    body = {"query": query, "search_depth": "basic", "max_results": MAX_RESULTS, "include_answer": True, "topic": topic}
    if time_range:
        body["time_range"] = time_range
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    try:
        async with _session.post(URL, json=body, headers={"Authorization": f"Bearer {KEY}"}) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
            elif resp.status in (401, 403):
                _key_ok = False
                log.error("Tavily: la clave TAVILY_API_KEY no es válida (%s). Revisa el .env", resp.status)
                return None
            elif resp.status in (432, 433):  # se acabó el plan del mes
                _paused_until = time.time() + 24 * 3600
                log.warning("Tavily: se acabaron las búsquedas del plan (%s). Se vuelve a probar mañana", resp.status)
                return None
            elif resp.status == 429:
                _paused_until = time.time() + 60
                log.warning("Tavily: demasiadas búsquedas seguidas, se espera un minuto")
                return None
            else:
                log.warning("Tavily respondió %s: %s", resp.status, (await resp.text())[:160])
                return None
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("Tavily: sin conexión (%s)", exc or type(exc).__name__)
        return None
    _count()
    result = {
        "answer": (data.get("answer") or "").strip(),
        "results": [{"title": r.get("title", ""), "url": r.get("url", ""), "content": (r.get("content") or "").strip()}
                    for r in data.get("results", []) if r.get("content")],
    }
    _cache[cache_key] = (time.time(), result)
    used, limit = usage()
    log.info("Búsqueda en internet (%d de %d este mes): %s", used, limit, query[:90])
    return result


def as_context(query: str, found: dict) -> str:
    """Los resultados como texto para la IA."""
    lines = [f"Resultados de buscar en internet AHORA ({datetime.now():%d/%m/%Y %H:%M}) \"{query}\":"]
    if found.get("answer"):
        lines.append(f"Resumen: {found['answer']}")
    for r in found.get("results", [])[:MAX_RESULTS]:
        lines.append(f"- {r['title']} ({r['url']}): {r['content'][:SNIPPET]}")
    lines.append("Responde con estos datos (son más nuevos que lo que recuerdas). Si no alcanzan para contestar, "
                 "dilo en vez de inventar.")
    return "\n".join(lines)


async def context_for(query: str, topic: str = "general", time_range: Optional[str] = None) -> str:
    found = await search(query, topic, time_range)
    if not found or not (found["answer"] or found["results"]):
        return ""
    return as_context(query, found)
