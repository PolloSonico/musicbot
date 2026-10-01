"""Configuración de las pruebas.

Las pruebas NO tocan tus datos reales: antes de empezar se copia el código del bot (los .py y la
carpeta personajes/) a una carpeta temporal, y todo lo que las pruebas escriben en data/ va ahí.
Tampoco se conectan a internet: Discord, Gemini, Riot, Spotify y el cliente de League se simulan.

Correrlas: windows\\probar_codigo.bat  (o `python -m pytest tests` desde la carpeta del bot).
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="lillia_pruebas_"))

for file in PROJECT.glob("*.py"):
    shutil.copy2(file, SANDBOX / file.name)
shutil.copytree(PROJECT / "personajes", SANDBOX / "personajes")
(SANDBOX / "data").mkdir()

# Variables de entorno de mentira (las de verdad están en .env, que acá no se lee).
os.environ.update({
    "DISCORD_TOKEN": "prueba", "GEMINI_API_KEY": "prueba", "RIOT_API_KEY": "RGAPI-prueba",
    "BACKUP_AI_KEY": "", "OWNER_ALERTS": "false", "LCU_MAYHEM": "false",
})
os.chdir(SANDBOX)
sys.path.insert(0, str(SANDBOX))
# bot.py carga .env al importarse: en la copia no hay .env, así que usa las variables de arriba.


@pytest.fixture
def data_dir() -> Path:
    """Carpeta data/ de la copia de pruebas, vacía al empezar cada prueba."""
    folder = SANDBOX / "data"
    for item in folder.iterdir():
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    return folder


def pytest_sessionfinish(session, exitstatus):
    os.chdir(PROJECT)  # en Windows no se puede borrar la carpeta en la que uno está parado
    shutil.rmtree(SANDBOX, ignore_errors=True)
