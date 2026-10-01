"""Guardado seguro de archivos JSON (todo lo de la carpeta data/).

Problema que evita: si el bot se cierra, se corta la luz o Windows reinicia JUSTO mientras se
escribe un archivo, con `write_text` el archivo queda a medias (JSON roto) y se pierde todo
lo guardado (historial, canciones de cada uno, memoria...).

Cómo lo hace ("escritura atómica"):
1. Escribe el contenido completo en un archivo temporal al lado (`archivo.json.tmp`).
2. Fuerza a que llegue al disco (`fsync`).
3. Reemplaza el archivo viejo por el nuevo con `os.replace`, que es una sola operación del
   sistema: o queda el archivo viejo entero, o el nuevo entero. Nunca uno a medias.

Además:
- Un candado por archivo evita que dos hilos escriban el mismo archivo a la vez.
- En Windows, si el antivirus o el indexador tienen el archivo abierto un instante, `os.replace`
  puede fallar con PermissionError: se reintenta unas veces.
- Si al leer el JSON está roto (por ejemplo, de una versión vieja del bot), se aparta como
  `archivo.json.roto` para que puedas revisarlo y se sigue con un valor vacío.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("jsonio")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


_MISSING = object()


def load_json(path: Path, default: Any = _MISSING) -> Any:
    """Lee un JSON. Si no existe devuelve `default` (por defecto {}); si está roto, lo aparta y
    devuelve `default`. Se puede pasar default=None para distinguir "no existe" de "vacío"."""
    if default is _MISSING:
        default = {}
    path = Path(path)
    with _lock_for(path):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default
        except OSError as exc:
            log.warning("No se pudo leer %s: %s", path.name, exc)
            return default
        if not text.strip():
            return default
        try:
            return json.loads(text)
        except ValueError as exc:
            broken = path.with_name(path.name + ".roto")
            try:
                os.replace(path, broken)
                log.error("%s estaba dañado (%s); se apartó como %s y se empieza de cero", path.name, exc, broken.name)
            except OSError:
                log.error("%s está dañado (%s) y no se pudo apartar", path.name, exc)
            return default


def save_json(path: Path, data: Any, indent: int | None = 1) -> None:
    """Guarda `data` como JSON de forma atómica (nunca deja el archivo a medias)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    tmp = path.with_name(path.name + ".tmp")
    with _lock_for(path):
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))


def append_jsonl(path: Path, record: dict) -> None:
    """Añade una línea a un archivo JSONL (un JSON por línea). Si se corta a mitad, solo se
    pierde esa línea: `read_jsonl` ignora las líneas rotas."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _lock_for(path):
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    records = []
    with _lock_for(path):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue  # línea cortada por un cierre inesperado: se ignora
        except FileNotFoundError:
            pass
    return records
