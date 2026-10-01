"""Consejos de la selección de campeones: cada modo con sus reglas (runas, al azar, banco)."""

import seleccion
from datadragon import dd


def setup_module():
    dd.version = "16.19.1"
    dd._set_champions({"data": {"Lillia": {"name": "Lillia", "key": "876"}, "Jinx": {"name": "Jinx", "key": "222"},
                                "Ahri": {"name": "Ahri", "key": "103"}, "Garen": {"name": "Garen", "key": "86"}}})


def draft(completed: bool, enemy: int = 0):
    return {"localPlayerCellId": 2, "benchEnabled": False,
            "myTeam": [{"cellId": 2, "championId": 876, "assignedPosition": "jungle"}, {"cellId": 1, "championId": 222}],
            "theirTeam": [{"cellId": 7, "championId": enemy}],
            "actions": [[{"type": "ban", "actorCellId": 2, "completed": True, "championId": 86}],
                        [{"type": "pick", "actorCellId": 2, "completed": completed, "championId": 876}]]}


def aram():
    return {"localPlayerCellId": 0, "benchEnabled": True, "benchChampions": [{"championId": 103}],
            "myTeam": [{"cellId": 0, "championId": 222}], "theirTeam": [], "actions": []}


def test_grieta_espera_a_que_confirme():
    mode = seleccion.mode_of({"id": 420, "gameMode": "CLASSIC"}, draft(False))
    assert mode.free_pick and mode.runes is True and mode.label == "Clasificatoria Solo/Dúo"
    assert seleccion.my_champion(draft(False), mode) == 0  # solo pasando el mouse: todavía no
    assert seleccion.my_champion(draft(True), mode) == 876
    prompt = seleccion.build_prompt("Mariano", mode, draft(True, enemy=103), 876)
    assert "Lillia de jungla" in prompt and "Ahri" in prompt and "runas" in prompt and "Jinx" in prompt


def test_aram_al_azar_con_banco():
    mode = seleccion.mode_of({"id": 450, "gameMode": "ARAM"}, aram())
    assert not mode.free_pick and mode.runes is True
    assert seleccion.my_champion(aram(), mode) == 222
    prompt = seleccion.build_prompt("Mariano", mode, aram(), 222)
    assert "banco: Ahri" in prompt and "al azar" in prompt


def test_caos_y_arena_sin_runas():
    caos = seleccion.mode_of({"id": 2400, "gameMode": "KIWI"}, aram())
    assert caos.label == "ARAM: Caos" and caos.runes is False and not caos.free_pick
    assert "NO hay runas" in seleccion.build_prompt("M", caos, aram(), 222)
    arena = seleccion.mode_of({"id": 1700, "gameMode": "CHERRY"}, draft(True))
    assert arena.free_pick and arena.runes is False and arena.arena
    assert "dupla" in seleccion.build_prompt("M", arena, draft(True), 876)


def test_modos_sin_consejo_o_desconocidos():
    assert seleccion.mode_of({"id": 1810, "gameMode": "STRAWBERRY"}, aram()) is None
    raro = seleccion.mode_of({"id": 9999, "gameMode": "NUEVO", "description": "Modo nuevo"}, draft(True))
    assert raro.runes is None and raro.label == "Modo nuevo"
    assert "no las recomiendes" in seleccion.build_prompt("M", raro, draft(True), 876)
