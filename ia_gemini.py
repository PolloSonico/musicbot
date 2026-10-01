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
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from google import genai
from google.genai import errors, types

from ia_respaldo import BackupAI
from jsonio import load_json, save_json
from personaje import Character

log = logging.getLogger("persona")
logging.getLogger("google_genai").setLevel(logging.WARNING)  # evita mensajes internos de la librería

DATA_DIR = Path(__file__).resolve().parent / "data"
HISTORY_FILE = DATA_DIR / "historial.json"
STATE_FILE = DATA_DIR / "gemini_estado.json"
SUMMARY_FILE = DATA_DIR / "resumenes.json"  # memoria larga: resumen de lo que salió de la memoria corta
SUMMARY_BATCH = 8  # cada cuántos mensajes olvidados se actualiza el resumen del canal
SUMMARY_MAX_CHARS = 1500
MAX_TURNS = 24  # mensajes que recuerda por canal (12 idas y vueltas)
MODEL_TIMEOUT = 20  # segundos máximos por modelo en una charla
FAST_MODEL_TIMEOUT = 6  # segundos máximos por modelo en un comentario de la música
RETIRED_RECHECK = 7 * 86400  # un modelo dado de baja (404) se vuelve a probar a la semana
EXHAUSTED_ALERT_MIN = 15 * 60  # solo se avisa al dueño si la IA va a estar sin cupo al menos esto
FALLBACK_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]
# El cupo diario gratis de Gemini se renueva a medianoche, hora del Pacífico.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")
# Versión mínima de Gemini que se usa: los anteriores (2.5 y más viejos) Google ya los dio de baja.
MIN_VERSION = float(os.getenv("GEMINI_MIN_VERSION", "3") or 3)
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
    data = load_json(path, {})
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict) -> None:
    save_json(path, data)  # guardado atómico: nunca deja el archivo a medias


def _next_quota_reset() -> float:
    now = datetime.now(QUOTA_TZ)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    return tomorrow.timestamp()


def _too_old(model: str) -> bool:
    """Modelos anteriores a MIN_VERSION (ej: gemini-2.5-flash): ni se listan ni se prueban."""
    match = MODEL_RE.match(model)
    return bool(match) and float(match.group(1)) < MIN_VERSION


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


_WRAPPED_RE = re.compile(r'^["“«](.*)["”»]\.?$', re.S)


def _strip_wrapping_quotes(text: str) -> str:
    """Si todo el mensaje vino entre comillas ("Hola..."), se las saca: Lillia habla, no cita."""
    match = _WRAPPED_RE.match(text.strip())
    if match and '"' not in match.group(1) and "“" not in match.group(1):
        return match.group(1).strip()
    return text


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


