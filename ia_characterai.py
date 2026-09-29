"""Proveedor de IA: Character.AI (no oficial, vía PyCharacterAI + parche cai_compat).

Desde sep-2026 Character.AI rechaza el chat (403) incluso desde el navegador; se deja aquí
por si vuelve a funcionar. Se activa con AI_PROVIDER=characterai.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import aiohttp

from PyCharacterAI import get_client
from PyCharacterAI.exceptions import ActionError

import cai_compat

log = logging.getLogger("persona")

DATA_DIR = Path(__file__).resolve().parent / "data"
CHATS_FILE = DATA_DIR / "cai_chats.json"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    return default if not value else value in ("1", "true", "si", "sí", "yes", "y")


def describe_error(exc: BaseException) -> str:
    """Texto del error incluyendo la causa real (la librería la esconde tras mensajes genéricos)."""
    parts = []
    while exc is not None and len(parts) < 4:
        parts.append(f"{type(exc).__name__}: {exc}")
        exc = exc.__cause__ or exc.__context__
    return " <- ".join(parts)


class CharacterAIBackend:
    provider = "Character.AI"

    def __init__(self, token: str, character_id: str) -> None:
        self.token = token
        self.character_id = character_id
        self.impersonate = os.getenv("CAI_IMPERSONATE", "").strip() or "firefox147"
        self.web_next_auth = os.getenv("CAI_WEB_NEXT_AUTH", "").strip()
        cai_compat.install(
            impersonate=self.impersonate,
            warmup=_env_bool("CAI_WARMUP", True),
            web_next_auth=self.web_next_auth,
            quote_token=_env_bool("CAI_QUOTE_TOKEN", True),
        )
        self.client = None
        self.character = None
        try:
            self.chats: dict[str, str] = json.loads(CHATS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.chats = {}
        self._lock = asyncio.Lock()
        self._broken = False

    @property
    def name(self) -> str:
        return self.character.name if self.character else "Personaje"

    @property
    def description(self) -> str:
        return (self.character.title or "") if self.character else ""

    async def avatar(self) -> Optional[bytes]:
        avatar = getattr(self.character, "avatar", None)
        if not avatar or not avatar.get_file_name():
            return None
        url = avatar.get_url().replace("webp=true", "webp=false")
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return await resp.read()

    def available(self) -> bool:
        return True

    def available_again_at(self) -> Optional[float]:
        return None

    def _save_chats(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        CHATS_FILE.write_text(json.dumps(self.chats, indent=2), encoding="utf-8")

    def reset(self, key: str) -> None:
        self.chats.pop(key, None)
        self._save_chats()

    async def close(self) -> None:
        client, self.client = self.client, None
        if client:
            try:
                await client.close_session()
            except Exception:
                pass

    async def _ensure_client(self) -> None:
        if self._broken:
            await self.close()
            self._broken = False
        if self.client is None:
            self.client = await get_client(
                token=self.token, impersonate=self.impersonate, web_next_auth=self.web_next_auth
            )
            if self.character is None:
                self.character = await self.client.character.fetch_character_info(self.character_id)

    async def start(self) -> None:
        async with self._lock:
            try:
                await self._ensure_client()
            except Exception as exc:
                self._broken = True
                raise RuntimeError(describe_error(exc)) from exc

    async def ask(
        self, key: str, text: str, context: str = "", wait: bool = True, fast: bool = False
    ) -> Optional[str]:
        if not wait and self._lock.locked():
            return None
        async with self._lock:
            try:
                return await self._send(key, text)
            except asyncio.CancelledError:
                self._broken = True  # la conexión quedó a medias, se rehace en el próximo mensaje
                raise

    async def _send(self, key: str, text: str) -> Optional[str]:
        for attempt in (1, 2):
            try:
                await self._ensure_client()
                chat_id = self.chats.get(key)
                if chat_id is None:
                    chat, _ = await self.client.chat.create_chat(self.character_id, greeting=False)
                    chat_id = self.chats[key] = chat.chat_id
                    self._save_chats()
                turn = await self.client.chat.send_message(self.character_id, chat_id, text)
                candidate = turn.get_primary_candidate()
                if candidate is None or candidate.is_filtered or not candidate.text.strip():
                    return None
                return candidate.text.strip()
            except Exception as exc:
                log.warning("Error con Character.AI (intento %d): %s", attempt, describe_error(exc))
                self._broken = True
                if attempt == 2 and isinstance(exc, ActionError):
                    self.reset(key)  # el chat pudo haberse borrado: la próxima vez se crea otro
        return None
