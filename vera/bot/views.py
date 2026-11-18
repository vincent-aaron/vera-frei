"""Buttons, selects and forms (modals).

Multi-user: every action runs for the person who clicked. Job and application
buttons are DynamicItems (custom_id encodes action + id), so they keep working
after the bot restarts. Application buttons only work for the application's owner.
"""

import re
from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import discord

from vera import config, preferences as P
from vera.bot import embeds as E
from vera.bot import runtime as R
from vera.sources import LABELS

EPH = config.PRIVATE_REPLIES   # False = replies visible to everyone


# --------------------------------------------------------------------------- helpers
def vera_of(interaction: discord.Interaction):
    return R.of(interaction.client).vera


def me(interaction: discord.Interaction):
    """The clicking user's own Vera session (profile, preferences, applications)."""
    return R.of(interaction.client).vera.user(interaction.user.id)


def yes(text: str, default: bool = False) -> bool:
    t = (text or "").strip().lower()
    return default if not t else t in ("y", "yes", "true", "1", "on")


async def main_channel(interaction: discord.Interaction):
    return await R.of(interaction.client).main_channel(interaction.guild)


class OwnedView(discord.ui.View):
    """A panel only the person who opened it can use (others get a hint)."""

    def __init__(self, owner_id: int, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This panel belongs to someone else. Open your own with the same command.",
                                                ephemeral=True)
        return False


# --------------------------------------------------------------------------- job card buttons
class JobButton(discord.ui.DynamicItem[discord.ui.Button], template=r"(?:frei|vera):job:(?P<action>apply|save|skip|details):(?P<key>\d+)"):
    STYLE = {"apply": (discord.ButtonStyle.success, "Apply", "📝"), "save": (discord.ButtonStyle.primary, "Save", "⭐"),
             "skip": (discord.ButtonStyle.secondary, "Skip", "⏭️"), "details": (discord.ButtonStyle.secondary, "View details", "🔍")}

    def __init__(self, action: str, key: int):
        style, label, emoji = self.STYLE[action]
        super().__init__(discord.ui.Button(style=style, label=label, emoji=emoji, custom_id=f"vera:job:{action}:{key}"))
        self.action, self.key = action, int(key)

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["action"], int(match["key"]))

    async def callback(self, interaction: discord.Interaction):
        vera, session = vera_of(interaction), me(interaction)
        job = vera.job_by_key(str(self.key))
        if not job:
            return await interaction.response.send_message("That job is no longer in the database.", ephemeral=True)
        if not vera.may_use_job(job, interaction.user.id):
            # Someone else added this link by hand. It stays theirs: it could point anywhere.
            return await interaction.response.send_message("Someone else added this job by hand, so it's theirs. If you want it too, "
                                                           "add it yourself with `/add-job` after checking the link.", ephemeral=True)
        who = interaction.user.display_name
        if self.action == "save":
            session.set_job_state(job["id"], "saved")
            return await interaction.response.send_message(f"⭐ {who} saved **{job['title']}**. See `/saved`.", ephemeral=EPH)
        if self.action == "skip":
            session.set_job_state(job["id"], "skipped")
            return await interaction.response.send_message(f"⏭️ {who} skipped **{job['title']}**. It won't be suggested to them again.",
                                                           ephemeral=EPH)
        if self.action == "details":
            await interaction.response.defer(ephemeral=EPH, thinking=True)
            from vera.agents.writer import summarize
            ev = session.evaluate(job)
            summary = await summarize(job)
            return await interaction.followup.send(content=f"Match for {interaction.user.mention}:",
                                                   embed=E.details_card(job, ev, summary), view=job_buttons(self.key), ephemeral=EPH)
        if self.action == "apply":
            await start_apply(interaction, job)


def job_buttons(key: int, include: tuple = ("apply", "save", "skip", "details")) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for a in include:
        v.add_item(JobButton(a, key))
    return v


