"""Búsqueda en internet (Tavily) y clima (Open-Meteo), con las respuestas simuladas."""

import asyncio
from datetime import date

import busqueda
import clima


def test_lugar_de_la_pregunta(monkeypatch):
    assert clima.place_in("este finde va a llover en berazategui?") == "berazategui"
    assert clima.place_in("qué temperatura hace en Mar del Plata?") == "Mar del Plata"
    monkeypatch.setattr(clima, "DEFAULT_PLACE", "Berazategui")
    assert clima.place_in("va a llover el finde?") == "Berazategui"
    assert clima.WEATHER_RE.search("este finde va a llover?") and clima.WEATHER_RE.search("llueve mañana?")
    assert not clima.WEATHER_RE.search("poné una canción")


def test_pronostico_en_texto():
    data = {"current": {"temperature_2m": 18.2, "apparent_temperature": 17.0, "relative_humidity_2m": 70,
                        "weather_code": 3, "wind_speed_10m": 12},
            "daily": {"time": ["2026-10-01", "2026-10-02", "2026-10-03"], "weather_code": [3, 61, 95],
                      "temperature_2m_max": [20, 18, 16], "temperature_2m_min": [12, 11, 10],
                      "precipitation_probability_max": [10, 60, 90], "precipitation_sum": [0, 3.5, 22],
                      "wind_speed_10m_max": [15, 20, 40]}}
    text = clima.format_forecast("Berazategui, Buenos Aires, Argentina", data, date(2026, 10, 1))
    assert "Ahora: 18.2 °C" in text and "nublado" in text
    assert "mañana 02/10: lluvia débil, 11 a 18 °C, probabilidad de lluvia 60% (3.5 mm)" in text
    assert "sábado 03/10: tormenta" in text and "90%" in text


def test_clima_completo_con_api_simulada(monkeypatch):
    async def get(url, params):
        if url == clima.GEO_URL:
            return {"results": [{"name": "Berazategui", "country_code": "ES", "latitude": 1, "longitude": 1},
                                {"name": "Berazategui", "admin1": "Buenos Aires", "country": "Argentina",
                                 "country_code": "AR", "latitude": -34.76, "longitude": -58.21}]}
        assert params["latitude"] == "-34.76"  # eligió el de Argentina
        return {"daily": {"time": ["2026-10-03"], "weather_code": [63], "temperature_2m_max": [17],
                          "temperature_2m_min": [11], "precipitation_probability_max": [80],
                          "precipitation_sum": [9], "wind_speed_10m_max": [22]}}

    monkeypatch.setattr(clima, "_get", get)
    text = asyncio.run(clima.context_for("este finde va a llover en berazategui?"))
    assert "Berazategui, Buenos Aires, Argentina" in text and "probabilidad de lluvia 80%" in text


def test_busqueda_cuenta_el_cupo_y_no_repite(data_dir, monkeypatch):
    calls = []

    class Resp:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self, content_type=None):
            return {"answer": "Jinx: Kraken, Huracán, Filo infinito.", "results": [
                {"title": "Jinx build", "url": "https://x.gg/jinx", "content": "Runas: Compás letal..."}]}

    class Session:
        closed = False

        def post(self, url, json, headers):
            calls.append((json["query"], headers["Authorization"]))
            return Resp()

    monkeypatch.setattr(busqueda, "KEY", "tvly-prueba")
    monkeypatch.setattr(busqueda, "_session", Session())
    monkeypatch.setattr(busqueda, "_cache", {})
    text = asyncio.run(busqueda.context_for("League of Legends parche 26.19: build de jinx"))
    assert "Resumen: Jinx: Kraken" in text and "https://x.gg/jinx" in text
    asyncio.run(busqueda.context_for("League of Legends parche 26.19: build de jinx"))
    assert calls == [("League of Legends parche 26.19: build de jinx", "Bearer tvly-prueba")]  # la 2.ª salió de la memoria
    assert busqueda.usage()[0] == 1
    monkeypatch.setattr(busqueda, "MONTHLY_LIMIT", 1)
    assert not busqueda.available()  # llegó al tope del mes: no gasta más


def test_orden_web():
    import persona

    text, actions = persona.extract_actions("[[WEB: precio dólar blue hoy]]")
    assert text == "" and actions == [("web", "precio dólar blue hoy")]
