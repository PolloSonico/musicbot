"""Carga la definición del personaje (nombre, personalidad y avatar) para la IA.

Formatos aceptados en PERSONA_FILE:
- .txt / .md: texto libre describiendo al personaje (el nombre sale de PERSONA_NAME o del archivo).
- .json: "character card" (formato de SillyTavern / chub.ai, versiones 1, 2 y 3).
- .png: character card con los datos escondidos dentro de la imagen (así se descargan de
  chub.ai). La imagen se usa además como avatar del bot.
"""

import base64
import json
import logging
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("persona")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_FILE = "personajes/lillia.txt"


@dataclass
class Character:
    name: str
    description: str  # texto corto para !personaje
    prompt: str  # todo lo que la IA necesita saber del personaje
    avatar: Optional[bytes] = None


def _png_text_chunks(data: bytes) -> dict[str, str]:
    """Lee los bloques de texto (tEXt / iTXt / zTXt) de un PNG."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("no es un PNG")
    chunks: dict[str, str] = {}
    pos = 8
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"tEXt":
            key, _, value = body.partition(b"\0")
            chunks[key.decode("latin-1")] = value.decode("latin-1")
        elif kind == b"zTXt":
            key, _, rest = body.partition(b"\0")
            chunks[key.decode("latin-1")] = zlib.decompress(rest[1:]).decode("latin-1")
        elif kind == b"iTXt":
            key, _, rest = body.partition(b"\0")
            compressed, rest = rest[0], rest[2:]
            _lang, _, rest = rest.partition(b"\0")
            _tkey, _, value = rest.partition(b"\0")
            chunks[key.decode("latin-1")] = (zlib.decompress(value) if compressed else value).decode("utf-8")
        elif kind == b"IEND":
            break
    return chunks


def _card_from_png(data: bytes) -> dict:
    chunks = _png_text_chunks(data)
    for key in ("ccv3", "chara"):
        if key in chunks:
            return json.loads(base64.b64decode(chunks[key]).decode("utf-8"))
    raise ValueError("el PNG no tiene datos de personaje (¿es una character card?)")


def _character_from_card(card: dict, avatar: Optional[bytes]) -> Character:
    data = card.get("data", card)  # v2/v3 guardan todo en "data"; v1 va plano
    name = (data.get("name") or "Personaje").strip()

    def fill(text: str) -> str:
        return (text or "").replace("{{char}}", name).replace("<BOT>", name) \
            .replace("{{user}}", "el usuario").replace("<USER>", "el usuario").strip()

    sections = [
        ("Descripción", data.get("description")),
        ("Personalidad", data.get("personality")),
        ("Escenario", data.get("scenario")),
        ("Instrucciones del autor de la tarjeta", data.get("system_prompt")),
        ("Ejemplos de cómo habla", data.get("mes_example")),
        ("Ejemplo de saludo", data.get("first_mes")),
        ("Notas finales", data.get("post_history_instructions")),
    ]
    prompt = "\n\n".join(f"## {title}\n{fill(text)}" for title, text in sections if text and text.strip())
    description = fill(data.get("creator_notes") or data.get("description") or "")[:300]
    return Character(name=name, description=description, prompt=prompt, avatar=avatar)


def load_character() -> Character:
    path = BASE_DIR / (os.getenv("PERSONA_FILE", "").strip() or DEFAULT_FILE)
    avatar_path = os.getenv("PERSONA_AVATAR", "").strip()
    avatar = (BASE_DIR / avatar_path).read_bytes() if avatar_path else None

    suffix = path.suffix.lower()
    if suffix == ".png":
        image = path.read_bytes()
        character = _character_from_card(_card_from_png(image), avatar or image)
    elif suffix == ".json":
        character = _character_from_card(json.loads(path.read_text(encoding="utf-8")), avatar)
    else:
        text = path.read_text(encoding="utf-8").strip()
        name = os.getenv("PERSONA_NAME", "").strip() or path.stem.replace("_", " ").title()
        first_line = text.splitlines()[0] if text else ""
        character = Character(name=name, description=first_line[:300], prompt=text, avatar=avatar)

    if os.getenv("PERSONA_NAME", "").strip():
        character.name = os.getenv("PERSONA_NAME").strip()
    log.info("Personaje cargado desde %s: %s", path.name, character.name)
    return character
