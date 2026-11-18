"""The owner's private channels, and job mail that only reports real responses."""

import asyncio
from types import SimpleNamespace

import discord
import pytest

from vera import config
from vera.agents import rounds, jobmail
from vera.bot import guide, private
from vera.bot.main import VeraBot
from vera.db import DB
from vera.services import Vera

OWNER, GUEST, BOT = 111, 222, 999


@pytest.fixture
def vera(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESUME_DIR", tmp_path / "resumes")
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    monkeypatch.setattr(config, "OUTREACH_EMAIL", "me@gmail.com")
    monkeypatch.setattr(config, "OUTREACH_APP_PASSWORD", "x")
    return Vera(DB(tmp_path / "t.db"))


def msg(sender="hr@acme.ph", subject="Update", body="", display="Acme HR", uid=1):
    return {"uid": uid, "from": sender, "display": display, "subject": subject, "date": "", "body": body}


# ----------------------------------------------------------------- which mail counts as a response
@pytest.mark.parametrize("subject,body", [
    ("Your application was successfully submitted", ""),
    ("Thanks for applying to Acme", "Our team will review it. If you are shortlisted, we will contact you for an interview."),
    ("Application received", "Next steps may include an assessment and an interview."),
    ("Thank you for your application", "If you are not selected, we will keep your resume on file."),
    ("Your application to Acme has been sent", ""),
])
def test_confirmations_are_never_called_interviews_or_rejections(subject, body):
    kind = jobmail.classify(subject, body, "noreply@ats.example")[0]
    assert kind == "received", kind
    assert not jobmail.is_response(msg("noreply@ats.example", subject, body) | {"kind": kind})


@pytest.mark.parametrize("subject,body,kind", [
    ("Thanks for applying", "We'd like to invite you to an interview next week.", "interview"),
    ("Thanks for applying", "Please complete this coding test by Friday.", "assessment"),
    ("Thank you for your interest", "Unfortunately we went with other candidates.", "rejected"),
    ("Your application", "We're pleased to offer you the role.", "offer"),
])
def test_a_real_next_step_still_gets_through_a_polite_opening(subject, body, kind):
    found = jobmail.classify(subject, body, "hr@acme.ph")[0]
    assert found == kind
    assert jobmail.is_response(msg("hr@acme.ph", subject, body) | {"kind": found})


@pytest.mark.parametrize("sender,subject,shown", [
    ("noreply@e.jobstreet.com", "Sample Finance has viewed your application", False),
    ("noreply@e.jobstreet.com", "The owner, new activity in jobs you applied for", False),
    ("noreply@e.jobstreet.com", "ACME: Your Application for Associate Software Engineer", False),
    ("noreply@e.jobstreet.com", "ACME sent you a message about your application", True),
    ("maria@acme.ph", "Re: Web Developer", True),
])
def test_only_people_responding_are_shown(sender, subject, shown):
    kind = jobmail.classify(subject, "", sender)[0]
    assert jobmail.is_response(msg(sender, subject) | {"kind": kind}) is shown


def test_a_confirmation_cant_change_your_application(vera):
    job = vera.add_manual_job("https://x/1", "Web Dev", "Acme Corp")
    app_id = vera.db.create_application(job, OWNER)
    vera.db.update_application(app_id, status="submitted")
    jobmail.track(vera.user(OWNER), [msg(subject="Thanks for applying", body="If shortlisted we'll contact you for an interview.")])
    assert vera.db.application(app_id)["status"] == "submitted"


def test_the_summary_counts_responses_and_says_what_it_left_out(vera):
    me = vera.user(OWNER)
    jobmail.track(me, [msg("noreply@e.jobstreet.com", "Your application was successfully submitted"), msg(subject="Interview", body="Are you available for a call?", uid=2)])
    text = jobmail.summary(me)
    assert "1 interview" in text and "1 automatic notice" in text and "received" not in text


# ----------------------------------------------------------------- is a channel really private?
class Target:
    def __init__(self, id_):
        self.id = id_


def channel(everyone_can_view=False, overwrites=None):
    everyone = Target(1)
    guild = SimpleNamespace(default_role=everyone)
    return SimpleNamespace(guild=guild, overwrites=overwrites or {}, category=SimpleNamespace(name=config.OWNER_CATEGORY),
                           permissions_for=lambda _role: SimpleNamespace(view_channel=everyone_can_view))


def allow():
    return discord.PermissionOverwrite(view_channel=True)


def test_private_means_only_you_and_vera():
    assert private.is_private(channel(overwrites={Target(OWNER): allow(), Target(BOT): allow()}), OWNER, BOT)
    assert not private.is_private(channel(everyone_can_view=True), OWNER, BOT)
    assert not private.is_private(channel(overwrites={Target(GUEST): allow()}), OWNER, BOT)     # someone was added


def test_only_your_private_channels_and_dms_count_as_private_places():
    assert private.is_owner_place(SimpleNamespace(guild=None), OWNER, BOT)
    assert private.is_owner_place(channel(), OWNER, BOT)
    assert not private.is_owner_place(channel(everyone_can_view=True), OWNER, BOT)
    elsewhere = channel()
    elsewhere.category = SimpleNamespace(name="vera")
    assert not private.is_owner_place(elsewhere, OWNER, BOT)


# ----------------------------------------------------------------- creating them
class FakeGuild:
    def __init__(self, existing=()):
        self.default_role = Target(1)
        self.existing, self.created = list(existing), []

    async def fetch_member(self, uid):
        if uid == GUEST:
            raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "no")
        return Target(uid)

    async def fetch_channels(self):
        return self.existing

    async def create_category(self, name, overwrites):
        guild = self

        async def create_text_channel(child, overwrites, topic):
            ch = SimpleNamespace(type=discord.ChannelType.text, name=child, category_id=50, rules=overwrites, topic=topic)
            guild.existing.append(ch)
            guild.created.append(child)
            return ch
        cat = SimpleNamespace(type=discord.ChannelType.category, name=name, id=50, rules=overwrites, create_text_channel=create_text_channel)
        self.existing.append(cat)
        return cat


