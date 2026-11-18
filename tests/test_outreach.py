"""Cold email: no network. Sending is faked and every email is captured."""

import asyncio
import smtplib

import pytest

from vera import config
from vera.agents import assistant, rounds, outreach
from vera.db import DB
from vera.services import Vera

OWNER, GUEST = 111, 222


@pytest.fixture
def vera(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESUME_DIR", tmp_path / "resumes")
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    monkeypatch.setattr(config, "OUTREACH_EMAIL", "me@gmail.com")
    monkeypatch.setattr(config, "OUTREACH_APP_PASSWORD", "x")
    monkeypatch.setattr(config, "OUTREACH_MAX_PER_DAY", 3)
    monkeypatch.setattr(outreach, "SEND_GAP_SECONDS", 0)
    for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.setattr(config, key, "")
    return Vera(DB(tmp_path / "t.db"))


@pytest.fixture
def me(vera, tmp_path, monkeypatch):
    async def no_ai(*_a, **_k):
        return None
    monkeypatch.setattr("vera.apply.llm.complete", no_ai)
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF")
    s = vera.user(OWNER)
    s.update_profile({"name": "Juan Cruz", "email": "j@example.com", "phone": "0900", "skills": ["PHP", "Laravel"],
                      "experience": [{"title": "Automation Developer", "company": "Acme", "details": []}],
                      "resumes": [{"name": "CV", "path": str(cv), "tags": []}]})
    return s


def say(s, text):
    return asyncio.run(assistant.handle(s, text))


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(outreach, "_send_now", lambda m: out.append(m))
    return out


def test_only_real_hiring_addresses():
    text = "Apply: careers@acme.ph or noreply@acme.ph, jane.doe@acme.ph, support@acme.ph, logo@2x.png, x@example.com, y@jobstreet.com"
    assert outreach.emails_in(text) == ["careers@acme.ph", "jane.doe@acme.ph"]


def test_nothing_sends_without_a_yes(me, sent):
    r = say(me, "email hr@acme.ph about openings")
    assert r["action"] == "none" and "Nothing has been sent" in r["reply"] and "hr@acme.ph" in r["reply"]
    assert not sent
    r = say(me, "yes")
    assert r["action"] == "send_outreach" and r["request"][0]["email"] == "hr@acme.ph"
    assert say(me, "yes")["action"] != "send_outreach"       # a second yes can't resend


def test_email_is_honest_and_has_a_way_out(me):
    item = asyncio.run(outreach.draft(me, {"email": "hr@acme.ph", "company": "Acme", "title": "Web Developer", "url": ""}))
    assert "Juan Cruz" in item["body"] and "won't email you again" in item["body"] and "Web Developer - Juan Cruz" == item["subject"]
    m = outreach.build_message(me, item)
    assert m["To"] == "hr@acme.ph" and "me@gmail.com" in m["From"]
    assert [a.get_filename() for a in m.iter_attachments()] == ["cv.pdf"]


def test_made_up_claims_are_removed(me, monkeypatch):
    async def liar(*_a, **_k):
        return ("I build web applications with Laravel and PHP for clients every day, and I care about reliable software. "
                "I am an expert in Kubernetes. I have 9 years in PHP. My résumé is attached, and I would welcome a short conversation about your team.")
    monkeypatch.setattr("vera.ai.complete", liar)
    body = asyncio.run(outreach.draft(me, {"email": "hr@a.ph", "company": "A", "title": ""}))["body"]
    assert "Kubernetes" not in body and "9 years" not in body


def test_same_company_isnt_emailed_twice(me, sent):
    item = asyncio.run(outreach.draft(me, {"email": "hr@acme.ph", "company": "Acme"}))
    assert len(asyncio.run(outreach.send_batch(me, [item]))["sent"]) == 1
    res = asyncio.run(outreach.send_batch(me, [{**item, "email": "jobs@acme.ph"}]))
    assert not res["sent"] and "already emailed" in res["skipped"][0][1] and len(sent) == 1


def test_do_not_contact_is_permanent(me, sent):
    assert "won't email" in say(me, "don't email hr@acme.ph")["reply"]
    assert "do-not-contact" in say(me, "email hr@acme.ph")["reply"]
    item = asyncio.run(outreach.draft(me, {"email": "hr@acme.ph", "company": "Acme"}))
    assert "do-not-contact" in asyncio.run(outreach.send_batch(me, [item]))["skipped"][0][1] and not sent


def test_daily_limit_holds(me, sent):
    items = [asyncio.run(outreach.draft(me, {"email": f"hr@c{i}.ph", "company": f"C{i}"})) for i in range(5)]
    res = asyncio.run(outreach.send_batch(me, items))
    assert len(res["sent"]) == 3 and len(res["skipped"]) == 2 and "daily limit" in res["skipped"][0][1]


def test_bad_password_stops_the_batch(me, monkeypatch):
    def bad(_m):
        raise smtplib.SMTPAuthenticationError(535, b"no")
    monkeypatch.setattr(outreach, "_send_now", bad)
    items = [asyncio.run(outreach.draft(me, {"email": f"hr@d{i}.ph", "company": "D"})) for i in range(2)]
    res = asyncio.run(outreach.send_batch(me, items))
    assert len(res["failed"]) == 1 and "app password" in res["failed"][0][1]


def test_no_password_means_setup_help_not_sending(me, monkeypatch):
    monkeypatch.setattr(config, "OUTREACH_APP_PASSWORD", "")
    assert "apppasswords" in say(me, "email hr@acme.ph")["reply"]
    r = say(me, "yes")
    assert r["action"] == "none" and "apppasswords" in r["reply"]


def test_skip_and_cancel(me):
    say(me, "email a@one.ph and b@two.ph")
    assert "a@one.ph" not in say(me, "skip 1")["reply"]
    assert "cancelled" in say(me, "no")["reply"]


def test_owner_only(vera):
    assert "own email" in say(vera.user(GUEST), "cold email 5 companies")["reply"]


def test_leads_come_from_postings_only(me, vera):
    from vera.sources.base import Source
    vera.db.upsert_job(Source.job("remotive", 1, title="Web Developer", company="Acme", description="Send CV to careers@acme.ph", url="https://x/1", location="Remote", remote=1, employment_type="", level="", tags=[], posted_at="2026-11-01"))
    leads = outreach.find_leads(me)
    assert [(l["email"], l["company"]) for l in leads] == [("careers@acme.ph", "Acme")]


def test_channels_set_the_default_board(me):
    r = asyncio.run(assistant.handle(me, "apply to 3 jobs", ["company_forms"]))
    assert "**Where:** company forms (Greenhouse, Lever, Ashby, Workable)\n" in r["reply"]


def test_answers_by_number_are_saved(me, vera):
    vera.db.set_json(f"open_questions:{OWNER}", ["How many years of Software Support experience do you have?"])
    r = say(me, "1: 0 years")
    assert "Saved" in r["reply"]
    assert "0 years" in (rounds.user_dir(OWNER) / "answers.json").read_text(encoding="utf-8")
