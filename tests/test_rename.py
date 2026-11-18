"""Frei / Franca Frei -> Vera Frei: old frei- and franca- channels are renamed in place, never duplicated."""

import asyncio
from types import SimpleNamespace

import discord

from vera import FULL_NAME, NAME, config
from vera.bot import rename


class Channel:
    def __init__(self, guild, cid, name, kind=discord.ChannelType.text, topic=None):
        self.guild, self.id, self.name, self.type, self.topic = guild, cid, name, kind, topic

    async def edit(self, reason=None, **changes):
        self.guild.edits.append((self.name, changes))
        for k, v in changes.items():
            setattr(self, k, v)


class Me:
    def __init__(self, nick=None, perms=True):
        self.nick, self.guild_permissions = nick, SimpleNamespace(manage_channels=perms, change_nickname=perms)
        self.edits = []

    @property
    def display_name(self):
        return self.nick or "Frei"

    async def edit(self, nick=None, reason=None):
        self.edits.append(nick)
        self.nick = nick


class Guild:
    name = "test"

    def __init__(self, names, me=None):
        self.edits, self.me = [], me or Me()
        self.channels = [Channel(self, i, n, *(k if isinstance(k, tuple) else ())) for i, (n, *k) in enumerate(names)]

    async def fetch_channels(self):
        return list(self.channels)

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)


def run(coro):
    return asyncio.run(coro)


def test_defaults_use_the_new_names():
    assert NAME == "Vera" and FULL_NAME == "Vera Frei"
    assert config.GUIDE_CHANNEL == "vera-instructions" and config.OWNER_CATEGORY == "vera-owner"
    assert all(name.startswith("vera-") for name in config.CHANNEL_SITES)


def test_old_channels_from_both_names_are_renamed_in_place():
    g = Guild([("frei-remote",), ("franca-owner", discord.ChannelType.category), ("general",), ("franca-instructions",), ("frei-notes",)])
    g.channels[3].topic = "How to use and test Franca"
    assert run(rename.rename_old_channels(g)) == 4
    assert [c.name for c in g.channels] == ["vera-remote", "vera-owner", "general", "vera-instructions", "vera-notes"]
    assert g.channels[3].topic == "How to use and test Vera"
    assert run(rename.rename_old_channels(g)) == 0          # second start: nothing left to do


def test_topics_keep_her_surname():
    assert rename.new_topic("What's new in Frei") == "What's new in Vera"
    assert rename.new_topic("Vera Frei's guide") == "Vera Frei's guide"


def test_an_existing_new_channel_is_not_clobbered():
    g = Guild([("franca-remote",), ("vera-remote",)])
    assert run(rename.rename_old_channels(g)) == 0
    assert [c.name for c in g.channels] == ["franca-remote", "vera-remote"]


def test_no_permission_means_no_changes():
    g = Guild([("franca-remote",)], me=Me(perms=False))
    assert run(rename.rename_old_channels(g)) == 0 and g.edits == []


def test_nickname_replaces_her_old_ones_but_keeps_a_custom_one():
    for old in (None, "Frei", "Franca Frei", "Vera Frei"):
        g = Guild([], me=Me(nick=old))
        assert run(rename.set_nickname(g)) is True and g.me.nick == "Vera"
        assert run(rename.set_nickname(g)) is False
    custom = Guild([], me=Me(nick="Job Bot"))
    assert run(rename.set_nickname(custom)) is False and custom.me.nick == "Job Bot"
