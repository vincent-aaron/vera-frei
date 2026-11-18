"""9 Nov 2026: Vera's Discord app was a Public Bot (and user-installable), so anyone could add her to their server.
She now leaves any server that isn't the owner's, and her tools refuse to work in one before she has left."""

import asyncio
from types import SimpleNamespace

import pytest

from vera import config
from vera.hermes import discord_runtime, identity, runtime, tools
from tests.test_hermes import FakeRuntime, OWNER, call, on_discord
from tests.test_hermes import vera  # noqa: F401  (fixture)

MINE, OTHER = 100000000000000001, 999000111


class Guild:
    def __init__(self, gid, name):
        self.id, self.name, self.left = gid, name, False

    async def leave(self):
        self.left = True


def test_she_leaves_every_server_but_his_and_tells_him(vera, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GUILDS", {MINE})
    mine, other = Guild(MINE, "Frei Automate"), Guild(OTHER, "Strangers")
    told = []

    class Updates:
        async def send(self, text, **kw):
            told.append(text)
    rt = discord_runtime.HermesVera(vera)
    rt.bot = SimpleNamespace(guilds=[mine, other])

    async def owner_channel(which):
        return Updates()
    rt.owner_channel = owner_channel
    asyncio.run(rt.leave_foreign_servers())
    assert other.left and not mine.left and "Strangers" in told[0]


def test_with_no_server_configured_she_leaves_nothing(vera, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GUILDS", set())
    other = Guild(OTHER, "Strangers")
    rt = discord_runtime.HermesVera(vera)
    rt.bot = SimpleNamespace(guilds=[other])
    asyncio.run(rt.leave_foreign_servers())
    assert not other.left


def test_her_tools_refuse_in_someone_elses_server(vera, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GUILDS", {MINE})
    fake = FakeRuntime(vera)
    monkeypatch.setattr(runtime, "_live", fake)
    for cid, gid in ((5001, OTHER), (5002, MINE)):
        fake.channels[cid] = SimpleNamespace(id=cid, name="general", guild=SimpleNamespace(id=gid))
    on_discord(monkeypatch, OWNER, 5001)
    assert "only work in the owner's own server" in call(tools.status)["error"]
    on_discord(monkeypatch, OWNER, 5002)
    assert identity.current().uid == OWNER
