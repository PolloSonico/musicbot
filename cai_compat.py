"""Parche para PyCharacterAI 2.2.x: Character.AI rechaza su websocket con 403 (sep-2026).

La web actual de Character.AI:
- se identifica como Chrome (la librería fuerza "firefox135" en el websocket, ignorando `impersonate`),
- visita la web antes de abrir el websocket (así obtiene las cookies de Cloudflare),
- manda las cabeceras Origin y Authorization en el websocket.

`install()` reemplaza la conexión del websocket de la librería por una que hace todo eso.
Si algún día la librería oficial lo arregla, este parche se puede borrar.
"""

import logging

import curl_cffi
from PyCharacterAI.exceptions import AuthenticationError, RequestError
from PyCharacterAI.requester import Requester

log = logging.getLogger("persona")

WS_URL = "wss://neo.character.ai/ws/"
WARMUP_URLS = ("https://character.ai/", "https://neo.character.ai/ping/")

_settings = {"impersonate": "chrome", "warmup": True}
_original_connect = Requester._Requester__ws_connect_async


async def _ws_connect(self: Requester, token: str) -> None:
    await self.ensure_session()
    session = self._Requester__requester_session
    if not session:
        raise RequestError
    if self._Requester__ws:
        await self.ws_close_async()

    impersonate = _settings["impersonate"]
    headers = {"Origin": "https://character.ai", "Authorization": f"Token {token}"}

    if _settings["warmup"]:
        for url in WARMUP_URLS:
            try:
                await session.get(url, impersonate=impersonate, headers={"Authorization": f"Token {token}"})
            except Exception as exc:
                log.debug("Warm-up %s falló: %s", url, exc)

    try:
        self._Requester__ws = await session.ws_connect(
            url=WS_URL,
            impersonate=impersonate,
            headers=headers,
            cookies={"HTTP_AUTHORIZATION": f"Token {token}"},
        )
    except curl_cffi.CurlError as exc:
        raise AuthenticationError(f"Character.AI rechazó el chat: {exc}") from exc
    if not self._Requester__ws:
        raise AuthenticationError("Character.AI rechazó el chat")


def install(impersonate: str = "chrome", warmup: bool = True) -> None:
    _settings.update(impersonate=impersonate, warmup=warmup)
    Requester._Requester__ws_connect_async = _ws_connect


def uninstall() -> None:
    Requester._Requester__ws_connect_async = _original_connect
