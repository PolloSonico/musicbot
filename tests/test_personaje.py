"""El personaje: órdenes ocultas, emojis, muletillas, comillas, IA de respaldo y memoria larga."""

import asyncio
import types

import pytest

import ia_gemini
import persona


@pytest.fixture
def lillia():
    p = persona.Persona.__new__(persona.Persona)
    p._recent_starts = {}
    return p


def test_ordenes_ocultas():
    text, actions = persona.extract_actions("¡Lo recuerdo!\n[[RECORDAR: su main es Jinx]]\n[[PLAY: Kesha - TiK ToK]]")
    assert text == "¡Lo recuerdo!"
    assert actions == [("recordar", "su main es Jinx"), ("play", "Kesha - TiK ToK")]


def test_como_mucho_dos_emojis(lillia):
    ch = types.SimpleNamespace(id=1)
    assert lillia._limit_emojis(ch, "Hola 🌸 qué lindo ✨🌿") == "Hola 🌸 qué lindo ✨"
    assert lillia._limit_emojis(ch, "Eep 🌸🌸") == "Eep 🌸"


def test_menos_ay(lillia):
    ch = types.SimpleNamespace(id=2)
    assert lillia._limit_emojis(ch, "¡Ay, otra canción!").startswith("¡Ay")  # la primera vez se deja
    assert lillia._limit_emojis(ch, "¡Ay, esa de Shakira!") == "¡Esa de Shakira!"
    assert lillia._limit_emojis(ch, "¡Ay! Qué lindo") == "Qué lindo"
    assert lillia._limit_emojis(ch, "¡Eep! Hola") == "¡Eep! Hola"
    assert lillia._limit_emojis(ch, "¡Ay, de nuevo!").startswith("¡Ay")  # pasaron 3 mensajes: se permite


def test_emojis_del_servidor():
    class Emoji:
        def __init__(self, name, id_):
            self.name, self.id, self.available, self.animated = name, id_, True, False

        def is_usable(self):
            return True

        def __str__(self):
            return f"<:{self.name}:{self.id}>"

    guild = types.SimpleNamespace(emojis=[Emoji("lillia_love", 1)])
    assert persona.apply_server_emojis("hola :lillia_love: y :otro:", guild) == "hola <:lillia_love:1> y :otro:"


def test_sin_comillas():
    assert ia_gemini._strip_wrapping_quotes('"¡Hola!"') == "¡Hola!"
    assert ia_gemini._strip_wrapping_quotes('Dijo "hola"') == 'Dijo "hola"'


def test_preguntas_detectadas():
    assert persona.LOL_RE.search("qué build le hago a Jinx")
    assert persona.ESPORTS_RE.search("cómo va T1 en el mundial de lol")
    assert not persona.ESPORTS_RE.search("el mundial de fútbol")
    assert persona.LOL_STATS_RE.search("como salió mi última partida?")
    assert persona.SONG_RE.search("de qué trata esta canción?")


def _backend(answers):
    """GeminiBackend con un cliente falso: `answers` es una lista de textos o excepciones."""
    character = types.SimpleNamespace(name="Lillia", prompt="x", description="", avatar=None)
    backend = ia_gemini.GeminiBackend("prueba", character, "español", [])
    backend.models = ["gemini-falso-flash-lite"]

    async def generate(model, contents, config):
        answer = answers.pop(0) if answers else "ok"
        if isinstance(answer, Exception):
            raise answer
        return types.SimpleNamespace(text=answer, candidates=[], usage_metadata=types.SimpleNamespace(total_token_count=100))

    backend.client = types.SimpleNamespace(aio=types.SimpleNamespace(models=types.SimpleNamespace(generate_content=generate)))
    return backend


def test_modelo_dado_de_baja_y_respaldo(data_dir):
    from google.genai import errors

    backend = _backend([errors.APIError(404, {"error": {"code": 404, "message": "no", "status": "NOT_FOUND"}})])
    backend.models = ["gemini-viejo", "gemini-falso-flash-lite"]
    assert asyncio.run(backend.ask("c", "hola")) == "ok"
    assert "gemini-viejo" in backend.retired and "gemini-viejo" not in backend.models

    # Sin cupo en Gemini -> responde el respaldo y se aprende el límite diario.
    (data_dir / "gemini_estado.json").unlink()  # empezar el día de cero
    quota = errors.APIError(429, {"error": {"code": 429, "message": "Quota exceeded per day", "status": "RESOURCE_EXHAUSTED"}})
    backend = _backend(["uno", quota])

    async def backup_chat(system, contents, max_tokens=900):
        return "Hola desde el respaldo"

    backend.backup = types.SimpleNamespace(available=lambda: True, chat=backup_chat, provider="Groq", close=None)
    assert asyncio.run(backend.ask("c", "hola")) == "uno"
    assert asyncio.run(backend.ask("c", "hola")) == "Hola desde el respaldo"
    assert not backend.gemini_available() and backend.available()
    assert backend.usage["limites"]["gemini-falso-flash-lite"] == 1


