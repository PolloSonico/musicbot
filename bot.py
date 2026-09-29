import asyncio
import base64
import logging
import logging.handlers
import os
import socket
import subprocess
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("PREFIX", "!").strip() or "!"
# Puerto local usado solo como "candado" para que no corran dos copias del bot a la vez
# (dos copias con el mismo token se pelean por el canal de voz y la música se corta).
INSTANCE_PORT = 47821


def setup_logging() -> None:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # necesario para leer comandos con prefijo
        intents.voice_states = True
        super().__init__(
            command_prefix=commands.when_mentioned_or(PREFIX),
            intents=intents,
            help_command=commands.DefaultHelpCommand(no_category="Otros"),
            case_insensitive=True,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, replied_user=False),
        )

    async def setup_hook(self) -> None:
        await self.load_extension("persona")
        await self.load_extension("music")

    async def on_ready(self) -> None:
        logging.info("Conectado como %s (id %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name=f"{PREFIX}help")
        )

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        from persona import say  # los avisos de error también los dice el personaje

        who = ctx.author.display_name
        if isinstance(error, commands.MissingRequiredArgument):
            situation = f"{who} usó el comando '{ctx.invoked_with}' pero le faltó escribir {error.param.name}."
            info = f"Falta un argumento: `{error.param.name}`. Usa `{PREFIX}help {ctx.command}`."
        elif isinstance(error, commands.BadArgument):
            situation = f"{who} usó el comando '{ctx.invoked_with}' con un dato que no entiendes."
            info = f"Argumento inválido. Usa `{PREFIX}help {ctx.command}`."
        elif isinstance(error, commands.CheckFailure):
            situation = f"{who} intentó usar el comando '{ctx.invoked_with}' pero no se pudo: {error}"
            info = str(error)
        else:
            logging.exception("Error en el comando %s", ctx.command, exc_info=error)
            situation = f"{who} usó el comando '{ctx.invoked_with}' y algo salió mal por dentro (un error del bot)."
            info = "Ocurrió un error inesperado. Revisa `logs/bot.log`."
        await say(self, ctx.channel, situation, info)


ALREADY_RUNNING = (
    "Ya hay otra copia del bot funcionando (probablemente la tarea automática){extra}.\n"
    "Dos copias a la vez se pelean por el canal de voz: la música se corta y los mensajes salen repetidos.\n"
    "Ejecuta windows\\detener_bot.bat y vuelve a intentarlo."
)


def other_bot_processes() -> list[int]:
    """Otras copias de bot.py en Windows (también las versiones viejas, que no usan el candado)."""
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -like 'python*' -and $_.CommandLine -match '(^|[ \\\\])bot\\.py' "
        "} | ForEach-Object { $_.ProcessId }"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()  # evita problemas con las comillas
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-EncodedCommand", encoded],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        logging.warning("No se pudo comprobar si hay otras copias del bot: %s", exc)
        return []
    mine = {os.getpid(), os.getppid()}  # el lanzador del .venv es nuestro proceso padre
    return [int(pid) for pid in result.stdout.split() if pid.isdigit() and int(pid) not in mine]


def acquire_single_instance() -> socket.socket:
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", INSTANCE_PORT))
    except OSError:
        raise SystemExit(ALREADY_RUNNING.format(extra=""))
    others = other_bot_processes()
    if others:
        raise SystemExit(ALREADY_RUNNING.format(extra=f" (procesos {', '.join(map(str, others))})"))
    return lock


async def main() -> None:
    setup_logging()
    _lock = acquire_single_instance()  # se libera sola al cerrar el bot
    if not TOKEN or TOKEN == "pega_aqui_tu_token":
        raise SystemExit("Falta DISCORD_TOKEN en el archivo .env")
    bot = MusicBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
