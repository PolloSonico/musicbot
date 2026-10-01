"""Datos oficiales y actuales de League of Legends desde Data Dragon (el CDN público de Riot).

No necesita ninguna clave. Sirve para que Lillia no dependa solo de lo que "sabe" la IA:

- Número del parche actual: se le pasa a la búsqueda de builds para que busque datos de ESTE
  parche y no guías viejas. También permite anunciar cuando sale un parche nuevo.
- Campeones: si alguien nombra un campeón, se le pasa su kit real (pasiva, Q, W, E, R) en
  español latino, así no inventa habilidades.
- Objetos: en preguntas de builds se le pasa la lista de objetos que EXISTEN en la Grieta en este
  parche (en español y en inglés), así no recomienda objetos que ya se quitaron del juego.

Todo se guarda en data/datadragon/ y solo se vuelve a descargar cuando cambia el parche.
Data Dragon no tiene estadísticas (win rates, builds populares): eso sigue saliendo de la
búsqueda en Google (PERSONA_LOL_SEARCH).
"""

import asyncio
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Optional

import aiohttp

from jsonio import load_json, save_json

log = logging.getLogger("datadragon")

BASE = "https://ddragon.leagueoflegends.com"
LOCALE = "es_MX"  # español latino
DATA_DIR = Path(__file__).resolve().parent / "data" / "datadragon"
STATE_FILE = DATA_DIR / "estado.json"
CHECK_EVERY = 3 * 3600  # cada cuánto se mira si salió un parche nuevo
RETRY_AFTER_FAIL = 120  # segundos de espera tras un fallo antes de volver a consultar
MAX_CHAMPIONS = 2  # campeones cuyo kit se pasa a la IA por mensaje

CLASSES = {"Fighter": "Luchador", "Mage": "Mago", "Assassin": "Asesino", "Marksman": "Tirador",
           "Support": "Soporte", "Tank": "Tanque"}

# Apodos comunes de la comunidad -> id de Data Dragon.
ALIASES = {
    "mf": "MissFortune", "tf": "TwistedFate", "asol": "AurelionSol", "j4": "JarvanIV", "jarvan": "JarvanIV",
    "lb": "Leblanc", "ez": "Ezreal", "yi": "MasterYi", "kog": "KogMaw", "kogmaw": "KogMaw", "cait": "Caitlyn",
    "mundo": "DrMundo", "nunu": "Nunu", "voli": "Volibear", "heimer": "Heimerdinger", "fiddle": "Fiddlesticks",
    "gp": "Gangplank", "trist": "Tristana", "kass": "Kassadin", "kata": "Katarina", "mord": "Mordekaiser",
    "morde": "Mordekaiser", "naut": "Nautilus", "noc": "Nocturne", "panth": "Pantheon", "sej": "Sejuani",
    "tk": "TahmKench", "tahm": "TahmKench", "xin": "XinZhao", "ww": "Warwick", "wu": "MonkeyKing",
    "wukong": "MonkeyKing", "kaisa": "Kaisa", "renata": "Renata", "belveth": "Belveth", "chogath": "Chogath",
    "cho": "Chogath", "khazix": "Khazix", "kha": "Khazix", "velkoz": "Velkoz", "reksai": "RekSai",
    "ksante": "KSante", "leesin": "LeeSin", "lee": "LeeSin", "malph": "Malphite", "blitz": "Blitzcrank",
    "ori": "Orianna", "yas": "Yasuo", "liss": "Lissandra", "ali": "Alistar", "hecarim": "Hecarim", "hec": "Hecarim",
}
# Nombres que también son palabras o nombres de persona comunes: solo cuentan si el mensaje
# claramente habla de League (si no, "lo vi ayer" metería el kit de Vi).
AMBIGUOUS = {"vi", "aurora", "diana", "karma", "leona", "mel", "lee", "ali", "cho", "wu", "ez", "lb", "tf", "noc"}
LOL_HINT_RE = re.compile(
    r"\b(lol|league|campe[oó]n|champ|pick|build|runas?|habilidad|skill|pasiva|ulti|definitiva|"
    r"top|jungla|jungle|mid|adc|support|supp|lane|línea|linea|q|w|e|r)\b",
    re.I,
)


