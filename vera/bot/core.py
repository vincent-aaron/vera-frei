"""What Vera does on Discord, whichever Discord connection she runs on.

VeraBot (her own discord.py client) and the Hermes runtime (Hermes's gateway connection) both build on VeraCore.
The class needs the connection's guilds, user, get_channel and fetch_user, plus self.vera and self._channels.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import discord

from vera import NAME, config
from vera.agents import assistant, rounds
from vera.bot import private, rename, updates
from vera.bot.guide import ensure_guide
from vera.bot import embeds as E
from vera.bot import views as V

log = logging.getLogger("vera.bot")


def split_message(text: str, limit: int = 1900) -> list:
    """Discord's limit is 2000 characters. Split between lines, so a job and its link stay together."""
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            # A line that's too long is cut at the last space before the limit, never inside a word.
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            parts.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    return parts + [current] if current else parts


def sent_line(latest: dict) -> str:
    """One job as it's sent: **Title** - Company (Board · Setup · Location). Never the link: links go in the final report."""
    info = latest.get("info") or {}
    clean = lambda s: re.sub(r"[`*_~|>@\\]", "", s or "").strip()          # web text can't ping or fake formatting
    title, company = clean(info.get("title")), clean(info.get("company"))
    if not title:                                                       # an older engine: only "Title - Company"
        title, company = clean(latest.get("job")), ""
    where = " · ".join(x for x in (latest.get("board"), clean(info.get("setup")) or latest.get("where"), clean(info.get("location"))) if x)
    return f"**{title}**" + (f" - {company}" if company else "") + (f" ({where})" if where else "")


