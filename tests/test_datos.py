"""Guardado de archivos, calendario, memoria por persona, Wrapped y respaldos."""

import types
import zipfile
from datetime import date

import calendario
import jsonio
import memoria_personas
import respaldo
import wrapped


def test_guardado_seguro(data_dir):
    path = data_dir / "x.json"
    jsonio.save_json(path, {"a": 1})
    assert jsonio.load_json(path) == {"a": 1}
    assert not (data_dir / "x.json.tmp").exists()
    # Un JSON roto se aparta y se sigue con el valor por defecto.
    path.write_text('{"roto":', encoding="utf-8")
    assert jsonio.load_json(path, {}) == {}
    assert (data_dir / "x.json.roto").exists()
    # default=None distingue "no existe" de "vacío" (bug que dejaba sin nombres a los campeones).
    assert jsonio.load_json(data_dir / "no_existe.json", None) is None


def test_jsonl_ignora_lineas_cortadas(data_dir):
    path = data_dir / "e.jsonl"
    jsonio.append_jsonl(path, {"n": 1})
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"cort')
    assert jsonio.read_jsonl(path) == [{"n": 1}]


def test_calendario_mundial_y_anuales():
    assert [e.name for e in calendario.events_on(date(2026, 11, 14))] == ["Gran final del Mundial 2026"]
    assert any("Fase Suiza" in e.name for e in calendario.events_on(date(2026, 10, 25)))
    assert [e.name for e in calendario.events_on(date(2030, 7, 22))] == ["Aniversario de Lillia"]
    # Fin de año cruza de un año al otro.
    assert [e.name for e in calendario.events_on(date(2031, 1, 1))] == ["Fin de año"]
    assert calendario.events_on(date(2026, 9, 1)) == []


def test_memoria_por_persona(data_dir):
    user = types.SimpleNamespace(id=1, name="mariano", display_name="Mariano")
    assert memoria_personas.add(user, "su main es Jinx")
    assert not memoria_personas.add(user, "Su main es Jinx.")  # repetido
    assert not memoria_personas.add(user, "su teléfono es 1155554444")  # dato privado
    assert memoria_personas.facts(user) == ["su main es Jinx"]
    assert memoria_personas.forget(user, "jinx") == 1
    assert memoria_personas.facts(user) == []


def test_wrapped_musica_y_league(data_dir):
    guild = types.SimpleNamespace(id=5, name="S")
    user = types.SimpleNamespace(id=1, name="mariano")
    track = lambda t: types.SimpleNamespace(title=t, url="u", uploader="", duration=200, requester_id=1, requester="M")
    wrapped.record_request(guild, user, [track("Kesha - TiK ToK (Official Video)"), track("Kesha - TiK ToK (Official Video)")])
    wrapped.record_listen(guild, track("Kesha - TiK ToK (Official Video)"), 190, [types.SimpleNamespace(id=1)], False)
    wrapped.record_lol(1, 5, "LA2_1", {"kills": 10, "deaths": 2, "assists": 5, "win": True}, {"gameDuration": 1500}, "ARAM", "Lillia")
    month = f"{date.today():%Y-%m}"
    st = wrapped.user_stats(month, 5, 1)
    assert st["pedidos"] == 2 and st["top_canciones"][0] == ("Kesha - TiK ToK", 2)
    assert st["top_artistas"][0][0] == "Kesha"
    lol = wrapped.lol_user_stats(month, 5, 1)
    assert lol["partidas"] == 1 and lol["victorias"] == 1 and lol["kda"] == 7.5


def test_meses():
    hoy = date(2026, 1, 15)
    assert wrapped.parse_month("", hoy) == "2026-01"
    assert wrapped.parse_month("pasado", hoy) == "2025-12"
    assert wrapped.parse_month("diciembre", hoy) == "2025-12"
    assert wrapped.parse_month("2026-09", hoy) == "2026-09"
    assert wrapped.parse_month("cualquiera", hoy) is None


def test_copia_de_seguridad(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(respaldo, "BACKUP_DIR", tmp_path)
    jsonio.save_json(data_dir / "memoria_personas.json", {"1": {}})
    (data_dir / "datadragon").mkdir()
    (data_dir / "datadragon" / "grande.json").write_text("{}")
    path = respaldo.make_backup()
    assert path is not None and zipfile.ZipFile(path).namelist() == ["memoria_personas.json"]
    assert respaldo.make_backup() == path  # una sola por día


def test_estado_separa_cortes_de_internet(tmp_path, monkeypatch):
    import estado

    today = date.today().isoformat()
    log = tmp_path / "bot.log"
    log.write_text("\n".join([
        f"{today} 09:17:53,453 [ERROR] discord.client: Attempting a reconnect in 1.69s",
        "Traceback (most recent call last):",
        f"{today} 09:17:54,014 [WARNING] datadragon: No se pudo consultar Data Dragon: Cannot connect to host x:443 [getaddrinfo failed]",
        f"{today} 09:18:03,192 [ERROR] discord.client: Attempting a reconnect in 15.53s",
        f"{today} 14:02:00,000 [WARNING] riot: Revisando partidas nuevas: sin conexión (timeout)",
        f"{today} 15:00:00,000 [ERROR] persona: algo se rompió de verdad",
        "2020-01-01 10:00:00,000 [ERROR] viejo: no cuenta",
    ]), encoding="utf-8")
    monkeypatch.setattr(estado, "LOG_FILE", log)
    result = estado._todays_problems()
    assert result["cortes"] == ["09:17", "14:02"]
    assert (result["errores"], result["advertencias"]) == (1, 0)
    assert result["ultimos"] == ["15:00 `persona` algo se rompió de verdad"]
