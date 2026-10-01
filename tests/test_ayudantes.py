"""Varios canales de voz a la vez: Lillia + bots ayudantes (con Discord simulado)."""

import asyncio
import types

import pytest
from discord.ext import commands

import ayudantes
import music


class FakeVC:
    def __init__(self, channel):
        self.channel, self.connected, self.source = channel, True, None

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    async def disconnect(self, force=False):
        self.connected = False
        self.channel.guild.voice_client = None

    async def move_to(self, channel):
        self.channel = channel


class FakeChannel:
    def __init__(self, guild, id_, name):
        self.guild, self.id, self.name, self.members = guild, id_, name, []

    async def connect(self, self_deaf=True, timeout=30):
        self.guild.voice_client = FakeVC(self)
        return self.guild.voice_client


class FakeGuild:
    def __init__(self, id_=5, name="S"):
        self.id, self.name, self.voice_client = id_, name, None
        self.channels = {1: FakeChannel(self, 1, "General"), 2: FakeChannel(self, 2, "Juegos")}

    def get_channel(self, cid):
        return self.channels.get(cid)


class FakeClient:
    def __init__(self, name):
        self.guild = FakeGuild()
        self.user = types.SimpleNamespace(id=hash(name), name=name)

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None


def ctx_in(main: FakeClient, channel_id: int, user_id: int):
    channel = main.guild.channels[channel_id]
    author = types.SimpleNamespace(id=user_id, display_name=f"u{user_id}",
                                   voice=types.SimpleNamespace(channel=channel))
    return types.SimpleNamespace(author=author, guild=main.guild, channel=types.SimpleNamespace(id=99))


def test_segundo_canal_lo_atiende_un_ayudante(monkeypatch):
    main, helper = FakeClient("Lillia"), FakeClient("Ayudante1")
    monkeypatch.setattr(ayudantes, "ready", lambda: [helper])

    async def run():
        cog = music.Music(main)
        p1 = await cog.ensure_player(ctx_in(main, 1, 10))
        p1.queue.append("canción")  # está ocupada
        p2 = await cog.ensure_player(ctx_in(main, 2, 20))
        same = await cog.ensure_player(ctx_in(main, 1, 30))  # alguien más en el canal 1: mismo reproductor
        result = (p1.client is main, p2.client is helper, same is p1, sorted(cog.players))
        # Un tercer canal sin bots libres: avisa en vez de cortar la música de otro canal.
        main.guild.channels[3] = FakeChannel(main.guild, 3, "Charla")
        helper.guild.channels[3] = FakeChannel(helper.guild, 3, "Charla")
        p2.queue.append("otra")
        with pytest.raises(commands.CheckFailure, match="Ya estoy poniendo música"):
            await cog.ensure_player(ctx_in(main, 3, 40))
        for p in list(cog.players.values()):
            p.task.cancel()
        return result

    assert asyncio.run(run()) == (True, True, True, [1, 2])


def test_bot_libre_se_muda_de_canal(monkeypatch):
    main = FakeClient("Lillia")
    monkeypatch.setattr(ayudantes, "ready", lambda: [])

    async def run():
        cog = music.Music(main)
        p1 = await cog.ensure_player(ctx_in(main, 1, 10))
        p1.reserved_until = 0  # ya pasó un rato y no suena nada ni hay nadie
        p2 = await cog.ensure_player(ctx_in(main, 2, 20))
        result = (p2 is p1, list(cog.players), p1.voice.channel.id)
        p1.task.cancel()
        return result

    assert asyncio.run(run()) == (True, [2], 2)