def normalize(text: str) -> str:
    """minúsculas, sin acentos ni signos: "Kai'Sa" -> "kaisa", "Dr. Mundo" -> "dr mundo"."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"['’.&]", "", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _clean_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _short(text: str, size: int) -> str:
    text = _clean_html(text)
    return text if len(text) <= size else text[: size - 1].rsplit(" ", 1)[0] + "…"


def patch_label(version: str) -> str:
    """Data Dragon numera 16.19.1 lo que el juego llama parche 26.19 (desde 2025 el número del
    parche es el año: 25.x en 2025, 26.x en 2026)."""
    parts = version.split(".")
    try:
        return f"{int(parts[0]) + 10}.{int(parts[1])}"
    except (IndexError, ValueError):
        return version


class DataDragon:
    def __init__(self) -> None:
        self._failed_at = 0.0  # última vez que no se pudo consultar (para no insistir sin internet)
        state = load_json(STATE_FILE, {})
        self.version: Optional[str] = state.get("version")
        self.checked_at: float = state.get("checked_at", 0.0)
        self.champions: dict[str, dict] = {}  # id -> {"name", "title", "tags"}
        self.by_key: dict[int, str] = {}  # número de campeón (lo que usa la Riot API en vivo) -> id
        self._names: list[tuple[str, str]] = []  # (nombre normalizado, id), los más largos primero
        self._kits: dict[str, str] = {}
        self._items_text: Optional[str] = None
        self._lock = asyncio.Lock()
        self._refresh_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        if self.version:
            self._load_champions_from_disk()

    # ---------- Descargas ----------

    async def _get(self, path: str):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        async with self._session.get(f"{BASE}/{path}") as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _cached(self, name: str, path: str):
        """Descarga `path` una vez por parche y lo guarda en data/datadragon/<versión>/<name>."""
        file = DATA_DIR / self.version / name
        data = load_json(file, None)
        if not data:  # no existe (o quedó vacío): se descarga
            data = await self._get(path)
            await asyncio.to_thread(save_json, file, data, None)
        return data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def refresh(self, force: bool = False) -> Optional[str]:
        """Mira si hay un parche nuevo. Devuelve la versión ANTERIOR si cambió (para anunciarlo),
        o None si sigue igual (o si es la primera vez que se descarga).
        Corre en una tarea propia protegida con shield: si quien la llamó se cansa de esperar
        (wait_for), la descarga sigue igual y no queda a medias."""
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh(force))
        return await asyncio.shield(self._refresh_task)

    async def _ensure_champions(self) -> None:
        if self.champions or not self.version:
            return
        self._load_champions_from_disk()
        if self.champions:
            return
        try:
            await self._load_champions()
            log.info("Data Dragon: %d campeones cargados", len(self.champions))
        except Exception as exc:
            log.warning("No se pudo descargar la lista de campeones: %s", exc)

    async def _refresh(self, force: bool = False) -> Optional[str]:
        async with self._lock:
            if not force and self.version and time.time() - self.checked_at < CHECK_EVERY:
                await self._ensure_champions()  # aunque el parche esté al día, la lista tiene que estar
                return None
            if time.time() - self._failed_at < RETRY_AFTER_FAIL:
                return None  # falló hace poco (sin internet): no se reintenta en cada llamada
            try:
                versions = await self._get("api/versions.json")
            except Exception as exc:
                self._failed_at = time.time()
                log.warning("No se pudo consultar Data Dragon: %s", exc)
                return None
            self._failed_at = 0.0
            latest = versions[0]
            previous = self.version
            self.checked_at = time.time()
            if latest != previous:
                self.version = latest
                self._kits.clear()
                self._items_text = None
                self.champions.clear()
                log.info("Data Dragon: parche %s (%s)", patch_label(latest), latest)
                self._remove_old_versions()
            save_json(STATE_FILE, {"version": self.version, "checked_at": self.checked_at})
            await self._ensure_champions()
            return previous if previous and latest != previous else None

    def _remove_old_versions(self) -> None:
        for folder in DATA_DIR.glob("*"):
            if folder.is_dir() and folder.name != self.version:
                for file in folder.glob("*"):
                    try:
                        file.unlink()
                    except OSError:
                        pass
                try:
                    folder.rmdir()
                except OSError:
                    pass

    async def _load_champions(self) -> None:
        data = await self._cached("champion.json", f"cdn/{self.version}/data/{LOCALE}/champion.json")
        self._set_champions(data)

    def _load_champions_from_disk(self) -> None:
        if not self.version:
            return
        data = load_json(DATA_DIR / self.version / "champion.json", None)
        if data:
            self._set_champions(data)

    def _set_champions(self, data: dict) -> None:
        self.champions = {
            cid: {"name": c["name"], "title": c.get("title", ""), "tags": c.get("tags", [])}
            for cid, c in data.get("data", {}).items()
        }
        self.by_key = {}
        for cid, c in data.get("data", {}).items():
            try:
                self.by_key[int(c.get("key", -1))] = cid
            except (TypeError, ValueError):
                pass
        names: dict[str, str] = {}
        for cid, c in self.champions.items():
            names[normalize(c["name"])] = cid
            names[normalize(cid)] = cid
            names[normalize(c["name"]).replace(" ", "")] = cid
        for alias, cid in ALIASES.items():
            if cid in self.champions:
                names.setdefault(alias, cid)
        names.pop("", None)
        self._names = sorted(names.items(), key=lambda x: -len(x[0]))

    # ---------- Nombres ----------

    def champion_name(self, champion: "str | int") -> str:
        """Nombre en español latino a partir del id ("MonkeyKing") o del número (62)."""
        if isinstance(champion, int) or (isinstance(champion, str) and champion.isdigit()):
            cid = self.by_key.get(int(champion))
            return self.champions.get(cid, {}).get("name", f"Campeón {champion}") if cid else f"Campeón {champion}"
        return self.champions.get(champion, {}).get("name", champion)

    def profile_icon_url(self, icon_id: int) -> Optional[str]:
        return f"{BASE}/cdn/{self.version}/img/profileicon/{icon_id}.png" if self.version and icon_id is not None else None

    def champion_icon_url(self, champion: "str | int") -> Optional[str]:
        cid = self.by_key.get(int(champion)) if str(champion).isdigit() else champion
        return f"{BASE}/cdn/{self.version}/img/champion/{cid}.png" if self.version and cid else None

    # ---------- Lo que usa el personaje ----------

    @property
    def patch(self) -> Optional[str]:
        return patch_label(self.version) if self.version else None

    def patch_text(self) -> str:
        if not self.version:
            return ""
        return (
            f"Parche actual de League of Legends: {self.patch} (Data Dragon {self.version}). Si das datos "
            f"de builds, runas o counters, que sean de ESTE parche; si solo encuentras datos de un "
            "parche anterior, dilo."
        )

    def find_champions(self, text: str, also_ambiguous: frozenset = frozenset()) -> list[str]:
        """Ids de los campeones nombrados en el texto (como mucho MAX_CHAMPIONS).
        also_ambiguous: nombres extra que solo cuentan si se habla de League (por ejemplo el del
        propio personaje: "hola Lillia" no necesita el kit de Lillia)."""
        if not self._names:
            return []
        norm = f" {normalize(text)} "
        lol_context = bool(LOL_HINT_RE.search(text))
        found: list[str] = []
        for name, cid in self._names:
            if cid in found:
                continue
            if f" {name} " in norm:
                if (name in AMBIGUOUS or name in also_ambiguous) and not lol_context:
                    continue
                found.append(cid)
                norm = norm.replace(f" {name} ", " ")  # que "lee sin" no cuente también como "lee"
                if len(found) == MAX_CHAMPIONS:
                    break
        return found

    async def champion_kit(self, cid: str) -> Optional[str]:
        if cid in self._kits:
            return self._kits[cid]
        if not self.version:
            return None
        try:
            data = await self._cached(f"{cid}.json", f"cdn/{self.version}/data/{LOCALE}/champion/{cid}.json")
            c = data["data"][cid]
        except Exception as exc:
            log.warning("No se pudo descargar el kit de %s: %s", cid, exc)
            return None
        tags = ", ".join(CLASSES.get(t, t) for t in c.get("tags", []))
        lines = [f"{c['name']}, {c.get('title', '')} (clase: {tags}; recurso: {c.get('partype') or 'ninguno'})."]
        passive = c.get("passive") or {}
        lines.append(f"- Pasiva, {passive.get('name', '?')}: {_short(passive.get('description', ''), 260)}")
        for key, spell in zip("QWER", c.get("spells", [])):
            lines.append(f"- {key}, {spell.get('name', '?')}: {_short(spell.get('description', ''), 260)}")
        kit = "\n".join(lines)
        self._kits[cid] = kit
        return kit

    async def champions_text(self, text: str, also_ambiguous: frozenset = frozenset()) -> str:
        found = self.find_champions(text, also_ambiguous)
        kits = [kit for cid in found if (kit := await self.champion_kit(cid))]
        if not kits:
            return ""
        return (
            f"Kit oficial (parche {self.patch}) de los campeones que se mencionan. Úsalo para no "
            "equivocarte con sus habilidades; no lo copies entero:\n" + "\n".join(kits)
        )

    async def items_text(self) -> str:
        """Objetos terminados que se pueden comprar en la Grieta del Invocador en este parche."""
        if self._items_text is not None:
            return self._items_text
        if not self.version:
            return ""
        try:
            es = await self._cached("item_es.json", f"cdn/{self.version}/data/{LOCALE}/item.json")
            en = await self._cached("item_en.json", f"cdn/{self.version}/data/en_US/item.json")
        except Exception as exc:
            log.warning("No se pudo descargar la lista de objetos: %s", exc)
            return ""
        names = []
        seen = set()
        for iid, item in es.get("data", {}).items():
            gold = item.get("gold", {})
            if not (item.get("maps", {}).get("11") and gold.get("purchasable") and item.get("inStore", True)):
                continue
            boots = "Boots" in item.get("tags", [])
            if boots and gold.get("total", 0) < 900:
                continue  # las botas básicas
            if not boots and (item.get("into") or gold.get("total", 0) < 2000):
                continue  # componentes y objetos baratos: no hacen falta para hablar de builds
            if item.get("requiredChampion") or item.get("requiredAlly"):
                continue
            english = en.get("data", {}).get(iid, {}).get("name", "")
            name = item.get("name", "")
            label = f"{name} ({english})" if english and english != name else name
            if label not in seen:
                seen.add(label)
                names.append(label)
        self._items_text = (
            f"Objetos terminados que EXISTEN en la Grieta en el parche {self.patch} (español latino, "
            "entre paréntesis en inglés). No recomiendes objetos que no estén en esta lista: "
            + "; ".join(sorted(names))
        ) if names else ""
        return self._items_text


dd = DataDragon()  # una sola instancia para todo el bot
