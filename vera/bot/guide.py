"""The #vera-instructions channel: a read-only guide to using and testing Vera. Reposted only when this text changes."""

import hashlib
import logging

import discord

from vera import config

log = logging.getLogger("vera.guide")

MESSAGES = [
    """# 🤖 How to use Vera
Vera finds jobs that match you and can apply for you. Everyone here has their own résumé, filters and applications.

**Just talk to her.** Send her a DM, or mention **@Vera** in the server. You don't need commands. For example:
• *recommend 10 jobs for me* (she lists them and applies to nothing)
• *apply to 5 web developer jobs*
• *apply to 10 jobs from company forms*
• *only remote jobs, up to 1 year of experience*
• *cold email 5 companies* (she shows you the drafts first)
• *any replies?*
• *make a table of my applications this week*, or *a summary for the last 3 days*
• *make a sheet of my applications* (or a *word document*, or a *pdf*)
• *which résumé did you use?*
• *how's it going?* or *stop*

When you ask her to apply, she tells you exactly what she'll do and asks about anything missing. Nothing is sent until you say **proceed** or **yes**. Say **test** to fill forms without sending.

She runs on the owner's PC, so she only answers while it's on and she's running. **No green dot next to her name means she's off, so contact the owner.** This works the same in the Discord phone app.""",

    """## 📺 Channels
Ask in a source's channel, and she uses that source. She posts each application as it goes, then a full report at the end.
• `#vera-multiboards-application`: every source in one run, taking turns between them
• `#vera-remote`: remote-focused sources (Real Work From Anywhere, Jobspresso, Remotive, Jobicy, RemoteOK, We Work Remotely, Working Nomads, Himalayas, LaraJobs, Hacker News, company job lists)
• `#vera-company-site-application`: company forms (Greenhouse, Lever, Ashby, Workable)
• `#vera-social-outreach`: cold email, including jobs posted on social media
• `#vera-instructions`: this guide

Reports, résumés and email replies stay private: members get them by DM, and the owner has a private category for them.

You can always name a source instead, like *apply to 5 jobs from remote boards*.

Each channel runs on its own, up to 3 at once. A second run in the same channel waits in line. **Stop** ends only that channel's run, and **stop all** ends all of yours.""",

    """## 🧪 How to test the new features
Each step is safe. Nothing real is sent until step 6.

**1. Recommendations.** DM her: *recommend 5 jobs for me*. You should get a list with links, and nothing is applied to.
**2. Company forms.** In `#vera-company-site-application`: *@Vera apply to 2 jobs*. Check the plan she shows, then reply **test**. Each job should say "Filled, not sent", and a full report should arrive at the end.
**3. Remote boards.** In `#vera-remote`: *@Vera apply to 3 jobs*, then **test**. Many of these jobs go to company sites she can't fill, so skipped jobs are normal.
**4. Every source.** In `#vera-multiboards-application`: *@Vera apply to 5 jobs*, then **test**. Results should come from more than one source, unless a site shows a bot check.
**5. Stopping.** Start any test, then say *stop*. It should end within seconds and send a report.
**6. Email.** DM her *email* followed by your own address, then **yes**. You should receive it with your résumé attached. Then reply to that email.
**7. Job mail (owner only).** Say *any replies?*. Your reply should appear in the private email channel. Automatic "application submitted" notices are left out.
**8. Chat changes.** Say *only remote jobs*, then *recommend 3 jobs for me*. Change it back with *remote, hybrid and on-site jobs*.
**9. Going live.** Only when the tests look right: *apply to 2 jobs*, then **yes**. These are real applications.

If something looks wrong, copy her message and what you expected.""",

    """## 🔎 Commands: find jobs
`/find-jobs` Search for jobs that match your preferences, and post the best ones
`/preferences` Change job titles, level, experience, location, remote/hybrid/on-site, salary, job type and exclusions
`/reset-preferences` Go back to the default filters
`/add-job` Add a job you found, so Vera can score and track it
`/saved` See the jobs you saved

## 📄 Commands: your details
`/resume add` Upload your résumé (PDF or DOCX). Set **import_profile** to True to fill in your profile from it
`/resume list` · `/resume get` · `/resume remove` See, download or remove your résumés
`/profile` View and edit your skills, education and experience
`/apply-details` Details forms ask for: expected pay, when you can start, work rights, LinkedIn""",

    """## 🚀 Application rounds
Ask her to apply (*apply to 5 jobs*). She shows the plan: where, how many, which titles and which résumé. Nothing starts until you reply **proceed** (real) or **test** (fills forms, sends nothing). Nothing ever runs on a schedule.
`/rounds` Pick one for **action**:
• **status** Your limits and the last round
• **test** Fill forms but send nothing, then send a report. **Start here**

`/answer` Give your answer to a question she couldn't answer honestly. You can also reply to her report, like *1: 0 years*.

**First time setup:** `/resume add` with **import_profile** set to True, then `/apply-details`, then ask her to apply and say **test**.

**Before you use it:** she submits real applications in your name, so use only your own details. Your résumé and details are stored on the owner's PC, where the owner can see them. Your work history and skills go to free AI services that write your cover letters.

**Using it yourself (not the owner):** you use your own résumé and details, never the owner's. DM her your résumé file and tell her *my email is...*, or she'll ask. The owner's email can't be used for you. She applies through company forms that need no login (Greenhouse, Lever, Ashby, Workable), up to 5 applications per round and 10 per day. She can write a cold email for you to send yourself. Your reports come by DM. Runs happen one person at a time.""",

    """## 📧 Cold email (owner only)
Only in the outreach and job mail channels. Say *cold email 5 companies* and she searches job posts, Facebook posts and company sites herself. You can also say *search facebook for 5 companies hiring*, *email hr@company.com*, link a company site, or paste a post. She shows you the drafts, and sends after you say **proceed** or **yes**. ⚠️ marks personal addresses from public posts, so check those first, because fake job posts often look like this.

## 📬 Job mail (owner only)
The private email channel shows only real responses: interviews, assessments, rejections, offers and replies. Automatic confirmations are left out, because the run report covers those. Say *any replies?* to check now. She only reads mail, and never answers or deletes it.

## ✅ Apply yourself and track it
`/apply` Prepare an application for one job. Nothing is sent until you approve it
`/applications` See your history and update each one's status
`/statistics` See how many you've sent, and your response and interview rates

## 💬 Other
`/ask` Ask for job advice, interview tips, or changes to your filters in plain words
`/status` Check that Vera is healthy
`/help` Short list of what she can do""",
]


