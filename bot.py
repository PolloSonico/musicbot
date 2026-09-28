import asyncio
import logging
import logging.handlers
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("PREFIX", "!").strip() or "!"


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
        )

    async def setup_hook(self) -> None:
        await self.load_extension("music")

    async def on_ready(self) -> None:
        logging.info("Conectado como %s (id %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name=f"{PREFIX}help")
        )

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Falta un argumento: `{error.param.name}`. Usa `{PREFIX}help {ctx.command}`.")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(f"Argumento inválido. Usa `{PREFIX}help {ctx.command}`.")
            return
        if isinstance(error, commands.CheckFailure):
            await ctx.send(str(error))
            return
        logging.exception("Error en el comando %s", ctx.command, exc_info=error)
        await ctx.send("Ocurrió un error inesperado. Revisa `logs/bot.log`.")


async def main() -> None:
    setup_logging()
    if not TOKEN or TOKEN == "pega_aqui_tu_token":
        raise SystemExit("Falta DISCORD_TOKEN en el archivo .env")
    bot = MusicBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
