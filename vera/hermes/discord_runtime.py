"""HermesVera (needs discord.py, loaded when Discord connects): Vera's Discord side, running on Hermes's gateway connection instead of her own client.

Hermes owns the Discord connection and its AI answers chat. This keeps everything else Vera did on Discord:
persistent job and application buttons, her typed slash commands (scoped to the server, so Hermes's own
command sync never removes them), run progress posts, the background loops and her start-up tidying.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import discord
from discord import app_commands

from vera import config
from vera.bot import runtime as R
from vera.bot import views as V
from vera.bot.core import VeraCore
from vera.services import Vera

log = logging.getLogger("vera.hermes")

# Seconds between runs. Job mail is checked hourly (The owner asked on 7 Nov 2026), and once more before the 8 PM summary.
LOOPS = (("check_mail", 3600), ("daily_tick", 300), ("leave_foreign_servers", 300))
STEP = 60          # how often a waiting loop looks at the clock


# Vera's role has Administrator (9 Nov 2026: The owner didn't want to pick permissions one by one), but she must never
# kick, ban, time out or change anyone's roles. Nothing of hers does that, and Hermes's Discord admin tools are off; this
# makes it impossible on her own Discord connection whatever is added later. Only her client: Libero's has its own.
MODERATION_CALLS = ("kick", "ban", "unban", "bulk_ban", "prune_members", "add_role", "remove_role")


class ModerationBlocked(PermissionError):
    pass


def lock_moderation(bot) -> None:
    http = getattr(bot, "http", None)
    if http is None or getattr(http, "__dict__", {}).get("_vera_no_moderation") is True:
        return

    def refuse(name):
        async def blocked(*args, **kwargs):
            log.warning(f"blocked a Discord {name} call: Vera never moderates members")
            raise ModerationBlocked(f"Vera can't {name.replace('_', ' ')}: only the owner does that, in Discord.")
        return blocked
    for name in MODERATION_CALLS:
        if hasattr(http, name):
            setattr(http, name, refuse(name))
    # Changing another member (timeout, mute, roles, nickname, moving them) is refused too. Her own nickname still works.
    real_edit_member = getattr(http, "edit_member", None)
    if real_edit_member is not None:
        def edit_member(guild_id, user_id, *args, **kwargs):
            me = getattr(getattr(bot, "user", None), "id", None)
            if me is None or int(user_id) != int(me):
                return refuse("edit_member")()
            return real_edit_member(guild_id, user_id, *args, **kwargs)
        http.edit_member = edit_member
    http._vera_no_moderation = True


class HermesVera(VeraCore):
    hermes = True          # commands check this: on Hermes, chat goes to Hermes's AI

    def __init__(self, vera: Optional[Vera] = None):
        self.vera = vera or Vera()
        self.bot: Optional[discord.Client] = None
        self._channels = {}
        self._loops = {}
        self._commands = None

    # ----------------------------------------------------------------- the Discord pieces VeraCore uses
    @property
    def guilds(self):
        return self.bot.guilds if self.bot else []

    @property
    def user(self):
        return self.bot.user if self.bot else None

    def get_channel(self, cid: int):
        return self.bot.get_channel(cid) if self.bot else None

    async def fetch_user(self, uid: int):
        return await self.bot.fetch_user(uid)

    async def fetch_channel(self, cid: int):
        return self.get_channel(cid) or await self.bot.fetch_channel(cid)

    @property
    def ready(self) -> bool:
        return self.bot is not None and self.bot.is_ready() and not self.bot.is_closed()

    # ----------------------------------------------------------------- wiring (Hermes calls this on each connect)
    def wire(self, bot: discord.Client) -> None:
        """A reconnect gives a brand-new Bot, so everything is attached again. Loops are started only once."""
        self.bot = bot
        self._channels = {}
        lock_moderation(bot)
        R.set_current(self)
        bot.add_dynamic_items(V.JobButton, V.AppButton)
        add_listener = getattr(bot, "add_listener", None)
        if add_listener is not None:           # added to someone else's server: leave at once (the 5-minute check backs this up)
            add_listener(self._joined, "on_guild_join")
        asyncio.get_running_loop().create_task(self._connected(bot))

    # ----------------------------------------------------------------- only the owner's server
    async def _joined(self, guild) -> None:
        await self.leave_foreign_servers()

    async def leave_foreign_servers(self) -> None:
        """Leave every server that isn't the owner's. Her Discord app was public (9 Nov 2026), so anyone could add her to
        their server and use her: his free AI quota, browsers on his laptop. Does nothing if no server is configured."""
        if not config.ALLOWED_GUILDS:
            return
        for g in list(self.guilds):
            if g.id in config.ALLOWED_GUILDS:
                continue
            log.warning(f"leaving a server that isn't the owner's: {g.name} ({g.id})")
            try:
                await g.leave()
            except Exception:
                log.exception(f"couldn't leave {g.id}")
                continue
            try:
                from vera.bot.core import split_message
                target = await self.owner_channel("updates")
                if target is not None:
                    for part in split_message(f"🚪 Someone added me to another server, **{discord.utils.escape_markdown(g.name)[:80]}** "
                                              f"(`{g.id}`), so I left it. I only work in your server. To allow one, add its ID to "
                                              "`VERA_ALLOWED_GUILDS` in `app\\.env`."):
                        await target.send(part, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                log.exception("couldn't tell the owner about the server I left")

    async def _connected(self, bot: discord.Client) -> None:
        try:
            await self.leave_foreign_servers()      # before start-up renames channels or posts anything in them
        except Exception:
            log.exception("couldn't check which servers I'm in")
        try:
            await self._sync_commands(bot)
        except Exception:
            log.exception("couldn't register Vera's slash commands")
        try:
            await self.startup()
        except Exception:
            log.exception("Vera's start-up steps failed")
        try:
            await self._move_first_notes()
        except Exception:
            log.exception("couldn't move the first notes to the notes channel")
        for name, every in LOOPS:
            task = self._loops.get(name)
            if task is None or task.done():
                self._loops[name] = asyncio.create_task(self._every(name, every))

    async def _every(self, name: str, seconds: int) -> None:
        """Run, then wait. One failure never stops the loop.
        The wait goes by the wall clock in short steps, so after the laptop sleeps, anything that came due runs within
        a minute of waking instead of a whole interval later. While Discord is down, it tries again after a step."""
        while True:
            ran = False
            if self.ready:
                ran = True
                try:
                    await getattr(self, name)()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(f"{name} failed")
            due = time.time() + (seconds if ran else STEP)
            while time.time() < due:
                await asyncio.sleep(min(STEP, max(1.0, due - time.time())))

    async def _move_first_notes(self) -> None:
        from vera.bot.core import split_message
        from vera.hermes import tools
        if not config.OWNER_ID or self.vera.db.get_json(tools.NOTES_MOVED):
            return
        target = await self.notes_channel()
        if target is None:
            return                         # tried again at the next connect
        text = tools.move_first_notes(self.vera.db)
        for part in split_message(text or ""):
            await target.send(part, allowed_mentions=discord.AllowedMentions.none())

    def command_list(self):
        """Her slash commands, built on a spare tree so they never collide with Hermes's own (it has a /help too)."""
        if self._commands is None:
            from vera.bot.main import register_commands
            holder = discord.Client(intents=discord.Intents(guilds=True))      # only holds the command objects; never connects
            tree = app_commands.CommandTree(holder)
            register_commands(tree)
            self._commands = tree.get_commands()
        return self._commands

    async def _sync_commands(self, bot: discord.Client) -> None:
        if not config.GUILD_ID:
            log.warning("DISCORD_GUILD_ID isn't set, so Vera's slash commands aren't registered")
            return
        guild = discord.Object(id=config.GUILD_ID)
        tree = bot.tree
        for cmd in self.command_list():
            tree.add_command(cmd, guild=guild, override=True)
        await tree.sync(guild=guild)
        log.info(f"registered {len(self.command_list())} slash commands in the server")

    # ----------------------------------------------------------------- for tools, which run in Hermes's worker threads
    def run(self, make: Callable[[], Awaitable[Any]], timeout: float = 300) -> Any:
        """Run a coroutine on the Discord loop and wait for it. Run state and Discord objects live there."""
        if not self.ready:
            raise RuntimeError("Vera isn't connected to Discord right now.")
        return asyncio.run_coroutine_threadsafe(make(), self.bot.loop).result(timeout)

    def spawn(self, make: Callable[[], Awaitable[Any]]) -> None:
        """Start something long on the Discord loop and return straight away. It posts its own progress."""
        if not self.ready:
            raise RuntimeError("Vera isn't connected to Discord right now.")

        async def guarded():
            try:
                await make()
            except Exception:
                log.exception("background job failed")
        self.bot.loop.call_soon_threadsafe(lambda: self._keep(asyncio.ensure_future(guarded())))

    def _keep(self, task) -> None:
        self._tasks = getattr(self, "_tasks", set())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