EXTRA_ACTIONS_PROMPT = """

## Recordar cosas de cada persona
En "Lo que está pasando ahora" verás "Lo que recuerdas de ..." cada persona: úsalo con naturalidad
(sin recitarlo). Si alguien te cuenta algo duradero sobre SÍ MISMO que valga la pena recordar (su main
o campeones favoritos, su rol o línea, su rango, cómo quiere que lo llames, sus gustos de música o de
juegos, algo importante que te cuente con gusto), añade AL FINAL de tu mensaje, en su propia línea:
[[RECORDAR: dato corto en tercera persona]]   (ej: [[RECORDAR: su main es Jinx y juega ADC]])
Reglas: solo cosas que la persona dijo de sí misma (no de otros), como mucho una por mensaje, nada que
ya recuerdes, nada de su rango, LP o partidas de League (eso lo sabes al día por su cuenta vinculada), y NUNCA datos privados o delicados (salud, dinero, dirección, teléfono, contraseñas,
política, religión). Si alguien te pide que olvides algo suyo, dile que use el comando !olvidame.

## Reaccionar a un mensaje
Si te dan ganas, puedes reaccionar al mensaje de la persona con un emoji añadiendo al final
[[REACCION: emoji]] (un emoji normal como 🌸, o uno del servidor como :nombre:). Úsalo de vez en
cuando, no siempre.

## Tirar dados
Si alguien te pide que tires un dado (o "tira iniciativa", "tira un d20", "tira 2d6+3"...), contesta
algo corto y añade AL FINAL, en su propia línea: [[DADO: 1d20]] (con la tirada que pidan, en formato
NdM+K; "ventaja" o "desventaja" también valen). NO inventes el resultado: el sistema tira de verdad y
lo muestra. Si en el chat aparece una tirada de otro bot de dados, puedes opinar de ella.

## Recordatorios
Si alguien te pide que le recuerdes algo (a sí mismo o a otras personas) a una hora, contesta corto y
añade AL FINAL, en su propia línea:
[[RECORDATORIO: AAAA-MM-DD HH:MM | para quién | qué recordar | repetir]]
- La fecha y hora exactas, calculadas con "Hoy es ..." y la hora actual de "Lo que está pasando ahora"
  (ej: si hoy es jueves y dicen "el viernes a las 9 de la noche", es el viernes siguiente a las 21:00).
  Si dicen solo el día, usa las 10:00. Si no dicen cuándo, NO pongas la orden: pregúntale cuándo.
- "para quién": "yo" si es para quien habla, o los nombres de las personas (separados por coma).
- "qué recordar": corto y en tercera persona o como recordatorio ("sesión de D&D").
- "repetir": vacío, "diario" o "semanal" (si dicen "todos los viernes", "cada día"...).
- No digas tú la fecha calculada como si fuera segura: el sistema confirma debajo la fecha que guardó.

## Imágenes
A veces te mandan imágenes (capturas de partidas, memes, fotos...). Míralas de verdad y comenta lo
que se ve con tu personalidad. Si es una captura de League (fin de partida, build, runas), fíjate en
los datos concretos (campeones, KDA, objetos) y opina sobre ellos.

## Emojis del servidor
Si en "Lo que está pasando ahora" aparece una lista de "Emojis propios de este servidor", puedes usar
alguno escribiendo :nombre: exactamente como aparece (solo esos; no inventes otros), cuando encaje y
sin abusar."""


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
        "algo largo. No empieces tus mensajes con tu nombre ni los escribas entre comillas.\n"
        "- NO describas acciones, gestos ni estados de ánimo con texto entre asteriscos o en cursiva "
        "(nada de *mueve las orejitas* o *sonríe*). Para expresar lo que haces o sientes usa emojis "
        "(por ejemplo 🌸😳✨🦌💤🌿🥺), sin abusar: como mucho dos por mensaje, y no hace falta ponerlos en "
        "todos los mensajes.\n"
        "- Muletillas: tu \"¡Ay!\" es parte de ti, pero no empieces todos los mensajes con él (más o menos "
        "uno de cada cuatro). Varía cómo arrancas: a veces directo, a veces con \"¡Eep!\", \"Mmm...\", "
        "\"¡Oh!\", un tartamudeo (\"E-esa...\") o el nombre de la persona."
        + EXTRA_ACTIONS_PROMPT
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
        # Modelos que Google dio de baja (responden 404 aunque sigan apareciendo en la lista de
        # modelos): no se vuelven a usar. Se reintentan una vez por semana, por si vuelven.
        self.retired: dict[str, float] = {m: t for m, t in state.get("retirados", {}).items() if not _too_old(m)}
        self._locks: dict[str, asyncio.Lock] = {}
        # Función para avisar al dueño por DM: alert(clave, texto, no_repetir_hasta). La pone persona.py.
        self.alert: Optional[Callable[[str, str, Optional[float]], None]] = None
        # Uso del día (pedidos y tokens por modelo) y límites diarios "aprendidos": Google no dice
        # cuánto cupo queda, así que cuando un modelo llega a su límite se anota cuántos pedidos llevaba.
        self.usage: dict = state.get("uso", {})
        self.usage.setdefault("modelos", {})
        self.usage.setdefault("limites", {})
        self.backup = BackupAI.from_env()
        if self.backup:
            log.info("IA de respaldo: %s (%s)", self.backup.provider, ", ".join(self.backup.models))
        self.summaries: dict[str, dict] = _load(SUMMARY_FILE)
        self._summarizing: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

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
            found = await self._discover_models() or FALLBACK_MODELS
            now = time.time()
            self.models = [m for m in found if now - self.retired.get(m, 0) >= RETIRED_RECHECK] or found
        log.info("Gemini usará los modelos (en orden): %s", ", ".join(self.models))
        skipped = [m for m in self.retired if m not in self.models]
        if skipped:
            log.info("Gemini: modelos dados de baja que no se usan: %s", ", ".join(skipped))

    async def _discover_models(self) -> list[str]:
        found: list[tuple[float, bool, str]] = []
        try:
            async for model in await self.client.aio.models.list():
                name = (model.name or "").removeprefix("models/")
                match = MODEL_RE.match(name)
                actions = model.supported_actions or []
                if match and float(match.group(1)) >= MIN_VERSION and (not actions or "generateContent" in actions):
                    found.append((float(match.group(1)), bool(match.group(2)), name))
        except errors.APIError as exc:
            if exc.code in (400, 401, 403):
                raise RuntimeError(f"La API key de Gemini no es válida ({exc.code} {exc.message})") from exc
            log.warning("No se pudo listar los modelos de Gemini: %s", exc)
            return []
        # Primero los Flash normales (del más nuevo al más viejo), después los Flash-Lite.
        found.sort(key=lambda m: (m[1], -m[0]))
        return [name for _, _, name in found]

    def gemini_available(self) -> bool:
        now = time.time()
        return any(self.cooldowns.get(m, 0) <= now for m in self.models or self.preferred or FALLBACK_MODELS)

    def available(self) -> bool:
        """Puede responder: Gemini, o si no, la IA de respaldo."""
        return self.gemini_available() or bool(self.backup and self.backup.available())

    # ---------- Uso del cupo ----------

    def _today(self) -> str:
        return datetime.now(QUOTA_TZ).date().isoformat()  # el cupo de Gemini se renueva en hora de California

    def _roll_usage(self) -> None:
        if self.usage.get("dia") != self._today():
            self.usage["dia"] = self._today()
            self.usage["modelos"] = {}

    def _count(self, model: str, response) -> None:
        self._roll_usage()
        entry = self.usage["modelos"].setdefault(model, {"pedidos": 0, "tokens": 0})
        entry["pedidos"] += 1
        meta = getattr(response, "usage_metadata", None)
        entry["tokens"] += getattr(meta, "total_token_count", 0) or 0

    def usage_report(self) -> list[tuple[str, int, int, Optional[int]]]:
        """[(modelo, pedidos hoy, tokens hoy, límite diario aprendido o None)] de los modelos usados hoy
        o con límite conocido."""
        self._roll_usage()
        rows = []
        for model in self.models:
            used = self.usage["modelos"].get(model, {})
            limit = self.usage["limites"].get(model)
            if used or limit:
                rows.append((model, used.get("pedidos", 0), used.get("tokens", 0), limit))
        return rows

    def available_again_at(self) -> Optional[float]:
        pending = [self.cooldowns.get(m, 0) for m in self.models]
        return min(pending) if pending and min(pending) > time.time() else None

    def check_exhausted(self) -> None:
        """Si se agotó el cupo de TODOS los modelos por un buen rato, avisa al dueño (una vez)."""
        if self.alert is None or not self.models or self.gemini_available():
            return
        until = self.available_again_at()
        if until is None or until - time.time() < EXHAUSTED_ALERT_MIN:
            return  # cortes cortos (modelos saturados un par de minutos): no vale la pena avisar
        from avisos import fecha_hora

        self.alert(
            "gemini_sin_cupo",
            f"😴 **{self.character.name} se quedó sin cupo de Gemini** (se agotó el límite gratis de todos "
            f"los modelos: {', '.join(self.models)}).\n"
            f"Vuelve a hablar **{fecha_hora(until)}** (hora de tu PC). El cupo diario se renueva a "
            "medianoche de California. "
            + (f"Mientras tanto responde la IA de respaldo ({self.backup.provider})."
               if self.backup and self.backup.available()
               else "Mientras tanto la música sigue funcionando con los textos fijos."),
            until,
        )

    def reset(self, key: str) -> None:
        self.history.pop(key, None)
        _save(HISTORY_FILE, self.history)
        if self.summaries.pop(key, None) is not None:
            _save(SUMMARY_FILE, self.summaries)

    def _save_state(self) -> None:
        now = time.time()
        self.cooldowns = {m: t for m, t in self.cooldowns.items() if t > now}
        self.retired = {m: t for m, t in self.retired.items() if now - t < RETIRED_RECHECK}
        _save(STATE_FILE, {"cooldowns": self.cooldowns, "thinking": self.thinking, "stats": self.stats,
                           "retirados": self.retired, "uso": self.usage})

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

    def ranking(self, usable_only: bool = False) -> list[tuple[str, Optional[float], float]]:
        """(modelo, segundos medios, tasa de fallos) en el orden en que se usarán.
        usable_only: sin los que están en pausa ahora (sin cupo, saturados...)."""
        result = []
        now = time.time()
        for model in self.ordered_models():
            if usable_only and self.cooldowns.get(model, 0) > now:
                continue
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
        images: Optional[list[tuple[str, bytes]]] = None,
    ) -> Optional[str]:
        """fast=True (comentarios de la música): primero los modelos Lite, que responden antes,
        y menos tiempo por modelo; así un modelo lento o saturado no retrasa los avisos.
        remember=False: no se guarda en la memoria del canal (por defecto, los comentarios
        automáticos no se guardan, para no desplazar lo que la gente le dijo).
        images: [(tipo MIME, bytes)] que la IA puede ver junto al texto (no se guardan en la memoria)."""
        if remember is None:
            remember = not fast
        models = self.ordered_models(fast)
        per_model = FAST_MODEL_TIMEOUT if fast else MODEL_TIMEOUT
        lock = self._locks.setdefault(key, asyncio.Lock())
        if not wait and lock.locked():
            return None  # el canal está ocupado con otra respuesta: no hacemos esperar a la música
        async with lock:  # un mensaje a la vez por canal, para no mezclar la conversación
            history = self.history.get(key, [])
            parts: list = [{"text": text}]
            for mime, data in images or []:
                parts.append(types.Part.from_bytes(data=data, mime_type=mime))
            contents = history + [{"role": "user", "parts": parts}]
            # En la memoria solo queda el texto: las imágenes pesan mucho y no se pueden guardar en JSON.
            note = f" [mandó {len(images)} imagen{'es' if len(images) > 1 else ''}]" if images else ""
            remembered_turn = {"role": "user", "parts": [{"text": text + note}]}
            system = self.system_prompt
            summary = self.summaries.get(key, {}).get("resumen")
            if summary:
                system += f"\n\n## Lo que recuerdas de charlas anteriores en este canal (resumen)\n{summary}"
            if context:
                system += f"\n\n## Lo que está pasando ahora (información real)\n{context}"
            reply = None
            for model in models:
                if self.cooldowns.get(model, 0) > time.time():
                    continue
                started = time.monotonic()
                try:
                    reply = await asyncio.wait_for(self._generate(model, contents, system), per_model)
                except asyncio.TimeoutError:
                    self.cooldowns[model] = time.time() + 60
                    self._record(model, ok=False)
                    log.warning("Gemini: %s tardó más de %ss, se prueba otro modelo", model, per_model)
                    continue
                if reply is None:
                    continue
                elapsed = time.monotonic() - started
                self._record(model, ok=True, seconds=elapsed)
                log.info("Gemini: %s respondió en %.1fs", model, elapsed)
                break
            if reply is None:
                self.check_exhausted()
                reply = await self._ask_backup(system, contents, fast)
            if reply is None:
                return None
            if remember:
                remembered = reply
                history = history + [remembered_turn, {"role": "model", "parts": [{"text": remembered}]}]
                self.history[key] = history[-MAX_TURNS:]
                self._queue_for_summary(key, history[:-MAX_TURNS])
                await asyncio.to_thread(_save, HISTORY_FILE, dict(self.history))
            return reply

    def _clean(self, reply: str) -> Optional[str]:
        reply = (reply or "").strip()
        prefix = f"{self.character.name}:"
        if reply.startswith(prefix):
            reply = reply[len(prefix):].strip()
        reply = strip_actions(reply) if reply else reply
        reply = _strip_wrapping_quotes(reply) if reply else reply
        return reply or None

    async def _ask_backup(self, system: str, contents: list, fast: bool) -> Optional[str]:
        """Si Gemini no pudo, contesta la IA de respaldo (sin imágenes)."""
        if not (self.backup and self.backup.available()):
            return None
        try:
            reply = await asyncio.wait_for(self.backup.chat(system, contents, 400 if fast else 900),
                                           FAST_MODEL_TIMEOUT * 2 if fast else MODEL_TIMEOUT * 2)
        except asyncio.TimeoutError:
            log.warning("Respaldo (%s): tardó demasiado", self.backup.provider)
            return None
        return self._clean(reply) if reply else None

    # ---------- Memoria larga (resumen por canal) ----------

    def _queue_for_summary(self, key: str, dropped: list[dict]) -> None:
        """Los mensajes que salen de la memoria corta se juntan y, cada SUMMARY_BATCH, se resumen."""
        if not dropped:
            return
        entry = self.summaries.setdefault(key, {"resumen": "", "pendientes": []})
        entry["pendientes"].extend(dropped)
        _save(SUMMARY_FILE, self.summaries)
        if len(entry["pendientes"]) >= SUMMARY_BATCH and key not in self._summarizing:
            self._summarizing.add(key)
            task = asyncio.create_task(self._summarize(key))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _summarize(self, key: str) -> None:
        try:
            entry = self.summaries.get(key)
            if not entry or not entry.get("pendientes"):
                return
            pending = list(entry["pendientes"])
            lines = []
            for turn in pending:
                text = " ".join(p.get("text", "") for p in turn.get("parts", []) if isinstance(p, dict))
                if turn.get("role") == "model":
                    text = f"{self.character.name}: {text}"
                lines.append(text.strip())
            prompt = (
                (f"Resumen anterior:\n{entry['resumen']}\n\n" if entry.get("resumen") else "")
                + "Mensajes nuevos de la charla (en orden):\n" + "\n".join(lines)
                + f"\n\nEscribe el resumen ACTUALIZADO (máximo {SUMMARY_MAX_CHARS} caracteres), en español, en "
                "viñetas cortas que empiecen con \"- \". Guarda lo útil para seguir charlando otro día: quién dijo "
                "qué, gustos, planes, bromas internas, preguntas pendientes. Junta lo viejo con lo nuevo y descarta "
                "lo que ya no importa. No inventes nada. Responde SOLO con las viñetas."
            )
            system = "Eres un asistente que resume conversaciones de un servidor de Discord de forma breve y fiel."
            contents = [{"role": "user", "parts": [{"text": prompt}]}]
            summary = None
            for model in self.ordered_models(fast=True):
                if self.cooldowns.get(model, 0) > time.time():
                    continue
                try:
                    summary = await asyncio.wait_for(self._generate(model, contents, system), MODEL_TIMEOUT)
                except asyncio.TimeoutError:
                    continue
                if summary:
                    break
            if not summary and self.backup and self.backup.available():
                summary = await self.backup.chat(system, contents, 600)
            if not summary:
                return  # se reintenta cuando se junten más mensajes
            entry["resumen"] = summary.strip()[:SUMMARY_MAX_CHARS]
            entry["pendientes"] = entry["pendientes"][len(pending):]
            _save(SUMMARY_FILE, self.summaries)
            log.info("Memoria larga del canal %s actualizada (%d caracteres)", key, len(entry["resumen"]))
        except Exception:
            log.exception("No se pudo resumir la charla del canal %s", key)
        finally:
            self._summarizing.discard(key)

    async def _generate(self, model: str, contents: list[dict], system: str) -> Optional[str]:
        start = self.thinking.get(model, 0)
        for index in range(start, len(THINKING_OPTIONS)):
            config = types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.9,
                max_output_tokens=1536,
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
                    if self.cooldowns[model] - time.time() > 3600:  # límite DIARIO: se aprende cuántos pedidos tiene
                        self._roll_usage()
                        reached = self.usage["modelos"].get(model, {}).get("pedidos", 0)
                        if reached:
                            self.usage["limites"][model] = max(reached, self.usage["limites"].get(model, 0))
                    until = datetime.fromtimestamp(self.cooldowns[model]).strftime("%d/%m %H:%M")
                    log.warning("Gemini: límite alcanzado en %s, no se usará hasta %s. %s", model, until,
                                (exc.message or "")[:150])
                elif exc.code == 404:
                    # Google lo dio de baja (sigue en la lista pero ya no responde): se deja de usar.
                    self.retired[model] = time.time()
                    if model in self.models and len(self.models) > 1:
                        self.models = [m for m in self.models if m != model]
                    log.warning("Gemini: el modelo %s fue dado de baja por Google; no se usará más", model)
                elif exc.code in (401, 403):
                    self.cooldowns[model] = time.time() + 3600
                    log.error("Gemini: la API key no tiene acceso (%s)", exc.message)
                    if self.alert:
                        self.alert(
                            "gemini_api_key",
                            f"🔑 La API key de Gemini no funciona ({exc.code}: {exc.message}). Revisa "
                            "`GEMINI_API_KEY` en `.env` (o crea otra en https://aistudio.google.com/apikey) "
                            "y reinicia el bot.",
                            None,
                        )
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
            self._count(model, response)
            self._save_state()
            reply = self._clean(response.text or "")
            if not reply:
                log.info("Gemini no devolvió texto (¿filtro de seguridad?) con %s", model)
                return None
            return reply
        return None

    async def close(self) -> None:
        if self.backup:
            await self.backup.close()
