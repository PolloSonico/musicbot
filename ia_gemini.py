"""Proveedor de IA: Google Gemini (API oficial, tiene plan gratuito).

- Elige solo el mejor modelo "Flash" disponible (o los de GEMINI_MODEL, en orden).
- El plan gratis tiene límites por minuto y por día POR MODELO: si un modelo se agota,
  pasa al siguiente; si se agotan todos, `available()` devuelve False hasta que se renueve
  el cupo, y el bot sigue funcionando con los textos fijos (la música nunca espera a la IA).
- Guarda la conversación de cada canal (últimos mensajes) en data/historial.json.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from google import genai
from google.genai import errors, types

from personaje import Character

log = logging.getLogger("persona")
logging.getLogger("google_genai").setLevel(logging.WARNING)  # evita mensajes internos de la librería

DATA_DIR = Path(__file__).resolve().parent / "data"
HISTORY_FILE = DATA_DIR / "historial.json"
STATE_FILE = DATA_DIR / "gemini_estado.json"
MAX_TURNS = 40  # mensajes que recuerda por canal
MODEL_TIMEOUT = 20  # segundos máximos por modelo en una charla
FAST_MODEL_TIMEOUT = 6  # segundos máximos por modelo en un comentario de la música
FALLBACK_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]
# El cupo diario gratis de Gemini se renueva a medianoche, hora del Pacífico.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")
MODEL_RE = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash(-lite)?$")

# Configuraciones de "pensamiento" a probar (menos pensamiento = más rápido y gasta menos cupo).
# No todos los modelos aceptan todas; se prueba en orden y se recuerda cuál funcionó.
THINKING_OPTIONS = [
    types.ThinkingConfig(thinking_level="low"),
    types.ThinkingConfig(thinking_budget=0),
    None,
]


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _next_quota_reset() -> float:
    now = datetime.now(QUOTA_TZ)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    return tomorrow.timestamp()


def _cooldown_for(exc: errors.APIError) -> float:
    """Hasta cuándo no usar un modelo que devolvió 429 (límite alcanzado)."""
    text = json.dumps(exc.details, ensure_ascii=False) if exc.details else str(exc)
    if re.search(r"per ?day|PerDay|daily", text, re.I):
        return _next_quota_reset()
    match = re.search(r'retryDelay"?\s*:\s*"?(\d+(?:\.\d+)?)s', text)
    delay = float(match.group(1)) if match else 60.0
    return time.time() + max(delay, 5.0)


def build_system_prompt(character: Character, language: str) -> str:
    return (
        f"{character.prompt}\n\n"
        "## Cómo participar en el chat\n"
        f"- Estás en un servidor de Discord y eres {character.name}. Nunca rompas el personaje "
        "ni digas que eres una IA o un modelo de lenguaje.\n"
        "- Los mensajes de la gente llegan como \"Nombre: mensaje\". Varias personas pueden "
        "hablar en el mismo canal; dirígete a ellas por su nombre cuando venga bien.\n"
        "- Los textos entre dobles paréntesis ((así)) son avisos automáticos del sistema sobre la "
        "música (qué canción suena, quién la pidió, errores...). Reacciona a ellos en personaje. "
        "Tú NUNCA escribas texto entre dobles paréntesis ni inventes avisos.\n"
        "- Si te preguntan qué canción suena, qué hay en la cola o qué pasó con la música, usa SOLO "
        "la sección \"Lo que está pasando ahora\". Puedes contar lo que sepas de la canción real "
        "(artista, de qué trata la letra), pero si no la conoces, no inventes: dilo con tu estilo.\n"
        f"- Responde siempre en {language}, con mensajes cortos (1 a 4 frases) salvo que te pidan "
        "algo largo. No empieces tus mensajes con tu nombre.\n"
        "- Puedes usar *acciones entre asteriscos* y algún emoji, sin abusar."
    )


class GeminiBackend:
    provider = "Gemini"

    def __init__(self, api_key: str, character: Character, language: str, models: list[str]) -> None:
        self.client = genai.Client(api_key=api_key)
        self.character = character
        self.system_prompt = build_system_prompt(character, language)
        self.preferred = models
        self.models: list[str] = []
        self.history: dict[str, list[dict]] = _load(HISTORY_FILE)
        state = _load(STATE_FILE)
        self.cooldowns: dict[str, float] = state.get("cooldowns", {})
        self.thinking: dict[str, int] = state.get("thinking", {})
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def name(self) -> str:
        return self.character.name

    @property
    def description(self) -> str:
        return self.character.description

    async def avatar(self) -> Optional[bytes]:
        return self.character.avatar

    async def start(self) -> None:
        if self.preferred:
            self.models = self.preferred
        else:
            self.models = await self._discover_models() or FALLBACK_MODELS
        log.info("Gemini usará los modelos (en orden): %s", ", ".join(self.models))

    async def _discover_models(self) -> list[str]:
        found: list[tuple[float, bool, str]] = []
        try:
            async for model in await self.client.aio.models.list():
                name = (model.name or "").removeprefix("models/")
                match = MODEL_RE.match(name)
                actions = model.supported_actions or []
                if match and (not actions or "generateContent" in actions):
                    found.append((float(match.group(1)), bool(match.group(2)), name))
        except errors.APIError as exc:
            if exc.code in (400, 401, 403):
                raise RuntimeError(f"La API key de Gemini no es válida ({exc.code} {exc.message})") from exc
            log.warning("No se pudo listar los modelos de Gemini: %s", exc)
            return []
        # Primero los Flash normales (del más nuevo al más viejo), después los Flash-Lite.
        found.sort(key=lambda m: (m[1], -m[0]))
        return [name for _, _, name in found]

    def available(self) -> bool:
        now = time.time()
        return any(self.cooldowns.get(m, 0) <= now for m in self.models or self.preferred or FALLBACK_MODELS)

    def available_again_at(self) -> Optional[float]:
        pending = [self.cooldowns.get(m, 0) for m in self.models]
        return min(pending) if pending and min(pending) > time.time() else None

    def reset(self, key: str) -> None:
        self.history.pop(key, None)
        _save(HISTORY_FILE, self.history)

    def _save_state(self) -> None:
        now = time.time()
        self.cooldowns = {m: t for m, t in self.cooldowns.items() if t > now}
        _save(STATE_FILE, {"cooldowns": self.cooldowns, "thinking": self.thinking})

    async def ask(
        self, key: str, text: str, context: str = "", wait: bool = True, fast: bool = False
    ) -> Optional[str]:
        """fast=True (comentarios de la música): primero los modelos Lite, que responden antes,
        y menos tiempo por modelo; así un modelo lento o saturado no retrasa los avisos."""
        models = [m for m in self.models if "lite" in m] + [m for m in self.models if "lite" not in m] \
            if fast else self.models
        per_model = FAST_MODEL_TIMEOUT if fast else MODEL_TIMEOUT
        lock = self._locks.setdefault(key, asyncio.Lock())
        if not wait and lock.locked():
            return None  # el canal está ocupado con otra respuesta: no hacemos esperar a la música
        async with lock:  # un mensaje a la vez por canal, para no mezclar la conversación
            history = self.history.get(key, [])
            contents = history + [{"role": "user", "parts": [{"text": text}]}]
            system = self.system_prompt
            if context:
                system += f"\n\n## Lo que está pasando ahora (información real)\n{context}"
            for model in models:
                if self.cooldowns.get(model, 0) > time.time():
                    continue
                try:
                    reply = await asyncio.wait_for(self._generate(model, contents, system), per_model)
                except asyncio.TimeoutError:
                    self.cooldowns[model] = time.time() + 60
                    log.warning("Gemini: %s tardó más de %ss, se prueba otro modelo", model, per_model)
                    continue
                if reply is None:
                    continue
                history = contents + [{"role": "model", "parts": [{"text": reply}]}]
                self.history[key] = history[-MAX_TURNS:]
                await asyncio.to_thread(_save, HISTORY_FILE, dict(self.history))
                return reply
            return None

    async def _generate(self, model: str, contents: list[dict], system: str) -> Optional[str]:
        start = self.thinking.get(model, 0)
        for index in range(start, len(THINKING_OPTIONS)):
            config = types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.9,
                max_output_tokens=1024,
                thinking_config=THINKING_OPTIONS[index],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )
            try:
                response = await self.client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except errors.APIError as exc:
                if exc.code == 400 and "think" in str(exc).lower() and index + 1 < len(THINKING_OPTIONS):
                    continue  # este modelo no acepta esa config de pensamiento: probamos la siguiente
                if exc.code == 429:
                    self.cooldowns[model] = _cooldown_for(exc)
                    until = datetime.fromtimestamp(self.cooldowns[model]).strftime("%d/%m %H:%M")
                    log.warning("Gemini: límite alcanzado en %s, no se usará hasta %s", model, until)
                elif exc.code == 404:
                    self.cooldowns[model] = time.time() + 24 * 3600
                    log.warning("Gemini: el modelo %s no existe o no está disponible", model)
                elif exc.code in (401, 403):
                    self.cooldowns[model] = time.time() + 3600
                    log.error("Gemini: la API key no tiene acceso (%s)", exc.message)
                elif exc.code in (500, 502, 503, 504):
                    # Modelo saturado ("high demand"): lo dejamos descansar un par de minutos.
                    self.cooldowns[model] = time.time() + 120
                    log.warning("Gemini: %s está saturado (%s), se prueba otro modelo", model, exc.code)
                else:
                    self.cooldowns[model] = time.time() + 30
                    log.warning("Gemini: error con %s: %s", model, exc)
                self._save_state()
                return None
            except Exception as exc:
                log.warning("Gemini: error de conexión con %s: %s", model, exc)
                return None

            if self.thinking.get(model) != index:
                self.thinking[model] = index
                self._save_state()
            reply = (response.text or "").strip()
            prefix = f"{self.character.name}:"
            if reply.startswith(prefix):
                reply = reply[len(prefix):].strip()
            if not reply:
                log.info("Gemini no devolvió texto (¿filtro de seguridad?) con %s", model)
            return reply or None
        return None

    async def close(self) -> None:
        pass
