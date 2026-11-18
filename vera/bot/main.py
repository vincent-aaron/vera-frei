"""Vera's Discord bot: slash commands, chat, the #application channel and background jobs.

Everyone in the server can use Vera; each person has their own profile,
preferences, saved jobs, applications and daily digest.
"""

import asyncio
import logging
import re
import socket
import sys
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from vera import NAME, __version__, ai, config
from vera.agents import assistant, rounds
from vera.bot import runtime as R, updates
from vera.bot import embeds as E
from vera.bot import views as V
from vera.preferences import split_list
from vera.services import APP_STATUSES, Vera
from vera.bot.core import VeraCore, split_message  # noqa: F401  (split_message is imported from here too)
from vera.sources import LABELS, SOURCES

log = logging.getLogger("vera.bot")
EPH = config.PRIVATE_REPLIES


class VeraBot(VeraCore, discord.Client):
    def __init__(self):
        intents = discord.Intents.default()     # mentions & DMs include message text without extra intents
        super().__init__(intents=intents, activity=discord.Activity(type=discord.ActivityType.listening, name="@Vera · /help"))
        self.tree = app_commands.CommandTree(self)
        self.vera = Vera()
        self._channels = {}

    async def setup_hook(self):
        self.add_dynamic_items(V.JobButton, V.AppButton)
        register_commands(self.tree)
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.background.start()
        self.mail_loop.start()


    @tasks.loop(hours=1)
    async def mail_loop(self):
        await self.check_mail()

    @mail_loop.before_loop
    async def _wait_mail(self):
        await self.wait_until_ready()

    async def on_ready(self):
        await self.startup()

    # ------------------------------------------------------------------ chat: @Vera ... or DM
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.user is None:
            return
        # Discord lists the Vera bot and the Vera role side by side under the same name, so either tag means her.
        my_role = message.guild.self_role if message.guild else None
        tagged_role = my_role is not None and my_role in message.role_mentions
        replied_to = getattr(getattr(message, "reference", None), "resolved", None)
        replied_to_me = isinstance(replied_to, discord.Message) and replied_to.author == self.user
        if not (message.guild is None or self.user in message.mentions or tagged_role or replied_to_me):
            return

        def tidy(content: str) -> str:
            content = re.sub(rf"<@!?{self.user.id}>", "", content or "")
            return (content.replace(f"<@&{my_role.id}>", "") if my_role else content).strip()

        text = tidy(message.content)
        if not text and not message.attachments and (tagged_role or replied_to_me):
            # Discord may hide the words unless the bot itself is tagged, so ask for the message.
            try:
                text = tidy((await message.channel.fetch_message(message.id)).content)
            except discord.HTTPException:
                text = ""
            if not text:
                return await message.reply("I saw you tag me, but Discord hid your message. Try again: type **@Vera** and pick the one marked **APP**, not the role.", mention_author=False)
        files = [a for a in message.attachments if a.filename.lower().endswith((".pdf", ".docx"))]
        if files:
            note = await self.save_sent_resume(message.author.id, files[0], in_private=message.guild is None)
            if not text:
                return await message.reply(note, mention_author=False)
            await message.reply(note, mention_author=False)
        if not text:
            return await message.reply("Hi! Ask me anything about your job search, or tell me what jobs you want, e.g. "
                                       "*only remote jobs that need no experience*. `/help` lists my commands.", mention_author=False)
        session = self.vera.user(message.author.id)
        async with message.channel.typing():
            try:
                channel_name = getattr(message.channel, "name", "")
                result = await assistant.handle(session, text, config.CHANNEL_SITES.get(channel_name), message.channel.id, channel_name)
            except Exception:
                log.exception("assistant failed")
                return await message.reply("Sorry, something went wrong on my side. Please try again.", mention_author=False)
        if result["action"] == "send_resume":
            return await message.reply(await self.send_resume(message.author, message.channel), mention_author=False)
        if rounds.is_owner(message.author.id):
            try:
                await self.note_new_rules(result)
            except discord.DiscordException:
                log.exception("couldn't post the new rule")
        if result["action"] == "set_updates_channel":
            self.vera.db.set_json(updates.KEY_CHANNEL, message.channel.id)
        if result["action"] == "stop_apply":
            result["reply"] = self.stop_for(message.author.id, message.channel.id, text)
        if result["action"] == "send_export":
            self._export_task = asyncio.create_task(self._send_export_safely(message.author, result["request"]["formats"], message.channel, result["request"].get("options")))
        if result["action"] == "check_mail":
            self._mail_task = asyncio.create_task(self.check_mail(report_to=message.channel))
        if result["action"] == "send_outreach":
            self._rounds_task = asyncio.create_task(self.run_outreach(message.author.id, result["request"], message.channel))
        if result["action"] == "recommend":
            r = result["request"]
            self._rounds_task = asyncio.create_task(self.run_recommend(message.author.id, r["count"], r["sites"], message.channel))
        if result["action"] == "start_apply":
            r = result["request"]
            # Owner: keep the conversation where it started. Others: private, so their applications aren't public.
            here = message.channel if rounds.is_owner(message.author.id) or message.guild is None else None
            turn = self.start_or_wait({"uid": message.author.id, "dry_run": r["dry_run"], "count": r["count"], "sites": r["sites"], "channel": here, "channel_id": message.channel.id})
            if turn:
                result["reply"] = turn
        reply = result.get("reply") or ("Done." if result["patch"] else "Okay.")
        if result["changes"]:
            reply += "\n**Updated your preferences:**\n" + "\n".join(result["changes"])
        for i in range(0, len(reply), 1900):          # long chat answers are split, not cut
            await (message.reply(reply[i:i + 1900], mention_author=False) if i == 0 else message.channel.send(reply[i:i + 1900]))
        action = result["action"]
        if action == "show_preferences":
            await message.channel.send(embed=E.prefs_card(session.prefs))
        elif action == "statistics":
            await message.channel.send(embed=E.stats_card(session.statistics()))
        elif action == "search":
            async with message.channel.typing():
                res = await session.find_jobs(result.get("query"), limit=session.prefs["results_per_search"], force=bool(result["patch"]))
            ch = message.channel      # results stay where you asked
            if not res["picks"]:
                await message.channel.send(f"I searched with your filters but found no new matches right now "
                                           f"(only jobs posted in the last {session.prefs['max_age_days']} days count). "
                                           "Try loosening a filter, or add jobs you find with `/add-job`.")
            else:
                await self.post_jobs(ch, message.author.id, res["picks"],
                                     f"🔎 {message.author.mention}: **{len(res['picks'])}** match(es) for you (of {res['total_matches']}):")
                if ch != message.channel:
                    await message.channel.send(f"Posted them in {ch.mention}.")

    # ------------------------------------------------------------------ background jobs (every 5 min)
    @tasks.loop(minutes=5)
    async def background(self):
        await self.daily_tick()

    @background.before_loop
    async def _wait(self):
        await self.wait_until_ready()