def test_channels_are_created_private_and_never_twice():
    guild = FakeGuild()
    made = asyncio.run(private.ensure_owner_channels(guild, OWNER, BOT))
    assert set(made) == set(config.OWNER_CHANNELS) and sorted(guild.created) == sorted(config.OWNER_CHANNELS.values())
    for ch in made.values():
        rules = ch.rules
        assert rules[guild.default_role].view_channel is False
        assert {t.id for t, r in rules.items() if r.view_channel} == {OWNER, BOT}
    asyncio.run(private.ensure_owner_channels(guild, OWNER, BOT))
    assert len(guild.created) == len(config.OWNER_CHANNELS)


def test_nothing_is_created_if_you_arent_in_that_server():
    guild = FakeGuild()
    assert asyncio.run(private.ensure_owner_channels(guild, GUEST, BOT)) == {}
    assert not guild.existing


def test_the_names_are_what_you_asked_for():
    assert config.OWNER_CATEGORY == "vera-owner"
    assert config.OWNER_CHANNELS["email"] == "vera-owner-email" and config.OWNER_CHANNELS["chat"] == "vera-owner"


# ----------------------------------------------------------------- where things go
class Sink:
    def __init__(self, name="sink"):
        self.name, self.sent, self.mention = name, [], f"#{name}"
        self.guild = SimpleNamespace(default_role=Target(1))

    async def send(self, text=None, **kw):
        self.sent.append((text, kw))


def bot(vera, email=None, resume=None, reports=None):
    channels = {"email": email, "resume": resume, "reports": reports}

    async def owner_channel(which):
        return channels.get(which)

    dm = Sink("dm")

    async def fetch_user(_uid):
        return dm
    b = SimpleNamespace(vera=vera, owner_channel=owner_channel, fetch_user=fetch_user, dm=dm, user=SimpleNamespace(id=BOT),
                        resume_files=lambda _uid: ([{"name": "CV"}], ["file"]), site_channel=lambda _s: None,
                        tell_user=lambda *_a: None, guilds=[])
    b.job_mail_channel = lambda: VeraBot.job_mail_channel(b)
    b.mail_targets = lambda: VeraBot.mail_targets(b)
    return b


def inbox(monkeypatch, messages):
    monkeypatch.setattr(jobmail, "fetch_new", lambda _last, _days=None, folder=jobmail.INBOX: (messages if folder == jobmail.INBOX else [], 99))


MAIL = [msg("noreply@e.jobstreet.com", "Your application was successfully submitted", display="Jobstreet"),
        msg("noreply@e.jobstreet.com", "Acme has viewed your application", display="Jobstreet", uid=2),
        msg("maria@acme.ph", "Interview for Web Developer", "Are you available for a call on Monday?", "Maria", 3)]


def test_the_senders_address_is_readable_and_still_cant_ping(vera):
    item = {**msg("maria@acme.ph", "Hi", "Hello", "Maria @everyone"), "label": "💬 Reply", "company": "", "note": ""}
    text = jobmail.render(item, True)
    assert "`maria@acme.ph`" in text and "@everyone" not in text
    odd = jobmail.render({**item, "from": "@everyone <not an address>"}, True)
    assert "@everyone" not in odd


def test_only_responses_reach_your_email_channel(vera, monkeypatch):
    inbox(monkeypatch, MAIL)
    email = Sink("vera-owner-email")
    b = bot(vera, email=email)
    asyncio.run(VeraBot.check_mail(b))
    assert len(email.sent) == 1 and "maria@acme.ph" in email.sent[0][0] and not b.dm.sent


