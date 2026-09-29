"""Prueba la conexión con Character.AI y muestra el error real. Uso: windows\\diagnostico_cai.bat"""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
import persona  # noqa: E402  (lee CAI_TOKEN / CAI_CHARACTER_ID del .env)
from PyCharacterAI import get_client  # noqa: E402


async def probar(nombre: str, **kwargs) -> bool:
    print(f"\n--- Prueba: {nombre} ---")
    client = None
    try:
        client = await get_client(token=persona.CAI_TOKEN, **kwargs)
        print("  [OK] Token aceptado (cuenta:", client.get_account_id(), ")")
        char = await client.character.fetch_character_info(persona.CAI_CHARACTER_ID)
        print("  [OK] Personaje:", char.name)
        chat, _ = await client.chat.create_chat(persona.CAI_CHARACTER_ID, greeting=False)
        print("  [OK] Chat creado")
        turn = await client.chat.send_message(persona.CAI_CHARACTER_ID, chat.chat_id, "Hola, esto es una prueba.")
        print("  [OK] Respuesta:", turn.get_primary_candidate().text[:200])
        return True
    except Exception as exc:
        print("  [FALLO]", persona._describe(exc))
        return False
    finally:
        if client:
            try:
                await client.close_session()
            except Exception:
                pass


async def main() -> None:
    if not persona.CAI_TOKEN or not persona.CAI_CHARACTER_ID:
        print("Falta CAI_TOKEN o CAI_CHARACTER_ID en .env")
        return
    import cai_compat
    import curl_cffi
    from importlib.metadata import version

    print("PyCharacterAI", version("PyCharacterAI"), "| curl_cffi", curl_cffi.__version__)

    variantes = [
        ("chrome", True), ("chrome", False),
        ("chrome131", True), ("edge101", True),
        ("safari", True), ("firefox147", True), ("firefox135", True),
    ]
    for navegador, warmup in variantes:
        cai_compat.install(impersonate=navegador, warmup=warmup)
        nombre = f"imitando {navegador}" + (" + visita previa" if warmup else "")
        if await probar(nombre, impersonate=navegador):
            print(f"\n>>> FUNCIONA con: {nombre}")
            if navegador != "chrome":
                print(f">>> Pon esta linea en tu .env:  CAI_IMPERSONATE={navegador}")
            if not warmup:
                print(">>> Pon esta linea en tu .env:  CAI_WARMUP=false")
            return
    print("\n>>> Ninguna variante funciono. Pasale este resultado a Claude.")


if __name__ == "__main__":
    asyncio.run(main())