class VeraCore:
    async def owner_channel(self, which: str) -> Optional[discord.TextChannel]:
        """One of the owner's private channels, or None. Callers fall back to a DM, never to a public channel."""
        if not config.OWNER_ID or self.user is None:
            return None
        for g in self.guilds:
            category = discord.utils.get(g.categories, name=config.OWNER_CATEGORY)
            channel = category and discord.utils.get(category.text_channels, name=config.OWNER_CHANNELS[which])
            if channel:
                if private.is_private(channel, config.OWNER_ID, self.user.id):
                    return channel
                log.warning(f"#{channel.name} isn't private any more, so it won't be used")
        return None

    async def updates_target(self):
        """Where he asked for updates to go, otherwise his private updates channel."""
        saved = self.vera.db.get_json(updates.KEY_CHANNEL)
        return (self.get_channel(int(saved)) if saved else None) or await self.owner_channel("updates")

    async def notes_channel(self) -> Optional[discord.TextChannel]:
        """#vera-notes: The owner's list of future improvements that need new code."""
        for g in self.guilds:
            ch = discord.utils.get(g.text_channels, name=config.NOTES_CHANNEL)
            if ch is not None:
                return ch
        return None

    async def note_new_rules(self, result: dict) -> None:
        """A rule or answer the owner just gave her. In a public channel, only what changed, not the value: it could be his salary."""
        notes = [result["learned"]] if result.get("learned") else []
        patch = result.get("patch") or {}
        target = await self.updates_target() if notes or patch else None
        if target is None:
            return
        if patch:
            is_private = private.is_owner_place(target, config.OWNER_ID, self.user.id)
            notes += assistant.describe({k: v for k, v in patch.items() if is_private or k != "salary"})
            if not is_private and "salary" in patch:
                notes.append("Changed your salary setting.")
        for part in split_message("🧠 **New rule**\n" + "\n".join(notes)):
            await target.send(part, allowed_mentions=discord.AllowedMentions.none())

    async def tell_user(self, uid: int, text: str) -> None:
        """Reports list someone's job applications, so they go by DM. The owner's go to their private reports channel."""
        chunks = split_message(text)
        if rounds.is_owner(uid):
            ch = await self.owner_channel("reports")
            if ch:
                for chunk in chunks:
                    await ch.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                return
        try:
            user = await self.fetch_user(uid)
            for chunk in chunks:
                await user.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:      # DMs closed: say so publicly, but never post the report itself
            for g in self.guilds:
                ch = await self.main_channel(g)
                if ch and (g.get_member(uid) or await g.fetch_member(uid)):
                    await ch.send(f"<@{uid}> Your application report is ready, but I can't DM you. Allow DMs from this server, then run `/rounds status`.")
                    return

    def start_or_wait(self, item: dict) -> Optional[str]:
        """Each channel gets its own browser, so runs go side by side. Returns what to tell the person if theirs has to wait."""
        if rounds.has_room(item["uid"], item.get("channel_id")):
            self._launch(item)
            return None
        place = rounds.enqueue(item)
        same_channel = any(r["uid"] == item["uid"] and r.get("channel_id") == item.get("channel_id") for r in rounds.active_runs())
        why = "A run is already going in this channel" if same_channel else "I'm running as many as I can at once"
        return f"⏳ {why}, so this one is **#{place} in line** and starts by itself. Nothing has been sent for it yet. Say **stop** here to cancel it."

    def _launch(self, item: dict) -> None:
        rounds.start_run(item)
        task = asyncio.create_task(self._run_then_next(item))
        self._run_tasks = getattr(self, "_run_tasks", set())
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)

    async def _run_then_next(self, item: dict) -> None:
        try:
            await self.run_rounds(item["uid"], dry_run=item["dry_run"], count=item["count"], sites=item["sites"], channel=item["channel"], run=item)
        finally:
            rounds.finish_run(item)
            while (waiting := rounds.next_waiting()) is not None:
                if waiting.get("channel") is not None:
                    try:
                        await waiting["channel"].send("▶️ Your turn: starting the run you asked for here.")
                    except discord.DiscordException:
                        log.exception("couldn't say the next run is starting")
                self._launch(waiting)

    def stop_for(self, uid: int, channel_id: int, text: str) -> str:
        """"Stop" ends the run from this channel only. "Stop all" ends everything of yours."""
        everything = bool(re.search(r"\b(all|everything|lahat)\b", text, re.I))
        cancelled = rounds.cancel_waiting(uid, None if everything else channel_id)
        mine = rounds.runs_for(uid)
        here = [r for r in mine if everything or r.get("channel_id") in (None, channel_id)]
        if here:
            for run in here:
                rounds.stop_run(run)
            return "🛑 Stopping now. Anything already sent has gone out, and I'll send you the report."
        if cancelled:
            return f"Okay, cancelled the {cancelled} run{'' if cancelled == 1 else 's'} waiting here. Nothing was sent for {'it' if cancelled == 1 else 'them'}."
        if mine:
            where = ", ".join(getattr(r.get("channel"), "mention", "another channel") for r in mine)
            return f"Nothing from this channel is running, so I left the others alone. Running in {where}. Say **stop** there, or **stop all** to end everything."
        return "There's no run of yours to stop right now."

    async def run_rounds(self, uid: int, dry_run: bool = False, count: Optional[int] = None,
                            sites: Optional[list] = None, channel: Optional[discord.abc.Messageable] = None, run: Optional[dict] = None) -> None:
        """Each application is posted as it goes, so a long run doesn't look stuck, and the full report comes at the end."""
        from vera.agents import exports
        latest = {"job": "", "where": "", "board": ""}
        quiet = discord.AllowedMentions.none()

        async def post(text: str) -> None:
            if channel is not None:
                return await channel.send(text, allowed_mentions=quiet)
            reports = await self.owner_channel("reports") if rounds.is_owner(uid) else None
            await (reports.send(text, allowed_mentions=quiet) if reports is not None else self.tell_user(uid, text))

        progress = {"checked": 0, "sent": 0, "queue": 0}
        EVERY = 10          # a short note every 10 jobs checked, so a run that skips a lot doesn't look stuck

        async def on_line(line: str) -> None:
            job = re.match(r"\[\d+/\d+\]\s+(\w+):\s+(.*)", line)
            where = re.match(r"\s*where:\s*(.*)", line)
            resting = re.match(r"RESTING (\S+) (\S+)", line)
            queue = re.match(r"QUEUE (\d+)", line)
            left_out = re.match(r"Of \d+ job\(s\) lined up, I left out .*", line)
            info = re.match(r"\s*info:\s*(\{.*\})\s*$", line)
            if left_out:
                await post("ℹ️ " + left_out.group(0)[:1500])
            elif queue:
                progress["queue"] = int(queue.group(1))
                await post(f"🔎 Found **{progress['queue']}** jobs to check. Checking them now. Each one that's sent gets posted here, "
                           "and ones that don't fit (location, level, experience) are skipped quietly.")
            elif resting:
                until = datetime.fromisoformat(resting.group(2))
                name = resting.group(1).replace("_", " ")
                await post(f"⏸ **{name}** showed a bot check, so I'm leaving it alone until {until:%I:%M %p}. I'll keep going on the other sites.".replace(" 0", " "))
            elif job:
                latest.update(board=exports.BOARDS.get(job.group(1), job.group(1).replace("_", " ").title()), job=job.group(2), where="", info=None)
                progress["checked"] += 1
                if progress["checked"] % EVERY == 0:
                    of = f" of {progress['queue']}" if progress["queue"] else ""
                    await post(f"⏳ Still going: checked **{progress['checked']}**{of} jobs, **{progress['sent']}** sent so far.")
            elif where:
                latest["where"] = where.group(1).split(" · ")[0]      # just the setup: the full report has the location and link
            elif info:
                try:
                    latest["info"] = json.loads(info.group(1))
                except ValueError:
                    latest["info"] = None
            elif "-> SUBMITTED" in line or "-> DRY-RUN" in line:
                progress["sent"] += 1
                start = "✅ Sent" if "SUBMITTED" in line else "🧪 Filled, not sent"
                await post(f"{start}: {sent_line(latest)}")

        self.vera.db.set_json(f"apply_started:{uid}", datetime.now().isoformat(timespec="seconds"))
        res = await rounds.run_once(self.vera, uid, dry_run=dry_run, count=count, sites=sites, on_line=on_line, run=run)
        try:
            text = rounds.report(res)
            reports = await self.owner_channel("reports") if rounds.is_owner(uid) else None
            if reports is None:
                if channel is None:
                    await self.tell_user(uid, text)
                else:
                    for part in split_message(text):
                        await channel.send(part, allowed_mentions=discord.AllowedMentions.none())
            else:
                for part in split_message(text):
                    await reports.send(part, allowed_mentions=discord.AllowedMentions.none())
                if channel is not None and channel is not reports:
                    await channel.send(f"📋 Run finished. The report is in {reports.mention}.")
        except discord.DiscordException:
            log.exception("couldn't send the round report")

    async def run_outreach(self, uid: int, items: list, fallback: discord.abc.Messageable) -> None:
        from vera.agents import outreach
        target = next((ch for g in self.guilds for ch in [discord.utils.get(g.text_channels, name=config.OUTREACH_CHANNEL)] if ch), fallback)
        if items and all(i.get("reply") for i in items):
            target = fallback               # answers to employers: reported where he asked, not in the cold-email channel

        async def on_sent(item) -> None:
            await target.send(f"✅ Emailed **{item['email']}** ({item.get('company') or '?'})", allowed_mentions=discord.AllowedMentions.none())
        res = await outreach.send_batch(self.vera.user(uid), items, on_sent)
        try:
            await target.send(outreach.report(res)[:1900], allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException:
            log.exception("couldn't send the outreach report")

    async def save_sent_resume(self, uid: int, file: discord.Attachment, in_private: bool) -> str:
        """A résumé someone sends in chat. It's saved only under their own account, for their own applications."""
        if file.size > 8 * 1024 * 1024:
            return "That file is over 8 MB. Please send a smaller résumé."
        session = self.vera.user(uid)
        # Its own temporary folder each time (two uploads can't overwrite each other), keeping the file's own name,
        # which employers see. Path(...).name drops any folders in the name, so it can't be written anywhere else.
        import secrets
        import shutil
        folder = config.DATA_DIR / f"upload-{int(uid)}-{secrets.token_hex(6)}"
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (Path(file.filename).name or "resume.pdf")
        try:
            await file.save(tmp)
            session.add_resume(tmp, Path(file.filename).stem, [])
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        res = await session.import_resume(Path(session.profile["resumes"][-1]["path"]))
        note = f"📄 Saved **{file.filename}** as your résumé." + (" I filled in your profile from it, so check it with `/profile`." if res.get("ok") else "")
        missing = rounds.blockers(rounds.build_profile(session))
        if missing:
            note += " I still need your own: " + ", ".join(missing) + "."
        if not in_private:
            note += "\n🔒 Next time, DM me your résumé, because everyone in this channel can open it. You may want to delete your message."
        return note

    async def job_mail_channel(self):
        """Where employers' replies go: #vera-job-mail, else his private email channel, else his DMs. And whether it's private."""
        for g in self.guilds:
            ch = discord.utils.get(g.text_channels, name=config.JOB_MAIL_CHANNEL)
            if ch is not None:
                return ch, private.is_private(ch, config.OWNER_ID, self.user.id)
        ch = await self.owner_channel("email")
        if ch is not None:
            return ch, True
        return await self.fetch_user(config.OWNER_ID), True

    async def mail_targets(self) -> list:
        """Every place an employer's reply goes, and whether it's private: #vera-job-mail for everyone (a summary and the
        job link), and his private email channel with the full message and the Gmail link. A DM if neither exists."""
        targets = []
        for g in self.guilds:
            ch = discord.utils.get(g.text_channels, name=config.JOB_MAIL_CHANNEL)
            if ch is not None:
                targets.append((ch, private.is_private(ch, config.OWNER_ID, self.user.id)))
                break
        mine = await self.owner_channel("email")
        if mine is not None and all(mine is not t for t, _ in targets):
            targets.append((mine, True))
        if not any(is_private for _, is_private in targets):
            targets.append((await self.fetch_user(config.OWNER_ID), True))
        return targets

    async def check_mail(self, report_to: Optional[discord.abc.Messageable] = None, recheck_days: Optional[int] = None) -> Optional[list]:
        """Post employers' responses to #vera-job-mail as they arrive. The message text is shown only if that channel is private.
        recheck_days looks again at that many days of mail, posting only what was missed. The mailbox is only ever read."""
        from vera.agents import jobmail, outreach
        if not (config.OWNER_ID and outreach.can_send()):
            return None
        session = self.vera.user(config.OWNER_ID)
        # The inbox, then Spam: employers' mail sometimes lands there. Each folder counts its own message numbers.
        keys = {jobmail.INBOX: f"mail_last_uid:{config.OWNER_ID}", jobmail.SPAM: f"mail_last_uid_spam:{config.OWNER_ID}"}
        messages, last = [], {}
        for folder, key in keys.items():
            try:
                got, last[folder] = await asyncio.to_thread(jobmail.fetch_new, int(session.db.get_json(key) or 0), recheck_days, folder)
                messages += got
            except Exception as e:     # includes imaplib errors, and network drops
                log.warning(f"couldn't read Gmail ({folder}): {type(e).__name__}")
                if folder == jobmail.INBOX:
                    if report_to is not None:
                        await report_to.send("I couldn't read your Gmail. Check `OUTREACH_EMAIL` and `OUTREACH_APP_PASSWORD` in `app\\.env`.")
                    return None
        found = jobmail.track(session, messages, recheck=bool(recheck_days))
        for folder, uid in last.items():
            session.db.set_json(keys[folder], max(uid, int(session.db.get_json(keys[folder]) or 0)))
        responses = [item for item in found if jobmail.is_response(item)]     # the rest are counted, and covered by the run report
        # Posts that failed last time (Discord dropped, or Vera was restarting) go out first: mail is only read once.
        waiting_key = f"mail_unposted:{config.OWNER_ID}"
        waiting = [w if isinstance(w, dict) else {"public": w[0], "private": w[1], "done": []} for w in session.db.get_json(waiting_key) or []]
        targets = await self.mail_targets() if responses or waiting else []
        unposted = []
        for post in waiting + [{"public": jobmail.render(i, False), "private": jobmail.render(i, True), "done": []} for i in responses]:
            for target, is_private in targets:
                where = str(getattr(target, "id", "dm"))
                if where in post["done"]:
                    continue                    # this channel already has it: a retry never posts it twice
                try:
                    await target.send(post["private"] if is_private else post["public"], allowed_mentions=discord.AllowedMentions.none())
                    post["done"].append(where)
                except Exception as e:          # Discord down, a dropped connection, or Vera shutting down: try next time
                    log.warning(f"couldn't post a job mail reply ({type(e).__name__}); it'll go out at the next check")
                    unposted.append(post)
                    break
        session.db.set_json(waiting_key, unposted[-50:] or None)
        if report_to is not None:
            places = [t.mention for t, _ in targets if getattr(t, "guild", None)]
            where = f", posted in {' and '.join(places)}" if places else ", sent to your DMs" if responses else ""
            await report_to.send(f"📬 **{len(responses)}** new response(s) from employers{where}.\n" + jobmail.summary(session),
                                 allowed_mentions=discord.AllowedMentions.none())
        return responses

    async def run_recommend(self, uid: int, count: int, sites: Optional[list], channel: discord.abc.Messageable) -> None:
        text = rounds.recommend_report(await rounds.recommend(self.vera, uid, count, sites))
        try:
            lines, chunk = text.split("\n"), ""
            for line in lines:        # split between lines, so a job and its link stay together
                if len(chunk) + len(line) > 1900:
                    await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                    chunk = ""
                chunk += line + "\n"
            if chunk:
                await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException:
            log.exception("couldn't send recommendations")

    async def startup(self):
        """Once connected: tidy channel names, the guide, the owner's private channels and update notes."""
        log.info(f"{NAME} online as {self.user} in {len(self.guilds)} server(s)")
        for g in self.guilds:
            try:        # before anything looks channels up by name, so the old ones are reused, not duplicated
                await rename.rename_old_channels(g)
                await rename.set_nickname(g)
            except discord.DiscordException:
                log.exception("couldn't move the old vera- channels to vera-")
            try:
                await ensure_guide(self, g)
            except discord.DiscordException:
                log.exception("couldn't update the guide channel")
            if config.OWNER_ID:
                try:
                    await private.ensure_owner_channels(g, config.OWNER_ID, self.user.id)
                except discord.DiscordException:
                    log.exception("couldn't set up the owner's private channels")
        try:
            await updates.post_new(self)
        except discord.DiscordException:
            log.exception("couldn't post what's new")
        for g in self.guilds:
            ch = await self.main_channel(g)
            if ch and not self.vera.db.get_json(f"greeted:v2:{g.id}"):
                await ch.send(f"👋 **{NAME}** is online for **everyone** here. Each person gets their own profile, filters and matches.\n"
                              "• New? Run `/resume add` with **import_profile: True** to set up your profile from your résumé.\n"
                              "• Then just talk to me: *@Vera find remote web developer jobs that need no experience*.\n"
                              "• `/help` lists everything.")
                self.vera.db.set_json(f"greeted:v2:{g.id}", True)
        # Once: replies passed on by job boards were filed as automatic notices before 6 Nov 2026. Look again and post what was missed.
        if config.OWNER_ID and not self.vera.db.get_json("mail_recheck:2026-11-06"):
            self.vera.db.set_json("mail_recheck:2026-11-06", True)
            try:
                await self.check_mail(recheck_days=14)
            except Exception:
                log.exception("couldn't re-check the job mail")

    async def main_channel(self, guild: Optional[discord.Guild]) -> Optional[discord.TextChannel]:
        """The #application channel (created if missing and allowed)."""
        if guild is None:
            return None
        if guild.id in self._channels:
            return self._channels[guild.id]
        ch = discord.utils.get(guild.text_channels, name=config.MAIN_CHANNEL)
        if ch is None and guild.me.guild_permissions.manage_channels:
            ch = await guild.create_text_channel(config.MAIN_CHANNEL, topic=f"{NAME}: job matches, applications and reminders")
        self._channels[guild.id] = ch
        return ch

    def resume_files(self, uid: int):
        rs = [r for r in self.vera.user(uid).profile["resumes"] if Path(r["path"]).is_file()]
        return rs, [discord.File(r["path"], filename=Path(r["path"]).name) for r in rs[:10]]

    async def send_resume(self, user: discord.abc.User, here: Optional[discord.abc.Messageable] = None) -> str:
        """Résumés contain contact details, so they never go in a public channel. The owner's stay where they asked
        if that's private, and otherwise go to their résumé channel. Everyone else's come by DM."""
        rs, files = self.resume_files(user.id)
        if not rs:
            return "You haven't uploaded a résumé yet. Use `/resume add` (tip: set **import_profile** to True to fill your profile too)."
        text = f"📄 Here's your résumé{'s' if len(rs) > 1 else ''}: " + ", ".join(f"**{r['name']}**" for r in rs)
        if rounds.is_owner(user.id):
            asked_privately = here is not None and private.is_owner_place(here, config.OWNER_ID, self.user.id)
            target = here if asked_privately else await self.owner_channel("resume")
            if target is not None:
                await target.send(text, files=files)
                return "📄 Here it is." if asked_privately else f"📄 Posted your résumé in {target.mention}, which only you can see."
        try:
            await user.send(text, files=files)
        except discord.Forbidden:
            return "I couldn't DM you (your DMs are closed). Use `/resume get` instead; only you will see it."
        return "📬 Sent your résumé to your DMs (kept private because it has your contact details)."

    async def send_export(self, user: discord.abc.User, formats: list, here: discord.abc.Messageable, options: Optional[dict] = None) -> None:
        """Their applications as a table, summary or files. Like a résumé, these never go in a public channel."""
        from vera.agents import exports
        options = options or {}
        everything = exports.collect(self.vera.user(user.id))
        rows = exports.narrow(everything, options)
        if not rows["sent"] and not rows["unfinished"]:
            if not everything["sent"] and not everything["unfinished"]:
                return await here.send("You don't have any applications yet, so there's nothing to show. Ask me to apply first.")
            when = f" for {options['label']}" if options.get("label") else ""
            return await here.send(f"I found no applications matching that{when}. You have {len(everything['sent'])} sent in total. Try a wider date range.")
        paths = await asyncio.to_thread(exports.write_all, rows, formats, rounds.user_dir(user.id) / "exports", options) if formats else []
        files = [discord.File(p, filename=p.name) for p in paths]
        if options.get("chat") and rounds.is_owner(user.id):     # he asked to be told right here. Any file still goes somewhere private
            for text in exports.render_chat(rows, options):
                await here.send(text, allowed_mentions=discord.AllowedMentions.none())
            if not files:
                return
            options = {**options, "chat": None}
        messages = exports.render_chat(rows, options) if options.get("chat") else ["📊 " + (f"**{options['label']}:** " if options.get("label") else "") + exports.summary(rows)]

        async def deliver(target) -> None:
            for i, text in enumerate(messages):
                extra = {"files": files} if files and i == len(messages) - 1 else {}
                await target.send(text, allowed_mentions=discord.AllowedMentions.none(), **extra)

        if rounds.is_owner(user.id):
            asked_privately = private.is_owner_place(here, config.OWNER_ID, self.user.id)
            target = here if asked_privately else await self.owner_channel("files")
            if target is not None:
                await deliver(target)
                if target is not here:
                    await here.send(f"📊 Posted in {target.mention}, which only you can see.")
                return
        try:
            await deliver(user)
            if getattr(here, "guild", None) is not None:
                await here.send("📬 Sent to your DMs, so your applications stay private.")
        except discord.Forbidden:
            await here.send("I couldn't DM you the file because your DMs are closed. Allow DMs from this server, then ask again.")

    async def _send_export_safely(self, user, formats, here, options=None) -> None:
        try:
            await self.send_export(user, formats, here, options)
        except Exception:
            log.exception("couldn't make the applications file")
            await here.send("Sorry, I couldn't make that file. Please try again.")

    async def post_jobs(self, channel: discord.abc.Messageable, uid: int, picks, header: str):
        session = self.vera.user(uid)
        await channel.send(header, allowed_mentions=discord.AllowedMentions.none())
        for job, ev in picks:
            key = self.vera.job_key(job["id"])
            msg = await channel.send(embed=E.job_card(job, ev), view=V.job_buttons(key))
            session.set_job_state(job["id"], "posted", msg.id)

    async def daily_mail_digest(self, local: datetime) -> bool:
        """Every evening, the day's replies from employers: in #vera-job-mail with job links, and in his private email channel
        with Gmail links too. Once a day."""
        from vera.agents import jobmail, outreach
        if not (config.OWNER_ID and outreach.can_send()):
            return False
        session = self.vera.user(config.OWNER_ID)
        last = session.db.get_json("mail_digest_last")
        today = local.date().isoformat()
        yesterday = (local.date() - timedelta(days=1)).isoformat()
        if local.hour >= config.MAIL_DIGEST_HOUR:
            day = today
        elif last and last < yesterday:
            day = yesterday          # the laptop was off or asleep at 8 PM: catch up in the morning
        else:
            return False
        if last == day or (last and last > day):
            return False
        await self.check_mail()                       # anything that came in since the last check is in it too
        session.db.set_json("mail_digest_last", day)
        late = "\n_(Posted late: I was offline at 8 PM.)_" if day != today else ""
        for target, is_private in await self.mail_targets():       # Gmail links only where only he can see them
            for part in split_message(jobmail.digest(session, day, gmail=is_private) + late):
                await target.send(part, allowed_mentions=discord.AllowedMentions.none())
        return True

    async def daily_tick(self):
        """Every 5 minutes: the daily digests when they're due, and interview and follow-up reminders."""
        tz = ZoneInfo(config.TIMEZONE)
        local = datetime.now(tz)
        today = local.date().isoformat()
        try:
            await self.daily_mail_digest(local)
        except Exception:
            log.exception("couldn't post the daily job mail summary")
        for uid in self.vera.db.users():
            session = self.vera.user(uid)
            prefs = session.prefs
            d = prefs["daily_digest"]
            if not d["enabled"] or local.hour != int(d["hour"]) or self.vera.db.get_json(f"last_digest:{uid}") == today:
                continue
            self.vera.db.set_json(f"last_digest:{uid}", today)
            try:
                res = await session.find_jobs(limit=int(prefs.get("digest_count") or 15))
            except Exception:
                log.exception(f"digest failed for {uid}")
                continue
            for g in self.guilds:
                if not g.get_member(uid):
                    continue
                ch = await self.main_channel(g)
                if not ch:
                    continue
                if res["picks"]:
                    await self.post_jobs(ch, uid, res["picks"], f"☀️ Good morning <@{uid}>! **{len(res['picks'])}** new job(s) posted in the "
                                                                f"last {prefs.get('max_age_days', 7)} days that match you:")
                else:
                    await ch.send(f"☀️ Good morning <@{uid}>! No new matching jobs today. Loosen a filter by telling me, "
                                  "e.g. *@Vera also include hybrid jobs in Pampanga*.")
                break
        for r in self.vera.due_reminders():
            a = r["app"]
            for g in self.guilds:
                if not g.get_member(a["user_id"]):
                    continue
                ch = await self.main_channel(g)
                if not ch:
                    continue
                # Only the person the reminder is for gets pinged: the job's title and company come from the web.
                just_them = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=int(a["user_id"]))])
                if r["kind"] == "interview":
                    when = r["when"].astimezone(tz)
                    await ch.send(f"⏰ <@{a['user_id']}> Interview for **{a['position']}** at **{a['company']}** "
                                  f"on {when:%a %d %b, %H:%M}. Good luck!", embed=E.application_detail(a), allowed_mentions=just_them)
                else:
                    await ch.send(f"📬 <@{a['user_id']}> No reply yet from **{a['company']}** for **{a['position']}** "
                                  f"(applied {a['applied_at'][:10]}). Consider a polite follow-up.",
                                  view=V.app_buttons(a["id"], stage="track"), allowed_mentions=just_them)
                break
            self.vera.mark_reminded(a["id"])
