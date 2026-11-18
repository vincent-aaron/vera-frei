"""Who is asking, where, and what they really typed.

Tools never take a user from the AI's arguments: the person comes from the Hermes session, so one member
can never act as another, or as the owner. The go-ahead rule checks the person's own last message,
recorded before the AI sees it, so the AI can't start a run or send email on its own.
"""

import re
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from vera import config

# The owner at his own laptop: the Hermes terminal, TUI or desktop app.
LOCAL_SURFACES = {"", "cli", "tui", "desktop", "local"}


@dataclass
class Asker:
    uid: int
    platform: str              # "discord", "local", or "kanban" (an approved task from Frei: read/draft tools only)
    chat_id: Optional[int]     # the Discord channel, thread or DM. None at the laptop
    channel_name: str          # "vera-remote", or "" for DMs and the laptop
    is_dm: bool

    @property
    def scope(self) -> Optional[int]:
        """What plans, chat state and channel rules belong to. None at the laptop, like the old command line."""
        return self.chat_id


class NotAllowed(Exception):
    pass


def _env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name) or ""
    except Exception:          # tests, or Hermes not loaded
        import os
        return os.getenv(name, "")


def current() -> Asker:
    """The person this turn belongs to. Raises NotAllowed on any other surface."""
    platform = _env("HERMES_SESSION_PLATFORM").lower()
    source = _env("HERMES_SESSION_SOURCE").lower()
    if platform == "discord":
        uid = _env("HERMES_SESSION_USER_ID")
        if not uid.isdigit():
            raise NotAllowed("I can't tell who's asking, so I won't do that.")
        chat = _env("HERMES_SESSION_CHAT_ID")
        chat_type = _env("HERMES_SESSION_CHAT_TYPE").lower()
        if chat_type != "dm" and chat.isdigit() and config.ALLOWED_GUILDS:
            # Someone else's server (she was a Public Bot until 9 Nov 2026): no tools there, until she has left it.
            from vera.hermes import runtime
            gid = runtime.guild_id(int(chat))
            if gid is not None and gid not in config.ALLOWED_GUILDS:
                raise NotAllowed("I only work in the owner's own server.")
        return Asker(int(uid), "discord", int(chat) if chat.isdigit() else None, channel_name(), chat_type == "dm")
    if source == "kanban":
        # A task from Frei (The owner's manager agent), only if the owner approved exactly this task there (frei_link.py).
        # Read and draft tools only (tools.KANBAN_OK): never applications or email, which keep their own go-ahead.
        import os
        from vera.hermes import frei_link
        if not config.OWNER_ID:
            raise NotAllowed("DISCORD_OWNER_ID isn't set in Vera's .env, so I don't know who you are.")
        if frei_link.approved_task(_env("HERMES_KANBAN_TASK") or os.getenv("HERMES_KANBAN_TASK", "")):
            return Asker(config.OWNER_ID, "kanban", None, "", True)
        raise NotAllowed("Only tasks from Frei that the owner approved can use my tools.")
    if platform in LOCAL_SURFACES and source in LOCAL_SURFACES | {""}:
        # The Hermes terminal leaves the platform blank. Inside the Discord gateway every real turn names its platform,
        # so a blank one there (a background job, a new adapter, a cleared context) is never taken to be the owner.
        from vera.hermes import runtime
        if not platform and runtime.in_gateway():
            raise NotAllowed("I can't tell where this came from, so I won't do that.")
        if not config.OWNER_ID:
            raise NotAllowed("DISCORD_OWNER_ID isn't set in Vera's .env, so I don't know who you are.")
        return Asker(config.OWNER_ID, "local", None, "", True)
    raise NotAllowed("I only work on Discord and on the owner's own laptop.")


def channel_name() -> str:
    """The channel's own name. Hermes calls it "Server / #vera-remote"; a thread falls back to its parent channel."""
    from vera.hermes import runtime
    for var in ("HERMES_SESSION_PARENT_CHAT_ID", "HERMES_SESSION_CHAT_ID"):
        cid = _env(var)
        if cid.isdigit():
            name = runtime.channel_name(int(cid))
            if name:
                return name
    label = _env("HERMES_SESSION_CHAT_NAME")
    m = re.search(r"#([\w-]+)\s*$", label)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------- what they actually typed
_lock = threading.Lock()
_said: Dict[Tuple[str, int], Tuple[str, float]] = {}


def _key(chat_id: Optional[int], uid: int) -> Tuple[str, int]:
    return (str(chat_id or "local"), int(uid))


def clean(text: str) -> str:
    """Without @mentions of the bot or its role."""
    return re.sub(r"<@[!&]?\d+>", "", text or "").strip()


_count: Dict[Tuple[str, int], int] = {}


def record(chat_id: Optional[int], uid: int, text: str, when: Optional[float] = None) -> None:
    with _lock:
        key = _key(chat_id, uid)
        _said[key] = (clean(text), when if when is not None else time.time())
        _count[key] = _count.get(key, 0) + 1


def last_said(chat_id: Optional[int], uid: int) -> Tuple[str, float]:
    with _lock:
        return _said.get(_key(chat_id, uid), ("", 0.0))


def said_count(chat_id: Optional[int], uid: int) -> int:
    """How many messages they've sent here since Vera started: tells "the very next message" apart from a later one."""
    with _lock:
        return _count.get(_key(chat_id, uid), 0)
