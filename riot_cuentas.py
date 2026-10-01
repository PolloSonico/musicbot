"""Cuentas de League vinculadas a cada miembro de Discord (data/riot_cuentas.json).

Cada uno vincula la suya con !vincular Nombre#TAG. Se guarda por ID de Discord:
  puuid, nombre, tag, plataforma, servidor (de Discord, para los resúmenes post-partida),
  ultima_partida (para detectar partidas nuevas), y un resumen en caché del rango y los
  campeones con más maestría (se refresca cada pocas horas), que Lillia usa en la charla.

No importa nada de persona.py: así persona.py lo puede usar sin importaciones circulares.
"""

from pathlib import Path
from typing import Optional

import discord

from jsonio import load_json, save_json

DATA_FILE = Path(__file__).resolve().parent / "data" / "riot_cuentas.json"


def load() -> dict:
    data = load_json(DATA_FILE, {})
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    save_json(DATA_FILE, data)


def get(user_id: int) -> Optional[dict]:
    return load().get(str(user_id))


def put(user_id: int, account: dict) -> None:
    data = load()
    data[str(user_id)] = account
    save(data)


def update(user_id: int, **fields) -> None:
    data = load()
    if str(user_id) in data:
        data[str(user_id)].update(fields)
        save(data)


def remove(user_id: int) -> bool:
    data = load()
    if data.pop(str(user_id), None) is None:
        return False
    save(data)
    return True


def riot_id(account: dict) -> str:
    return f"{account.get('nombre', '?')}#{account.get('tag', '?')}"


def summary(user: discord.abc.User) -> Optional[str]:
    """Texto para el contexto de la IA (sin pedir nada a Riot: usa lo guardado)."""
    account = get(user.id)
    if not account:
        return None
    parts = [f"{user.display_name} vinculó su cuenta de League: {riot_id(account)} ({account.get('servidor_lol', 'LAS')})."]
    if account.get("rango_texto"):
        parts.append(f"Rango: {account['rango_texto']}.")
    if account.get("mains"):
        parts.append(f"Campeones con más maestría: {', '.join(account['mains'])}.")
    if account.get("ultima_resumen"):
        parts.append(f"Su última partida: {account['ultima_resumen']}.")
    return " ".join(parts)
