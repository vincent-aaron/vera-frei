"""Stops Hermes from posting the same Discord reply twice when the network drops mid-send.

What happened (6-7 Nov 2026): on a flaky connection, Discord received a message, but the reply to that request was lost
("[WinError 64] The specified network name is no longer available"). Hermes counted it as not sent and sent it again,
so the plan, "Starting now" and "Stopping now" each showed up twice.

The fix: each Hermes send gets one nonce, and its retries reuse it. With enforce_nonce (discord.py sets it whenever a nonce
is given), Discord hands back the message it already has instead of posting a second one. Sends outside Hermes's retry
(Vera's own run posts) are left alone. If Hermes or discord.py change shape, this does nothing rather than break sending.
"""

import contextvars
import hashlib
import logging
import secrets

log = logging.getLogger("vera.hermes.send_guard")

_seed: contextvars.ContextVar = contextvars.ContextVar("vera_send_seed", default=None)
_MARK = "_vera_send_guard"


def nonce_for(seed: str, content) -> str:
    """Same send and same text, same nonce. Discord allows at most 25 characters."""
    return hashlib.sha1(f"{seed}|{content}".encode("utf-8", "replace")).hexdigest()[:25]


def install() -> bool:
    """Safe to call on every (re)connect: it patches once."""
    try:
        import discord.abc
        from gateway.platforms.base import BasePlatformAdapter
    except ImportError:
        return False
    retry = getattr(BasePlatformAdapter, "_send_with_retry", None)
    send = getattr(discord.abc.Messageable, "send", None)
    if retry is None or send is None:
        log.warning("Hermes or discord.py changed: the duplicate-reply guard is off")
        return False
    if not getattr(retry, _MARK, False):
        async def send_with_retry(self, *args, **kwargs):
            token = _seed.set(secrets.token_hex(8))
            try:
                return await retry(self, *args, **kwargs)
            finally:
                _seed.reset(token)
        setattr(send_with_retry, _MARK, True)
        send_with_retry.__wrapped__ = retry
        BasePlatformAdapter._send_with_retry = send_with_retry
    if not getattr(send, _MARK, False):
        async def guarded_send(self, content=None, **kwargs):
            seed = _seed.get()
            if seed is not None and kwargs.get("nonce") is None:
                kwargs["nonce"] = nonce_for(seed, content)
            return await send(self, content, **kwargs)
        setattr(guarded_send, _MARK, True)
        guarded_send.__wrapped__ = send
        discord.abc.Messageable.send = guarded_send
    return True
