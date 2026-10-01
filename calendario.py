"""Calendario de eventos (personajes/eventos.json): fechas especiales que Lillia tiene presentes.

Cada evento tiene:
  "nombre":    cómo se llama ("Mundial 2026: Fase Suiza").
  "desde"/"hasta": fechas, inclusive. "AAAA-MM-DD" para una fecha concreta, o "MM-DD" para algo
               que se repite todos los años (Navidad, Halloween...). "hasta" es opcional (un solo día).
  "contexto":  lo que Lillia sabe/siente sobre el evento mientras dura (se le pasa a la IA en cada
               charla, así lo menciona cuando viene al caso).
  "anunciar":  true = el primer día del evento lo anuncia sola en el canal de eventos (opcional).
  "buscar":    true = al anunciarlo busca en internet datos del día (partidos, resultados...) (opcional).

El archivo se vuelve a leer solo cuando lo cambias: no hace falta reiniciar el bot.

Además, eventos.py busca solo cada 2 semanas las fechas del MSI y de las finales de las ligas que
todavía no están en el calendario, y las guarda en data/eventos_esports.json (mismo formato). Si un
evento ya está en personajes/eventos.json, manda el tuyo.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("eventos")

CALENDAR_FILE = Path(__file__).resolve().parent / "personajes" / "eventos.json"
AUTO_FILE = Path(__file__).resolve().parent / "data" / "eventos_esports.json"
CLASH_FILE = Path(__file__).resolve().parent / "data" / "eventos_clash.json"  # lo escribe riot.py


@dataclass
class Event:
    name: str
    start: date
    end: date
    context: str
    announce: bool
    search: bool

    @property
    def key(self) -> str:
        return f"{self.name}:{self.start.isoformat()}"


_cache: dict[Path, tuple[float, list[dict]]] = {}


def _read(path: Path) -> list[dict]:
    """Eventos de un archivo, releyéndolo solo si cambió."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cached = _cache.get(path)
    if cached is None or cached[0] != mtime:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            events = data.get("eventos", data) if isinstance(data, dict) else data
            cached = (mtime, [e for e in events if isinstance(e, dict)])
        except (OSError, ValueError) as exc:
            log.error("%s tiene un error (%s); se ignora hasta que lo arregles", path.name, exc)
            cached = (mtime, [])
        _cache[path] = cached
    return cached[1]


def manual_events() -> list[dict]:
    return _read(CALENDAR_FILE)


def _raw_events() -> list[dict]:
    return manual_events() + _read(AUTO_FILE) + _read(CLASH_FILE)


def _parse(text: str, year: int) -> date:
    parts = [int(p) for p in text.strip().split("-")]
    return date(*parts) if len(parts) == 3 else date(year, parts[0], parts[1])


def events_on(day: Optional[date] = None) -> list[Event]:
    """Eventos activos en `day` (hoy por defecto)."""
    day = day or date.today()
    active = []
    for raw in _raw_events():
        try:
            start_text = raw["desde"]
            end_text = raw.get("hasta") or start_text
            yearly = len(start_text.split("-")) == 2
            candidates = [day.year - 1, day.year] if yearly else [0]
            for year in candidates:
                start = _parse(start_text, year)
                end = _parse(end_text, year)
                if end < start:  # cruza el año (ej: 12-31 a 01-01)
                    end = _parse(end_text, year + 1)
                if start <= day <= end:
                    active.append(Event(
                        name=raw.get("nombre", "Evento"), start=start, end=end,
                        context=raw.get("contexto", ""), announce=bool(raw.get("anunciar")),
                        search=bool(raw.get("buscar")),
                    ))
                    break
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Evento mal escrito en eventos.json (%s): %s", exc, raw)
    return active


def active_events_text(day: Optional[date] = None) -> str:
    """Texto para el contexto de la IA (vacío si no hay nada especial hoy)."""
    day = day or date.today()
    events = events_on(day)
    if not events:
        return ""
    lines = []
    for event in events:
        if event.start == event.end:
            when = "hoy"
        elif day == event.start:
            when = f"empieza hoy y dura hasta el {event.end:%d/%m}"
        elif day == event.end:
            when = "hoy es el último día"
        else:
            when = f"del {event.start:%d/%m} al {event.end:%d/%m}"
        lines.append(f"- {event.name} ({when}): {event.context}")
    return (
        f"Fechas especiales de estos días (hoy es {day:%d/%m/%Y}). Menciónalas solo si viene al caso o si "
        "te preguntan, sin repetirlas en cada mensaje:\n" + "\n".join(lines)
    )


def upcoming(days: int = 30, day: Optional[date] = None) -> list[tuple[date, Event]]:
    """Eventos que empiezan en los próximos `days` días (para !eventos)."""
    day = day or date.today()
    seen, result = set(), []
    for offset in range(days + 1):
        current = day + timedelta(days=offset)
        for event in events_on(current):
            if event.key not in seen and (event.start >= day or offset == 0):
                seen.add(event.key)
                result.append((event.start, event))
    return result
