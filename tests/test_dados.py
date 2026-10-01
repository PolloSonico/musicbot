"""Dados: leer las tiradas del bot de D&D y el !dado propio."""

import random
import types

import discord

import dados
import persona


def bot_message(title, fields, name="D&D Bot", content=""):
    embed = discord.Embed(title=title)
    for field_name, value in fields:
        embed.add_field(name=field_name, value=value)
    author = types.SimpleNamespace(id=1, bot=True, name=name, display_name=name)
    return types.SimpleNamespace(content=content, embeds=[embed], author=author)


def test_lee_la_tirada_del_bot_de_dnd():
    msg = bot_message("1d20 roll by Lucia (pollosonico)", [("Sum of roll(s): 20", "​"), ("Roll #1", "20")])
    roll = dados.parse_roll(msg)
    assert (roll.count, roll.sides, roll.total, roll.rolls, roll.who) == (1, 20, 20, [20], "Lucia")
    assert roll.nat20 and not roll.nat1


def test_varios_dados_y_modificador():
    msg = bot_message("2d6+3 roll by Mariano (x)", [("Sum of roll(s): 10", "-"), ("Roll #1", "1"), ("Roll #2", "6")])
    roll = dados.parse_roll(msg)
    assert (roll.modifier, roll.rolls, roll.total) == (3, [1, 6], 10)
    assert not roll.nat20 and roll.luck() == 0.5
    # 1 en un d20 = pifia; un 1 en un d6 no.
    assert dados.parse_roll(bot_message("1d20 roll by A (a)", [("Sum of roll(s): 1", "-")])).nat1
    assert not dados.parse_roll(bot_message("1d6 roll by A (a)", [("Sum of roll(s): 1", "-")])).nat1


def test_solo_bots_de_dados_y_mensajes_que_son_tiradas():
    assert dados.is_dice_bot(types.SimpleNamespace(id=1, bot=True, name="D&D Bot", display_name="D&D Bot"))
    assert not dados.is_dice_bot(types.SimpleNamespace(id=2, bot=True, name="Lillia", display_name="Lillia"))
    assert not dados.is_dice_bot(types.SimpleNamespace(id=3, bot=False, name="dice lover", display_name="x"))
    assert dados.parse_roll(bot_message("Ayuda", [("Comandos", "/roll")])) is None


def test_notacion_de_dado():
    assert dados.parse_notation("") == (1, 20, 0, "")
    assert dados.parse_notation("2d6+3") == (2, 6, 3, "")
    assert dados.parse_notation("d100") == (1, 100, 0, "")
    assert dados.parse_notation("20") == (1, 20, 0, "")
    assert dados.parse_notation("+5") == (1, 20, 5, "")
    assert dados.parse_notation("ventaja") == (2, 20, 0, "alta")
    assert dados.parse_notation("desventaja -1") == (2, 20, -1, "baja")
    assert dados.parse_notation("1000d6") is None and dados.parse_notation("hola") is None


def test_ventaja_se_queda_con_el_mayor():
    roll, thrown = dados.roll_dice(2, 20, 2, "alta", random.Random(1))
    assert roll.rolls == [max(thrown)] and roll.total == max(thrown) + 2


def test_orden_oculta_de_dado():
    text, actions = persona.extract_actions("¡Ahí va!\n[[DADO: 1d20+2]]")
    assert text == "¡Ahí va!" and actions == [("dado", "1d20+2")]