# ============================================================================ commands
def register_commands(tree: app_commands.CommandTree):

    def me(interaction: discord.Interaction):
        return R.of(interaction.client).vera.user(interaction.user.id)

    @tree.command(name="find-jobs", description="Search for jobs that match your preferences")
    @app_commands.describe(query="Optional keywords (default: your preferred positions)", limit="How many to show (1-20)",
                           refresh="Ignore the cache and fetch fresh listings")
    async def find_jobs(interaction: discord.Interaction, query: Optional[str] = None,
                        limit: Optional[app_commands.Range[int, 1, 20]] = None, refresh: bool = False):
        await interaction.response.defer(ephemeral=EPH, thinking=True)
        bot: VeraBot = R.of(interaction.client)
        res = await me(interaction).find_jobs(query, limit, refresh)
        status = " · ".join(f"{LABELS.get(k, k)} {v}" for k, v in res["status"].items()) or "no sources enabled"
        if not res["picks"]:
            return await interaction.followup.send(
                f"No new matches for {interaction.user.mention} above the minimum match score.\nSources: {status}\n"
                "Try telling me what to loosen (e.g. *@Vera include junior jobs up to 1 year experience*) or `/add-job`.", ephemeral=EPH)
        ch = interaction.channel or await bot.main_channel(interaction.guild)
        await bot.post_jobs(ch, interaction.user.id, res["picks"], f"🔎 {interaction.user.mention}: **{len(res['picks'])}** best of "
                                                                     f"{res['total_matches']} matches" + (f" for “{query}”" if query else "") + ":")
        await interaction.followup.send(f"Posted {len(res['picks'])} job(s) in {ch.mention}.\nSources: {status}", ephemeral=EPH)

    @tree.command(name="preferences", description="View and change your job-search preferences")
    async def prefs(interaction: discord.Interaction):
        await interaction.response.send_message(f"{interaction.user.mention}'s preferences:", embed=E.prefs_card(me(interaction).prefs),
                                                view=V.PrefsPanel(interaction.user.id), ephemeral=EPH)

    @tree.command(name="reset-preferences", description="Reset your search preferences to defaults")
    async def reset(interaction: discord.Interaction):
        await interaction.response.send_message("Reset **all** your search preferences to the defaults?",
                                                view=V.ConfirmReset(interaction.user.id), ephemeral=EPH)

    @tree.command(name="profile", description="View and edit your candidate profile")
    async def profile(interaction: discord.Interaction):
        await interaction.response.send_message(embed=E.profile_card(me(interaction).profile, private=EPH),
                                                view=V.ProfilePanel(interaction.user.id), ephemeral=EPH)

    resume = app_commands.Group(name="resume", description="Manage the résumés Vera can attach")

    @resume.command(name="add", description="Upload a résumé (PDF/DOCX); optionally fill your profile from it")
    @app_commands.describe(file="Your résumé file", name="A name, e.g. 'Web developer CV'", tags="Comma-separated: web, python, android",
                           import_profile="Read the résumé and fill your profile from it (recommended for first setup)")
    async def resume_add(interaction: discord.Interaction, file: discord.Attachment, name: str, tags: Optional[str] = "",
                         import_profile: bool = False):
        if not file.filename.lower().endswith((".pdf", ".docx")):
            return await interaction.response.send_message("Please upload a PDF or DOCX file.", ephemeral=True)
        if file.size > 8 * 1024 * 1024:
            return await interaction.response.send_message("That file is over 8 MB. Please upload a smaller résumé.", ephemeral=True)
        await interaction.response.defer(ephemeral=EPH, thinking=True)
        session = me(interaction)
        tmp = config.DATA_DIR / f"upload-{interaction.user.id}-{Path(file.filename).name}"
        await file.save(tmp)
        r = session.add_resume(tmp, name, split_list(tags))
        tmp.unlink(missing_ok=True)
        msg = f"📄 Saved **{r['name']}** for {interaction.user.mention} (tags: {', '.join(r['tags']) or 'none'})."
        if import_profile:
            res = await session.import_resume(Path(r["path"]))
            if res["ok"]:
                msg += (f"\n🧾 Profile filled from your résumé ({', '.join(res['fields'])})"
                        + ("" if res["ai_used"] else ". AI was busy, so I only picked up skills; run it again later for the rest")
                        + ". Check it with `/profile`.")
            else:
                msg += f"\n⚠️ Couldn't import: {res['reason']}."
        await interaction.followup.send(msg, ephemeral=EPH)

    @resume.command(name="get", description="Get your résumé file (only you can see it)")
    async def resume_get(interaction: discord.Interaction):
        rs, files = R.of(interaction.client).resume_files(interaction.user.id)
        if not rs:
            return await interaction.response.send_message("You haven't uploaded a résumé yet. Use `/resume add`.", ephemeral=True)
        await interaction.response.send_message("📄 " + ", ".join(f"**{r['name']}**" for r in rs), files=files, ephemeral=True)

    @resume.command(name="list", description="List your résumés")
    async def resume_list(interaction: discord.Interaction):
        rs = me(interaction).profile["resumes"]
        await interaction.response.send_message(embed=E.lines_embed(f"📄 {interaction.user.display_name}'s résumés",
                                                                   [f"**{r['name']}**: {', '.join(r.get('tags', [])) or 'no tags'}" for r in rs],
                                                                   "None yet. Use /resume add."), ephemeral=EPH)

    @resume.command(name="remove", description="Remove one of your résumés")
    async def resume_remove(interaction: discord.Interaction, name: str):
        session = me(interaction)
        pr = session.profile
        before = len(pr["resumes"])
        pr["resumes"] = [r for r in pr["resumes"] if r["name"] != name]
        session.db.set_profile(session.uid, pr)
        await interaction.response.send_message("🗑️ Removed." if len(pr["resumes"]) < before else "Not found.", ephemeral=EPH)

    @resume_remove.autocomplete("name")
    async def _resume_ac(interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in me(interaction).profile["resumes"]
                if current.lower() in r["name"].lower()][:25]

    tree.add_command(resume)

    @tree.command(name="saved", description="View your saved job listings")
    async def saved(interaction: discord.Interaction):
        vera, session = R.of(interaction.client).vera, me(interaction)
        jobs = session.db.saved_jobs(session.uid)
        lines = [f"⭐ `{vera.job_key(j['id'])}` **{E.clip(j['title'], 60)}** — {E.clip(j['company'] or '', 40)} · {j.get('score') or '?'}%" for j in jobs]

        async def open_job(i: discord.Interaction, key: str):
            job = vera.job_by_key(key)
            ev = vera.user(i.user.id).evaluate(job)
            await i.response.send_message(embed=E.job_card(job, ev), view=V.job_buttons(int(key), ("apply", "skip", "details")), ephemeral=EPH)
        view = V.OpenSelect(interaction.user.id, [(str(vera.job_key(j["id"])), j["title"], j["company"] or "") for j in jobs], open_job) if jobs else None
        await interaction.response.send_message(embed=E.lines_embed(f"⭐ {interaction.user.display_name}'s saved jobs ({len(jobs)})", lines,
                                                                   "Nothing saved yet."), view=view, ephemeral=EPH)

    @tree.command(name="apply", description="Start an application (Vera prepares it; you approve before anything is sent)")
    @app_commands.describe(job="A saved/recommended job, its number, or a job link")
    async def apply(interaction: discord.Interaction, job: str):
        j = R.of(interaction.client).vera.job_by_key(job.strip())
        if not R.of(interaction.client).vera.may_use_job(j, interaction.user.id):
            return await interaction.response.send_message("I don't know that job. Pick one from the list, or add it with /add-job.", ephemeral=True)
        await V.start_apply(interaction, j)

    @apply.autocomplete("job")
    async def _apply_ac(interaction: discord.Interaction, current: str):
        rows = R.of(interaction.client).vera.db.rows(
            "SELECT j.rowid k, j.title, j.company FROM user_job_state s JOIN jobs j ON j.id=s.job_id "
            "WHERE s.user_id=? AND s.state IN ('saved','posted') ORDER BY s.updated_at DESC LIMIT 50", (interaction.user.id,))
        return [app_commands.Choice(name=E.clip(f"{r['title']} — {r['company'] or ''}", 100), value=str(r["k"]))
                for r in rows if current.lower() in f"{r['title']} {r['company']}".lower()][:25]

    @tree.command(name="applications", description="View your application history")
    @app_commands.describe(status="Filter by status")
    @app_commands.choices(status=[app_commands.Choice(name=s, value=s) for s in APP_STATUSES])
    async def applications(interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None):
        session = me(interaction)
        apps = session.db.applications(session.uid, status.value if status else None, limit=25)

        async def open_app(i: discord.Interaction, app_id: str):
            a = session.db.application(int(app_id))
            stage = "approve" if a["status"] == "ready" else "track"
            await i.response.send_message(embed=E.application_detail(a), view=V.app_buttons(a["id"], stage), ephemeral=EPH)
        view = V.OpenSelect(interaction.user.id, [(str(a["id"]), f"#{a['id']} {a['position']}", f"{a['company']} · {a['status']}") for a in apps],
                            open_app) if apps else None
        await interaction.response.send_message(embed=E.lines_embed(f"📋 {interaction.user.display_name}'s applications",
                                                                   [E.application_row(a) for a in apps], "No applications yet."),
                                                view=view, ephemeral=EPH)

    @tree.command(name="statistics", description="Your job-search statistics")
    async def statistics(interaction: discord.Interaction):
        await interaction.response.send_message(content=f"{interaction.user.mention}'s progress:",
                                                embed=E.stats_card(me(interaction).statistics()), ephemeral=EPH)

    @tree.command(name="add-job", description="Add a job you found on any other site, to evaluate and track it")
    async def add_job(interaction: discord.Interaction):
        await interaction.response.send_modal(V.AddJobModal())

    @tree.command(name="ask", description="Ask Vera anything: job advice, interview tips, or change your filters in plain words")
    @app_commands.describe(message="e.g. 'How do I prepare for a junior developer interview?' or 'only remote jobs'")
    async def ask(interaction: discord.Interaction, message: str):
        if getattr(R.of(interaction.client), "hermes", False):      # on Hermes, its AI answers chat: mentions and DMs
            return await interaction.response.send_message(
                f"Ask me by tagging **@{NAME}** in a channel, or in a DM, and I'll answer there:\n> {E.clip(message, 200)}", ephemeral=True)
        await interaction.response.defer(ephemeral=EPH, thinking=True)
        result = await assistant.handle(me(interaction), message)
        if result["action"] == "send_resume":
            rs, files = R.of(interaction.client).resume_files(interaction.user.id)
            if not rs:
                return await interaction.followup.send("You haven't uploaded a résumé yet. Use `/resume add`.", ephemeral=True)
            return await interaction.followup.send("📄 " + ", ".join(f"**{r['name']}**" for r in rs), files=files, ephemeral=True)
        reply = result.get("reply") or ("Done." if result["patch"] else "Okay.")
        if result["changes"]:
            reply += "\n**Updated your preferences:**\n" + "\n".join(result["changes"])
        await interaction.followup.send(f"> {E.clip(message, 200)}\n{E.clip(reply, 1800)}", ephemeral=EPH)
        if result["action"] == "search":
            res = await me(interaction).find_jobs(result.get("query"))
            if res["picks"]:
                ch = interaction.channel or await R.of(interaction.client).main_channel(interaction.guild)
                await R.of(interaction.client).post_jobs(ch, interaction.user.id, res["picks"],
                                                   f"🔎 {interaction.user.mention}: **{len(res['picks'])}** match(es) for you:")

    @tree.command(name="rounds", description="Application rounds: your limits and status, or a test round that sends nothing")
    @app_commands.describe(action="status, or a test round that fills forms but sends nothing",
                           max_per_run="Applications per round", max_per_day="Applications per day",
                           cover_letters="Write a cover letter for each job")
    @app_commands.choices(action=[app_commands.Choice(name=n, value=n) for n in ("status", "test")])
    async def rounds_cmd(interaction: discord.Interaction, action: str,
                            max_per_run: Optional[app_commands.Range[int, 1, 50]] = None, max_per_day: Optional[app_commands.Range[int, 1, 200]] = None,
                            cover_letters: Optional[bool] = None):
        """Real rounds start in chat: ask her to apply, read the plan, and answer **proceed**."""
        client, uid, session = R.of(interaction.client), interaction.user.id, me(interaction)
        patch = {k: v for k, v in (("max_per_run", max_per_run), ("max_per_day", max_per_day), ("cover_letters", cover_letters)) if v is not None}
        if patch:
            session.update_prefs({"rounds": patch})
        text = rounds.status_text(client.vera, uid)
        if action == "test":
            missing = rounds.blockers(rounds.build_profile(session))
            if missing:
                text = "I can't fill forms for you yet. Still needed: " + ", ".join(missing) + ". Use `/apply-details` and `/resume add`."
            elif rounds.is_running():
                text = "Someone's run is in progress, and they run one at a time. Please try again in a while."
            else:
                client._rounds_task = asyncio.create_task(client.run_rounds(uid, dry_run=True))
                where = f"in #{config.OWNER_CHANNELS['reports']}" if rounds.is_owner(uid) else "by DM"
                text = f"🧪 Test started: I'll fill forms but send nothing. I'll send the report {where} when it's done."
        await interaction.response.send_message(text, ephemeral=True)

    @tree.command(name="apply-details", description="Details application forms ask for. Only you see this.")
    @app_commands.describe(salary_php="Expected monthly salary in pesos, e.g. 28000", salary_usd="Expected monthly salary for overseas jobs, in USD",
                           start="When you can start, e.g. 2 weeks' notice", work_in_philippines="Can you legally work in the Philippines?",
                           relocate="Willing to relocate?", onsite="Willing to work on-site?", first_name="First name(s)", last_name="Last name",
                           email="Email for applications", phone="Phone number", location="City, Province, Country",
                           linkedin="LinkedIn link", current_title="Current job title", current_company="Current company")
    async def apply_details(interaction: discord.Interaction, salary_php: Optional[app_commands.Range[int, 1]] = None,
                            salary_usd: Optional[app_commands.Range[int, 1]] = None, start: Optional[str] = None,
                            work_in_philippines: Optional[bool] = None, relocate: Optional[bool] = None, onsite: Optional[bool] = None,
                            first_name: Optional[str] = None, last_name: Optional[str] = None, email: Optional[str] = None, phone: Optional[str] = None,
                            location: Optional[str] = None, linkedin: Optional[str] = None, current_title: Optional[str] = None, current_company: Optional[str] = None):
        session = me(interaction)
        app = {k: v for k, v in (("expected_salary_monthly", salary_php), ("expected_salary_usd_monthly", salary_usd), ("notice_period", start),
                                 ("work_authorized", work_in_philippines), ("willing_to_relocate", relocate), ("willing_to_work_onsite", onsite),
                                 ("first_name", first_name), ("last_name", last_name), ("current_title", current_title), ("current_company", current_company)) if v is not None}
        top = {k: v for k, v in (("email", email), ("phone", phone), ("location", location)) if v is not None}
        if linkedin is not None:
            top["links"] = {"linkedin": linkedin}
        session.update_profile({**top, "application": app})
        missing = rounds.missing_details(rounds.build_profile(session))
        await interaction.response.send_message("✅ Saved." + (f" Still needed: {', '.join(missing)}." if missing else " You have everything I need. Try `/rounds test`."), ephemeral=True)

    @tree.command(name="answer", description="Save your answer to a question a round couldn't answer honestly")
    @app_commands.describe(question="The question, as shown in the report", answer="Your answer")
    async def answer_cmd(interaction: discord.Interaction, question: str, answer: str):
        rounds.save_answer(interaction.user.id, question, answer)
        await interaction.response.send_message(f"Saved. Next time I see **{question[:100]}**, I'll answer: {answer[:200]}", ephemeral=True)

    @tree.command(name="status", description="Vera's health: AI, sources, schedule")
    async def status(interaction: discord.Interaction):
        vera, p = R.of(interaction.client).vera, me(interaction).prefs
        ages = {s: vera.db.row("SELECT MAX(fetched_at) t FROM source_cache WHERE source=?", (s,))["t"] for s in SOURCES}
        e = discord.Embed(title=f"🤖 {NAME} v{__version__}", color=E.BLUE)
        e.add_field(name="AI (free, tried in this order)", value=" → ".join(ai.providers()) or "not configured", inline=False)
        e.add_field(name="Sources", value="\n".join(f"{'✅' if s in p['sources'] else '⏸️'} {LABELS[s]} · last fetch {(ages[s] or 'never')[:16]}" for s in SOURCES), inline=False)
        e.add_field(name="Users", value=str(len(vera.db.users())))
        e.add_field(name="Your daily digest", value=f"{p['digest_count']} jobs at {p['daily_digest']['hour']:02d}:00 {config.TIMEZONE}" if p["daily_digest"]["enabled"] else "off")
        e.add_field(name="Applies through", value="Company forms (Greenhouse, Lever, Ashby, Workable), after you approve a plan")
        await interaction.response.send_message(embed=e, ephemeral=EPH)

    @tree.command(name="help", description="What Vera can do")
    async def help_(interaction: discord.Interaction):
        lines = [
            "**Just talk to me:** *@Vera find remote jobs that need no experience* · *@Vera how should I answer 'tell me about yourself'?* "
            "· DMs work too · or `/ask`",
            "**Pick your jobs:** *@Vera only Web Developer, QA Tester and IT Support jobs, no experience, remote or Bulacan*: "
            "I'll suggest only those, just for you",
            "**Your résumé:** *@Vera can I see my resume?* (I DM it to you) · `/resume get`",
            "",
            "`/resume add` (with **import_profile: True**): set up your profile from your résumé",
            "`/find-jobs [query] [limit]`: search and post your matches in #application",
            "`/preferences`: levels, experience, positions, locations, salary, types, exclusions, requirements, sources, daily digest",
            "`/profile`: your facts (I never invent qualifications) · `/resume list|remove`",
            "`/saved` · `/apply <job>` · `/applications [status]` · `/statistics` · `/add-job` (a job you found on any other site)",
            "`/reset-preferences` · `/status`",
            "**Application rounds:** `/resume add` → `/apply-details` → *@Vera apply to 5 jobs* → read the plan → reply **test** first, then **proceed**. "
            "Nothing runs on a schedule · `/answer`: answer a question a round couldn't",
            "",
            "Everyone has their own profile, filters and matches. On each job: **Apply · Save · Skip · View details** (they act for whoever clicks). "
            "Applications are only sent after their owner approves: a round's plan with **proceed**, or one job's **Approve & submit**.",
        ]
        await interaction.response.send_message(embed=E.lines_embed(f"🧭 {NAME}: your job-hunting assistant", lines, ""), ephemeral=EPH)


