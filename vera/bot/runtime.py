"""Which Vera is live.

Slash commands and buttons arrive on whichever Discord client she runs on: her own VeraBot, or Hermes's
gateway bot. Hermes's bot isn't a VeraCore, so commands and views look her up here instead of using
interaction.client directly.
"""

_current = None


def set_current(core) -> None:
    global _current
    _current = core


def of(client):
    """The live VeraCore, or the client itself when it is one (her own bot, and tests)."""
    return _current if _current is not None else client
