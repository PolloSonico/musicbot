"""IA de respaldo: entra cuando Gemini se queda sin cupo (o no responde), así Lillia no "se duerme".

Funciona con cualquier proveedor compatible con la API de OpenAI. Por defecto está pensado para
Groq (https://console.groq.com → API Keys): plan gratis sin tarjeta, con modelos que hablan bien
español (GPT-OSS 120B, Qwen). En .env:

  BACKUP_AI_KEY=gsk_...                 (la clave; vacío = sin respaldo)
  BACKUP_AI_URL=https://api.groq.com/openai/v1
  BACKUP_AI_MODELS=openai/gpt-oss-120b,qwen/qwen3.8-27b,openai/gpt-oss-20b

Los proveedores dan de baja modelos cada tanto. Si TODOS los de BACKUP_AI_MODELS dejan de existir, el
bot pide al proveedor la lista de modelos que tiene hoy y elige solo los mejores (y lo avisa en el log).

Para usar otro proveedor (OpenRouter, Cerebras, Mistral...) cambia BACKUP_AI_URL y los modelos.

Diferencias con Gemini: no ve imágenes.
Groq informa en cada respuesta cuántos pedidos quedan en el día: se guardan para !cupo y !estado.
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger("persona")

KEY = os.getenv("BACKUP_AI_KEY", "").strip()
URL = (os.getenv("BACKUP_AI_URL", "").strip() or "https://api.groq.com/openai/v1").rstrip("/")
DEFAULT_MODELS = "openai/gpt-oss-120b,qwen/qwen3.8-27b,openai/gpt-oss-20b"
MODELS = [m.strip() for m in (os.getenv("BACKUP_AI_MODELS", "").strip() or DEFAULT_MODELS).split(",") if m.strip()]
# Para elegir modelos solo cuando los configurados ya no existen: de mejor a peor (para charlar en español).
PREFERRED = [r"gpt-oss-120b", r"qwen.?3", r"llama-4", r"llama-3\.3-70b", r"gpt-oss-20b", r"llama", r"mistral|gemma"]
NOT_CHAT = re.compile(r"guard|whisper|tts|playai|orpheus|compound|embed|vision|distil|audio|moderation", re.I)
DISCOVER_EVERY = 3600  # como mucho una consulta de la lista de modelos por hora
THINK_RE = re.compile(r"<think>.*?</think>", re.S)  # algunos modelos (Qwen) "piensan en voz alta"


def pick_models(ids: list[str], exclude: set = frozenset(), limit: int = 3) -> list[str]:
    """Los mejores modelos para charlar de una lista de ids, según PREFERRED."""
    chat = [i for i in ids if i and i not in exclude and not NOT_CHAT.search(i)]

    def rank(model: str) -> tuple[int, str]:
        for index, pattern in enumerate(PREFERRED):
            if re.search(pattern, model, re.I):
                return index, model
        return len(PREFERRED), model

    ranked = sorted(chat, key=rank)
    return [m for m in ranked if rank(m)[0] < len(PREFERRED)][:limit]


def provider_name(url: str = URL) -> str:
    host = re.sub(r"^https?://(api\.)?", "", url).split("/")[0]
    return host.split(".")[0].title() if host else "Respaldo"


def _seconds(value: Optional[str]) -> Optional[float]:
    """'2m59.56s', '7.66s', '1h2m' o '30' -> segundos."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    total, found = 0.0, False
    for amount, unit in re.findall(r"([\d.]+)(ms|h|m|s)", value):
        found = True
        total += float(amount) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    return total if found else None


