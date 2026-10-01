"""Letras de canciones como CONTEXTO para la IA (no se publican en el chat).

Las saca de LRCLIB (https://lrclib.net), una base de letras gratuita y sin clave. Sirven para que
Lillia sepa de qué trata de verdad la canción que suena: al presentarla, en las curiosidades y
cuando alguien le pregunta por la canción. Se le indica que no copie la letra (como mucho una
línea corta), porque las letras tienen derechos de autor.

- Se buscan una sola vez por canción y se guardan en memoria (las últimas CACHE_SIZE).
- Si la canción es instrumental, no se encuentra, o LRCLIB tarda, simplemente no hay letra:
  nunca frena la música ni las respuestas.
"""

import asyncio
import logging
import re
from collections import OrderedDict
from typing import Optional

import aiohttp

log = logging.getLogger("letras")

API = "https://lrclib.net/api/search"
USER_AGENT = "LilliaMusicBot/1.0 (bot de Discord privado)"
TIMEOUT = 6
CACHE_SIZE = 100
MAX_CHARS = 2500  # cuánto de la letra se le pasa a la IA (una canción típica entra entera)
INSTRUMENTAL = "(La canción es instrumental: no tiene letra.)"

_cache: "OrderedDict[str, Optional[str]]" = OrderedDict()
_pending: dict[str, asyncio.Task] = {}
_session: Optional[aiohttp.ClientSession] = None


# ---------- Artista y título a partir del título de YouTube ----------

def artist_of(title: str, uploader: str = "") -> str:
    """'Kesha - TiK ToK (Official Video)' -> 'Kesha'. Si el título no lo dice, el canal de YouTube."""
    if " - " in title:
        artist = title.split(" - ", 1)[0]
    elif " – " in title:
        artist = title.split(" – ", 1)[0]
    else:
        artist = uploader
    artist = re.sub(r"\s*-\s*Topic$|VEVO$|\s*\((?:Official|Oficial)[^)]*\)|\s*\[[^\]]*\]", "", artist or "", flags=re.I)
    artist = re.sub(r"\s+(ft\.?|feat\.?|x|&|,)\s+.*$", "", artist, flags=re.I)  # solo el artista principal
    return artist.strip(" -–\"'") or ""


def song_name(title: str) -> str:
    """Título sin la basura típica de YouTube: (Official Video), [Lyrics], | HD..."""
    cleaned = re.sub(r"\s*[\(\[][^\)\]]*(official|oficial|video|lyric|letra|audio|hd|4k|mv|visualizer)[^\)\]]*[\)\]]",
                     "", title, flags=re.I)
    cleaned = re.sub(r"\s*\|.*$", "", cleaned)
    return cleaned.strip() or title


def track_only(title: str) -> str:
    """Solo el nombre de la canción: 'Kesha - TiK ToK (Official Video)' -> 'TiK ToK'."""
    name = song_name(title)
    for sep in (" - ", " – "):
        if sep in name:
            name = name.split(sep, 1)[1]
            break
    name = re.sub(r"\s*[\(\[](?:feat|ft|with|prod|from)\b.*$", "", name, flags=re.I)  # (ft. ...) hasta el final
    name = re.sub(r"\s+(?:feat|ft)\.?\s+.*$", "", name, flags=re.I)
    return name.strip(" \"'“”") or title


# ---------- Búsqueda ----------

async def _get(params: dict) -> list:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT), headers={"User-Agent": USER_AGENT}
        )
    async with _session.get(API, params=params) as resp:
        if resp.status == 404:
            return []
        resp.raise_for_status()
        data = await resp.json(content_type=None)
        return data if isinstance(data, list) else []


def _pick(results: list, duration: Optional[int]) -> Optional[dict]:
    """El resultado más parecido: con letra y, si se sabe, de duración parecida (±15 s)."""
    useful = [r for r in results if r.get("plainLyrics") or r.get("instrumental")]
    if duration:
        close = [r for r in useful if r.get("duration") and abs(r["duration"] - duration) <= 15]
        if close:
            return min(close, key=lambda r: abs(r["duration"] - duration))
        # Si no hay ninguna con duración parecida puede ser un video con intro larga: se acepta
        # igual el primer resultado, pero solo si la diferencia no es enorme.
        useful = [r for r in useful if not r.get("duration") or abs(r["duration"] - duration) <= 90]
    return useful[0] if useful else None


async def _fetch(title: str, uploader: str, duration: Optional[int]) -> Optional[str]:
    artist = artist_of(title, uploader)
    track = track_only(title)
    attempts = []
    if artist and track and artist.lower() != track.lower():
        attempts.append({"track_name": track, "artist_name": artist})
    attempts.append({"q": f"{artist} {track}".strip() if artist else song_name(title)})
    for params in attempts:
        try:
            best = _pick(await _get(params), duration)
        except Exception as exc:
            log.info("No se pudo buscar la letra de '%s': %s", title, exc)
            raise  # error de conexión: no se guarda "sin letra", se reintenta la próxima vez
        if best:
            if best.get("instrumental") and not best.get("plainLyrics"):
                return INSTRUMENTAL
            lyrics = re.sub(r"\n{3,}", "\n\n", best["plainLyrics"].strip())
            log.info("Letra encontrada para '%s': %s - %s", title, best.get("artistName"), best.get("trackName"))
            header = f"{best.get('artistName', artist)} - {best.get('trackName', track)}"
            return f"{header}\n{lyrics}"
    log.info("No hay letra para '%s'", title)
    return None


async def get_lyrics(title: str, uploader: str = "", duration: Optional[int] = None,
                     url: str = "", timeout: float = TIMEOUT) -> Optional[str]:
    """Letra de la canción (o None). Nunca lanza errores."""
    key = url or title
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    task = _pending.get(key)
    if task is None:
        task = asyncio.create_task(_fetch(title, uploader, duration))
        _pending[key] = task

        def done(t: asyncio.Task, key: str = key) -> None:
            _pending.pop(key, None)
            if not t.cancelled() and t.exception() is None:
                _cache[key] = t.result()
                while len(_cache) > CACHE_SIZE:
                    _cache.popitem(last=False)

        task.add_done_callback(done)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except Exception:
        return None  # sigue buscando en segundo plano: la próxima vez ya estará


def prefetch(title: str, uploader: str = "", duration: Optional[int] = None, url: str = "") -> None:
    """Empieza a buscar la letra sin esperar (para tenerla lista cuando haga falta)."""
    key = url or title
    if key in _cache or key in _pending:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.ensure_future(get_lyrics(title, uploader, duration, url, timeout=TIMEOUT + 1))


def for_ai(lyrics: Optional[str], max_chars: int = MAX_CHARS) -> str:
    """Bloque de contexto para la IA con la letra (y la regla de no copiarla)."""
    if not lyrics:
        return ""
    if lyrics == INSTRUMENTAL:
        return INSTRUMENTAL
    text = lyrics if len(lyrics) <= max_chars else lyrics[:max_chars].rsplit("\n", 1)[0] + "\n[...]"
    return (
        "Letra real de la canción (es solo para que entiendas de qué trata y cómo se siente; NO la copies "
        "ni la recites: como mucho cita una línea corta si viene muy al caso):\n" + text
    )


async def close() -> None:
    if _session and not _session.closed:
        await _session.close()
