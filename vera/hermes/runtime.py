"""The live Discord side, if Vera is connected. Light on purpose: it never imports discord.py itself.

Hermes loads plugins before its Discord client exists (and the terminal never has one), so the discord.py
part (discord_runtime.HermesVera) is only imported when Discord connects.
"""

import logging
from typing import Any, Optional

log = logging.getLogger("vera.hermes")

_live: Optional[Any] = None       # discord_runtime.HermesVera once connected
_vera = None


def in_gateway() -> bool:
    """Running inside Hermes's gateway (the Discord side), as opposed to the Hermes terminal on the laptop."""
    import sys
    args = [a.lower() for a in sys.argv]
    return _live is not None or ("gateway" in args and "run" in args)


def shared_vera():
    """One Vera (one database connection) for tools, hooks and the Discord side."""
    global _vera
    if _vera is None:
        from vera.services import Vera
        _vera = Vera()
    return _vera


def live():
    return _live


def connect(bot) -> None:
    """Hermes's Discord factory: called on every (re)connect with the new Bot."""
    global _live
    if _live is None:
        from vera.hermes.discord_runtime import HermesVera
        _live = HermesVera(shared_vera())
    _live.wire(bot)


def guild_id(cid: int):
    """The server a channel or thread belongs to, or None if unknown (not connected, or a DM)."""
    rt = _live
    ch = rt.get_channel(cid) if rt is not None else None
    return getattr(getattr(ch, "guild", None), "id", None)


def channel_name(cid: int) -> str:
    rt = _live
    ch = rt.get_channel(cid) if rt is not None else None
    return getattr(ch, "name", "") or ""
