"""Prueba la IA configurada (Gemini) y muestra qué modelos usa y si hay cupo. Uso: windows\\diagnostico_ia.bat"""
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
logging.basicConfig(level=logging.INFO, format="  %(message)s")

import persona  # noqa: E402


async def main() -> None:
    backend = persona.create_backend()
    if backend is None:
        print("\n>>> La IA no está configurada. Revisa GEMINI_API_KEY en .env y el mensaje de arriba.")
        return
    print(f"\nIA: {backend.provider} | Personaje: {backend.name}")
    try:
        await backend.start()
    except Exception as exc:
        print(f"\n>>> No se pudo iniciar: {exc}")
        return
    for model in getattr(backend, "models", []):
        until = backend.cooldowns.get(model, 0)
        estado = "sin cupo hasta " + datetime.fromtimestamp(until).strftime("%d/%m %H:%M") if until > datetime.now().timestamp() else "disponible"
        print(f"  - {model}: {estado}")
    print("\nMandando un mensaje de prueba...")
    reply = await backend.ask("diagnostico", "Prueba: Hola, preséntate en una frase.")
    backend.reset("diagnostico")
    if reply:
        print(f"\n>>> FUNCIONA. Respuesta de {backend.name}:\n{reply}")
    elif not backend.available():
        print("\n>>> Se acabó el cupo gratis por ahora. La música sigue funcionando; la IA vuelve sola.")
    else:
        print("\n>>> No hubo respuesta. Pásale lo de arriba a Claude.")


if __name__ == "__main__":
    asyncio.run(main())
