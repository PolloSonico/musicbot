"""Bots ayudantes: otras cuentas de bot que solo ponen audio, para tener música en varios canales
de voz del mismo servidor a la vez (Discord no deja que un bot esté en dos canales de un servidor).

Lillia sigue siendo la única que habla y la que recibe los comandos. Cuando alguien pide música
desde un canal de voz donde ella no está y ella ya está ocupada en otro, la pone un ayudante libre.
Los mensajes ("Reproduciendo", botones, comentarios) los sigue mandando Lillia.

Configuración (.env):  HELPER_TOKENS=token_del_ayudante_1,token_del_ayudante_2
Cada ayudante tiene que estar invitado al servidor con permisos de ver canales, conectar y hablar.
No necesitan ningún "intent" especial.
"""

import asyncio
import logging
import os
import re
from typing import Optional

import discord

log = logging.getLogger("ayudantes")

TOKENS = [t.strip() for t in re.split(r"[,\s]+", os.getenv("HELPER_TOKENS", "")) if t.strip()]


class Helper(discord.Client):
    """Un ayudante: se conecta a Discord solo para entrar a canales de voz y reproducir."""

    def __init__(self, number: int) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, status=discord.Status.idle,
                         activity=discord.CustomActivity(name="Esperando a Lillia 🌸"))
        self.number = number

    async def on_ready(self) -> None:
        log.info("Ayudante %d listo: %s (en %d servidor(es))", self.number, self.user, len(self.guilds))

    async def set_song(self, title: Optional[str]) -> None:
        try:
            if title:
                await self.change_presence(status=discord.Status.online,
                                           activity=discord.Activity(type=discord.ActivityType.listening, name=title[:128]))
            else:
                await self.change_presence(status=discord.Status.idle, activity=discord.CustomActivity(name="Esperando a Lillia 🌸"))
        except Exception as exc:
            log.debug("No se pudo cambiar el estado del ayudante: %s", exc)


helpers: list[Helper] = []
_tasks: list[asyncio.Task] = []
_started = False


async def start_all() -> None:
    """Arranca los ayudantes de HELPER_TOKENS (en el mismo programa que Lillia). Si un token no sirve,
    se avisa en el log y se sigue sin ese ayudante."""
    global _started
    if _started:
        return
    _started = True
    for number, token in enumerate(TOKENS, start=1):
        helper = Helper(number)

        async def run(helper: Helper = helper, token: str = token) -> None:
            try:
                await helper.start(token)
            except discord.LoginFailure:
                log.error("El token del ayudante %d no es válido (revisa HELPER_TOKENS en .env)", helper.number)
            except Exception:
                log.exception("El ayudante %d se cayó", helper.number)
            finally:
                if helper in helpers:
                    helpers.remove(helper)

        helpers.append(helper)
        _tasks.append(asyncio.create_task(run()))


async def close_all() -> None:
    global _started
    for helper in list(helpers):
        try:
            await helper.close()
        except Exception:
            pass
    helpers.clear()
    _started = False


def ready() -> list[Helper]:
    return [h for h in helpers if h.is_ready()]
