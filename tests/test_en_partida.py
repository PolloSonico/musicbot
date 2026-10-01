"""'¿Alguien está en partida?': Lillia consulta a Riot quién juega ahora."""

import asyncio
import types

import persona
import riot
import riot_cuentas as cuentas


def test_detecta_la_pregunta():
    for text in ("@Lillia alguien esta en partida de lol?", "quién está jugando?", "@Lucía está en una ranked?"):
        assert persona.LIVE_RE.search(text) and persona.LIVE_HINT_RE.search(text), text
    assert persona.LIVE_EVERYONE_RE.search("alguien esta en partida de lol?")
    assert not persona.LIVE_RE.search("poné una canción")


def test_quien_juega_ahora(data_dir, monkeypatch):
    cuentas.put(1, {"puuid": "P1", "nombre": "Pollo Sonico", "tag": "0CH8", "plataforma": "la2", "servidor": 5})
    cuentas.put(2, {"puuid": "P2", "nombre": "Lucia", "tag": "LAS", "plataforma": "la2", "servidor": 5})
    cuentas.put(3, {"puuid": "P3", "nombre": "Otro", "tag": "X", "plataforma": "la2", "servidor": 9})
    game = {"queueId": 420, "gameLength": 600, "participants": [
        {"puuid": "P1", "championId": 876, "teamId": 100}, {"puuid": "P2", "championId": 222, "teamId": 100}]}
    asked = []

    async def live_game(puuid, platform):
        asked.append(puuid)
        return game if puuid in ("P1", "P2") else None

    monkeypatch.setattr(riot.client, "live_game", live_game)
    monkeypatch.setattr(riot, "enabled", lambda: True)
    monkeypatch.setattr(riot.lcu_mod, "ENABLED", False)
    cog = riot.Riot.__new__(riot.Riot)
    cog._live_cache = {}
    names = {1: "Mariano", 2: "Lucía"}
    guild = types.SimpleNamespace(id=5, get_member=lambda uid: types.SimpleNamespace(display_name=names[uid]) if uid in names else None)
    cog.bot = types.SimpleNamespace(get_user=lambda uid: None)

    async def no_dd():
        return None

    cog._ensure_dd = no_dd
    text = asyncio.run(cog.live_context(guild, [], everyone=True))
    assert "Mariano (Pollo Sonico#0CH8): EN PARTIDA de Clasificatoria Solo/Dúo" in text
    assert "junto con Lucía" in text and "10 min" in text
    assert "Otro" not in text  # es de otro servidor
    asyncio.run(cog.live_context(guild, [], everyone=True))
    assert sorted(asked) == ["P1", "P2"]  # la segunda vez usa lo guardado (1 minuto)


def test_fin_de_partida_solo_comentario(data_dir, monkeypatch):
    cuentas.put(1, {"puuid": "P1", "nombre": "El", "tag": "X", "plataforma": "la2", "servidor": 5})
    sent, asked = [], []

    class Channel:
        async def send(self, text=None, **kwargs):
            sent.append((text, kwargs.get("embed")))

    guild = types.SimpleNamespace(id=5, name="S", get_member=lambda uid: types.SimpleNamespace(display_name="El"))
    cog = riot.Riot.__new__(riot.Riot)
    cog.bot = types.SimpleNamespace(get_guild=lambda gid: guild, guilds=[guild],
                                    get_cog=lambda name: types.SimpleNamespace(channel_for=lambda g: Channel()))

    async def comment(channel, situation, timeout=30):
        asked.append(situation)
        return "¡El! Vi que terminó tu batalla en la Arena con Viktor..."

    cog._comment = comment
    import time as _time
    match = {"metadata": {"matchId": "LA2_9"}, "info": {
        "queueId": 1700, "gameMode": "CHERRY", "gameDuration": 1474, "gameEndTimestamp": int(_time.time() * 1000),
        "participants": [{"puuid": "P1", "championName": "Viktor", "kills": 4, "deaths": 8, "assists": 4,
                          "placement": 6, "win": False, "totalDamageDealtToChampions": 26500}]}}
    asyncio.run(cog._announce_match(match, [(1, cuentas.get(1))]))
    assert sent == [("¡El! Vi que terminó tu batalla en la Arena con Viktor...", None)]  # sin cuadro de resultado
    assert "NO menciones números" in asked[0]