def test_mail_goes_to_a_dm_never_a_public_channel_if_the_private_one_is_gone(vera, monkeypatch):
    inbox(monkeypatch, MAIL)
    b = bot(vera, email=None)
    asyncio.run(VeraBot.check_mail(b))
    assert len(b.dm.sent) == 1


def test_any_replies_answers_where_you_asked_without_copying_the_mail_there(vera, monkeypatch):
    inbox(monkeypatch, MAIL)
    email, asked = Sink("vera-owner-email"), Sink("vera-outreach")
    asyncio.run(VeraBot.check_mail(bot(vera, email=email), report_to=asked))
    assert "#vera-owner-email" in asked.sent[0][0] and "maria@acme.ph" not in asked.sent[0][0]


def owner_user():
    return SimpleNamespace(id=OWNER, send=None)


def test_your_resume_goes_to_your_channel_when_asked_in_public(vera):
    resume = Sink("vera-owner-resume")
    public = channel(everyone_can_view=True)
    public.send = None     # posting it here would be a failure
    reply = asyncio.run(VeraBot.send_resume(bot(vera, resume=resume), owner_user(), public))
    assert resume.sent[0][1]["files"] == ["file"] and "#vera-owner-resume" in reply


def test_your_resume_stays_where_you_asked_if_thats_private(vera):
    here = Sink("vera-owner")
    here.category, here.overwrites = SimpleNamespace(name=config.OWNER_CATEGORY), {}
    here.permissions_for = lambda _r: SimpleNamespace(view_channel=False)
    b = bot(vera, resume=Sink("vera-owner-resume"))
    asyncio.run(VeraBot.send_resume(b, owner_user(), here))
    assert here.sent[0][1]["files"] == ["file"]


def test_members_still_get_their_resume_by_dm(vera):
    sent = []

    async def send(text, files):
        sent.append(files)
    member = SimpleNamespace(id=GUEST, send=send)
    resume = Sink("vera-owner-resume")
    reply = asyncio.run(VeraBot.send_resume(bot(vera, resume=resume), member, channel(everyone_can_view=True)))
    assert sent == [["file"]] and not resume.sent and "DM" in reply


def run_report(vera, monkeypatch, uid, reports, asked):
    async def run_once(*_a, **_k):
        return {"rows": []}
    monkeypatch.setattr(rounds, "run_once", run_once)
    monkeypatch.setattr(rounds, "report", lambda _r: "THE REPORT")
    told = []

    async def tell_user(_uid, text):
        told.append(text)
    b = bot(vera, reports=reports)
    b.tell_user = tell_user
    asyncio.run(VeraBot.run_rounds(b, uid, channel=asked))
    return told


def test_your_report_goes_to_your_reports_channel_with_a_note_where_you_asked(vera, monkeypatch):
    reports, asked = Sink("vera-owner-reports"), Sink("vera-company-site-application")
    run_report(vera, monkeypatch, OWNER, reports, asked)
    assert reports.sent[0][0] == "THE REPORT"
    assert "#vera-owner-reports" in asked.sent[0][0] and "THE REPORT" not in asked.sent[0][0]


def test_members_reports_never_reach_your_channel(vera, monkeypatch):
    reports = Sink("vera-owner-reports")
    told = run_report(vera, monkeypatch, GUEST, reports, None)
    assert told == ["THE REPORT"] and not reports.sent


def test_the_guide_doesnt_point_at_the_old_mail_channel():
    assert "vera-job-mail" not in "\n".join(guide.MESSAGES)


def test_replies_go_to_the_job_mail_channel_and_only_a_private_one_shows_the_sender(vera, monkeypatch):
    # Since 9 Nov 2026 the email's own text is never posted, private or not; a private channel shows the sender's address.
    inbox(monkeypatch, MAIL)
    for everyone_can_view, private in ((True, False), (False, True)):
        jm = channel(everyone_can_view=everyone_can_view)
        jm.name, jm.sent, jm.mention = config.JOB_MAIL_CHANNEL, [], "#vera-job-mail"

        async def send(text=None, _jm=jm, **kw):
            _jm.sent.append(text)
        jm.send = send
        b = bot(vera, email=Sink("vera-owner-email"))
        b.guilds = [SimpleNamespace(text_channels=[jm])]
        vera.db.set_json(f"mail_last_uid:{OWNER}", 0)
        monkeypatch.setattr(jobmail, "logged", lambda _uid: [])
        asyncio.run(VeraBot.check_mail(b))
        assert len(jm.sent) == 1 and "Are you available" not in jm.sent[0] and ("maria@acme.ph" in jm.sent[0]) == private