class BackupAI:
    def __init__(self, key: str = KEY, url: str = URL, models: Optional[list[str]] = None) -> None:
        self.key = key
        self.url = url
        self.models = list(models or MODELS)
        self.provider = provider_name(url)
        self.cooldowns: dict[str, float] = {}
        self.dead: set[str] = set()  # modelos que el proveedor ya no tiene
        self.quota: dict[str, dict] = {}  # modelo -> {"limite", "quedan", "actualizado"} (de los encabezados)
        self.used_today: dict[str, int] = {}
        self.tokens_today: dict[str, int] = {}
        self._day = ""
        self._session: Optional[aiohttp.ClientSession] = None
        self._discovered_at = 0.0

    @classmethod
    def from_env(cls) -> Optional["BackupAI"]:
        return cls() if KEY else None

    def _roll_day(self) -> None:
        today = datetime.now(ZoneInfo("UTC")).date().isoformat()  # Groq renueva el cupo diario en UTC
        if today != self._day:
            self._day = today
            self.used_today.clear()
            self.tokens_today.clear()

    def usable_models(self) -> list[str]:
        now = time.time()
        return [m for m in self.models if m not in self.dead and self.cooldowns.get(m, 0) <= now]

    def all_dead(self) -> bool:
        return bool(self.models) and all(m in self.dead for m in self.models)

    def can_discover(self) -> bool:
        return self.all_dead() and time.time() - self._discovered_at > DISCOVER_EVERY

    def available(self) -> bool:
        return bool(self.key) and (bool(self.usable_models()) or self.can_discover())

    async def _discover(self) -> list[str]:
        """Pide al proveedor sus modelos actuales (GET /models) y suma los mejores para charlar."""
        self._discovered_at = time.time()
        try:
            async with self._session.get(f"{self.url}/models", headers={"Authorization": f"Bearer {self.key}"}) as resp:
                if resp.status != 200:
                    log.warning("Respaldo (%s): no se pudo pedir la lista de modelos (%s)", self.provider, resp.status)
                    return []
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Respaldo (%s): no se pudo pedir la lista de modelos: %s", self.provider, exc)
            return []
        ids = [m.get("id", "") for m in data.get("data", []) if m.get("active", True) is not False]
        picked = pick_models(ids, exclude=self.dead)
        if picked:
            self.models += [m for m in picked if m not in self.models]
            log.warning("Respaldo (%s): los modelos de BACKUP_AI_MODELS ya no existen; uso %s. Actualiza BACKUP_AI_MODELS "
                        "en .env (o bórralo para usar los de siempre)", self.provider, ", ".join(picked))
        else:
            log.error("Respaldo (%s): no encontré ningún modelo para charlar en la lista del proveedor", self.provider)
        return picked

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _messages(system: str, contents: list) -> list[dict]:
        """Historial en formato Gemini -> mensajes en formato OpenAI (solo texto)."""
        messages = [{"role": "system", "content": system}]
        for turn in contents:
            role = turn.get("role") if isinstance(turn, dict) else getattr(turn, "role", "user")
            parts = turn.get("parts", []) if isinstance(turn, dict) else getattr(turn, "parts", [])
            texts = []
            for part in parts:
                text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                if text:
                    texts.append(text)
                elif not isinstance(part, dict):
                    texts.append("[mandó una imagen que ahora no puedes ver]")
            if texts:
                messages.append({"role": "assistant" if role == "model" else "user", "content": "\n".join(texts)})
        return messages

    async def chat(self, system: str, contents: list, max_tokens: int = 900) -> Optional[str]:
        """Respuesta del primer modelo que conteste, o None."""
        if not self.key:
            return None
        self._roll_day()
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        messages = self._messages(system, contents)
        if self.can_discover():
            await self._discover()
        for model in self.usable_models():
            payload = {"model": model, "messages": messages, "temperature": 0.9, "max_tokens": max_tokens}
            if "gpt-oss" in model:  # piensan antes de responder: que piensen poco y les alcance para contestar
                payload["reasoning_effort"] = "low"
                payload["max_tokens"] = max(max_tokens * 2, 2048)
            started = time.monotonic()
            try:
                async with self._session.post(
                    f"{self.url}/chat/completions", json=payload,
                    headers={"Authorization": f"Bearer {self.key}"},
                ) as resp:
                    self._read_quota(model, resp.headers)
                    if resp.status == 200:
                        data = await resp.json()
                    elif resp.status == 429:
                        wait = _seconds(resp.headers.get("retry-after")) or 60
                        self.cooldowns[model] = time.time() + max(wait, 5)
                        log.warning("Respaldo (%s): límite alcanzado en %s por %.0fs", self.provider, model, wait)
                        continue
                    elif resp.status in (400, 404) and re.search(r"model_not_found|decommissioned|does not exist|"
                                                                 r"not found|no longer supported", body := await resp.text(), re.I):
                        self.dead.add(model)
                        log.warning("Respaldo (%s): el modelo %s ya no existe; se prueba otro", self.provider, model)
                        continue
                    elif resp.status in (401, 403):
                        log.error("Respaldo (%s): la clave BACKUP_AI_KEY no es válida (%s)", self.provider, resp.status)
                        self.cooldowns[model] = time.time() + 3600
                        return None
                    else:
                        self.cooldowns[model] = time.time() + 60
                        detail = (await resp.text())[:160]
                        log.warning("Respaldo (%s): %s respondió %s: %s", self.provider, model, resp.status, detail)
                        continue
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Respaldo (%s): error de conexión con %s: %s", self.provider, model, exc)
                self.cooldowns[model] = time.time() + 60
                continue
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            text = THINK_RE.sub("", text).strip()
            if not text:
                continue
            self.used_today[model] = self.used_today.get(model, 0) + 1
            self.tokens_today[model] = self.tokens_today.get(model, 0) + (data.get("usage") or {}).get("total_tokens", 0)
            log.info("Respaldo (%s): %s respondió en %.1fs", self.provider, model, time.monotonic() - started)
            return text
        if self.can_discover():  # se dieron de baja todos en esta vuelta: se buscan otros y se reintenta una vez
            if await self._discover():
                return await self.chat(system, contents, max_tokens)
        return None

    def _read_quota(self, model: str, headers) -> None:
        """Groq (y otros) informan el límite diario y lo que queda en los encabezados."""
        limit, remaining = headers.get("x-ratelimit-limit-requests"), headers.get("x-ratelimit-remaining-requests")
        if limit and remaining and limit.isdigit() and remaining.isdigit():
            self.quota[model] = {"limite": int(limit), "quedan": int(remaining), "actualizado": time.time()}

    def report(self) -> list[tuple[str, int, int, Optional[int], Optional[int]]]:
        """[(modelo, pedidos hoy, tokens hoy, límite diario, quedan)]."""
        self._roll_day()
        rows = []
        for model in self.models:
            if model in self.dead:
                continue
            q = self.quota.get(model, {})
            rows.append((model, self.used_today.get(model, 0), self.tokens_today.get(model, 0),
                         q.get("limite"), q.get("quedan")))
        return rows
