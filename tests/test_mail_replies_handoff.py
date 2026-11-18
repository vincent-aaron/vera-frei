"""9 Nov 2026: replies Vera missed, answering an employer, and carrying a conversation to another channel.

What went wrong that day, in #vera-job-mail:
- Example Software answered his own email ("Re: Software Developer - Juan Dela Cruz") and it was thrown away.
- Sample Staffing sent a pre-screening test, filed as an automatic "application received".
- Northwind wrote through Workable ("Job title - Northwind"), and it wasn't matched to his application.
- vera_applications(statuses=["interview"]) said 0 while two applications were marked interview.
- vera_job_mail returned "checking" and nothing else, so the AI said there were no replies.
- Asked to send a decline, the cold email tool drafted a job application to the recruiter.
- "Let's continue in #vera-owner-email" arrived there with nothing.
"""

import json
import sqlite3
import time

import pytest

from vera import config
from vera.agents import exports, jobmail, outreach
from vera.hermes import identity, tools
from tests.test_hermes import FakeRuntime, NAMES, OWNER, GUEST, call, on_discord, ready
from tests.test_hermes import vera  # noqa: F401  (fixture)

JOB_MAIL, OWNER_EMAIL, PUBLIC = 2001, 2002, 2003


@pytest.fixture
def rt(vera, monkeypatch):
    from vera.hermes import runtime
    from tests.test_hermes import Channel
    monkeypatch.setitem(NAMES, JOB_MAIL, config.JOB_MAIL_CHANNEL)
    monkeypatch.setitem(NAMES, OWNER_EMAIL, config.OWNER_CHANNELS["email"])
    monkeypatch.setitem(NAMES, PUBLIC, "vera-multiboards-application")
    fake = FakeRuntime(vera)
    for cid in (JOB_MAIL, OWNER_EMAIL, PUBLIC):
        fake.channels[cid] = Channel(cid)
    fake.guilds = []
    monkeypatch.setattr(runtime, "_live", fake)
    monkeypatch.setattr(tools, "_private_here", lambda asker: asker.chat_id == OWNER_EMAIL or asker.platform == "local")
    return fake


def msg(sender, subject, body, display="", **extra):
    return {"uid": 1, "from": sender, "display": display, "subject": subject, "date": "", "body": body, "message_id": f"<{abs(hash(subject))}@x>", **extra}


def me(vera, tmp_path):
    s = ready(vera.user(OWNER), tmp_path)
    s.update_profile({"name": "Juan Dela Cruz"})
    return s


def app(vera, company, title, url):
    job = vera.add_manual_job(url, title, company)
    app_id = vera.db.create_application(job, OWNER)
    vera.db.update_application(app_id, status="submitted")
    return app_id


# ----------------------------------------------------------------- the three emails that were missed
EXAMPLE_SW = ("recruiter@example-software.ph", "Re: Software Developer - Juan Dela Cruz",
            "Hi Juan Good morning! Thank you for your interest in Example Software, Inc. and for taking the time to submit your application. "
            "At the moment, we do not have an open position that matches your qualifications. However, we would be happy to keep your application on file.")
PRESCREEN = ("recruit@samplestaffingbpo.com", "Pre-screening test for AV Design Engineer (Night Shift) | ZR_0000_JOB",
             "Dear Dela Cruz, Thank you for your interest in the AV Design Engineer (Night Shift) | ZR_0000_JOB position. To move forward with our hiring "
             "process, we request you complete a brief pre-screening test. You can start by clicking the link below.")
NORTHWIND = ("noreply@candidates.workablemail.com", "AI Automation & DevOps Developer (12-Month Contract) - Northwind",
               "Hi Juan Dela Cruz, First up, thank you so much for looking at our opportunity and for making the time and effort to apply. "
               "We are excited to have opened this role and to be starting to talk to people about it.")


def test_a_person_answering_his_own_email_counts_even_from_an_unknown_address(vera, tmp_path):
    item = jobmail.track(me(vera, tmp_path), [msg(*EXAMPLE_SW, display="Abaya")])
    assert len(item) == 1 and item[0]["kind"] == "rejected" and jobmail.is_response(item[0])
    assert jobmail.logged(OWNER)[-1]["counts"] == "1"


