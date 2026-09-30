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
MAX_TURNS = 24  # mensajes que recuerda por canal (12 idas y vueltas)
MODEL_TIMEOUT = 20  # segundos máximos por modelo en una charla
SEARCH_MODEL_TIMEOUT = 40  # con búsqueda en Google (datos de League) tarda más
FAST_MODEL_TIMEOUT = 6  # segundos máximos por modelo en un comentario de la música
FALLBACK_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]
# El cupo diario gratis de Gemini se renueva a medianoche, hora del Pacífico.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")
MODEL_RE = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash(-lite)?$")

# Configuraciones de "pensamiento" a probar (menos pensamiento = más rápido y gasta menos cupo).
# No todos los modelos aceptan todas; se prueba en orden y se recuerda cuál funcionó.
THINKING_OPTIONS = [
    types.ThinkingConfig(thinking_level="minimal"),
    types.ThinkingConfig(thinking_level="low"),
    types.ThinkingConfig(thinking_budget=0),
    None,
]


# Ranking de modelos aprendido: cada modelo guarda su tiempo de respuesta medio y su tasa de
# fallos (saturado / demasiado lento). Los fallos se "olvidan" a la mitad cada FAIL_HALF_LIFE
# horas, para que un modelo castigado pueda volver a subir cuando Google lo descongestione.
FAIL_HALF_LIFE = 6.0
FAIL_PENALTY = 40.0  # segundos "virtuales" que suma un fallo seguro
EWMA = 0.3  # cuánto pesa la última experiencia


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


# Acciones de rol entre asteriscos o guiones bajos, como *mueve las orejitas* o _sonríe_.
# Solo si tienen varias palabras: un énfasis de una palabra (*eep*) se deja.
_ACTION_RE = re.compile(r"(?<![*\w])([*_])(?![*_\s])([^*_\n]*?\s[^*_\n]*?)(?<![\s*_])\1(?![*\w])")


def strip_actions(text: str) -> str:
    """Quita las acciones de rol (*así*) que el personaje no debe escribir; usa emojis en su lugar."""
    cleaned = _ACTION_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+([,.!?…])", r"\1", cleaned)  # espacios que quedaron antes de puntuación
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines())
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned and text.strip():
        return "✨"  # el mensaje era solo una acción: queda un emoji en su lugar
    return cleaned


SOURCES_MARK = "\n-# Fuentes:"


def _format_sources(response) -> str:
    """Línea pequeña con las páginas que consultó el modelo al buscar en Google."""
    try:
        chunks = response.candidates[0].grounding_metadata.grounding_chunks or []
    except (AttributeError, IndexError, TypeError):
        return ""
    links, seen = [], set()
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        if not web or not web.uri:
            continue
        title = (web.title or web.uri).strip()
        if title in seen:
            continue
        seen.add(title)
        links.append(f"[{title}](<{web.uri}>)")  # <...> evita que Discord muestre la vista previa
        if len(links) == 4:
            break
    return f"{SOURCES_MARK} {' · '.join(links)}" if links else ""


