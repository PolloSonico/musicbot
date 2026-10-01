"""OP.GG (servidor MCP): traducir la pregunta a parámetros y armar el contexto, con OP.GG simulado."""

import asyncio

import opgg
from datadragon import dd


def setup_module():
    dd.version = "16.19.1"
    dd._set_champions({"data": {"KogMaw": {"name": "Kog'Maw", "key": "96"}, "Jinx": {"name": "Jinx", "key": "222"},
                                "MonkeyKing": {"name": "Wukong", "key": "62"}, "Ahri": {"name": "Ahri", "key": "103"}}})


def test_nombres_posicion_y_modo():
    assert opgg.champion_keys("KogMaw")[:2] == ["KOG_MAW", "KOGMAW"]
    assert opgg.champion_keys("MonkeyKing")[0] == "WUKONG"
    assert opgg.position_in("build de jinx adc") == "adc"
    assert opgg.position_in("qué runas llevo en la jungla") == "jungle"
    assert opgg.position_in("build de jinx") == "none"
    assert opgg.game_mode_in("build de jinx en aram") == "aram" and opgg.game_mode_in("build de jinx") == "ranked"


def test_build_prueba_nombres_hasta_que_opgg_acepta(data_dir, monkeypatch):
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments.get("champion"), arguments.get("position"), arguments.get("lang")))
        if arguments.get("champion") == "KOG_MAW":
            raise opgg.ToolError("campeón desconocido")
        return '{"data": {"core_items": {"ids_names": ["Filo de la ira de Guinsoo"]}}}'

    monkeypatch.setattr(opgg, "call_tool", call_tool)
    text = asyncio.run(opgg.context_for("qué build le hago a kog'maw adc?"))
    assert "Kog'Maw de adc" in text and "Guinsoo" in text
    assert ("lol_get_champion_analysis", "KOGMAW", "adc", "es_ES") in calls
    calls.clear()
    asyncio.run(opgg.context_for("runas de kog'maw adc"))
    assert [c[1] for c in calls] == ["KOGMAW"]  # ya recuerda la forma que funcionó


def test_enfrentamiento_y_tier_list(data_dir, monkeypatch):
    calls = []

    async def call_tool(name, arguments):
        calls.append(name)
        return "datos"

    monkeypatch.setattr(opgg, "call_tool", call_tool)
    text = asyncio.run(opgg.context_for("cómo juego jinx contra ahri en mid"))
    assert "Jinx contra Ahri" in text and calls[0] == "lol_get_lane_matchup_guide"
    calls.clear()
    assert "Tier list" in asyncio.run(opgg.context_for("cuál es el meta de la jungla?"))
    assert calls == ["lol_list_lane_meta_champions"]


def test_si_opgg_no_responde_no_rompe(data_dir, monkeypatch):
    async def call_tool(name, arguments):
        raise RuntimeError("caído")

    monkeypatch.setattr(opgg, "call_tool", call_tool)
    assert asyncio.run(opgg.context_for("build de jinx")) == ""


def test_sin_linea_prueba_las_habituales_y_arena_no_se_consulta(data_dir, monkeypatch):
    dd.champions["KogMaw"]["tags"] = ["Marksman", "Mage"]
    assert opgg.positions_for("KogMaw", "none") == ["adc", "mid", "support"]
    assert opgg.positions_for("KogMaw", "top") == ["top"]
    calls = []

    async def call_tool(name, arguments):
        calls.append((arguments.get("champion"), arguments.get("position")))
        if arguments.get("position") == "adc":
            raise opgg.ToolError('{"position":["The selected position is invalid."]}')
        return "build de mid"

    monkeypatch.setattr(opgg, "call_tool", call_tool)
    text = asyncio.run(opgg.context_for("qué build le armo a kogmaw?"))
    assert "Kog'Maw de mid" in text
    assert {c[1] for c in calls} == {"adc", "mid"} and {c[0] for c in calls} == {"KOG_MAW"}  # no culpó al nombre
    calls.clear()
    assert asyncio.run(opgg.context_for("que build le armo a kogmaw en arena?")) == "" and calls == []