def test_a_pre_screening_test_is_an_assessment_not_a_receipt(vera, tmp_path):
    app(vera, "Sample Staffing BPO Inc.", "AV System Design Engineer (Night Shift) | ZR_0000_JOB", "https://ph.jobstreet.com/job/95304003")
    item = jobmail.track(me(vera, tmp_path), [msg(*PRESCREEN)])
    assert item[0]["kind"] == "assessment" and jobmail.is_response(item[0]) and item[0]["company"] == "Sample Staffing BPO Inc."


def test_an_employer_message_through_a_company_form_is_matched_by_its_subject(vera, tmp_path):
    app(vera, "Northwind Information Technology, Inc.", "AI Automation & DevOps Developer (12-month Contract)", "https://ph.jobstreet.com/job/95371879")
    item = jobmail.track(me(vera, tmp_path), [msg(*NORTHWIND)])
    assert item and item[0]["company"] == "Northwind Information Technology, Inc." and jobmail.is_response(item[0])


def test_job_board_nags_and_short_names_still_dont_match(vera, tmp_path):
    app(vera, "AI Co", "Developer", "https://x/1")
    s = me(vera, tmp_path)
    nag = msg("noreply@e.jobstreet.com", "Sample Connect Philippines, Inc. is still accepting applications - don’t forget to submit your application", "x", bulk=True)
    out = jobmail.track(s, [nag])
    assert not out or not jobmail.is_response(out[0])
    # A "Re:" from a robot isn't a person answering him.
    robot = msg("noreply@e.jobstreet.com", "Re: Software Developer - Juan Dela Cruz", "x", bulk=True)
    assert not [i for i in jobmail.track(s, [robot]) if jobmail.is_response(i)]


def test_a_conditional_keep_on_file_is_still_a_confirmation():
    kind, _ = jobmail.classify("Thank you for your application", "If you are not selected, we will keep your resume on file.", "noreply@ats.example")
    assert kind == "received"


def test_the_recheck_posts_what_was_filed_wrongly_before(vera, tmp_path):
    s = me(vera, tmp_path)
    jobmail.log_mail(OWNER, {"time": "2026-11-07T11:57:00", "from": PRESCREEN[0], "subject": PRESCREEN[1], "kind": "received"})
    again = jobmail.track(s, [msg(*PRESCREEN)], recheck=True)
    assert again and again[0]["kind"] == "assessment"


# ----------------------------------------------------------------- what the AI is told
def test_the_list_says_who_and_what_but_never_the_email_text(vera, tmp_path):
    jobmail.track(me(vera, tmp_path), [msg(*EXAMPLE_SW, display="Abaya"), msg(*PRESCREEN)])
    rows = jobmail.brief(jobmail.responses(vera.user(OWNER), 14))
    assert {r["kind"] for r in rows} == {"rejected", "assessment"}
    assert "open position" not in json.dumps(rows) and "pre-screening test" not in json.dumps(rows).replace("Pre-screening test", "")


def test_job_mail_waits_for_the_check_and_returns_the_real_list(vera, rt, monkeypatch, tmp_path):
    s = me(vera, tmp_path)
    on_discord(monkeypatch, OWNER, JOB_MAIL)

    async def fake_check(report_to=None, recheck_days=None):
        return jobmail.track(s, [msg(*EXAMPLE_SW, display="Abaya")])
    rt.check_mail = fake_check
    out = call(tools.job_mail, check=True)
    assert out["checked_now"] and out["new_since_last_check"][0]["kind"] == "rejected"
    assert out["employer_responses_last_14_days"][0]["company"]


def test_interviews_are_found_whatever_case_the_ai_uses(vera, tmp_path):
    rows = {"sent": [{"date": "2026-11-05", "title": "Web Dev", "company": "V", "board": "Company form", "status": "Interview", "setup": "Remote"},
                     {"date": "2026-11-05", "title": "QA", "company": "W", "board": "Remote board", "status": "Sent", "setup": "On-site"}], "unfinished": []}
    assert len(exports.narrow(rows, {"statuses": ["interview"]})["sent"]) == 1
    assert len(exports.narrow(rows, {"statuses": ["Interview"]})["sent"]) == 1
    assert len(exports.narrow(rows, {"boards": ["company_form"]})["sent"]) == 1
    assert len(exports.narrow(rows, {"boards": ["remote_board"]})["sent"]) == 1
    assert len(exports.narrow(rows, {"statuses": ["submitted"]})["sent"]) == 1


