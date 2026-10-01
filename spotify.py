"""Links de Spotify -> canciones para buscar en YouTube.

Spotify no deja reproducir su audio (tiene DRM), así que el bot hace lo mismo que todos los bots de
música: lee del link el artista, el título y la duración, y busca esa canción en YouTube recién
cuando le toca sonar (así una playlist de 100 canciones se encola al instante).

Acepta links de canción, álbum y playlist (open.spotify.com/..., spotify.link/... o spotify:track:...).

Cómo lee los datos, en orden:
1. Si en .env hay SPOTIFY_CLIENT_ID y SPOTIFY_CLIENT_SECRET (app gratis en
   https://developer.spotify.com/dashboard), usa la API oficial: es lo más confiable.
2. Si no, la página pública de "embed" de Spotify (la misma que se ve cuando un link se incrusta en
   una web). No necesita nada, pero si Spotify cambia esa página puede dejar de funcionar.
3. Para una canción suelta, como último recurso, el título de la página
   ("Canción - song and lyrics by Artista | Spotify").
"""

import base64
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger("spotify")

LINK_RE = re.compile(
    r"(?:https?://)?(?:open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(?:embed/)?|spotify:)"
    r"(track|album|playlist)[/:]([A-Za-z0-9]{22})",
    re.I,
)
SHORT_RE = re.compile(r"https?://(?:spotify\.link|spoti\.fi)/\S+", re.I)
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
MAX_TRACKS = 100
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36",
    "Accept-Language": "es-419,es;q=0.9,en;q=0.8",
}


class SpotifyError(RuntimeError):
    """No se pudo leer el link de Spotify (no es culpa de yt-dlp)."""


@dataclass
class SpotifyTrack:
    artist: str
    title: str
    duration: Optional[int]  # segundos

    @property
    def query(self) -> str:
        """Lo que se busca en YouTube."""
        return f"{self.artist} - {self.title}" if self.artist else self.title


def is_spotify(text: str) -> bool:
    return bool(LINK_RE.search(text) or SHORT_RE.match(text.strip()))


_token: tuple[str, float] = ("", 0.0)


async def _session_get(session: aiohttp.ClientSession, url: str, **kwargs):
    async with session.get(url, **kwargs) as resp:
        resp.raise_for_status()
        if "json" in resp.headers.get("Content-Type", ""):
            return await resp.json()
        return await resp.text()


# ---------- 1. API oficial (opcional) ----------

async def _api_token(session: aiohttp.ClientSession) -> Optional[str]:
    global _token
    if not (CLIENT_ID and CLIENT_SECRET):
        return None
    if _token[0] and _token[1] > time.time() + 60:
        return _token[0]
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    async with session.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {auth}"},
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    _token = (data["access_token"], time.time() + data.get("expires_in", 3600))
    return _token[0]


def _from_api_track(t: dict) -> Optional[SpotifyTrack]:
    if not t or not t.get("name"):
        return None
    artists = ", ".join(a["name"] for a in t.get("artists", [])[:2] if a.get("name"))
    ms = t.get("duration_ms")
    return SpotifyTrack(artists, t["name"], round(ms / 1000) if ms else None)


async def _from_api(session: aiohttp.ClientSession, kind: str, sid: str) -> Optional[tuple[str, list[SpotifyTrack]]]:
    token = await _api_token(session)
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    base = "https://api.spotify.com/v1"
    if kind == "track":
        t = await _session_get(session, f"{base}/tracks/{sid}", headers=headers)
        track = _from_api_track(t)
        return (track.query if track else "", [track] if track else [])
    meta = await _session_get(session, f"{base}/{kind}s/{sid}", headers=headers, params={"fields": "name"} if kind == "playlist" else None)
    name = meta.get("name", "")
    url = f"{base}/{kind}s/{sid}/tracks?limit=50" if kind == "album" else f"{base}/playlists/{sid}/tracks?limit=100"
    tracks: list[SpotifyTrack] = []
    while url and len(tracks) < MAX_TRACKS:
        page = await _session_get(session, url, headers=headers)
        for item in page.get("items", []):
            track = _from_api_track(item.get("track", item) if kind == "playlist" else item)
            if track:
                tracks.append(track)
        url = page.get("next")
    return name, tracks[:MAX_TRACKS]