ALREADY_RUNNING = 3
_instance_lock = None


def claim_single_instance() -> bool:
    """Only one Vera at a time, or every message and application would happen twice."""
    global _instance_lock
    _instance_lock = socket.socket()
    try:
        _instance_lock.bind(("127.0.0.1", 48917))
        return True
    except OSError:
        return False


def setup_logging() -> None:
    config.ensure_dirs()
    handlers = [logging.FileHandler(config.DATA_DIR / "vera.log", encoding="utf-8")]
    if sys.stderr is not None:      # pythonw (background mode) has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    for noisy in ("discord.gateway", "discord.http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def background() -> None:
    """Windowless mode used by the scheduled task: stays online, restarting after a crash."""
    setup_logging()
    if not claim_single_instance():
        log.info("Vera is already running; this copy is exiting")
        raise SystemExit(ALREADY_RUNNING)
    while True:
        try:
            run(setup=False)
        except discord.LoginFailure:
            log.error("Discord rejected the bot token. Fix DISCORD_TOKEN in app/.env")
            raise SystemExit(1)
        except Exception:
            log.exception("Vera stopped unexpectedly")
        log.info("Restarting in 30 seconds")
        time.sleep(30)


def run(setup: bool = True):
    if setup:
        setup_logging()
        if not claim_single_instance():
            print("Vera is already running in the background. Use rollback\\stop-vera-hidden.bat first if you want to run her here.")
            raise SystemExit(ALREADY_RUNNING)
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Put your bot token in app/.env (see .env.example).")
    try:
        VeraBot().run(config.DISCORD_TOKEN, log_handler=None)
    except Exception:
        log.exception("Vera crashed")          # so we can always see why she went offline
        raise
    finally:
        log.info("Vera stopped")