# ----------------------------------------------------------------- answering an employer
RELAYED_INTERVIEW = ("noreply@jobstreet.com", "Example Ventures (PH) Inc. has responded to your application for Web Developer (Next.js)",
            "Maria Santos from Example Ventures (PH) Inc. has sent you a message. Please confirm your availability for a video interview.")
DECLINE = ("Hello Maria,\n\nThank you for the invitation to interview for the Web Developer (Next.js) position. I won't be able to attend this week.\n\n"
           "Best regards,\nJuan Dela Cruz")


def test_a_reply_goes_to_the_employers_reply_to_with_his_exact_text(vera, rt, monkeypatch, tmp_path):
    app(vera, "Example Ventures (PH) Inc.", "Web Developer (Next.js)", "https://ph.jobstreet.com/job/10000001")
    jobmail.track(me(vera, tmp_path), [msg(*RELAYED_INTERVIEW, display="Maria Santos at Example Ventures (PH) Inc. via Jobstreet",
                                           reply_to="maria@exampleventures.ph")])
    on_discord(monkeypatch, OWNER, JOB_MAIL)
    out = call(tools.reply_email, which="example ventures", body=DECLINE)
    assert out["drafts_posted"] == 1 and "Maria" in out["sender_name"]
    item = vera.db.get_json(f"outreach_pending:{OWNER}")["items"][0]
    assert item["email"] == "maria@exampleventures.ph" and item["body"] == DECLINE and item["reply"] and item["subject"].startswith("Re: Example Ventures")
    posted = rt.channels[JOB_MAIL].sent[-1]
    assert DECLINE.splitlines()[2] in posted and "no résumé attached" in posted and "résumé attached, " not in posted
    m = outreach.build_message(vera.user(OWNER), item)
    assert m["In-Reply-To"] == item["in_reply_to"] and not list(m.iter_attachments())


def test_a_reply_isnt_blocked_by_the_cold_email_cooldown(vera, monkeypatch, tmp_path):
    import asyncio
    s = me(vera, tmp_path)
    outreach.log_send(OWNER, {"time": "2026-11-09T09:00:00", "email": "maria@exampleventures.ph", "status": "sent"})
    sent = []
    monkeypatch.setattr(outreach, "_send_now", lambda m: sent.append(m))
    res = asyncio.run(outreach.send_batch(s, [{"email": "maria@exampleventures.ph", "subject": "Re: x", "body": DECLINE, "reply": True}]))
    assert len(res["sent"]) == 1 and sent
    outreach.block(OWNER, "maria@exampleventures.ph")
    res = asyncio.run(outreach.send_batch(s, [{"email": "maria@exampleventures.ph", "subject": "Re: x", "body": DECLINE, "reply": True}]))
    assert not res["sent"] and "do-not-contact" in res["skipped"][0][1]


def test_a_relay_with_no_reply_address_asks_him_instead_of_guessing(vera, rt, monkeypatch, tmp_path):
    jobmail.track(me(vera, tmp_path), [msg(*RELAYED_INTERVIEW)])
    monkeypatch.setattr(jobmail, "headers_of", lambda mid, folder="INBOX": {})
    on_discord(monkeypatch, OWNER, JOB_MAIL)
    out = call(tools.reply_email, which="example ventures", body=DECLINE)
    assert "no-reply" in out["error"] and vera.db.get_json(f"outreach_pending:{OWNER}") is None
    monkeypatch.setattr(jobmail, "headers_of", lambda mid, folder="INBOX": {"reply_to": "maria@exampleventures.ph", "display": "Maria"})
    assert call(tools.reply_email, which="example ventures", body=DECLINE)["drafts_posted"] == 1


def test_replies_and_email_work_in_his_private_email_channel_but_not_elsewhere(vera, rt, monkeypatch, tmp_path):
    jobmail.track(me(vera, tmp_path), [msg(*EXAMPLE_SW, display="Abaya")])
    on_discord(monkeypatch, OWNER, OWNER_EMAIL)
    assert call(tools.reply_email, which="1", body=DECLINE)["drafts_posted"] == 1
    on_discord(monkeypatch, OWNER, PUBLIC)
    assert "Email only happens in" in call(tools.reply_email, which="1", body=DECLINE)["error"]
    on_discord(monkeypatch, GUEST, JOB_MAIL)
    assert "Only the owner" in call(tools.reply_email, which="1", body=DECLINE)["error"]