def guide_hash() -> str:
    return hashlib.sha256("\n".join(MESSAGES).encode("utf-8")).hexdigest()[:16]


async def ensure_guide(bot, guild: discord.Guild) -> None:
    """Create or rename the channel if needed, and post the guide if it's new or has changed."""
    store = bot.vera.db
    key = f"guide:{guild.id}"
    saved = store.get_json(key) or {}
    channel = guild.get_channel(saved.get("channel") or 0) or discord.utils.get(guild.text_channels, name=config.GUIDE_CHANNEL)
    if channel is None:
        if not guild.me.guild_permissions.manage_channels:
            log.warning("Can't create the guide channel: missing Manage Channels")
            return
        channel = await guild.create_text_channel(
            config.GUIDE_CHANNEL, topic="How to use and test Vera",
            overwrites={guild.default_role: discord.PermissionOverwrite(send_messages=False),
                        guild.me: discord.PermissionOverwrite(send_messages=True, view_channel=True)})
    if saved.get("hash") == guide_hash() and saved.get("channel") == channel.id:
        return
    for mid in saved.get("ids", []) if saved.get("channel") == channel.id else []:
        try:
            await channel.get_partial_message(mid).delete()
        except discord.HTTPException:
            pass
    ids = [(await channel.send(text)).id for text in MESSAGES]
    store.set_json(key, {"hash": guide_hash(), "channel": channel.id, "ids": ids})
