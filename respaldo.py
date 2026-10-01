"""Copia de seguridad diaria de la carpeta data/ (memoria de cada persona, historial del Wrapped,
canciones pedidas, estado de la IA...).

- Una vez por día (y al arrancar, si hoy todavía no se hizo) crea backups/data_AAAA-MM-DD.zip.
- Guarda las últimas BACKUP_DAYS copias y borra las más viejas.
- No copia lo que se puede volver a descargar (data/datadragon/) ni archivos temporales.
- BACKUP_DIR en .env permite guardarlas en otra carpeta, por ejemplo una de OneDrive o Google Drive
  para tenerlas también en la nube. Si se pierde algo: cierra el bot, descomprime el zip en data/ y
  vuelve a arrancarlo.
"""

import logging
import os
import zipfile
from datetime import date
from pathlib import Path

log = logging.getLogger("respaldo")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "").strip() or BASE_DIR / "backups")
if not BACKUP_DIR.is_absolute():
    BACKUP_DIR = BASE_DIR / BACKUP_DIR
BACKUP_DAYS = max(1, int(os.getenv("BACKUP_DAYS", "14") or 14))
SKIP_DIRS = {"datadragon"}
SKIP_SUFFIXES = {".tmp"}


def today_backup() -> Path:
    return BACKUP_DIR / f"data_{date.today().isoformat()}.zip"


def make_backup() -> Path | None:
    """Crea la copia de hoy (si no existe) y borra las viejas. Devuelve la ruta, o None si no había nada."""
    target = today_backup()
    if target.exists() or not DATA_DIR.is_dir():
        return target if target.exists() else None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".zip.tmp")
    count = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(DATA_DIR.rglob("*")):
            rel = file.relative_to(DATA_DIR)
            if not file.is_file() or rel.parts[0] in SKIP_DIRS or file.suffix in SKIP_SUFFIXES:
                continue
            try:
                zf.write(file, rel.as_posix())
                count += 1
            except OSError as exc:  # un archivo que se está escribiendo justo ahora: se saltea
                log.warning("No se pudo copiar %s: %s", rel, exc)
    os.replace(tmp, target)
    log.info("Copia de seguridad creada: %s (%d archivos)", target, count)
    old = sorted(BACKUP_DIR.glob("data_*.zip"))[:-BACKUP_DAYS]
    for file in old:
        try:
            file.unlink()
        except OSError:
            pass
    return target
