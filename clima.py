"""Clima y pronóstico con Open-Meteo (https://open-meteo.com): gratis, sin clave ni registro.

Cuando alguien pregunta por el clima ("¿este finde llueve en Berazategui?"), se busca el lugar
(geocodificación) y se trae el tiempo actual y el pronóstico de 10 días. Eso se le pasa a Lillia como
información real, y ella contesta por los días que le preguntaron.

Configuración (.env, opcional):
  WEATHER_DEFAULT_PLACE=Berazategui   lugar que se usa si la pregunta no dice dónde
  WEATHER_COUNTRY=AR                  país preferido cuando hay varios lugares con el mismo nombre
"""

import asyncio
import logging
import os
import re
import time
from datetime import date, datetime
from typing import Optional

import aiohttp

log = logging.getLogger("clima")

DEFAULT_PLACE = os.getenv("WEATHER_DEFAULT_PLACE", "").strip()
COUNTRY = os.getenv("WEATHER_COUNTRY", "AR").strip().upper()
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DAYS = 10
CACHE_SECONDS = 900
WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

WEATHER_RE = re.compile(
    r"\b(llov\w*|lluev\w*|lluvi\w*|llovizna\w*|clima|pron[oó]stico|temperatura|grados|calor|fr[ií]o|tormenta\w*|"
    r"niev\w*|nevar|granizo|viento|humedad|sensaci[oó]n t[eé]rmica|nublado|soleado|paraguas|va a estar lindo|"
    r"c[oó]mo est[aá] el (?:d[ií]a|tiempo)|qu[eé] tiempo)\b",
    re.I,
)
# "en Berazategui", "para Mar del Plata", "de Córdoba": el lugar es lo que sigue, hasta un corte.
PLACE_RE = re.compile(
    r"\b(?:en|para|por|de)\s+((?:[A-ZÁÉÍÓÚÑ][\wáéíóúñü.'-]*|del?|la|las|los|el|san|santa)(?:\s+(?:[A-ZÁÉÍÓÚÑ][\wáéíóúñü.'-]*|"
    r"del?|la|las|los|el))*)"
)
NOT_PLACES = {"lillia", "lol", "league", "discord", "el", "la", "los", "las", "de", "del"}

# Códigos de tiempo de la OMM que usa Open-Meteo.
CODES = {
    0: "despejado", 1: "mayormente despejado", 2: "parcialmente nublado", 3: "nublado", 45: "niebla", 48: "niebla con escarcha",
    51: "llovizna débil", 53: "llovizna", 55: "llovizna intensa", 56: "llovizna helada", 57: "llovizna helada intensa",
    61: "lluvia débil", 63: "lluvia", 65: "lluvia fuerte", 66: "lluvia helada", 67: "lluvia helada fuerte",
    71: "nevada débil", 73: "nevada", 75: "nevada fuerte", 77: "granos de nieve", 80: "chaparrones débiles",
    81: "chaparrones", 82: "chaparrones fuertes", 85: "chaparrones de nieve", 86: "chaparrones de nieve fuertes",
    95: "tormenta", 96: "tormenta con granizo", 99: "tormenta fuerte con granizo",
}

_cache: dict[str, tuple[float, object]] = {}
_session: Optional[aiohttp.ClientSession] = None


async def close() -> None:
    if _session and not _session.closed:
        await _session.close()


async def _get(url: str, params: dict):
    global _session
    key = url + repr(sorted(params.items()))
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    async with _session.get(url, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    _cache[key] = (time.time(), data)
    return data


def place_in(text: str) -> str:
    """El lugar nombrado en la pregunta ("... en Mar del Plata?"), o el lugar por defecto."""
    for match in PLACE_RE.finditer(text):
        place = re.sub(r"\s+(?:del?|la|las|los|el)$", "", match.group(1).strip(" .,'-"))
        if place and place.lower() not in NOT_PLACES and place[0].isupper():
            return place
    # Sin mayúscula ("en berazategui"): lo que sigue a "en" hasta el final o un signo.
    loose = re.search(r"\ben\s+([a-záéíóúñü][\wáéíóúñü' -]{2,40}?)\s*(?:[?!.,]|$)", text, re.I)
    if loose:
        place = loose.group(1).strip()
        if not re.match(r"(?:el|la|los|las|este|esta|estos|un|una|mi|tu|su|estos?)\b", place, re.I):
            return place
    return DEFAULT_PLACE


async def locate(place: str) -> Optional[dict]:
    data = await _get(GEO_URL, {"name": place, "count": "5", "language": "es", "format": "json"})
    results = data.get("results") or []
    if not results:
        return None
    return next((r for r in results if r.get("country_code") == COUNTRY), results[0])


def _day_name(day: date, today: date) -> str:
    delta = (day - today).days
    label = "hoy" if delta == 0 else "mañana" if delta == 1 else WEEKDAYS[day.weekday()]
    return f"{label} {day:%d/%m}"


def format_forecast(where: str, data: dict, today: Optional[date] = None) -> str:
    today = today or date.today()
    lines = [f"Clima real en {where} (Open-Meteo, consultado ahora; hoy es {WEEKDAYS[today.weekday()]} {today:%d/%m}):"]
    cur = data.get("current") or {}
    if cur:
        lines.append(
            f"- Ahora: {cur.get('temperature_2m', '?')} °C (sensación {cur.get('apparent_temperature', '?')} °C), "
            f"{CODES.get(cur.get('weather_code'), 'sin datos')}, humedad {cur.get('relative_humidity_2m', '?')}%, "
            f"viento {cur.get('wind_speed_10m', '?')} km/h")
    daily = data.get("daily") or {}
    for i, day_text in enumerate(daily.get("time", [])):
        day = datetime.strptime(day_text, "%Y-%m-%d").date()

        def value(name: str):
            values = daily.get(name) or []
            return values[i] if i < len(values) and values[i] is not None else "?"

        lines.append(
            f"- {_day_name(day, today)}: {CODES.get(value('weather_code'), 'sin datos')}, "
            f"{value('temperature_2m_min')} a {value('temperature_2m_max')} °C, "
            f"probabilidad de lluvia {value('precipitation_probability_max')}% ({value('precipitation_sum')} mm), "
            f"viento hasta {value('wind_speed_10m_max')} km/h")
    lines.append("Responde por los días que te preguntan con estos datos (el fin de semana es sábado y domingo), con tu "
                 "personalidad pero sin dejar afuera el dato. No inventes nada que no esté acá.")
    return "\n".join(lines)


async def context_for(text: str) -> str:
    """Bloque para la IA con el clima del lugar de la pregunta, o '' (sin lugar, o no se pudo consultar)."""
    place = place_in(text)
    if not place:
        return ("La persona pregunta por el clima pero no dijo de qué lugar (y no hay un lugar por defecto): "
                "pregúntale de dónde.")
    try:
        spot = await asyncio.wait_for(locate(place), 10)
        if spot is None:
            return f"No encontraste ningún lugar llamado \"{place}\" para ver el clima: dilo y pide que lo aclaren."
        data = await asyncio.wait_for(_get(FORECAST_URL, {
            "latitude": str(spot["latitude"]), "longitude": str(spot["longitude"]), "timezone": "auto",
            "forecast_days": str(DAYS),
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                     "precipitation_sum,wind_speed_10m_max",
        }), 10)
    except Exception as exc:
        log.warning("No se pudo consultar el clima de %s: %s", place, exc or type(exc).__name__)
        return ""
    where = ", ".join(x for x in (spot.get("name"), spot.get("admin1"), spot.get("country")) if x)
    return format_forecast(where, data)