def test_memoria_larga(data_dir, monkeypatch):
    monkeypatch.setattr(ia_gemini, "MAX_TURNS", 4)
    monkeypatch.setattr(ia_gemini, "SUMMARY_BATCH", 4)
    backend = _backend(["r1", "r2", "r3", "r4", "- El dijo que le gusta Shakira"])

    async def run():
        for i in range(4):  # con memoria corta de 4: a partir del 3.º empiezan a salir mensajes
            await backend.ask("c", f"El: mensaje {i}")
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert backend.summaries["c"]["resumen"] == "- El dijo que le gusta Shakira"
    assert backend.summaries["c"]["pendientes"] == []
    backend.reset("c")
    assert "c" not in backend.summaries


def test_sin_cupo_de_busqueda_responde_sin_buscar(data_dir):
    from google.genai import errors

    quota = errors.APIError(429, {"error": {"code": 429, "message": "Quota exceeded: grounding", "status": "RESOURCE_EXHAUSTED"}})
    backend = _backend([quota, "respuesta sin buscar"])
    assert asyncio.run(backend.ask("c", "build de jinx", search=True)) == "respuesta sin buscar"
    assert backend.gemini_available() and not backend.cooldowns.get("gemini-falso-flash-lite")
    assert backend.search_paused_until > 0


def test_respaldo_elige_modelos_nuevos_si_los_viejos_no_existen():
    import ia_respaldo

    ids = ["whisper-large-v3", "openai/gpt-oss-20b", "meta-llama/llama-prompt-guard-2-86m", "qwen/qwen3.8-27b",
           "openai/gpt-oss-120b", "groq/compound", "playai-tts"]
    assert ia_respaldo.pick_models(ids) == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]

    ai = ia_respaldo.BackupAI(key="k", url="https://x/v1", models=["viejo-1", "viejo-2"])
    calls = []

    class Resp:
        def __init__(self, status, body):
            self.status, self.body, self.headers = status, body, {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return str(self.body)

        async def json(self):
            return self.body

    class Session:
        closed = False

        def post(self, url, json, headers):
            calls.append(json["model"])
            if json["model"].startswith("viejo"):
                return Resp(400, {"error": {"code": "model_decommissioned"}})
            return Resp(200, {"choices": [{"message": {"content": "¡Hola!"}}]})

        def get(self, url, headers):
            return Resp(200, {"data": [{"id": i} for i in ids]})

    ai._session = Session()
    assert ai.available()
    assert asyncio.run(ai.chat("sistema", [{"role": "user", "parts": [{"text": "hola"}]}])) == "¡Hola!"
    assert calls == ["viejo-1", "viejo-2", "openai/gpt-oss-120b"]


def test_preguntas_de_calendario_buscan():
    assert persona.ESPORTS_RE.search("decime cuando vuelve a jugar josedeodo para la lcs")
    assert persona.SCHEDULE_RE.search("cuando vuelve a jugar josedeodo")
    assert persona.SCHEDULE_RE.search("contra quién juega Isurus?")
    assert persona.SCHEDULE_RE.search("quién ganó la final?")
    assert not persona.SCHEDULE_RE.search("poneme una de Shakira")
    assert not persona.ESPORTS_RE.search("la última partida me fue mal")
    assert persona._local_tz().startswith("UTC")
    assert "{tz}" not in persona.ESPORTS_INSTRUCTIONS.format(tz="UTC-3")


def test_preguntas_de_hoy_buscan():
    for text in ("este finde va a llover en berazategui?", "qué temperatura hace?", "a cuánto está el dólar blue?",
                 "qué pasó con la final?"):
        assert persona.NOW_RE.search(text) or persona.SCHEDULE_RE.search(text), text
    assert not persona.NOW_RE.search("hola lillia, cómo estás?")
