"""The owner's private category: their mail, résumé and reports, kept apart and away from the rest of the server.

Other members keep getting these by DM. Nothing is posted here unless the channel really is private,
so a changed permission means a DM, never a public post.
"""

import logging
from typing import Dict

import discord

from vera import config

log = logging.getLogger("vera.private")

TOPICS = {
    "chat": "Private chat with Vera",
    "email": "Responses from employers only. Automatic confirmations are left out",
    "resume": "Your résumé files",
    "reports": "End-of-run application reports",
    "outreach": "Cold email results",
    "files": "Your application sheets, Word documents and PDFs",
    "updates": "What's new in Vera, and rules she picked up",
}


def is_private(channel, owner_id: int, bot_id: int) -> bool:
    """Only the owner and Vera are let in: @everyone is shut out, and nobody else has been added."""
    if channel.permissions_for(channel.guild.default_role).view_channel:
        return False
    return not any(rule.view_channel and target.id not in (owner_id, bot_id) for target, rule in channel.overwrites.items())


def is_owner_place(channel, owner_id: int, bot_id: int) -> bool:
    """A DM, or one of the owner's own private channels."""
    if getattr(channel, "guild", None) is None:
        return True
    category = getattr(channel, "category", None)
    return category is not None and category.name == config.OWNER_CATEGORY and is_private(channel, owner_id, bot_id)


def _overwrites(guild, owner, me) -> Dict:
    allowed = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
    return {guild.default_role: discord.PermissionOverwrite(view_channel=False), owner: allowed, me: allowed}


async def ensure_owner_channels(guild, owner_id: int, bot_id: int) -> Dict[str, object]:
    """Create whatever is missing. Safe to run at every start: existing channels are found, not duplicated or changed."""
    try:
        owner = await guild.fetch_member(owner_id)
    except discord.NotFound:
        return {}            # the owner isn't in this server
    me = await guild.fetch_member(bot_id)
    existing = await guild.fetch_channels()
    overwrites = _overwrites(guild, owner, me)
    category = next((c for c in existing if c.type == discord.ChannelType.category and c.name == config.OWNER_CATEGORY), None)
    if category is None:
        category = await guild.create_category(config.OWNER_CATEGORY, overwrites=overwrites)
    channels = {}
    for which, name in config.OWNER_CHANNELS.items():
        channel = next((c for c in existing if c.type == discord.ChannelType.text and c.name == name and c.category_id == category.id), None)
        if channel is None:
            channel = await category.create_text_channel(name, overwrites=overwrites, topic=TOPICS[which])
        channels[which] = channel
    return channels
