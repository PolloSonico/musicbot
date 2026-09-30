"""Historial de canciones pedidas por cada persona (data/canciones_por_usuario.json).

Se guarda por el ID de Discord (no cambia nunca) junto con el nombre de usuario de la cuenta
(@usuario), no el apodo del servidor, que la gente cambia seguido. El personaje lo usa para
saber qué música le gusta a cada uno ("pon algo que me pueda gustar", "¿qué música me gusta?").
"""

import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

import discord

log = logging.getLogger("persona")

DATA_FILE = Path(__file__).resolve().parent / "data" / "canciones_por_usuario.json"
MAX_PER_USER = 50  # canciones que se recuerdan por persona
MAX_PER_REQUEST = 5  # de una playlist larga solo se guardan las primeras
IN_CONTEXT = 30  # cuántas se le pasan a la IA (las más recientes)


def _load() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record(user: discord.abc.User, titles: list[str]) -> None:
    """Guarda las canciones que pidió alguien (las más nuevas al final)."""
    if not titles:
        return
    try:
        data = _load()
        entry = data.setdefault(str(user.id), {"canciones": []})
        entry["usuario"] = user.name  # nombre de la cuenta de Discord, no el apodo del servidor
        today = date.today().isoformat()
        entry["canciones"].extend({"titulo": title, "fecha": today} for title in titles[:MAX_PER_REQUEST])
        entry["canciones"] = entry["canciones"][-MAX_PER_USER:]
        DATA_FILE.parent.mkdir(exist_ok=True)
        DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        log.exception("No se pudo guardar el historial de canciones")  # nunca debe frenar la música


def titles(user: discord.abc.User) -> list[str]:
    """Todas las canciones guardadas de una persona (de la más vieja a la más nueva)."""
    entry = _load().get(str(user.id)) or {}
    return [song["titulo"] for song in entry.get("canciones", [])]


def summary(user: discord.abc.User, shown_as: Optional[str] = None) -> Optional[str]:
    """Texto corto con las canciones que pidió una persona, para el contexto de la IA."""
    entry = _load().get(str(user.id))
    if not entry or not entry.get("canciones"):
        return None
    songs = [song["titulo"] for song in reversed(entry["canciones"][-IN_CONTEXT:])]
    total = len(entry["canciones"])
    name = shown_as or user.display_name
    return (
        f"Canciones que pidió {name} (cuenta de Discord @{entry.get('usuario', user.name)}), "
        f"de la más reciente a la más antigua ({len(songs)} de {total} guardadas): " + "; ".join(songs)
    )
