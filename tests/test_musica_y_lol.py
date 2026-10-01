"""Música (Spotify, letras), League (Data Dragon, Riot, cliente local) y que el bot cargue entero."""

import asyncio
import json
import time
import types

import datadragon
import lcu
import letras
import riot_api
import spotify


# ---------- Música ----------

def test_links_de_spotify():
    assert spotify.LINK_RE.search("https://open.spotify.com/intl-es/track/4uLU6hMCjMI75M1A2tKUQC?si=x").groups() == ("track", "4uLU6hMCjMI75M1A2tKUQC")
    assert spotify.is_spotify("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    assert not spotify.is_spotify("https://youtube.com/watch?v=x")


def test_spotify_embed_y_titulo():
    data = {"props": {"pageProps": {"state": {"data": {"entity": {"name": "Mix", "trackList": [
        {"title": "BbY WOW", "subtitle": "KAROL G, Judeline", "duration": 225000}]}}}}}}
    page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    name, tracks = spotify._parse_embed(page)
    assert name == "Mix" and tracks[0].query == "KAROL G, Judeline - BbY WOW" and tracks[0].duration == 225
    track = spotify._parse_title("<title>TiK ToK - song and lyrics by Kesha | Spotify</title>")
    assert (track.artist, track.title) == ("Kesha", "TiK ToK")


def test_titulos_de_youtube():
    assert letras.artist_of("Kesha - TiK ToK (Official Video)") == "Kesha"
    assert letras.artist_of("Runaway", "AURORA - Topic") == "AURORA"
    assert letras.track_only("K/DA - POP/STARS (ft Madison Beer, (G)I-DLE) | Music Video") == "POP/STARS"


def test_elegir_letra_por_duracion():
    results = [{"duration": 260, "plainLyrics": "remix"}, {"duration": 199, "plainLyrics": "original"}]
    assert letras._pick(results, 200)["plainLyrics"] == "original"


# ---------- League ----------

def test_parche_y_nombres_de_campeones(data_dir):
    assert datadragon.patch_label("16.19.1") == "26.19"
    dd = datadragon.DataDragon()
    dd.version = "16.19.1"
    dd._set_champions({"data": {"KogMaw": {"name": "Kog'Maw", "key": "96"}, "Vi": {"name": "Vi", "key": "254"}}})
    assert dd.champion_name(96) == "Kog'Maw"
    assert dd.champion_name("KogMaw") == "Kog'Maw"
    assert dd.find_champions("lo vi ayer") == []  # "vi" es palabra común: solo cuenta hablando de League
    assert dd.find_champions("cómo juego vi jungla") == ["Vi"]


def test_limitador_de_pedidos_riot():
    async def run():
        limiter = riot_api._RateLimiter([(3, 0.3)])
        start = time.monotonic()
        for _ in range(4):
            await limiter.wait()
        return time.monotonic() - start

    assert asyncio.run(run()) >= 0.3  # el 4.º pedido espera


def test_rangos_en_espanol():
    assert riot_api.tier_text({"tier": "GOLD", "rank": "I", "leaguePoints": 6}) == "Oro I (6 LP)"
    assert riot_api.tier_text({"tier": "MASTER", "rank": "I", "leaguePoints": 120}) == "Maestro (120 LP)"
    assert riot_api.tier_text(None) == "Sin clasificar"


def test_partida_del_cliente_local_y_amigos():
    raw = {"gameId": 7, "queueId": 2400, "gameCreation": 1_000_000, "gameDuration": 1200,
           "participants": [{"participantId": 1, "championId": 96, "teamId": 100, "stats": {"win": True, "kills": 15, "deaths": 4, "assists": 20}},
                            {"participantId": 2, "championId": 1, "teamId": 200, "stats": {"win": False}}],
           "participantIdentities": [{"participantId": 1, "player": {"gameName": "Pollo Sonico", "tagLine": "0CH8"}},
                                     {"participantId": 2, "player": {"gameName": "Otro", "tagLine": "1"}}]}
    match = lcu.to_match(raw, lambda key: {96: "KogMaw"}.get(key, str(key)))
    assert match["metadata"]["matchId"] == "LCU_7" and match["info"]["gameEndTimestamp"] == 1_000_000 + 1_200_000
    accounts = {"111": {"nombre": "Pollo Sonico", "tag": "0ch8", "puuid": "P1"}}
    found = lcu.link_participants(match, accounts)
    assert [uid for uid, _ in found] == [111]
    assert match["info"]["participants"][0]["puuid"] == "P1"


# ---------- El bot entero ----------

def test_el_bot_carga_todo_sin_choques_de_comandos():
    import bot

    async def run():
        b = bot.MusicBot()
        await b.setup_hook()
        names = {}
        for command in b.walk_commands():
            for name in [command.name, *command.aliases]:
                assert name not in names, f"'{name}' lo usan {names.get(name)} y {command.name}"
                names[name] = command.name
        cogs = set(b.cogs)
        await b.close()
        return names, cogs

    names, cogs = asyncio.run(run())
    assert {"Música", "Personaje", "Wrapped", "Eventos", "League", "Estado", "Panel"} <= cogs
    for spanish in ("saltar", "pausa", "seguir", "parar", "volumen", "cola", "mezclar", "panel", "cupo"):
        assert spanish in names


# ---------- Comentarios dobles y panel ----------

def test_sin_comentario_doble_si_vino_de_la_charla(monkeypatch):
    import persona

    asked = []

    async def comment(channel, situation):
        asked.append(situation)
        return "comentario"

    monkeypatch.setattr(persona, "_persona", lambda bot: types.SimpleNamespace(comment=comment))

    class Channel:
        id = 1

        async def send(self, *a, **k):
            return types.SimpleNamespace(edit=lambda **kw: asyncio.sleep(0))

    async def run(from_chat: bool):
        token = persona.CHAT_ACTION.set(from_chat)
        try:
            await persona.say(types.SimpleNamespace(get_cog=lambda name: None), Channel(), "algo pasó", "✅ En cola")
        finally:
            persona.CHAT_ACTION.reset(token)
        await asyncio.sleep(0.01)

    asyncio.run(run(True))
    assert asked == []  # ya lo había comentado al responder
    asyncio.run(run(False))
    assert asked == ["algo pasó"]


def test_panel_de_estadisticas(data_dir):
    import jsonio
    import panel
    import wrapped

    jsonio.append_jsonl(wrapped.EVENTS_FILE, {"tipo": "escucha", "t": "2026-09-10T21:00:00", "g": 5, "titulo": "AURORA - Runaway",
                                              "url": "u", "canal_yt": "", "dur": 240, "seg": 240, "pidio": 1, "oyentes": [1]})
    guild = types.SimpleNamespace(id=5, name="Server <prueba>", get_member=lambda uid: None)
    data = panel.build_data(guild)
    assert data["kpis"][0]["value"] == 1 and data["artistas"][0]["label"] == "AURORA"
    page = panel.render(data)
    assert "Server &lt;prueba&gt;" in page  # el nombre del servidor se escapa (no rompe la página)
    assert "const DATA = {" in page


def test_sin_internet_data_dragon_no_insiste(data_dir):
    calls = []
    dd = datadragon.DataDragon()

    async def fail(path):
        calls.append(path)
        raise OSError("getaddrinfo failed")

    dd._get = fail

    async def run():
        for _ in range(5):
            await dd.refresh(force=True)

    asyncio.run(run())
    assert len(calls) == 1  # un intento y espera, en vez de 5 avisos seguidos


def test_imagen_del_personaje_que_no_existe(monkeypatch):
    import personaje

    monkeypatch.setenv("PERSONA_AVATAR", "no_existe_Lillia_19.jpg")
    character = personaje.load_character()  # antes: error y la IA quedaba apagada
    assert character.name  # se carga igual, solo sin foto nueva
