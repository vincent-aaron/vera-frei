"""7 Nov 2026: the notes channel, no doubled replies, a looser go-ahead, cold email that looks less like spam,
and job mail that reads Spam and spots employers writing from their own address."""

import asyncio
import email.message
import sys
import time
import types
from datetime import datetime
from types import SimpleNamespace

import pytest

from vera import config
from vera.agents import assistant, jobmail, outreach
from vera.bot.core import VeraCore
from vera.hermes import identity, runtime, send_guard, tools
from tests.test_hermes import FORMS, OWNER, GUEST, call, on_discord, rt, vera  # noqa: F401  (fixtures)


# ----------------------------------------------------------------- #vera-notes
@pytest.fixture
def notes(rt):
    ch = SimpleNamespace(name=config.NOTES_CHANNEL, sent=[])

    async def send(text=None, **kw):
        ch.sent.append(text)
    ch.send = send

    async def notes_channel():
        return ch
    rt.notes_channel = notes_channel
    return ch


def test_a_note_goes_to_the_notes_channel_not_to_what_she_learned(vera, rt, notes, monkeypatch):
    on_discord(monkeypatch, OWNER, FORMS)
    identity.record(FORMS, OWNER, "add to notes: a voice channel for vera", when=time.time())
    out = call(tools.note, add="Add a voice channel function")
    assert out["saved"] and out["note"]["n"] == 1 and out["posted_in_notes_channel"]
    assert "Note #1" in notes.sent[0] and "voice channel" in notes.sent[0] and "#vera-company-site-application" in notes.sent[0]
    assert vera.db.get_json(tools.NOTES_KEY) is None                  # not filed as a lesson
    assert [x["note"] for x in call(tools.note)["open_notes"]] == ["Add a voice channel function"]
    done = call(tools.note, done=1)
    assert done["done"] == 1 and "is done" in notes.sent[-1] and call(tools.note)["open_notes"] == []
    assert "no note #7" in call(tools.note, done=7)["error"]


def test_only_the_owner_keeps_notes(vera, rt, notes, monkeypatch):
    on_discord(monkeypatch, GUEST, FORMS)
    assert "Only the owner" in call(tools.note, add="free stuff")["error"] and not notes.sent


def test_a_note_is_saved_even_if_the_channel_is_missing(vera, rt, monkeypatch):
    async def none():
        return None
    rt.notes_channel = none
    on_discord(monkeypatch, OWNER, FORMS)
    identity.record(FORMS, OWNER, "add to notes: read facebook job posts", when=time.time())
    out = call(tools.note, add="Read Facebook job posts")
    assert out["saved"] and not out["posted_in_notes_channel"] and "couldn't post" in out["tell_them"]


def test_the_notes_tool_is_offered_and_explains_when_not_to_use_it():
    schema = next(s for _fn, s in tools.TOOLS if s["name"] == "vera_note")
    assert "NEW CODE" in schema["description"] and "vera_learn" in schema["description"]
    learn = next(s for _fn, s in tools.TOOLS if s["name"] == "vera_learn")
    assert "vera_note" in learn["description"]


def test_the_notes_channel_is_found_by_name(vera):
    core = VeraCore.__new__(VeraCore)
    ch = SimpleNamespace(name=config.NOTES_CHANNEL)
    core.vera, core._channels = vera, {}
    type(core).guilds = property(lambda self: [SimpleNamespace(text_channels=[SimpleNamespace(name="general"), ch])])
    try:
        assert asyncio.run(core.notes_channel()) is ch
    finally:
        del type(core).guilds


def test_the_6_nov_notes_move_out_of_what_she_learned_once(vera):
    vera.db.set_json(tools.NOTES_KEY, [{"n": 1, "lesson": "Add a voice channel", "asked": "include on notes a voice channel"},
                                       {"n": 2, "lesson": "The owner likes short replies", "asked": "keep it short"}])
    text = tools.move_first_notes(vera.db)
    assert "voice channel" in text and "✅" in text and "🛠️" in text
    assert [x["lesson"] for x in vera.db.get_json(tools.NOTES_KEY)] == ["The owner likes short replies"]
    assert len(vera.db.get_json(tools.FUTURE_KEY)) == len(tools.FIRST_NOTES)
    assert tools.move_first_notes(vera.db) is None and len(vera.db.get_json(tools.FUTURE_KEY)) == len(tools.FIRST_NOTES)


