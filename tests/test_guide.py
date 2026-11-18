"""The #vera-commands guide: fits Discord's limits, and lists every real command."""

import asyncio
import re

import discord
from discord import app_commands

from vera.bot import guide
from vera.bot.main import register_commands


def test_each_message_fits_discord():
    assert all(0 < len(m) <= 2000 for m in guide.MESSAGES)


def test_guide_lists_every_command_and_only_real_ones():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_commands(tree)
    real = set()
    for c in tree.get_commands():
        if isinstance(c, app_commands.Group):
            real |= {f"{c.name} {sub.name}" for sub in c.commands}
        else:
            real.add(c.name)
    text = "\n".join(guide.MESSAGES)
    mentioned = set(re.findall(r"`/([a-z-]+(?: [a-z]+)?)`", text))
    assert mentioned <= real, mentioned - real
    assert real <= mentioned, real - mentioned


def test_guide_explains_how_to_test_and_lists_the_channels():
    text = "\n".join(guide.MESSAGES)
    assert "How to test" in text
    for name in ("vera-multiboards-application", "vera-remote", "vera-company-site-application", "vera-social-outreach", "vera-instructions"):
        assert f"#{name}" in text


def test_guide_has_nothing_private():
    text = "\n".join(guide.MESSAGES).lower()
    for word in ("examplecorp", "@gmail", "hagonoy", "vincent", "jane.colleague"):
        assert word not in text


class FakeChannel:
    def __init__(self, name="vera-instructions"):
        self.name, self.id, self.sent, self.deleted = name, 7, [], []

    async def send(self, text):
        self.sent.append(text)

        class M:
            id = len(self.sent)
        return M()

    def get_partial_message(self, mid):
        outer = self

        class P:
            async def delete(self):
                outer.deleted.append(mid)
        return P()


class FakeDB:
    def __init__(self):
        self.kv = {}

    def get_json(self, k):
        return self.kv.get(k)

    def set_json(self, k, v):
        self.kv[k] = v


class FakeBot:
    def __init__(self):
        self.vera = type("F", (), {"db": FakeDB()})()


class FakeGuild:
    def __init__(self, channel):
        self.id, self.text_channels = 1, [channel]
        self._channel = channel

    def get_channel(self, cid):
        return self._channel if cid == self._channel.id else None


def test_guide_posts_once_and_reposts_only_when_it_changes(monkeypatch):
    bot, ch = FakeBot(), FakeChannel()
    g = FakeGuild(ch)
    asyncio.run(guide.ensure_guide(bot, g))
    assert len(ch.sent) == len(guide.MESSAGES)
    asyncio.run(guide.ensure_guide(bot, g))            # restart: nothing new
    assert len(ch.sent) == len(guide.MESSAGES) and not ch.deleted
    monkeypatch.setattr(guide, "MESSAGES", ["New guide"])
    asyncio.run(guide.ensure_guide(bot, g))            # text changed: old messages replaced
    assert ch.sent[-1] == "New guide" and len(ch.deleted) >= 1


def test_she_keeps_the_name_you_gave_the_guide_channel():
    bot, ch = FakeBot(), FakeChannel("my-own-name")
    g = FakeGuild(ch)
    bot.vera.db.set_json(f"guide:{g.id}", {"channel": ch.id, "hash": "old", "ids": []})
    asyncio.run(guide.ensure_guide(bot, g))
    assert ch.name == "my-own-name" and len(ch.sent) == len(guide.MESSAGES)