# ---------- 2. Página de "embed" ----------

def _walk(node, found: list) -> None:
    """Busca en el JSON de la página la lista de canciones ("trackList") o la canción suelta."""
    if isinstance(node, dict):
        if isinstance(node.get("trackList"), list):
            found.append(("lista", node))
        elif node.get("type") == "track" and (node.get("name") or node.get("title")) and node.get("artists"):
            found.append(("cancion", node))
        for value in node.values():
            _walk(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk(value, found)


def _duration(node: dict) -> Optional[int]:
    ms = node.get("duration") or node.get("duration_ms")
    if isinstance(ms, dict):
        ms = ms.get("totalMilliseconds")
    return round(ms / 1000) if isinstance(ms, (int, float)) and ms > 0 else None


def _parse_embed(page: str) -> Optional[tuple[str, list[SpotifyTrack]]]:
    match = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not match:
        return None
    found: list = []
    _walk(json.loads(match.group(1)), found)
    for kind, node in found:
        if kind == "lista":
            tracks = []
            for item in node["trackList"][:MAX_TRACKS]:
                title = (item.get("title") or item.get("name") or "").strip()
                artist = (item.get("subtitle") or "").replace(" ", " ").strip()
                artist = ", ".join(a.strip() for a in artist.split(",")[:2])
                if title:
                    tracks.append(SpotifyTrack(artist, title, _duration(item)))
            name = node.get("name") or node.get("title") or ""
            if tracks:
                return name, tracks
    for kind, node in found:
        if kind == "cancion":
            artists = ", ".join(a.get("name", "") for a in node["artists"][:2] if isinstance(a, dict))
            track = SpotifyTrack(artists, node.get("name") or node.get("title"), _duration(node))
            return track.query, [track]
    return None


def _parse_title(page: str) -> Optional[SpotifyTrack]:
    """'TiK ToK - song and lyrics by Kesha | Spotify' (o en español: 'canción y letra de')."""
    match = re.search(r"<title>(.*?)</title>", page, re.S)
    if not match:
        return None
    title = html.unescape(match.group(1)).strip()
    m = re.match(r"(.+?) - (?:song(?: and lyrics)? by|canción y letra de|canción de) (.+?) \| Spotify", title, re.I)
    return SpotifyTrack(m.group(2).strip(), m.group(1).strip(), None) if m else None


# ---------- Entrada principal ----------

async def resolve(link: str) -> tuple[str, list[SpotifyTrack]]:
    """(nombre del álbum/playlist o de la canción, canciones). Lanza SpotifyError si no pudo leerlo."""
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
        if SHORT_RE.match(link.strip()):  # spotify.link/...: se sigue la redirección
            async with session.get(link.strip(), allow_redirects=True) as resp:
                link = str(resp.url)
        match = LINK_RE.search(link)
        if not match:
            raise SpotifyError("no es un link de canción, álbum o playlist de Spotify")
        kind, sid = match.group(1).lower(), match.group(2)

        try:
            result = await _from_api(session, kind, sid)
            if result and result[1]:
                return result
        except Exception as exc:
            log.warning("API de Spotify falló con %s/%s: %s (se prueba sin API)", kind, sid, exc)

        try:
            page = await _session_get(session, f"https://open.spotify.com/embed/{kind}/{sid}")
            result = _parse_embed(page) if isinstance(page, str) else None
            if result and result[1]:
                return result
        except Exception as exc:
            log.warning("No se pudo leer el embed de Spotify %s/%s: %s", kind, sid, exc)

        if kind == "track":
            try:
                page = await _session_get(session, f"https://open.spotify.com/track/{sid}")
                track = _parse_title(page) if isinstance(page, str) else None
                if track:
                    return track.query, [track]
            except Exception as exc:
                log.warning("No se pudo leer la página de Spotify %s: %s", sid, exc)
    raise SpotifyError(f"no pude leer las canciones del link de Spotify ({kind})")