MUSIC_CONTROL_PROMPT = """
## Controlar la música con órdenes
Eres la DJ: cuando alguien te pide algo de la música con palabras normales, además de responder
(corto, por ejemplo "¡ahí la busco!"), añade AL FINAL de tu mensaje las órdenes necesarias, cada
una en su propia línea y escritas exactamente así:
[[PLAY: búsqueda para YouTube]]  -> buscar y poner (o encolar) una canción
[[SKIP]]  -> saltar la canción actual
[[STOP]]  -> parar la música y vaciar la cola
[[PAUSE]] / [[RESUME]]  -> pausar / continuar
[[LOOP]]  -> activar o quitar la repetición de la canción actual
[[VOLUME: 0-100]]  -> cambiar el volumen
[[LEAVE]]  -> salir del canal de voz
[[BUSCAR: búsqueda]]  -> mostrar 5 resultados para que la persona elija
[[RADIO: on|off]]  -> modo radio
Reglas:
- Usa órdenes SOLO si te piden claramente hacer algo con la música. Si solo charlan o preguntan
  qué suena, no pongas ninguna.
- En PLAY escribe una búsqueda precisa, idealmente "Artista - Título" (ej: [[PLAY: Kesha - TiK ToK]]).
  Si te pasan un link, pon el link tal cual. Como mucho 3 PLAY por mensaje.
- Si alguien te pide "algo que me pueda gustar" o pregunta qué música le gusta, fíjate en sus
  "Canciones que pidió" (en "Lo que está pasando ahora"): describe sus gustos o elige una canción
  REAL parecida (mismo estilo, artista o época) que no esté ya en su lista.
- Si el pedido es ambiguo (no queda claro qué canción o qué versión quieren) o te piden que
  busques, usa [[BUSCAR: búsqueda]] en vez de PLAY: el sistema muestra 5 resultados de YouTube
  para que la persona elija.
- [[RADIO: on]] / [[RADIO: off]] activa o apaga el modo radio (cuando se vacía la cola, eliges tú
  canciones parecidas a los gustos de los que están escuchando).
- Si te piden que elijas tú una canción, elige una canción REAL que de verdad te guste a ti
  (según tu personalidad y tus gustos) y cuenta en una frase por qué la elegiste. Varía: no
  elijas siempre la misma.
- No expliques las órdenes ni las menciones: el sistema las ejecuta y las oculta."""


def build_system_prompt(character: Character, language: str, music_control: bool = True) -> str:
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
        "- NO describas acciones, gestos ni estados de ánimo con texto entre asteriscos o en cursiva "
        "(nada de *mueve las orejitas* o *sonríe*). Para expresar lo que haces o sientes usa emojis "
        "(por ejemplo 🌸😳✨🦌💤🌿🥺), sin abusar: uno a tres por mensaje."
        + (MUSIC_CONTROL_PROMPT if music_control else "")
    )


