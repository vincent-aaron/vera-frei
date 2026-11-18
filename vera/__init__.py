"""Vera Frei: a personal AI job-hunting assistant controlled from Discord.

Architecture (modular, see README.md):
    Discord bot (vera.bot)  ->  services (vera.services)  ->  agents (vera.agents)
    ->  job sources (vera.sources)  ->  SQLite database (vera.db)
"""

__version__ = "1.0.0"
NAME = "Vera"
SURNAME = "Frei"
FULL_NAME = f"{NAME} {SURNAME}"
