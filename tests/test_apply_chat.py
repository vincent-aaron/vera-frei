"""Applying through normal conversation: no network, no browser, AI off."""

import asyncio
from pathlib import Path

import pytest

from vera import config
from vera.agents import assistant, rounds
from vera.db import DB
from vera.services import Vera

OWNER, GUEST = 111, 222


@pytest.fixture
def vera(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESUME_DIR", tmp_path / "resumes")
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.setattr(config, key, "")
    return Vera(DB(tmp_path / "t.db"))


def ready(vera, uid, tmp_path):
    cv = tmp_path / f"cv{uid}.pdf"
    cv.write_bytes(b"%PDF")
    me = vera.user(uid)
    me.update_profile({"name": "Juan Cruz", "email": "j@example.com", "phone": "0900", "location": "Hagonoy, Bulacan, Philippines",
                       "resumes": [{"name": "CV", "path": str(cv), "tags": []}],
                       "application": {"expected_salary_monthly": 28000, "notice_period": "2 weeks", "work_authorized": True}})
    return me


def say(me, text):
    return asyncio.run(assistant.handle(me, text))


@pytest.mark.parametrize("text,count", [
    ("use company forms and apply for me to at least 5 jobs", 5), ("apply to 10 web developer jobs", 10),
    ("apply for me atleast 3", 3), ("apply for me", None),
])
def test_how_many_is_understood(text, count):
    assert assistant.parse_count(text) == count


def test_remote_channel_means_only_remote_boards(vera, tmp_path, monkeypatch):
    from vera.sources.base import Source
    me = ready(vera, OWNER, tmp_path)
    r = asyncio.run(assistant.handle(me, "apply to 3 jobs", config.CHANNEL_SITES["vera-remote"]))
    assert "remote boards" in r["reply"] and "company forms" not in r["reply"]
    assert asyncio.run(assistant.handle(me, "yes"))["request"]["sites"] == ["remote_boards"]

    me.update_prefs({"positions": ["Web Developer"], "min_match": 0, "max_age_days": 0, "locations": []})
    for src, n in (("remotive", 1), ("jobicy", 2), ("arbeitnow", 3)):
        vera.db.upsert_job(Source.job(src, n, title=f"Web Developer {src}", company=f"C{n}", url=f"https://{src}.example/{n}", location="Worldwide", remote=1,
                                      employment_type="Full-time", level="", description="Web developer, no experience required.", tags=[], posted_at="2026-11-04"))

    async def no_refresh(*_a, **_k):
        return {}
    monkeypatch.setattr(rounds.scout, "refresh_sources", no_refresh)
    urls = asyncio.run(rounds.board_urls(me, remote_only=True))
    assert {u.split("/")[2] for u in urls} == {"remotive.example", "jobicy.example"}     # Arbeitnow isn't remote-focused


def test_sites_are_understood():
    assert assistant.parse_sites("use company forms and remote boards") == ["company_forms", "remote_boards"]
    assert assistant.parse_sites("greenhouse jobs only") == ["company_forms"]
    assert assistant.parse_sites("apply for me") is None


@pytest.mark.parametrize("text", ["how do I apply?", "did you apply already?", "what jobs did you apply to?", "should I apply to this one?"])
def test_questions_about_applying_dont_start_anything(vera, tmp_path, text):
    me = ready(vera, OWNER, tmp_path)
    assert say(me, text)["action"] != "start_apply"
    assert vera.db.get_json(f"apply_request:{OWNER}") is None


def test_nothing_starts_without_a_yes(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    first = say(me, "use company forms and apply for me to at least 5 jobs")
    assert first["action"] == "none"
    assert "company forms" in first["reply"] and "up to 5" in first["reply"] and "yes" in first["reply"]
    second = say(me, "yes")
    assert second["action"] == "start_apply"
    assert second["request"] == {"count": 5, "sites": ["company_forms"], "dry_run": False}
    assert vera.db.get_json(f"apply_request:{OWNER}") is None     # a later "yes" can't start another run


def test_she_asks_what_she_needs(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    first = say(me, "apply for me")
    assert "tell me a number" in first["reply"]
    assert say(me, "yes")["action"] == "none"                     # no number yet, so no run
    assert "up to 7" in say(me, "7")["reply"]
    assert say(me, "yes")["request"]["count"] == 7


def test_missing_details_are_asked_for(vera):
    first = say(vera.user(GUEST), "apply to 5 jobs")
    assert "still need" in first["reply"] and "résumé" in first["reply"]
    assert say(vera.user(GUEST), "yes")["action"] == "none"


def test_details_given_in_chat_are_saved(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    say(me, "apply to 5 jobs")
    say(me, "my expected salary is 30k and I can start in 3 weeks")
    app = me.profile["application"]
    assert app["expected_salary_monthly"] == 30000 and "3 weeks" in app["notice_period"]


def test_changes_are_shown_again_before_starting(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    say(me, "apply to 5 jobs")
    changed = say(me, "only remote, up to 2 years of experience")
    assert changed["action"] == "none"
    assert me.prefs["work_setup"] == ["Remote"] and me.prefs["reject_mandatory_experience_over"] == 2
    assert "Remote" in changed["reply"] and "0 to 2 years" in changed["reply"]


def test_test_mode_sends_nothing(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    say(me, "apply to 3 jobs")
    assert say(me, "test it first")["request"]["dry_run"] is True


def test_no_cancels(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    say(me, "apply to 3 jobs")
    assert say(me, "no")["action"] == "none"
    assert vera.db.get_json(f"apply_request:{OWNER}") is None


def test_stop_ends_a_run(vera, tmp_path, monkeypatch):
    me = ready(vera, OWNER, tmp_path)
    assert say(me, "stop")["action"] == "none"                    # nothing running
    monkeypatch.setattr(rounds, "_active", [{"uid": OWNER, "channel_id": None}])
    assert say(me, "stop")["action"] == "stop_apply"
    monkeypatch.setattr(rounds, "_active", [{"uid": GUEST, "channel_id": None}])
    assert say(me, "stop")["action"] == "none"                    # someone else's run stays untouched


def test_other_people_are_told_they_get_company_forms_and_agree(vera, tmp_path):
    guest = ready(vera, GUEST, tmp_path)
    first = say(guest, "apply to 3 jobs")
    assert "company forms" in first["reply"] and "owner's" in first["reply"] and "in your name" in first["reply"]
    assert say(guest, "yes")["action"] == "start_apply"
    assert guest.prefs["rounds"]["consent"] is True
    assert rounds.write_profile(guest, count=3)["limits"]["max_per_run"] == 3


def test_requested_count_reaches_the_engine(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    assert rounds.write_profile(me, count=4)["limits"]["max_per_run"] == 4
    assert rounds.write_profile(me, count=999)["limits"]["max_per_run"] == 999       # no limit for the owner
    guest = ready(vera, GUEST, tmp_path)
    assert rounds.write_profile(guest, count=999)["limits"]["max_per_run"] == config.SHARED_MAX_PER_DAY


def test_you_have_no_limits_but_members_do(vera, tmp_path):
    me, guest = ready(vera, OWNER, tmp_path), ready(vera, GUEST, tmp_path)
    mine = rounds.build_profile(me)["limits"]
    theirs = rounds.build_profile(guest)["limits"]
    assert mine["max_per_run"] >= rounds.NO_LIMIT and mine["max_per_day"] >= rounds.NO_LIMIT and mine["max_per_company"] == 0
    assert theirs["max_per_run"] == config.SHARED_MAX_PER_RUN and theirs["max_per_day"] == config.SHARED_MAX_PER_DAY and theirs["max_per_company"] == 2
    text = rounds.status_text(vera, OWNER)
    assert "no limit per run" in text and "no daily limit" in text


def test_fifty_across_boards(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    reply = say(me, "apply to 50 jobs use different boards make the total application 50")["reply"]
    assert "up to 50 in total" in reply and "remote boards" in reply and "company forms" in reply and "spread" in reply
    assert say(me, "yes")["request"] == {"count": 50, "sites": None, "dry_run": False}


def test_until_i_say_stop(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    assert "until you say **stop**" in say(me, "keep applying until i say stop")["reply"]
    assert say(me, "yes")["request"]["count"] == rounds.NO_LIMIT


@pytest.mark.parametrize("text", ["recommend me some jobs", "can you recommend 5 jobs from remote boards", "suggest jobs for me"])
def test_recommend_applies_to_nothing(vera, tmp_path, text):
    me = ready(vera, OWNER, tmp_path)
    r = say(me, text)
    assert r["action"] == "recommend" and "won't apply" in r["reply"]
    assert vera.db.get_json(f"apply_request:{OWNER}") is None


def test_recommend_report_lists_jobs_with_links():
    text = rounds.recommend_report({"picks": [{"title": "Web Dev", "company": "Acme", "location": "Remote", "url": "https://x/1"}], "blocked": ["himalayas.app"]})
    assert "Web Dev" in text and "https://x/1" in text and "Bot check on himalayas.app" in text and "Nothing was sent" in text


def test_which_resume_is_answered(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    assert f"cv{OWNER}.pdf" in say(me, "what resume did you use for the application")["reply"]


def test_a_crashed_run_says_so_instead_of_showing_zeros(vera, tmp_path, monkeypatch):
    async def fake_engine(uid, queue, budget, dry_run, on_line=None):
        return "Traceback: browser profile is in use"

    async def no_boards(_s, **_k):
        return []
    monkeypatch.setattr(rounds, "_run_engine", fake_engine)
    monkeypatch.setattr(rounds, "board_urls", no_boards)
    ready(vera, OWNER, tmp_path)
    text = rounds.report(asyncio.run(rounds.run_once(vera, OWNER)))
    assert "ended early" in text and "profile is in use" in text


def test_several_boards_means_all_of_them(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    reply = say(me, "apply to 50 jobs across different boards")["reply"]
    for board in ("remote boards", "company job lists", "company forms"):
        assert board in reply
    assert say(me, "yes")["request"]["sites"] is None


def test_naming_one_source_still_means_just_that_source(vera, tmp_path):
    me = ready(vera, OWNER, tmp_path)
    reply = say(me, "apply to 5 jobs from company forms")["reply"]
    assert "company forms" in reply and "remote boards" not in reply


def test_no_run_ever_uses_job_board_accounts(vera):
    for uid in (OWNER, GUEST):
        profile = rounds.write_profile(vera.user(uid), 5)
        assert "allow_job_sites" not in profile and "sites" not in profile["search"]
