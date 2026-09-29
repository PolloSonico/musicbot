"""Parche para PyCharacterAI 2.2.x: Character.AI rechaza su websocket con 403 (sep-2026).

La web actual (según las cabeceras reales de un navegador) abre el chat en wss://neo.character.ai/ws/:
- SIN cabecera Authorization, solo con Origin,
- con las cookies de la web: HTTP_AUTHORIZATION, web-next-auth (la sesión) y las de Cloudflare
  (__cf_bm, __cfwaitingroom_cai, GCLB, deployment-*), que se consiguen al visitar la web.

La librería solo manda HTTP_AUTHORIZATION, con huella fija de Firefox 135 y sin visitar la web.
`install()` reemplaza esa conexión por una que imita al navegador.
Si algún día la librería oficial lo arregla, este parche se puede borrar.
"""

import logging

import curl_cffi
from PyCharacterAI.exceptions import AuthenticationError, RequestError
from PyCharacterAI.requester import Requester

log = logging.getLogger("persona")

WS_URL = "wss://neo.character.ai/ws/"
WARMUP_URLS = ("https://character.ai/", "https://neo.character.ai/ping/")

_settings = {
    "impersonate": "firefox147",
    "warmup": True,
    "web_next_auth": "",
    "quote_token": True,
    "auth_header": False,
}
_original_connect = Requester._Requester__ws_connect_async


def _jar_cookies(session) -> dict:
    """Cookies que la web de Character.AI dejó en la sesión durante la visita previa."""
    cookies = {}
    try:
        for cookie in session.cookies.jar:
            if cookie.domain.lstrip(".").endswith("character.ai"):
                cookies[cookie.name] = cookie.value
    except Exception as exc:
        log.debug("No se pudieron leer las cookies de la sesión: %s", exc)
    return cookies


async def _ws_connect(self: Requester, token: str) -> None:
    await self.ensure_session()
    session = self._Requester__requester_session
    if not session:
        raise RequestError
    if self._Requester__ws:
        await self.ws_close_async()

    impersonate = _settings["impersonate"]
    web_next_auth = _settings["web_next_auth"]
    base_cookies = {"HTTP_AUTHORIZATION": f'"Token {token}"' if _settings["quote_token"] else f"Token {token}"}
    if web_next_auth:
        base_cookies["web-next-auth"] = web_next_auth

    if _settings["warmup"]:
        for url in WARMUP_URLS:
            try:
                await session.get(url, impersonate=impersonate, cookies=base_cookies)
            except Exception as exc:
                log.debug("Visita previa a %s falló: %s", url, exc)

    cookies = {**_jar_cookies(session), **base_cookies}
    headers = {"Origin": "https://character.ai"}
    if _settings["auth_header"]:
        headers["Authorization"] = f"Token {token}"

    try:
        self._Requester__ws = await session.ws_connect(
            url=WS_URL,
            impersonate=impersonate,
            headers=headers,
            cookies=cookies,
        )
    except curl_cffi.CurlError as exc:
        raise AuthenticationError(
            f"Character.AI rechazó el chat (cookies enviadas: {', '.join(sorted(cookies))}): {exc}"
        ) from exc
    if not self._Requester__ws:
        raise AuthenticationError("Character.AI rechazó el chat")


def install(**settings) -> None:
    unknown = set(settings) - set(_settings)
    if unknown:
        raise TypeError(f"Opciones desconocidas: {unknown}")
    _settings.update(settings)
    Requester._Requester__ws_connect_async = _ws_connect


def uninstall() -> None:
    Requester._Requester__ws_connect_async = _original_connect