# ----------------------------------------------------------------- the go-ahead
@pytest.mark.parametrize("text", ["proceed", "got it proceed", "Okay proceed", "okay then, proceed", "go ahead with the plan", "you can proceed now", "yes do it"])
def test_a_go_ahead_with_filler_words_counts(text):
    assert assistant.plain_yes(text)


@pytest.mark.parametrize("text", ["proceed but only remote", "proceed with 5", "yes, what about 5?", "Yes do it also apply to factset", "dont proceed", "got it"])
def test_a_change_or_a_question_still_waits(text):
    assert not assistant.plain_yes(text)


# ----------------------------------------------------------------- no doubled replies
@pytest.fixture
def fake_hermes(monkeypatch):
    """Stand-ins for Hermes's adapter base class and discord.py's Messageable, so the guard can be tested without either."""
    sent = []

    class Messageable:
        async def send(self, content=None, **kw):
            sent.append(kw.get("nonce"))
            return "msg"

    class Base:
        async def _send_with_retry(self, chat_id, content, attempts=2):
            for _ in range(attempts):              # every attempt "reaches Discord", like the 6 Nov drops
                await Messageable().send(content=content)
            return "ok"
    discord_mod, abc_mod = types.ModuleType("discord"), types.ModuleType("discord.abc")
    abc_mod.Messageable, discord_mod.abc = Messageable, abc_mod
    gw, plat, base = types.ModuleType("gateway"), types.ModuleType("gateway.platforms"), types.ModuleType("gateway.platforms.base")
    base.BasePlatformAdapter, plat.base, gw.platforms = Base, base, plat
    for name, mod in {"discord": discord_mod, "discord.abc": abc_mod, "gateway": gw, "gateway.platforms": plat, "gateway.platforms.base": base}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return SimpleNamespace(Base=Base, Messageable=Messageable, sent=sent)


def test_a_retried_send_reuses_its_nonce_so_discord_posts_it_once(fake_hermes):
    assert send_guard.install() and send_guard.install()            # patching twice is harmless
    asyncio.run(fake_hermes.Base()._send_with_retry("1", "Here's the plan"))
    asyncio.run(fake_hermes.Base()._send_with_retry("1", "Here's the plan"))      # a separate send of the same text
    first, retry, second, _ = fake_hermes.sent
    assert first and first == retry and len(first) <= 25 and second != first


def test_sends_outside_hermess_retry_get_no_nonce(fake_hermes):
    send_guard.install()
    asyncio.run(fake_hermes.Messageable().send(content="✅ Sent: a job"))
    assert fake_hermes.sent == [None]


def test_the_guard_steps_aside_if_hermes_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", None)
    assert send_guard.install() is False


# ----------------------------------------------------------------- cold email
def test_no_dear_gmail_team():
    assert outreach.company_of("maria.santos@gmail.com") == "" and outreach.company_of("hr@acme-labs.ph") == "Acme Labs"
    assert outreach.from_pasted_text("Hiring a Web Developer! Send CV to jobs.ph@gmail.com")[0]["company"] == ""


def test_the_gap_between_emails_varies(monkeypatch):
    gaps = {round(outreach.send_gap()) for _ in range(30)}
    assert len(gaps) > 5 and min(gaps) >= outreach.SEND_GAP_SECONDS and max(gaps) <= outreach.SEND_GAP_SECONDS * 2.6
    monkeypatch.setattr(outreach, "SEND_GAP_SECONDS", 0)
    assert outreach.send_gap() == 0


