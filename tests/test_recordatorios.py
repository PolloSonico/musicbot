"""Recordatorios: entender el "cuándo", guardar, repetir, borrar y a quién se pinguea."""

import asyncio
import types
from datetime import datetime

import pytest

import persona
import recordatorios as r

NOW = datetime(2026, 10, 1, 15, 30)  # jueves


@pytest.mark.parametrize("text, when, rest, repeat", [
    ("en 30 minutos sacar la pizza", datetime(2026, 10, 1, 16, 0), "sacar la pizza", None),
    ("mañana 21:00 sesión de D&D", datetime(2026, 10, 2, 21, 0), "sesión de D&D", None),
    ("viernes 21hs D&D", datetime(2026, 10, 2, 21, 0), "D&D", None),
    ("el jueves a las 10 algo", datetime(2026, 10, 8, 10, 0), "algo", None),  # hoy ya pasó: el próximo
    ("a las 9 de la noche cena", datetime(2026, 10, 1, 21, 0), "cena", None),
    ("15/10 20:30 cumple", datetime(2026, 10, 15, 20, 30), "cumple", None),
    ("todos los viernes 21hs D&D", datetime(2026, 10, 2, 21, 0), "D&D", "semanal"),
    ("sábado sesión", datetime(2026, 10, 3, 10, 0), "sesión", None),
])
def test_entiende_el_cuando(text, when, rest, repeat):
    assert r.parse_when(text, NOW) == (when, rest, repeat)


def test_sin_fecha_no_adivina():
    assert r.parse_when("comprar pan", NOW) is None


def test_guardar_repetir_y_borrar(data_dir):
    with pytest.raises(ValueError, match="ya pasó"):
        r.add(1, 5, 9, [], datetime(2026, 10, 1, 10, 0), "x", now=NOW)
    a = r.add(1, 5, 9, [2, 2, 3], datetime(2026, 10, 2, 21, 0), "D&D", "semanal", now=NOW)
    assert a["para"] == [2, 3] and a["id"] == 1
    due = r.take_due(datetime(2026, 10, 2, 21, 1))
    assert [d["id"] for d in due] == [1]
    assert r.pending_for(2)[0]["cuando"] == "2026-10-09T21:00"  # semanal: se reprograma
    assert r.remove(1, 99) == "ajeno"
    assert r.remove(1, 2) == "salido" and r.pending_for(2) == [] and r.pending_for(3)
    assert r.remove(1, 1) == "borrado" and r.pending_for(3) == []


def test_solo_pinguea_a_los_destinatarios(data_dir):
    sent = {}

    class Channel:
        async def send(self, text, allowed_mentions=None):
            sent["text"], sent["allowed"] = text, allowed_mentions
            return types.SimpleNamespace(edit=lambda **k: asyncio.sleep(0))

    bot = types.SimpleNamespace(get_guild=lambda gid: None, get_channel=lambda cid: Channel(),
                                get_user=lambda uid: None, get_cog=lambda name: None)
    cog = r.Recordatorios.__new__(r.Recordatorios)
    cog.bot = bot
    reminder = {"id": 1, "autor": 1, "para": [2], "servidor": 5, "canal": 9,
                "cuando": datetime.now().isoformat(timespec="minutes"), "texto": "@everyone D&D"}
    asyncio.run(cog._deliver(reminder))
    allowed = sent["allowed"]
    assert "<@2>" in sent["text"]
    assert allowed.everyone is False and allowed.roles is False and [u.id for u in allowed.users] == [2]


def test_orden_oculta_de_recordatorio():
    text, actions = persona.extract_actions("¡Te aviso!\n[[RECORDATORIO: 2026-10-02 21:00 | yo | sesión de D&D | semanal]]\n[[RECORDAR: juega D&D]]")
    assert actions == [("recordatorio", "2026-10-02 21:00 | yo | sesión de D&D | semanal"), ("recordar", "juega D&D")]
