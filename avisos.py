"""Avisos privados (DM) al dueño del bot cuando pasa algo que conviene saber sin abrir los logs:

- La IA (Gemini) se quedó sin cupo: a qué hora y qué día vuelve.
- La API key de Gemini dejó de funcionar.
- yt-dlp falló 3 veces seguidas (casi siempre YouTube cambió algo y hay que actualizarlo).

El dueño es OWNER_ID del .env; si está vacío, el dueño de la aplicación en el Discord Developer
Portal (o el dueño del equipo, si la app es de un equipo).

Cada aviso tiene una "clave" y no se repite hasta una fecha (por ejemplo, el de cupo no se repite
hasta que el cupo vuelve). Eso se guarda en data/avisos.json, así un reinicio no repite avisos.
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord

from jsonio import load_json, save_json

log = logging.getLogger("avisos")

STATE_FILE = Path(__file__).resolve().parent / "data" / "avisos.json"
OWNER_ID = int((re.findall(r"\d+", os.getenv("OWNER_ID", "")) or ["0"])[0])
ENABLED = os.getenv("OWNER_ALERTS", "true").strip().lower() not in ("0", "false", "no")

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

_owner: Optional[discord.abc.User] = None
_tasks: set[asyncio.Task] = set()


def fecha_hora(timestamp: float) -> str:
    """'hoy a las 04:01', 'mañana (jueves 01/10) a las 04:01' o 'el jueves 01/10 a las 04:01',
    en la hora del PC donde corre el bot."""
    when = datetime.fromtimestamp(timestamp)
    days = (when.date() - datetime.now().date()).days
    hour = when.strftime("%H:%M")
    if days == 0:
        return f"hoy a las {hour}"
    if days == 1:
        return f"mañana ({DIAS[when.weekday()]} {when:%d/%m}) a las {hour}"
    return f"el {DIAS[when.weekday()]} {when:%d/%m} a las {hour}"


async def get_owner(bot: discord.Client) -> Optional[discord.abc.User]:
    global _owner
    if _owner is not None:
        return _owner
    try:
        if OWNER_ID:
            _owner = bot.get_user(OWNER_ID) or await bot.fetch_user(OWNER_ID)
        else:
            app = await bot.application_info()
            _owner = app.team.owner if app.team and app.team.owner else app.owner
    except discord.HTTPException as exc:
        log.warning("No se pudo saber quién es el dueño del bot: %s", exc)
    return _owner


def _silenced_until(key: str) -> float:
    state = load_json(STATE_FILE, {})
    return float(state.get(key, 0)) if isinstance(state, dict) else 0.0


def _silence(key: str, until: float) -> None:
    state = load_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    now = time.time()
    state = {k: v for k, v in state.items() if v > now}  # limpia avisos viejos
    state[key] = until
    save_json(STATE_FILE, state)


def notify(bot: discord.Client, key: str, text: str, quiet_until: Optional[float] = None) -> None:
    """Manda `text` por DM al dueño, salvo que el aviso `key` ya se haya mandado y siga
    silenciado. `quiet_until`: hasta cuándo no repetirlo (por defecto, 6 horas)."""
    if not ENABLED:
        return
    now = time.time()
    if _silenced_until(key) > now:
        return
    _silence(key, quiet_until if quiet_until and quiet_until > now else now + 6 * 3600)

    async def send() -> None:
        owner = await get_owner(bot)
        if owner is None:
            log.warning("Aviso sin enviar (no se encontró al dueño): %s", text)
            return
        try:
            await owner.send(text[:2000])
            log.info("Aviso enviado al dueño (%s)", key)
        except discord.HTTPException as exc:
            log.warning("No se pudo mandar el aviso por DM (¿tienes los mensajes privados cerrados?): %s", exc)

    try:
        task = asyncio.get_running_loop().create_task(send())
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def clear(key: str) -> None:
    """Permite que el aviso `key` se vuelva a mandar (por ejemplo, cuando el problema se arregló)."""
    if _silenced_until(key):
        _silence(key, 0)