class GeminiBackend:
    provider = "Gemini"

    def __init__(
        self, api_key: str, character: Character, language: str, models: list[str], music_control: bool = True
    ) -> None:
        self.client = genai.Client(api_key=api_key)
        self.character = character
        self.system_prompt = build_system_prompt(character, language, music_control)
        self.preferred = models
        self.models: list[str] = []
        self.history: dict[str, list[dict]] = _load(HISTORY_FILE)
        # Limpia acciones viejas guardadas en la memoria, para que no las siga imitando.
        for turns in self.history.values():
            for turn in turns:
                if turn.get("role") == "model":
                    for part in turn.get("parts", []):
                        if "text" in part:
                            part["text"] = strip_actions(part["text"])
        state = _load(STATE_FILE)
        self.cooldowns: dict[str, float] = state.get("cooldowns", {})
        self.thinking: dict[str, int] = state.get("thinking", {})
        self.stats: dict[str, dict] = state.get("stats", {})
        self._locks: dict[str, asyncio.Lock] = {}
        self.no_search: set[str] = set()  # modelos que no aceptan la búsqueda en Google

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
        _save(STATE_FILE, {"cooldowns": self.cooldowns, "thinking": self.thinking, "stats": self.stats})

    # ---------- Ranking de modelos ----------

    def _fail_rate(self, stat: dict) -> float:
        hours = (time.time() - stat.get("t", 0)) / 3600
        return stat.get("fail", 0.0) * 0.5 ** (hours / FAIL_HALF_LIFE)

    def _record(self, model: str, ok: bool, seconds: float = 0.0) -> None:
        stat = self.stats.setdefault(model, {"lat": seconds or 5.0, "fail": 0.0, "n": 0})
        stat["fail"] = self._fail_rate(stat) * (1 - EWMA) + (0.0 if ok else 1.0) * EWMA
        if ok:
            stat["lat"] = stat["lat"] * (1 - EWMA) + seconds * EWMA
        stat["n"] = stat.get("n", 0) + 1
        stat["t"] = time.time()
        self._save_state()

    def _score(self, model: str, position: int) -> float:
        """Menor = mejor. Los modelos sin datos se prueban pronto (así se descubre si son buenos)."""
        stat = self.stats.get(model)
        if not stat:
            return position * 0.1
        return stat["lat"] + self._fail_rate(stat) * FAIL_PENALTY + position * 0.1

    def ordered_models(self, fast: bool = False) -> list[str]:
        ranked = sorted(self.models, key=lambda m: self._score(m, self.models.index(m)))
        if fast:  # comentarios cortos: primero los Lite (más rápidos y menos solicitados)
            ranked = [m for m in ranked if "lite" in m] + [m for m in ranked if "lite" not in m]
        return ranked

    def ranking(self) -> list[tuple[str, Optional[float], float]]:
        """(modelo, segundos medios, tasa de fallos) en el orden en que se usarán."""
        result = []
        for model in self.ordered_models():
            stat = self.stats.get(model)
            result.append((model, stat["lat"] if stat else None, self._fail_rate(stat) if stat else 0.0))
        return result

    async def ask(
        self,
        key: str,
        text: str,
        context: str = "",
        wait: bool = True,
        fast: bool = False,
        remember: Optional[bool] = None,
        search: bool = False,
    ) -> Optional[str]:
        """fast=True (comentarios de la música): primero los modelos Lite, que responden antes,
        y menos tiempo por modelo; así un modelo lento o saturado no retrasa los avisos.
        remember=False: no se guarda en la memoria del canal (por defecto, los comentarios
        automáticos no se guardan, para no desplazar lo que la gente le dijo).
        search=True: el modelo puede buscar en Google (para datos actuales, como builds de League)."""
        if remember is None:
            remember = not fast
        models = self.ordered_models(fast)
        per_model = FAST_MODEL_TIMEOUT if fast else SEARCH_MODEL_TIMEOUT if search else MODEL_TIMEOUT
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
                started = time.monotonic()
                try:
                    reply = await asyncio.wait_for(self._generate(model, contents, system, search), per_model)
                except asyncio.TimeoutError:
                    self.cooldowns[model] = time.time() + 60
                    self._record(model, ok=False)
                    log.warning("Gemini: %s tardó más de %ss, se prueba otro modelo", model, per_model)
                    continue
                if reply is None:
                    continue
                elapsed = time.monotonic() - started
                self._record(model, ok=True, seconds=elapsed)
                log.info("Gemini: %s respondió en %.1fs%s", model, elapsed, " (con búsqueda)" if search else "")
                if remember:
                    remembered = reply.split(SOURCES_MARK)[0].rstrip()  # las fuentes no hace falta recordarlas
                    history = contents + [{"role": "model", "parts": [{"text": remembered}]}]
                    self.history[key] = history[-MAX_TURNS:]
                    await asyncio.to_thread(_save, HISTORY_FILE, dict(self.history))
                return reply
            return None

    async def _generate(self, model: str, contents: list[dict], system: str, search: bool = False) -> Optional[str]:
        start = self.thinking.get(model, 0)
        use_search = search and model not in self.no_search
        for index in range(start, len(THINKING_OPTIONS)):
            config = types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.9,
                max_output_tokens=1536 if use_search else 1024,
                thinking_config=THINKING_OPTIONS[index],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                tools=[types.Tool(google_search=types.GoogleSearch())] if use_search else None,
            )
            try:
                response = await self.client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except errors.APIError as exc:
                if exc.code == 400 and "think" in str(exc).lower() and index + 1 < len(THINKING_OPTIONS):
                    continue  # este modelo no acepta esa config de pensamiento: probamos la siguiente
                if use_search and exc.code in (400, 403) and re.search(r"search|tool|ground", str(exc), re.I):
                    # Este modelo (o el plan gratis) no permite buscar en Google: se responde sin buscar.
                    log.warning("Gemini: %s no permite búsqueda en Google (%s)", model, exc.message)
                    self.no_search.add(model)
                    return await self._generate(model, contents, system, search=False)
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
                    # Modelo saturado ("high demand"): lo dejamos descansar y baja en el ranking.
                    self.cooldowns[model] = time.time() + 120
                    self._record(model, ok=False)
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
            reply = strip_actions(reply) if reply else reply
            if not reply:
                log.info("Gemini no devolvió texto (¿filtro de seguridad?) con %s", model)
                return None
            if use_search:
                reply += _format_sources(response)
            return reply
        return None

    async def close(self) -> None:
        pass