async def start_apply(interaction: discord.Interaction, job: Dict[str, Any]) -> None:
    """Spec 7: prepare everything with the clicker's profile, then post it for their approval."""
    session = me(interaction)
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=EPH, thinking=True)
    if not session.profile.get("name") and not session.profile.get("resume_text"):
        return await interaction.followup.send("Set up your profile first: `/resume add` with **import_profile: True**, "
                                               "or `/profile`. I only use facts you give me.", ephemeral=EPH)
    prep = await session.start_application(job)
    if prep.get("error"):
        return await interaction.followup.send(prep["error"], ephemeral=EPH)
    channel = await main_channel(interaction) or interaction.channel
    msg = await channel.send(content=f"{interaction.user.mention}, your application is ready to review:",
                             embed=E.application_card(prep), view=app_buttons(prep["application_id"], stage="approve"))
    await interaction.followup.send(f"📝 Application #{prep['application_id']} is ready for your approval: {msg.jump_url}", ephemeral=EPH)


# --------------------------------------------------------------------------- application buttons (owner only)
APP_ACTIONS = {
    "approve": (discord.ButtonStyle.success, "Approve & submit", "✅"),
    "edit": (discord.ButtonStyle.primary, "Edit an answer", "✏️"),
    "cancel": (discord.ButtonStyle.danger, "Cancel", "✖️"),
    "applied": (discord.ButtonStyle.success, "Mark as applied", "📨"),
    "interview": (discord.ButtonStyle.primary, "Interview", "🗓️"),
    "offer": (discord.ButtonStyle.success, "Offer", "🎉"),
    "rejected": (discord.ButtonStyle.secondary, "Rejected", "❌"),
    "withdrawn": (discord.ButtonStyle.secondary, "Withdraw", "↩️"),
    "note": (discord.ButtonStyle.secondary, "Add note", "🗒️"),
}


