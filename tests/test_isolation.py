"""Other members must never be able to use the owner's Gmail, job-site accounts, résumé or details."""

import asyncio
import json

import pytest

from vera import config
from vera.agents import applier, assistant, rounds, jobmail, outreach
from vera.db import DB
from vera.services import Vera

OWNER, GUEST = 111, 222
OWNER_MAIL = "owner.private@gmail.com"


@pytest.fixture
def vera(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESUME_DIR", tmp_path / "resumes")
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    monkeypatch.setattr(config, "OUTREACH_EMAIL", OWNER_MAIL)
    monkeypatch.setattr(config, "OUTREACH_APP_PASSWORD", "secret")
    for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.setattr(config, key, "")
    f = Vera(DB(tmp_path / "t.db"))
    cv = tmp_path / "owner_cv.pdf"
    cv.write_bytes(b"%PDF")
    f.user(OWNER).update_profile({"name": "Owner Person", "email": OWNER_MAIL, "phone": "0999", "location": "Hagonoy, Bulacan, Philippines",
                                  "resumes": [{"name": "CV", "path": str(cv), "tags": []}],
                                  "application": {"expected_salary_monthly": 28000, "notice_period": "2 weeks", "work_authorized": True}})
    return f


def say(session, text, sites=None):
    return asyncio.run(assistant.handle(session, text, sites))


def test_a_new_member_starts_with_nothing_of_yours(vera):
    p = rounds.build_profile(vera.user(GUEST))
    assert not p["email"] and not p["phone"] and not p["resume_path"] and not p["first_name"]
    assert OWNER_MAIL not in json.dumps(p)


def test_a_member_cant_apply_with_nothing_of_their_own(vera, monkeypatch):
    async def must_not_run(*_a, **_k):
        raise AssertionError("the engine must not start")
    monkeypatch.setattr(rounds, "_run_engine", must_not_run)
    res = asyncio.run(rounds.run_once(vera, GUEST))
    assert "résumé" in res["error"] and "email" in res["error"]


def test_she_asks_members_for_their_own_details(vera):
    guest = vera.user(GUEST)
    r = say(guest, "apply to 3 jobs")
    assert "your own" in r["reply"] and "résumé" in r["reply"] and "email" in r["reply"]
    say(guest, "my email is guest@example.com and my phone number is 0917 000 1111")
    p = guest.profile
    assert p["email"] == "guest@example.com" and p["phone"] == "0917 000 1111"
    assert vera.user(OWNER).profile["email"] == OWNER_MAIL           # yours is untouched


def test_details_can_be_given_outside_a_request_too(vera):
    r = say(vera.user(GUEST), "my email is guest@example.com")
    assert "Saved" in r["reply"] and vera.user(GUEST).profile["email"] == "guest@example.com"


def test_members_get_company_forms_and_never_the_owners_auto_submit(vera, tmp_path):
    guest = vera.user(GUEST)
    p = rounds.write_profile(guest, 5)
    assert "allow_job_sites" not in p and "sites" not in p["search"]
    assert not applier.can_auto_submit("https://jobs.lever.co/acme/1", GUEST)
    assert "company forms" in say(guest, "apply to 3 jobs")["reply"]


def test_members_cant_reach_your_signed_in_browser(vera):
    owner, guest = rounds.user_dir(OWNER), rounds.user_dir(GUEST)
    assert owner != guest and guest.name == str(GUEST) and owner.name == "owner"
    assert owner not in guest.parents and guest not in owner.parents


def test_the_engine_only_knows_sites_that_need_no_login():
    from vera.apply import sites
    assert {s.name for s in sites.ALL_SITES} == {"company_form", "remote_board"}
    assert not hasattr(sites, "BY_NAME")                     # no account-based job boards at all


def test_members_cant_send_or_read_your_gmail(vera, monkeypatch):
    guest = vera.user(GUEST)
    monkeypatch.setattr(outreach, "_send_now", lambda _m: (_ for _ in ()).throw(AssertionError("must not send")))
    for text in ("cold email 5 companies", "search facebook for 5 companies hiring", "send emails to recruiters"):
        r = say(guest, text)
        assert r["action"] == "none" and "own email" in r["reply"]
    assert say(guest, "any replies?")["action"] == "none"
    assert say(guest, "check my email")["action"] == "none"
    with pytest.raises(PermissionError):
        asyncio.run(outreach.send_batch(guest, [{"email": "a@b.ph", "subject": "s", "body": "b"}]))


def test_a_yes_from_a_member_cant_send_your_drafts(vera):
    owner, guest = vera.user(OWNER), vera.user(GUEST)
    say(owner, "email hr@acme.ph about openings")
    assert say(guest, "yes")["action"] != "send_outreach"
    assert vera.db.get_json(f"outreach_pending:{OWNER}")             # your drafts are still waiting for you


def test_member_drafts_show_their_address_not_yours(vera):
    guest = vera.user(GUEST)
    guest.update_profile({"name": "Guest Person", "email": "guest@example.com"})
    r = say(guest, "write an email to hr@acme.ph")
    assert "guest@example.com" in r["reply"] and OWNER_MAIL not in r["reply"] and "haven't sent" in r["reply"]
    assert r["action"] == "none" and vera.db.get_json(f"outreach_pending:{GUEST}") is None


def test_owner_emails_use_your_sending_address(vera):
    assert OWNER_MAIL in outreach.footer(vera.user(OWNER))


def test_members_cant_stop_your_run_or_answer_for_you(vera, monkeypatch):
    monkeypatch.setattr(rounds, "_active", [{"uid": OWNER, "channel_id": None}])
    assert say(vera.user(GUEST), "stop")["action"] == "none"
    vera.db.set_json(f"open_questions:{OWNER}", ["Do you have a visa?"])
    say(vera.user(GUEST), "1: yes")
    assert not (rounds.user_dir(OWNER) / "answers.json").exists()


def test_a_members_mail_log_and_history_are_separate(vera):
    outreach.log_send(OWNER, {"time": "2026-11-05T10:00:00", "email": "hr@acme.ph", "status": "sent"})
    assert outreach.history(GUEST) == []
    assert jobmail.summary(vera.user(GUEST)) == "No replies tracked yet."


def test_nothing_about_you_reaches_a_members_chat_context(vera):
    text = assistant._context(vera.user(GUEST))
    assert OWNER_MAIL not in text and "Owner Person" not in text and "0999" not in text


def test_nobody_gets_your_accounts_if_the_owner_setting_is_missing(vera, monkeypatch):
    monkeypatch.setattr(config, "OWNER_ID", 0)
    assert not rounds.is_owner(OWNER) and not rounds.is_owner(0)
    assert not applier.can_auto_submit("https://jobs.lever.co/acme/1", OWNER)
    assert not applier.can_auto_submit("https://jobs.lever.co/acme/1")
    assert rounds.user_dir(OWNER).name != "owner"
    with pytest.raises(PermissionError):
        asyncio.run(outreach.send_batch(vera.user(OWNER), [{"email": "a@b.ph", "subject": "s", "body": "b"}]))


def test_the_form_engine_never_holds_your_passwords(monkeypatch, tmp_path):
    from vera.apply import llm
    for name in config.ENGINE_HIDDEN:
        monkeypatch.setenv(name, "secret-value")
    env = config.engine_env(tmp_path)
    assert not set(config.ENGINE_HIDDEN) & set(env) and env["VERA_APPLY_DATA"] == str(tmp_path)
    assert not set(config.ENGINE_HIDDEN) & set(llm._env())
    import pathlib
    for rel in ("vera/agents/rounds.py", "vera/agents/applier.py"):
        source = (pathlib.Path(config.ROOT) / rel).read_text(encoding="utf-8")
        assert "config.engine_env(" in source and "**os.environ" not in source


def test_each_person_runs_from_their_own_folder(vera):
    mine, theirs = config.engine_env(rounds.user_dir(OWNER)), config.engine_env(rounds.user_dir(GUEST))
    assert mine["VERA_APPLY_DATA"].endswith("owner") and theirs["VERA_APPLY_DATA"].endswith(str(GUEST))


def test_new_sources_need_no_account():
    from vera.sources import SOURCES
    assert {"companyforms", "hnhiring"} <= set(SOURCES)
