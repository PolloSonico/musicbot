"""Memoria por persona (data/memoria_personas.json).

Además de la memoria de la charla de cada canal (que se va olvidando), Lillia guarda datos
duraderos que cada persona le cuenta de sí misma: su main, su rol, su rango, cómo quiere que la
llamen... La IA los pide guardar con la orden oculta [[RECORDAR: dato]] y después se le pasan en
el contexto cuando esa persona habla (o la mencionan).

Se guarda por ID de Discord (no cambia aunque cambien el apodo). Cada uno puede ver lo que Lillia
recuerda de él con !quesabes y borrarlo con !olvidame.
"""

import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

import discord

from jsonio import load_json, save_json

log = logging.getLogger("persona")

DATA_FILE = Path(__file__).resolve().parent / "data" / "memoria_personas.json"
MAX_FACTS = 15  # datos por persona; al pasarse, se olvida el más viejo
MAX_FACT_CHARS = 160

# Por si la IA intenta guardar algo que no corresponde (no debería, el prompt lo prohíbe).
BLOCKED_RE = re.compile(
    r"contraseñ|password|clave|token|tarjeta|cbu|cvu|dni|pasaporte|tel[eé]fono|direcci[oó]n|"
    r"\b\d{6,}\b|@\w+\.\w+",
    re.I,
)


def _load() -> dict:
    data = load_json(DATA_FILE, {})
    return data if isinstance(data, dict) else {}


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def add(user: discord.abc.User, fact: str) -> bool:
    """Guarda un dato de una persona. False si estaba vacío, repetido o no se permite."""
    fact = re.sub(r"\s+", " ", fact).strip(" .\"'“”")[:MAX_FACT_CHARS]
    if len(fact) < 3 or BLOCKED_RE.search(fact):
        return False
    try:
        data = _load()
        entry = data.setdefault(str(user.id), {"datos": []})
        entry["usuario"] = user.name
        known = {_norm(f["texto"]) for f in entry["datos"]}
        if _norm(fact) in known:
            return False
        entry["datos"].append({"texto": fact, "fecha": date.today().isoformat()})
        entry["datos"] = entry["datos"][-MAX_FACTS:]
        save_json(DATA_FILE, data)
        log.info("Memoria: %s -> %s", user.name, fact)
        return True
    except Exception:
        log.exception("No se pudo guardar la memoria de %s", user)
        return False


def facts(user: discord.abc.User) -> list[str]:
    entry = _load().get(str(user.id)) or {}
    return [f["texto"] for f in entry.get("datos", [])]


def forget(user: discord.abc.User, what: str = "") -> int:
    """Borra todo lo que se recuerda de alguien, o solo los datos que contienen `what`.
    Devuelve cuántos datos se borraron."""
    data = _load()
    entry = data.get(str(user.id))
    if not entry:
        return 0
    before = len(entry.get("datos", []))
    if what.strip():
        needle = _norm(what)
        entry["datos"] = [f for f in entry["datos"] if needle not in _norm(f["texto"])]
        removed = before - len(entry["datos"])
        if not entry["datos"]:
            data.pop(str(user.id))
    else:
        data.pop(str(user.id))
        removed = before
    if removed:
        save_json(DATA_FILE, data)
    return removed


def summary(user: discord.abc.User) -> Optional[str]:
    """Texto para el contexto de la IA, o None si no se recuerda nada de esa persona."""
    known = facts(user)
    if not known:
        return None
    return f"Lo que recuerdas de {user.display_name} (@{user.name}): " + "; ".join(known) + "."
