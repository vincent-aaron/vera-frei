"""Vera inside Hermes.

Hermes's AI does the talking and deciding. These modules give it Vera's work as tools, and keep the parts
that need a real Discord connection (buttons, slash commands, run progress, background loops) running on
Hermes's gateway connection:

    plugin.py    register(ctx): tools, hooks and the Discord wiring
    tools.py     the tools Hermes's AI calls
    identity.py  who is asking, where, and what they actually typed (for the go-ahead rule)
    runtime.py   HermesVera: VeraCore on Hermes's Discord bot
"""