# ----------------------------------------------------------------- moving to another channel
def fake_state_db(path, rows):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE sessions (id TEXT, source TEXT, user_id TEXT, display_name TEXT, started_at REAL)")
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
        for sid, uid, name in (("s1", str(OWNER), "Frei Automate / #vera-job-mail"), ("s2", str(GUEST), "Frei Automate / #vera-job-mail"),
                               ("s3", str(OWNER), "Frei Automate / #vera-owner-email")):
            c.execute("INSERT INTO sessions VALUES (?, 'discord', ?, ?, 1)", (sid, uid, name))
        for sid, role, text in rows:
            c.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)", (sid, role, text, time.time()))


def test_she_reads_back_his_own_conversation_from_another_channel(vera, rt, monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    fake_state_db(db, [("s1", "user", "decline the Example Ventures interview"), ("s1", "assistant", "Here's the draft"), ("s1", "tool", '{"secret": 1}'),
                       ("s2", "user", "a member's message")])
    monkeypatch.setattr(tools, "STATE_DB", db)
    on_discord(monkeypatch, OWNER, OWNER_EMAIL)
    out = call(tools.other_channel, channel="vera-job-mail")
    said = [m["said"] for m in out["messages"]]
    assert said == ["decline the Example Ventures interview", "Here's the draft"]       # no tool output, no member's messages


def test_a_private_conversation_isnt_read_into_a_public_channel(vera, rt, monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    fake_state_db(db, [("s3", "user", "my private stuff")])
    monkeypatch.setattr(tools, "STATE_DB", db)
    on_discord(monkeypatch, OWNER, JOB_MAIL)
    assert "private" in call(tools.other_channel, channel="vera-ownr-email")["error"]      # his typo still finds it
    on_discord(monkeypatch, OWNER, OWNER_EMAIL)
    assert "private" in call(tools.handoff, to="vera-remote", summary="x")["error"]


def test_a_handoff_shows_up_in_the_other_channels_live_facts(vera, rt, monkeypatch, tmp_path):
    from vera.hermes import hooks
    db = tmp_path / "state.db"
    fake_state_db(db, [("s1", "user", "decline the Example Ventures interview")])
    monkeypatch.setattr(tools, "STATE_DB", db)
    me(vera, tmp_path)
    on_discord(monkeypatch, OWNER, JOB_MAIL)
    out = call(tools.handoff, to="vera-ownr-email", summary="Declining Example Ventures's interview; reply drafted, not sent.")
    assert out["handed_over"] and out["to"] == "#vera-owner-email"
    on_discord(monkeypatch, OWNER, OWNER_EMAIL)
    facts = hooks.facts(identity.current())
    assert "Declining Example Ventures" in facts and "decline the Example Ventures interview" in facts
    # Members never get it, and it's not shown in a channel it wasn't sent to.
    on_discord(monkeypatch, OWNER, PUBLIC)
    assert "Declining Example Ventures" not in hooks.facts(identity.current())


# ----------------------------------------------------------------- no applying from the email channels
def test_email_channels_never_plan_or_start_job_applications(vera, rt, monkeypatch, tmp_path):
    me(vera, tmp_path)
    for chat in (JOB_MAIL, OWNER_EMAIL):
        on_discord(monkeypatch, OWNER, chat)
        out = call(tools.plan_applications, count=50)
        assert "vera_draft_emails" in out["error"] and vera.db.get_json(f"apply_request:{OWNER}:{chat}") is None
        vera.db.set_json(f"apply_request:{OWNER}:{chat}", {"count": 50, "sites": None, "shown_at": 0})   # one made before the fix
        identity.record(chat, OWNER, "proceed", when=time.time() + 1)
        assert "error" in call(tools.start_applications, mode="real") and not rt.launched
    on_discord(monkeypatch, OWNER, PUBLIC)
    assert "plan" in call(tools.plan_applications, count=5)
