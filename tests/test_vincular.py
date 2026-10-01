"""Vincular la cuenta de League de otra persona: solo el dueño o los admins."""

import asyncio
import types

import riot


def make(is_owner: bool, admin: bool):
    sent = []
    friend = types.SimpleNamespace(id=22, bot=False, name="lucia", display_name="Lucía")
    author = types.SimpleNamespace(id=11, guild_permissions=types.SimpleNamespace(manage_guild=admin))

    async def send(text, **kwargs):
        sent.append(text)

    async def owner(user):
        return is_owner

    ctx = types.SimpleNamespace(author=author, send=send, guild=None,
                                message=types.SimpleNamespace(mentions=[friend]))
    cog = riot.Riot.__new__(riot.Riot)
    cog.bot = types.SimpleNamespace(is_owner=owner)
    return cog, ctx, sent, friend


def test_para_uno_mismo_no_hace_falta_permiso():
    cog, ctx, sent, _ = make(False, False)
    target, rest, other = asyncio.run(cog._other_member(ctx, "Pollo Sonico#0CH8"))
    assert target is ctx.author and rest == "Pollo Sonico#0CH8" and not other and not sent


def test_dueno_o_admin_vinculan_a_otro():
    for is_owner, admin in ((True, False), (False, True)):
        cog, ctx, sent, friend = make(is_owner, admin)
        target, rest, other = asyncio.run(cog._other_member(ctx, "<@22> Lucia#LAS1 lan"))
        assert target is friend and rest == "Lucia#LAS1 lan" and other and not sent


def test_sin_permiso_no_puede():
    cog, ctx, sent, _ = make(False, False)
    target, _, other = asyncio.run(cog._other_member(ctx, "<@!22> Lucia#LAS1"))
    assert target is None and other and "Solo el dueño" in sent[0]