def test_emails_carry_a_date_and_message_id(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OUTREACH_EMAIL", "me@gmail.com")
    session = SimpleNamespace(uid=OWNER, profile={"name": "Juan", "resumes": []})
    monkeypatch.setattr(outreach, "resume_for_email", lambda _s: None)
    m = outreach.build_message(session, {"email": "hr@acme.ph", "subject": "Web Developer - Juan", "body": "Hello"})
    assert m["Date"] and m["Message-ID"].endswith("@gmail.com>")


# ----------------------------------------------------------------- job mail
from tests.test_jobmail import applied, me, msg  # noqa: E402,F401  (fixtures and helpers)


def test_an_employer_writing_from_their_own_gmail_counts():
    body = "Hi Juan, we came across your resume on JobStreet and would like to invite you to an initial interview on Monday."
    assert jobmail.is_relevant("maria.recruits@gmail.com", "Hello from Maria", body, set(), None)
    assert not jobmail.is_relevant("maria.recruits@gmail.com", "Hello from Maria", body, set(), None, bulk=True)    # a mailing list
    assert not jobmail.is_relevant("promo@shop.ph", "Big sale", "Book a call with our team", set(), None, bulk=True)


def test_spam_needs_clear_signs_of_a_real_employer():
    assert not jobmail.is_relevant("deals@x.com", "Your application for a loan", "Apply now", set(), None, spam=True)
    assert jobmail.is_relevant("hr@acme.ph", "Interview", "We would like to invite you to an interview.", set(), None, spam=True)
    assert jobmail.is_relevant("hr@acme.ph", "Hi", "Hello", set(), {"company": "Acme"}, spam=True)
    assert not jobmail.is_relevant("hr@acme.ph", "Hi", "Hello", set(), {"company": "Acme"}, bulk=True, spam=True)


def test_mail_naming_the_company_is_matched_but_he_decides_the_status(vera, me):
    app_id = applied(vera, company="Ascendion Philippines Inc.")
    found = jobmail.track(me, [{**msg("sarah.t.hr@gmail.com", "Your application",
                                      "Hi, this is Sarah from Ascendion Philippines. We would like to invite you to an interview.", "Sarah"),
                                "folder": jobmail.INBOX, "bulk": False}])
    assert found and found[0]["company"] == "Ascendion Philippines Inc." and "tell me" in found[0]["note"]
    assert vera.db.application(app_id)["status"] == "submitted"


def test_a_reply_found_in_spam_says_so_and_links_to_it(vera, me):
    applied(vera, company="Acme Corp")
    found = jobmail.track(me, [{**msg("maria@acme.ph", "Interview for Web Developer", "Are you available for a call on Monday?", "Maria"),
                                "folder": jobmail.SPAM, "bulk": False, "message_id": "<abc@acme.ph>"}])
    assert found and "Spam" in found[0]["note"] and "in%3Aanywhere" in found[0]["gmail"]
    assert jobmail.logged(OWNER)[-1]["folder"] == jobmail.SPAM


def test_unknown_senders_get_a_scam_warning(vera, me):
    found = jobmail.track(me, [{**msg("talent@newco.io", "Interview invitation", "We would like to invite you to an interview.", "NewCo"),
                                "folder": jobmail.INBOX, "bulk": False}])
    assert found and "doesn't match any job you applied to" in found[0]["note"]


class FakeIMAP:
    calls = []

    def __init__(self, *_a, **_k):
        FakeIMAP.calls = []

    def login(self, *_a):
        pass

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"']

    def select(self, box, readonly=False):
        FakeIMAP.calls.append(("select", box, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        FakeIMAP.calls.append((command, *args))
        if command == "search":
            return "OK", [b"7"]
        m = email.message.EmailMessage()
        m["From"] = "Shop <news@shop.ph>"
        m["Subject"] = "Sale"
        m["List-Unsubscribe"] = "<mailto:u@shop.ph>"
        m.set_content("Deals")
        return "OK", [(b"x", m.as_bytes())]

    def logout(self):
        pass


def test_spam_is_read_from_gmails_spam_folder_read_only(monkeypatch):
    monkeypatch.setattr(jobmail.imaplib, "IMAP4_SSL", FakeIMAP)
    messages, last = jobmail.fetch_new(0, None, jobmail.SPAM)
    assert ("select", '"[Gmail]/Spam"', True) in FakeIMAP.calls and last == 7
    assert messages[0]["folder"] == jobmail.SPAM and messages[0]["bulk"] is True
    assert {c[0] for c in FakeIMAP.calls} <= {"select", "search", "fetch"}


def test_a_hidden_spam_folder_is_skipped_quietly(monkeypatch):
    class Hidden(FakeIMAP):
        def select(self, box, readonly=False):
            return "NO", [b"unknown mailbox"]
    monkeypatch.setattr(jobmail.imaplib, "IMAP4_SSL", Hidden)
    assert jobmail.fetch_new(3, None, jobmail.SPAM) == ([], 3)


# ----------------------------------------------------------------- public summary vs. his private channel
JOBSTREET = {**msg("noreply@e.jobstreet.com", "Application update for Software Developer at Contoso Outdoors",
                   "Hi Juan Dela Cruz, it appears your application for Software Developer advertised by Contoso Outdoors is unlikely to progress "
                   "further %%strtoreplaceopentracking%% jobstreet [https://url.jobstreet.com/ss/c/u001.3Q7llEJ8xPfGy0]", "Jobstreet Applications"),
             "kind": "rejected", "label": "😔 Not moving forward", "company": "Contoso Outdoors", "title": "Software Developer",
             "url": "https://ph.jobstreet.com/job/1", "gmail": "https://mail.google.com/mail/u/0/#search/rfc822msgid%3Ax", "note": ""}


def test_the_public_job_mail_post_has_a_summary_and_the_job_link_but_no_gmail_link():
    text = jobmail.render(JOBSTREET, private=False)
    assert "In short:** They're not moving forward" in text and "https://ph.jobstreet.com/job/1" in text
    assert "mail.google.com" not in text and "noreply@" not in text and "unlikely to progress" not in text


def test_your_private_post_has_the_gmail_link_but_not_the_email_text():
    text = jobmail.render(JOBSTREET, private=True)
    # 9 Nov 2026: "I don't need the Gmail message, don't include it."
    assert "mail.google.com" in text and "unlikely to progress further" not in text and "noreply@e.jobstreet.com" in text
    assert "%%" not in text and "url.jobstreet.com" not in text


def test_a_reply_goes_to_job_mail_for_everyone_and_to_your_email_channel_in_full(vera):
    from tests.test_private_channels import Sink, bot, channel
    from vera.bot.main import VeraBot
    public = channel(everyone_can_view=True)
    public.name, public.sent = config.JOB_MAIL_CHANNEL, []
    email_ch = Sink("vera-owner-email")
    b = bot(vera, email=email_ch)
    b.guilds = [SimpleNamespace(text_channels=[public])]
    targets = asyncio.run(VeraBot.mail_targets(b))
    assert [(t, p) for t, p in targets] == [(public, False), (email_ch, True)]


def test_the_public_8pm_summary_leaves_out_gmail_links(vera, me):
    jobmail.log_mail(OWNER, {"time": "2026-11-06T18:48:00", "from": "hr@acme.ph", "subject": "Interview", "kind": "interview",
                             "company": "Acme", "title": "Web Dev", "url": "https://x/1", "gmail": "https://mail.google.com/x"})
    assert "mail.google.com" not in jobmail.digest(me, "2026-11-06", gmail=False) and "https://x/1" in jobmail.digest(me, "2026-11-06", gmail=False)
    assert "mail.google.com" in jobmail.digest(me, "2026-11-06")


def test_each_reply_says_whose_it_is_without_pinging(vera, me):
    found = jobmail.track(me, [{**msg("maria@acme.ph", "Interview for Web Developer", "We would like to invite you to an interview.", "Maria"),
                                "folder": jobmail.INBOX, "bulk": False}])
    text = jobmail.render(found[0], private=False)
    assert "**For:** Juan Cruz" in text and "<@" not in text
    assert "Replies from employers for Juan Cruz" in jobmail.digest(me)


# ----------------------------------------------------------------- social media posts, never logged in
from vera.agents import web  # noqa: E402

FB_POST = ('<html><head><meta property="og:title" content="Acme PH is hiring!"/>'
           '<meta property="og:description" content="We&#039;re hiring a Junior Web Developer in Makati. Send your CV to hr@acmeph.com"/></head></html>')


def test_a_public_posts_preview_is_read():
    assert "Junior Web Developer" in web.preview_text(FB_POST) and "Acme PH is hiring" in web.preview_text(FB_POST)


def test_a_login_wall_is_not_taken_for_the_post():
    wall = '<meta property="og:description" content="Log in or sign up to view. See posts, photos and more on Facebook."/>'
    assert web.preview_text(wall) == ""
    assert web.site_name("https://m.facebook.com/groups/1/posts/2") == "Facebook" and web.site_name("https://www.linkedin.com/posts/x") == "LinkedIn"


def test_reading_a_post_falls_back_to_the_search_summary(monkeypatch):
    async def no_page(_client, _url):
        return "<html>Log in</html>"

    async def search(query, limit=10):
        return [{"url": "https://www.facebook.com/acme/posts/pfbid0abc123xyz", "title": "Acme PH",
                 "snippet": "Hiring: QA Tester. Email jobs@acmeph.com"}]
    monkeypatch.setattr(web, "fetch_html", no_page)
    monkeypatch.setattr(web, "search", search)
    post = asyncio.run(web.social_post("https://www.facebook.com/acme/posts/pfbid0abc123xyz"))
    assert post["how"] == "search" and "QA Tester" in post["text"]


def test_a_private_post_asks_them_to_paste_it(vera, rt, monkeypatch):
    async def nothing(url):
        return {"url": url, "site": "Facebook", "text": "", "how": ""}
    monkeypatch.setattr(web, "social_post", nothing)
    on_discord(monkeypatch, GUEST, FORMS)
    out = call(tools.social, url="https://facebook.com/groups/1/posts/2")
    assert out["read"] is False and "Paste its text" in out["tell_them"]


def test_a_read_post_gives_its_emails_and_role(vera, rt, monkeypatch):
    async def post(url):
        return {"url": url, "site": "Facebook", "text": web.preview_text(FB_POST), "how": "preview"}
    monkeypatch.setattr(web, "social_post", post)
    on_discord(monkeypatch, GUEST, FORMS)
    out = call(tools.social, url="https://facebook.com/acme/posts/1")
    assert out["read"] and out["emails"] == ["hr@acmeph.com"] and "Web Developer" in out["role"]


def test_social_search_keeps_only_hiring_posts(monkeypatch):
    async def search(query, limit=10):
        return [{"url": "https://www.linkedin.com/posts/a", "title": "We're hiring a Web Developer", "snippet": "Send your CV"},
                {"url": "https://www.linkedin.com/posts/b", "title": "My trip to Baguio", "snippet": "Lovely weather"}]

    async def no_wait(_s):
        return None
    monkeypatch.setattr(web, "search", search)
    monkeypatch.setattr(web.asyncio, "sleep", no_wait)
    found = asyncio.run(web.social_search(["Web Developer"], ["linkedin"]))
    assert [x["url"] for x in found] == ["https://www.linkedin.com/posts/a"] and found[0]["site"] == "LinkedIn"


def test_vera_never_pretends_to_be_a_browser():
    import inspect
    assert "Chrome" not in inspect.getsource(web) and "facebookexternalhit" not in inspect.getsource(web)


# ----------------------------------------------------------------- the 8 PM summary after a night offline
class Target:
    def __init__(self):
        self.sent = []

    async def send(self, text=None, **kw):
        self.sent.append(text)


def digest_bot(vera, monkeypatch):
    b = SimpleNamespace(vera=vera, target=Target(), checked=[])

    async def check_mail(**_k):
        b.checked.append(1)

    async def mail_targets():
        return [(b.target, True)]
    b.check_mail, b.mail_targets = check_mail, mail_targets
    monkeypatch.setattr(outreach, "can_send", lambda: True)
    return b


def test_a_missed_evening_summary_is_posted_the_next_morning(vera, me, monkeypatch):
    b = digest_bot(vera, monkeypatch)
    vera.db.set_json("mail_digest_last", "2026-11-05")
    assert asyncio.run(VeraCore.daily_mail_digest(b, datetime(2026, 11, 7, 9, 0)))
    assert "Fri 6 Nov" in b.target.sent[0] and "Posted late" in b.target.sent[0]
    assert vera.db.get_json("mail_digest_last") == "2026-11-06"
    assert not asyncio.run(VeraCore.daily_mail_digest(b, datetime(2026, 11, 7, 10, 0)))      # once
    assert asyncio.run(VeraCore.daily_mail_digest(b, datetime(2026, 11, 7, 20, 5)))          # tonight's as usual
    assert "Posted late" not in b.target.sent[-1]


def test_no_catch_up_when_yesterdays_was_posted(vera, me, monkeypatch):
    b = digest_bot(vera, monkeypatch)
    vera.db.set_json("mail_digest_last", "2026-11-06")
    assert not asyncio.run(VeraCore.daily_mail_digest(b, datetime(2026, 11, 7, 9, 0)))


# ----------------------------------------------------------------- company-forms runs
def test_a_company_forms_run_lines_up_only_company_forms(vera, me, monkeypatch):
    from vera.agents import rounds, scout
    jobs = [({"id": i, "url": f"https://x/{i}", "title": "Web Developer", "company": "Acme", "location": "Remote", "source": src}, {})
            for i, src in enumerate(["companyforms", "remotive", "companyforms", "himalayas"])]

    async def refresh(*_a, **_k):
        return None
    monkeypatch.setattr(scout, "refresh_sources", refresh)
    monkeypatch.setattr(scout, "rank", lambda *_a, **_k: jobs)
    monkeypatch.setattr("vera.apply.filters.rejects", lambda *_a, **_k: False)
    only = asyncio.run(rounds.board_urls(me, only_sources=("companyforms",)))
    every = asyncio.run(rounds.board_urls(me))
    assert only == ["https://x/0", "https://x/2"] and len(every) == 4


def test_jobs_left_out_are_explained_in_the_channel():
    import inspect
    from vera.bot import core
    assert "lined up, I left out" in inspect.getsource(core.VeraCore)


# ----------------------------------------------------------------- loops after the laptop sleeps
def test_job_mail_is_checked_hourly():
    from vera.hermes import discord_runtime
    assert dict(discord_runtime.LOOPS)["check_mail"] == 3600


def test_a_loop_runs_soon_after_the_clock_jumps(monkeypatch):
    """Sleeping the laptop moves the wall clock ahead; the loop notices within one step instead of a whole interval."""
    from vera.hermes import discord_runtime
    clock = {"now": 1000.0}
    runs, sleeps = [], []
    monkeypatch.setattr(discord_runtime.time, "time", lambda: clock["now"])

    async def fake_sleep(seconds):
        if len(runs) >= 2:
            raise asyncio.CancelledError
        sleeps.append(seconds)
        clock["now"] += 10_000           # the laptop slept through the whole hour during this short wait
    monkeypatch.setattr(discord_runtime.asyncio, "sleep", fake_sleep)
    rv = SimpleNamespace(ready=True)

    async def tick():
        runs.append(clock["now"])
    rv.tick = tick
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(discord_runtime.HermesVera._every(rv, "tick", 3600))
    assert len(runs) == 2 and sleeps == [discord_runtime.STEP]       # it ran again after one short step, not after another hour


def test_a_reply_that_couldnt_be_posted_goes_out_at_the_next_check(vera, me, monkeypatch):
    from tests.test_private_channels import MAIL, Sink, bot, inbox
    from vera.bot.main import VeraBot
    inbox(monkeypatch, MAIL)
    email = Sink("vera-owner-email")
    sends = {"n": 0}
    real_send = email.send

    async def flaky(*a, **k):
        sends["n"] += 1
        if sends["n"] == 1:
            raise RuntimeError("Session is closed")          # Vera restarting mid-post
        return await real_send(*a, **k)
    email.send = flaky
    b = bot(vera, email=email)
    asyncio.run(VeraBot.check_mail(b))
    assert not email.sent and vera.db.get_json(f"mail_unposted:{config.OWNER_ID}")
    monkeypatch.setattr(jobmail, "fetch_new", lambda _l, _d=None, folder=jobmail.INBOX: ([], 99))     # nothing new arrives
    asyncio.run(VeraBot.check_mail(b))
    assert len(email.sent) == 1 and "maria@acme.ph" in email.sent[0][0] and vera.db.get_json(f"mail_unposted:{config.OWNER_ID}") is None


def test_a_retry_never_posts_twice_in_a_channel_that_already_got_it(vera, me, monkeypatch):
    from tests.test_private_channels import MAIL, Sink, bot, channel, inbox
    from vera.bot.main import VeraBot
    inbox(monkeypatch, MAIL)
    public = channel(everyone_can_view=True)
    public.name, public.sent, public.id = config.JOB_MAIL_CHANNEL, [], 5

    async def ok(text=None, **kw):
        public.sent.append(text)
    public.send = ok
    email = Sink("vera-owner-email")
    email.id = 6
    real, calls = email.send, {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Session is closed")
        return await real(*a, **k)
    email.send = flaky
    b = bot(vera, email=email)
    b.guilds = [SimpleNamespace(text_channels=[public])]
    asyncio.run(VeraBot.check_mail(b))                    # public got it, private failed
    monkeypatch.setattr(jobmail, "fetch_new", lambda _l, _d=None, folder=jobmail.INBOX: ([], 99))
    asyncio.run(VeraBot.check_mail(b))                    # the retry goes to the private channel only
    assert len(public.sent) == 1 and len(email.sent) == 1
