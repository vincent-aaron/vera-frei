"""Her old names: "Frei" (channels frei-*), then briefly "Franca Frei" (franca-*). She's Vera Frei now (vera-*).

Runs at every start and only touches what still has an old name, so after the first start it does nothing.
Channels are renamed in place, so their messages, permissions and saved IDs stay the same.
"""

import asyncio
import logging
import re

import discord

from vera import FULL_NAME, NAME

log = logging.getLogger("vera.rename")

OLD_PREFIXES, NEW_PREFIX = ("frei-", "franca-"), "vera-"
OLD_NICKS = (None, "Frei", "Franca Frei", "Vera Frei")   # nicknames she set herself, safe to replace
NICKNAME = NAME            # just "Vera", so people tag @Vera


def new_name(name: str) -> str:
    for old in OLD_PREFIXES:
        if name.startswith(old):
            return NEW_PREFIX + name[len(old):]
    return name


def new_topic(topic: str) -> str:
    # "Frei" alone was her first name; "Frei" after "Vera" is her surname and stays.
    topic = re.sub(r"\bFranca\b", NAME, topic)
    return re.sub(rf"(?<!{NAME} )\bFrei\b", NAME, topic)


async def rename_old_channels(guild: discord.Guild) -> int:
    """frei-x / franca-x -> vera-x, for text channels and categories. Skips one if vera-x already exists."""
    if not guild.me.guild_permissions.manage_channels:
        log.warning(f"Can't rename the old channels in {guild.name}: missing Manage Channels")
        return 0
    channels = await guild.fetch_channels()
    taken = {(c.type, c.name) for c in channels}
    done, renamed = 0, {}
    for channel in channels:
        target = new_name(channel.name)
        if target == channel.name:
            continue
        if (channel.type, target) in taken:
            log.warning(f"#{channel.name} not renamed: #{target} already exists")
            continue
        changes = {"name": target}
        topic = getattr(channel, "topic", None)
        if topic and new_topic(topic) != topic:
            changes["topic"] = new_topic(topic)
        try:
            await channel.edit(reason=f"She's {FULL_NAME} now", **changes)
            done += 1
            renamed[channel.id] = target
            taken.add((channel.type, target))
            log.info(f"renamed #{channel.name} -> #{target}")
        except discord.HTTPException:
            log.exception(f"couldn't rename #{channel.name}")
    # Other start-up steps find channels by name in discord.py's cache, which updates when Discord's event
    # arrives. Wait for it, so they don't miss a renamed channel and make a second one.
    for _ in range(20):
        if all(getattr(guild.get_channel(cid), "name", target) == target for cid, target in renamed.items()):
            break
        await asyncio.sleep(0.5)
    return done


async def set_nickname(guild: discord.Guild) -> bool:
    """Show her name in the member list. A nickname someone else picked is left alone."""
    me = guild.me
    if me.nick not in OLD_NICKS or me.display_name == NICKNAME:
        return False
    if not me.guild_permissions.change_nickname:
        log.warning(f"Can't set my nickname in {guild.name}: missing Change Nickname")
        return False
    try:
        await me.edit(nick=NICKNAME, reason=f"She's {FULL_NAME} now")
        return True
    except discord.HTTPException:
        log.exception("couldn't set my nickname")
        return False