class AppButton(discord.ui.DynamicItem[discord.ui.Button], template=r"(?:frei|vera):app:(?P<action>[a-z]+):(?P<id>\d+)"):
    def __init__(self, action: str, app_id: int):
        style, label, emoji = APP_ACTIONS[action]
        super().__init__(discord.ui.Button(style=style, label=label, emoji=emoji, custom_id=f"vera:app:{action}:{app_id}"))
        self.action, self.app_id = action, int(app_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["action"], int(match["id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        app = vera_of(interaction).db.application(self.app_id)
        if app and int(app.get("user_id") or 0) == interaction.user.id:
            return True
        await interaction.response.send_message("🔒 Only the person this application belongs to can change it.", ephemeral=True)
        return False

    async def callback(self, interaction: discord.Interaction):
        session = me(interaction)
        app = session.db.application(self.app_id)
        if not app:
            return await interaction.response.send_message("Application not found.", ephemeral=True)
        a = self.action
        if a == "approve":
            if app["status"] not in ("ready", "failed"):
                return await interaction.response.send_message(f"Already {app['status']}.", ephemeral=True)
            await interaction.response.defer(thinking=True)
            res = await session.approve(self.app_id)
            if res["status"] == "submitted":
                await interaction.followup.send(f"📨 Submitted **{app['position']}** at **{app['company']}**. {res['detail']}",
                                                allowed_mentions=discord.AllowedMentions.none())
                return await interaction.message.edit(view=app_buttons(self.app_id, stage="track"))
            if res["status"] == "manual":
                text = (f"✅ Approved. This site can't be submitted automatically, so apply here: {res['manual_url']}\n"
                        "Your approved answers are in the card above. Press **Mark as applied** when you've sent it.")
            else:
                text = (f"⚠️ Couldn't submit automatically ({res['status']}: {res['detail']}).\n"
                        f"Please apply manually: {res['manual_url']}, then press **Mark as applied**.")
            await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())     # the detail comes from a web page
            return await interaction.message.edit(view=app_buttons(self.app_id, stage="manual"))
        if a == "edit":
            answers = (app.get("answers") or {}).get("answers", {})
            if not answers:
                return await interaction.response.send_message("No answers to edit.", ephemeral=True)
            return await interaction.response.send_message("Which answer?", view=EditAnswerPicker(interaction.user.id, self.app_id, answers),
                                                           ephemeral=True)
        if a == "cancel":
            session.mark(self.app_id, "withdrawn", "cancelled before submitting")
            await interaction.response.send_message("✖️ Application cancelled. Nothing was submitted.", ephemeral=EPH)
            return await interaction.message.edit(view=None)
        if a == "interview":
            return await interaction.response.send_modal(InterviewModal(self.app_id))
        if a == "note":
            return await interaction.response.send_modal(NoteModal(self.app_id))
        session.mark(self.app_id, a)
        await interaction.response.send_message(f"{E.STATUS_ICON.get(a, '')} Application #{self.app_id} marked **{a}**.", ephemeral=EPH)
        if a == "applied":
            await interaction.message.edit(view=app_buttons(self.app_id, stage="track"))


def app_buttons(app_id: int, stage: str = "approve") -> discord.ui.View:
    actions = {"approve": ("approve", "edit", "cancel"),
               "manual": ("applied", "edit", "cancel"),
               "track": ("interview", "offer", "rejected", "withdrawn", "note")}[stage]
    v = discord.ui.View(timeout=None)
    for a in actions:
        v.add_item(AppButton(a, app_id))
    return v


class EditAnswerPicker(OwnedView):
    def __init__(self, owner_id: int, app_id: int, answers: Dict[str, str]):
        super().__init__(owner_id, timeout=300)
        self.app_id, self.answers = app_id, answers
        sel = discord.ui.Select(placeholder="Choose a question", options=[
            discord.SelectOption(label=E.clip(q, 100), value=str(i)) for i, q in enumerate(list(answers)[:25])])
        sel.callback = self.pick
        self.add_item(sel)

    async def pick(self, interaction: discord.Interaction):
        q = list(self.answers)[int(interaction.data["values"][0])]
        await interaction.response.send_modal(EditAnswerModal(self.app_id, q, self.answers[q]))


class EditAnswerModal(discord.ui.Modal, title="Edit answer"):
    def __init__(self, app_id: int, question: str, answer: str):
        super().__init__()
        self.app_id, self.question = app_id, question
        self.answer = discord.ui.TextInput(label=E.clip(question, 45), style=discord.TextStyle.paragraph,
                                           default=answer[:4000], max_length=4000)
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        me(interaction).update_answer(self.app_id, self.question, str(self.answer))
        await interaction.response.send_message("✏️ Answer updated. It will be used when you approve.", ephemeral=True)


class InterviewModal(discord.ui.Modal, title="Interview schedule"):
    when = discord.ui.TextInput(label="Date & time (YYYY-MM-DD HH:MM)", placeholder="2026-11-05 14:00")
    note = discord.ui.TextInput(label="Notes (type, link, interviewer...)", style=discord.TextStyle.paragraph, required=False)

    def __init__(self, app_id: int):
        super().__init__()
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            when = datetime.strptime(str(self.when).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(config.TIMEZONE))
        except ValueError:
            return await interaction.response.send_message("Use the format YYYY-MM-DD HH:MM (24-hour).", ephemeral=True)
        me(interaction).set_interview(self.app_id, when, str(self.note))
        await interaction.response.send_message(f"🗓️ Interview saved for {when:%a %d %b %Y, %H:%M} ({config.TIMEZONE}). "
                                                "I'll remind you the day before.", ephemeral=EPH)


class NoteModal(discord.ui.Modal, title="Add a note"):
    note = discord.ui.TextInput(label="Note", style=discord.TextStyle.paragraph)

    def __init__(self, app_id: int):
        super().__init__()
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction):
        me(interaction).add_note(self.app_id, str(self.note))
        await interaction.response.send_message("🗒️ Note added.", ephemeral=True)


# --------------------------------------------------------------------------- add a job manually
class AddJobModal(discord.ui.Modal, title="Add a job you found elsewhere"):
    url = discord.ui.TextInput(label="Job link", placeholder="https://...")
    title_ = discord.ui.TextInput(label="Position", max_length=200)
    company = discord.ui.TextInput(label="Company · Location", placeholder="Acme Inc · Makati (Hybrid)", required=False)
    salary = discord.ui.TextInput(label="Salary · Employment type", placeholder="PHP 25,000-30,000 monthly · Full-time", required=False)
    desc = discord.ui.TextInput(label="Job description (paste it)", style=discord.TextStyle.paragraph, max_length=4000, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        vera, session = vera_of(interaction), me(interaction)
        company, _, location = str(self.company).partition("·")
        salary, _, etype = str(self.salary).partition("·")
        job = vera.add_manual_job(str(self.url).strip(), str(self.title_).strip(), company.strip(), location.strip(),
                                  str(self.desc), salary.strip(), etype.strip(), added_by=interaction.user.id)
        ev = session.evaluate(job)
        key = vera.job_key(job["id"])
        session.set_job_state(job["id"], "posted")
        await interaction.response.send_message(content=f"Added by {interaction.user.mention}, match for them:",
                                                embed=E.job_card(job, ev), view=job_buttons(key))


# --------------------------------------------------------------------------- preferences panel
class _PrefsModal(discord.ui.Modal):
    async def save(self, interaction: discord.Interaction, patch: Dict[str, Any], msg: str = "Saved"):
        prefs = me(interaction).update_prefs(patch)
        await interaction.response.send_message(f"✅ {msg} for {interaction.user.mention}. Used from the next search.",
                                                embed=E.prefs_card(prefs), ephemeral=EPH)


class PositionsModal(_PrefsModal, title="Preferred positions"):
    def __init__(self, p):
        super().__init__()
        self.positions = discord.ui.TextInput(label="Positions (comma or one per line)", style=discord.TextStyle.paragraph,
                                              default=", ".join(p["positions"]), required=False, max_length=2000)
        self.only = discord.ui.TextInput(label="Suggest ONLY these positions? (yes/no)",
                                         default="yes" if p.get("only_listed_positions") else "no")
        self.add_item(self.positions)
        self.add_item(self.only)

    async def on_submit(self, interaction):
        await self.save(interaction, {"positions": P.split_list(str(self.positions)),
                                      "only_listed_positions": yes(str(self.only))}, "Positions updated")


class LocationsModal(_PrefsModal, title="Locations"):
    def __init__(self, p):
        super().__init__()
        self.locs = discord.ui.TextInput(label="One per line: City, Province, Region, Country", style=discord.TextStyle.paragraph,
                                         default="\n".join(P.location_label(l) for l in p["locations"]), required=False)
        self.add_item(self.locs)

    async def on_submit(self, interaction):
        default = next((l.get("country") for l in me(interaction).prefs["locations"] if l.get("country")), "Philippines")
        await self.save(interaction, {"locations": P.fill_country(P.parse_locations(str(self.locs)), default)}, "Locations updated")


class SalaryModal(_PrefsModal, title="Salary expectations"):
    def __init__(self, p):
        super().__init__()
        s = p["salary"]
        self.min = discord.ui.TextInput(label="Minimum (blank = none)", default=str(s["min"] or ""), required=False)
        self.max = discord.ui.TextInput(label="Maximum (blank = none)", default=str(s["max"] or ""), required=False)
        self.unit = discord.ui.TextInput(label="Currency and period", default=f"{s['currency']} {s['period']}", placeholder="PHP monthly")
        self.neg = discord.ui.TextInput(label="Negotiable? (yes/no)", default="yes" if s["negotiable"] else "no")
        self.und = discord.ui.TextInput(label="Accept jobs that don't show salary? (yes/no)", default="yes" if s["accept_undisclosed"] else "no")
        for i in (self.min, self.max, self.unit, self.neg, self.und):
            self.add_item(i)

    async def on_submit(self, interaction):
        try:
            lo, hi = P.parse_money(str(self.min)), P.parse_money(str(self.max))
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        parts = str(self.unit).split()
        period = next((x.lower() for x in parts if x.lower() in P.SALARY_PERIODS), "monthly")
        currency = next((x.upper() for x in parts if x.lower() not in P.SALARY_PERIODS), "PHP")
        await self.save(interaction, {"salary": {"min": lo, "max": hi, "currency": currency, "period": period,
                                                 "negotiable": yes(str(self.neg), True), "accept_undisclosed": yes(str(self.und), True)}},
                        "Salary updated")


class ExclusionsModal(_PrefsModal, title="Exclusions"):
    def __init__(self, p):
        super().__init__()
        ex = p["exclude"]
        self.fields_ = {}
        for k, label in (("positions", "Excluded positions"), ("industries", "Excluded industries"), ("companies", "Excluded companies"),
                         ("locations", "Excluded locations"), ("keywords", "Excluded keywords (e.g. call center, BPO)")):
            t = discord.ui.TextInput(label=label, default=", ".join(ex[k]), required=False, style=discord.TextStyle.paragraph, max_length=1500)
            self.fields_[k] = t
            self.add_item(t)

    async def on_submit(self, interaction):
        await self.save(interaction, {"exclude": {k: P.split_list(str(t)) for k, t in self.fields_.items()}}, "Exclusions updated")


class RequirementsModal(_PrefsModal, title="Additional requirements"):
    def __init__(self, p):
        super().__init__()
        r = p["requirements"]
        self.degree = discord.ui.TextInput(label="Degree / education wanted", default=r["degree"], required=False)
        self.langs = discord.ui.TextInput(label="Required programming languages", default=", ".join(r["languages"]), required=False)
        self.skills = discord.ui.TextInput(label="Extra skills to match on", default=", ".join(r["skills"]), required=False)
        self.schedule = discord.ui.TextInput(label="Work schedule", default=r["schedule"], required=False, placeholder="Day shift, Mon-Fri")
        self.other = discord.ui.TextInput(label="Other conditions (comma-separated)", default=", ".join(r["other"]), required=False)
        for i in (self.degree, self.langs, self.skills, self.schedule, self.other):
            self.add_item(i)

    async def on_submit(self, interaction):
        await self.save(interaction, {"requirements": {"degree": str(self.degree).strip(), "languages": P.split_list(str(self.langs)),
                                                       "skills": P.split_list(str(self.skills)), "schedule": str(self.schedule).strip(),
                                                       "other": P.split_list(str(self.other))}}, "Requirements updated")


class SearchSettingsModal(_PrefsModal, title="Search settings"):
    def __init__(self, p):
        super().__init__()
        d = p["daily_digest"]
        self.min_match = discord.ui.TextInput(label="Minimum match % to recommend", default=str(p["min_match"]))
        self.per = discord.ui.TextInput(label="Results per /find-jobs (1-20)", default=str(p["results_per_search"]))
        self.digest = discord.ui.TextInput(label="Daily digest: hour, jobs (e.g. 8, 15; blank=off)",
                                           default=f"{d['hour']}, {p['digest_count']}" if d["enabled"] else "", required=False)
        self.age = discord.ui.TextInput(label="Only jobs posted within N days", default=str(p["max_age_days"]))
        self.follow = discord.ui.TextInput(label="Follow-up reminder after N days (0 = off)", default=str(p["followup_after_days"]))
        for i in (self.min_match, self.per, self.digest, self.age, self.follow):
            self.add_item(i)

    async def on_submit(self, interaction):
        try:
            parts = [x.strip() for x in str(self.digest).split(",") if x.strip()]
            hour = int(parts[0]) if parts else None
            count = int(parts[1]) if len(parts) > 1 else 15
            patch = {"min_match": max(0, min(100, int(str(self.min_match)))), "results_per_search": max(1, min(20, int(str(self.per)))),
                     "daily_digest": {"enabled": hour is not None, "hour": max(0, min(23, hour)) if hour is not None else 8},
                     "digest_count": max(1, min(20, count)), "max_age_days": max(1, int(str(self.age))),
                     "followup_after_days": max(0, int(str(self.follow)))}
        except ValueError:
            return await interaction.response.send_message("❌ Please enter numbers where asked.", ephemeral=True)
        await self.save(interaction, patch, "Search settings updated")


class LevelsLimitsModal(_PrefsModal, title="Custom levels & experience limits"):
    def __init__(self, p):
        super().__init__()
        self.custom = discord.ui.TextInput(label="Custom job levels (comma-separated)", default=", ".join(p["custom_levels"]), required=False)
        self.maxy = discord.ui.TextInput(label="Custom max years you'd accept (blank = auto)",
                                         default="" if p["custom_experience_max_years"] is None else str(p["custom_experience_max_years"]), required=False)
        self.reject = discord.ui.TextInput(label="Reject jobs that REQUIRE more than N years",
                                           default="" if p["reject_mandatory_experience_over"] is None else str(p["reject_mandatory_experience_over"]), required=False)
        for i in (self.custom, self.maxy, self.reject):
            self.add_item(i)

    async def on_submit(self, interaction):
        num = lambda t: float(t) if str(t).strip() else None
        try:
            patch = {"custom_levels": P.split_list(str(self.custom)), "custom_experience_max_years": num(str(self.maxy)),
                     "reject_mandatory_experience_over": num(str(self.reject))}
        except ValueError:
            return await interaction.response.send_message("❌ Years must be numbers.", ephemeral=True)
        await self.save(interaction, patch, "Levels & limits updated")


class MultiSelectView(OwnedView):
    """Multi-select menus that save straight to the owner's preferences."""

    def __init__(self, owner_id: int, selects: List[tuple], extra_buttons: List[discord.ui.Button] = ()):
        super().__init__(owner_id, timeout=600)
        for key, placeholder, options, current in selects:
            sel = discord.ui.Select(placeholder=placeholder, min_values=0, max_values=len(options),
                                    options=[discord.SelectOption(label=E.clip(lbl, 100), value=val, default=val in current)
                                             for val, lbl in options])
            sel.callback = self._make_cb(key)
            self.add_item(sel)
        for b in extra_buttons:
            self.add_item(b)

    def _make_cb(self, key):
        async def cb(interaction: discord.Interaction):
            me(interaction).update_prefs({key: list(interaction.data.get("values", []))})
            await interaction.response.send_message(f"✅ {key.replace('_', ' ').title()} saved for {interaction.user.display_name}.",
                                                    ephemeral=EPH)
        return cb


class PrefsPanel(OwnedView):
    def _p(self, interaction):
        return me(interaction).prefs

    @discord.ui.button(label="Levels & experience", emoji="🎯", row=0)
    async def levels(self, interaction, _):
        p = self._p(interaction)
        btn = discord.ui.Button(label="Custom levels & limits", emoji="✏️")
        btn.callback = lambda i: i.response.send_modal(LevelsLimitsModal(me(i).prefs))
        await interaction.response.send_message("Pick all that apply:", ephemeral=EPH, view=MultiSelectView(interaction.user.id, [
            ("job_levels", "Job levels", [(x, x) for x in P.JOB_LEVELS], p["job_levels"]),
            ("experience", "Experience requirements", [(x, x) for x in P.EXPERIENCE], p["experience"])], [btn]))

    @discord.ui.button(label="Positions", emoji="💼", row=0)
    async def positions(self, interaction, _):
        await interaction.response.send_modal(PositionsModal(self._p(interaction)))

    @discord.ui.button(label="Location & setup", emoji="📍", row=0)
    async def location(self, interaction, _):
        p = self._p(interaction)
        edit = discord.ui.Button(label="Edit locations", emoji="✏️")
        edit.callback = lambda i: i.response.send_modal(LocationsModal(me(i).prefs))
        reloc = discord.ui.Button(label=f"Willing to relocate: {'yes' if p['willing_to_relocate'] else 'no'}", emoji="🚚")

        async def toggle(i: discord.Interaction):
            cur = me(i).prefs["willing_to_relocate"]
            me(i).update_prefs({"willing_to_relocate": not cur})
            await i.response.send_message(f"🚚 Willing to relocate: {'yes' if not cur else 'no'}", ephemeral=EPH)
        reloc.callback = toggle
        await interaction.response.send_message("Work setup (pick all that apply), then edit your locations:", ephemeral=EPH,
                                                view=MultiSelectView(interaction.user.id,
                                                                     [("work_setup", "Work setup", [(x, x) for x in P.WORK_SETUPS], p["work_setup"])],
                                                                     [edit, reloc]))

    @discord.ui.button(label="Salary", emoji="💰", row=0)
    async def salary(self, interaction, _):
        await interaction.response.send_modal(SalaryModal(self._p(interaction)))

    @discord.ui.button(label="Employment type", emoji="🕒", row=0)
    async def etype(self, interaction, _):
        p = self._p(interaction)
        await interaction.response.send_message("Employment types:", ephemeral=EPH, view=MultiSelectView(interaction.user.id, [
            ("employment_types", "Employment types", [(x, x) for x in P.EMPLOYMENT_TYPES], p["employment_types"])]))

    @discord.ui.button(label="Exclusions", emoji="🚫", row=1)
    async def exclusions(self, interaction, _):
        await interaction.response.send_modal(ExclusionsModal(self._p(interaction)))

    @discord.ui.button(label="Requirements", emoji="🎓", row=1)
    async def reqs(self, interaction, _):
        await interaction.response.send_modal(RequirementsModal(self._p(interaction)))

    @discord.ui.button(label="Sources", emoji="🌐", row=1)
    async def sources(self, interaction, _):
        from vera.sources import SOURCES
        p = self._p(interaction)
        await interaction.response.send_message("Job sources to search (free public APIs):", ephemeral=EPH, view=MultiSelectView(
            interaction.user.id, [("sources", "Sources", [(k, LABELS[k]) for k in SOURCES], p["sources"])]))

    @discord.ui.button(label="Search settings", emoji="🔧", row=1)
    async def search(self, interaction, _):
        await interaction.response.send_modal(SearchSettingsModal(self._p(interaction)))

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction, _):
        await interaction.response.edit_message(embed=E.prefs_card(self._p(interaction)), view=self)

    @discord.ui.button(label="Cover letters on/off", emoji="✉️", row=2)
    async def cover(self, interaction, _):
        cur = self._p(interaction)["include_cover_letter"]
        me(interaction).update_prefs({"include_cover_letter": not cur})
        await interaction.response.send_message(f"✉️ Cover letters: {'on' if not cur else 'off'}", ephemeral=EPH)


# --------------------------------------------------------------------------- profile panel
class _ProfileModal(discord.ui.Modal):
    async def save(self, interaction, patch):
        profile = me(interaction).update_profile(patch)
        await interaction.response.send_message(f"✅ Profile updated for {interaction.user.mention}.",
                                                embed=E.profile_card(profile, private=EPH), ephemeral=EPH)


class BasicsModal(_ProfileModal, title="Basics"):
    def __init__(self, pr):
        super().__init__()
        self.f = {k: discord.ui.TextInput(label=lbl, default=pr.get(k) or "", required=False, max_length=200)
                  for k, lbl in (("name", "Full name"), ("email", "Email"), ("phone", "Phone"), ("location", "Location"),
                                 ("headline", "Headline (e.g. BSIT graduate, web developer)"))}
        for t in self.f.values():
            self.add_item(t)

    async def on_submit(self, interaction):
        await self.save(interaction, {k: str(v).strip() for k, v in self.f.items()})


class ExperienceModal(_ProfileModal, title="Summary & experience"):
    def __init__(self, pr):
        super().__init__()
        self.summary = discord.ui.TextInput(label="Summary", style=discord.TextStyle.paragraph, default=pr.get("summary") or "", required=False)
        self.years = discord.ui.TextInput(label="Total years of work experience", default=str(pr.get("years_experience") or 0))
        exp = "\n".join(f"{x.get('title', '')} | {x.get('company', '')} | {x.get('dates', '')} | {'; '.join(x.get('details') or [])}"
                        for x in pr.get("experience") or [])
        self.exp = discord.ui.TextInput(label="Experience: Title | Company | Dates | detail; detail", style=discord.TextStyle.paragraph,
                                        default=exp[:4000], required=False, max_length=4000)
        self.avail = discord.ui.TextInput(label="Availability (e.g. Immediately)", default=pr.get("availability") or "", required=False)
        for i in (self.summary, self.years, self.exp, self.avail):
            self.add_item(i)

    async def on_submit(self, interaction):
        try:
            years = float(str(self.years))
        except ValueError:
            return await interaction.response.send_message("❌ Years must be a number.", ephemeral=True)
        exp = []
        for line in str(self.exp).splitlines():
            parts = [p.strip() for p in line.split("|")]
            if parts and parts[0]:
                parts += [""] * (4 - len(parts))
                exp.append({"title": parts[0], "company": parts[1], "dates": parts[2], "details": P.split_list(parts[3].replace(";", ","))})
        await self.save(interaction, {"summary": str(self.summary).strip(), "years_experience": years, "experience": exp,
                                      "availability": str(self.avail).strip()})


class SkillsModal(_ProfileModal, title="Skills & more"):
    def __init__(self, pr):
        super().__init__()
        links = pr.get("links") or {}
        self.skills = discord.ui.TextInput(label="Skills (comma-separated)", style=discord.TextStyle.paragraph, default=", ".join(pr.get("skills") or []), required=False)
        self.langs = discord.ui.TextInput(label="Spoken languages", default=", ".join(pr.get("languages") or []), required=False)
        self.certs = discord.ui.TextInput(label="Certifications", default=", ".join(pr.get("certifications") or []), required=False)
        self.ach = discord.ui.TextInput(label="Achievements / honors", default=", ".join(pr.get("achievements") or []), required=False)
        self.links = discord.ui.TextInput(label="Links: github=..., linkedin=..., portfolio=...",
                                          default=", ".join(f"{k}={v}" for k, v in links.items() if v), required=False)
        for i in (self.skills, self.langs, self.certs, self.ach, self.links):
            self.add_item(i)

    async def on_submit(self, interaction):
        links = {}
        for part in P.split_list(str(self.links)):
            k, _, v = part.partition("=")
            if v:
                links[k.strip().lower()] = v.strip()
        await self.save(interaction, {"skills": P.split_list(str(self.skills)), "languages": P.split_list(str(self.langs)),
                                      "certifications": P.split_list(str(self.certs)), "achievements": P.split_list(str(self.ach)),
                                      "links": links})


class EducationModal(_ProfileModal, title="Education"):
    def __init__(self, pr):
        super().__init__()
        cur = "\n".join(" | ".join(x.get(k, "") for k in ("degree", "school", "year", "honors")) for x in pr.get("education") or [])
        self.edu = discord.ui.TextInput(label="One per line: Degree | School | Year | Honors", style=discord.TextStyle.paragraph,
                                        default=cur, required=False)
        self.add_item(self.edu)

    async def on_submit(self, interaction):
        edu = []
        for line in str(self.edu).splitlines():
            parts = [p.strip() for p in line.split("|")] + ["", "", ""]
            if parts[0]:
                edu.append({"degree": parts[0], "school": parts[1], "year": parts[2], "honors": parts[3]})
        await self.save(interaction, {"education": edu})


class ProfilePanel(OwnedView):
    def _pr(self, i):
        return me(i).profile

    @discord.ui.button(label="Basics", emoji="👤")
    async def basics(self, i, _):
        await i.response.send_modal(BasicsModal(self._pr(i)))

    @discord.ui.button(label="Summary & experience", emoji="💼")
    async def exp(self, i, _):
        await i.response.send_modal(ExperienceModal(self._pr(i)))

    @discord.ui.button(label="Skills & more", emoji="🛠️")
    async def skills(self, i, _):
        await i.response.send_modal(SkillsModal(self._pr(i)))

    @discord.ui.button(label="Education", emoji="🎓")
    async def edu(self, i, _):
        await i.response.send_modal(EducationModal(self._pr(i)))


class ConfirmReset(OwnedView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id, timeout=120)

    @discord.ui.button(label="Yes, reset my preferences", style=discord.ButtonStyle.danger)
    async def yes_(self, interaction, _):
        prefs = me(interaction).reset_prefs()
        await interaction.response.edit_message(content="♻️ Preferences reset to defaults.", embed=E.prefs_card(prefs), view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no_(self, interaction, _):
        await interaction.response.edit_message(content="Nothing changed.", view=None)


class OpenSelect(OwnedView):
    """Pick an item from a list (saved jobs / applications) to open it."""

    def __init__(self, owner_id: int, items: List[tuple], on_pick):
        super().__init__(owner_id, timeout=600)
        sel = discord.ui.Select(placeholder="Open…", options=[discord.SelectOption(label=E.clip(lbl, 100), value=val, description=E.clip(desc, 100) or None)
                                                              for val, lbl, desc in items[:25]])

        async def cb(interaction: discord.Interaction):
            await on_pick(interaction, interaction.data["values"][0])
        sel.callback = cb
        self.add_item(sel)
