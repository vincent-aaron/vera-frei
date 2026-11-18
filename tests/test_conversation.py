"""Vera holds a conversation about her plan: questions get answers, and only a clear yes sends anything."""

import asyncio

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
    for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.setattr(config, key, "")
    return Vera(DB(tmp_path / "t.db"))


@pytest.fixture
def me(vera, tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF")
    s = vera.user(OWNER)
    s.update_profile({"name": "Juan Cruz", "email": "j@example.com", "phone": "0900", "location": "Manila, Philippines",
                      "resumes": [{"name": "CV", "path": str(cv), "tags": []}],
                      "application": {"expected_salary_monthly": 28000, "notice_period": "2 weeks", "work_authorized": True}})
    s.update_prefs({"positions": ["Software Developer"], "reject_mandatory_experience_over": 1})
    return s


def say(session, text, sites=None):
    return asyncio.run(assistant.handle(session, text, sites))


@pytest.fixture
def planned(me):
    say(me, "apply to 10 jobs from company forms")
    return me


# ----------------------------------------------------------------- your question from the screenshot
def test_your_question_gets_an_answer_not_the_plan_again(planned):
    r = say(planned, "Experience 0 - 1 year is what you mean right")
    assert r["action"] == "none"
    assert r["reply"].startswith("Yep!") and "0 to 1 year" in r["reply"] and "no experience" in r["reply"]
    assert "Job titles" not in r["reply"] and "proceed" in r["reply"]                       # short, and she says what she's waiting for


def test_a_question_changes_nothing_and_the_plan_keeps_waiting(planned):
    say(planned, "Experience 0 - 1 year is what you mean right")
    assert planned.prefs["reject_mandatory_experience_over"] == 1
    assert say(planned, "yes")["action"] == "start_apply"


def test_she_doesnt_say_yep_to_something_thats_not_true(planned):
    r = say(planned, "so jobs asking 3 years are okay right?")
    assert not r["reply"].startswith("Yep") and "0 to 1 year" in r["reply"] and "up to 3 years" in r["reply"]


@pytest.mark.parametrize("question,expected", [
    ("what's the difference between test and yes?", "stops before sending"),
    ("which resume will you use?", "cv.pdf"),
    ("how many will you send?", "Up to 10"),
    ("where will you look?", "company forms"),
    ("what job titles are you looking for?", "Software Developer"),
    ("is it remote?", "Remote"),
    ("is that for junior level?", "Tell me if you want that changed"),
    ("ano yung experience na hanap mo?", "0 to 1 year"),
])
def test_plan_questions_are_answered_from_the_real_settings(planned, question, expected):
    r = say(planned, question)
    assert r["action"] == "none" and expected in r["reply"] and "Job titles:" not in r["reply"]


def test_a_question_she_cant_look_up_goes_to_the_ai_and_never_claims_a_start(planned, monkeypatch):
    prompts = []

    async def fake_ai(prompt, **_k):
        prompts.append(prompt)
        return "Probably a day or so, depending on the employer."
    monkeypatch.setattr(assistant.ai, "complete", fake_ai)
    r = say(planned, "how long until companies reply?")
    assert "Probably a day" in r["reply"] and r["action"] == "none" and "NOT started" in prompts[-1]


def test_she_stays_polite_when_the_ai_is_busy(planned):
    r = say(planned, "how long until companies reply?")
    assert "busy" in r["reply"] and r["action"] == "none" and planned.db.get_json(f"apply_request:{OWNER}")


# ----------------------------------------------------------------- only a clear yes sends anything
@pytest.mark.parametrize("text", ["proceed", "Proceed", "ok proceed", "yes proceed", "proceed po", "continue", "tuloy", "ituloy na", "push through", "let's go", "apply now", "yes", "Yes!", "ok", "okay", "yes please", "go ahead", "go", "sige", "sige po", "oo", "opo", "okay, go", "yes send them", "yes, apply", "alright", "all right", "do it", "looks good, go ahead"])
def test_a_clear_yes_starts_it(planned, text):
    assert assistant.plain_yes(text), text
    assert say(planned, text)["action"] == "start_apply"


@pytest.mark.parametrize("text", [
    "proceed but only remote", "proceed with 5 jobs", "continue?", "should i proceed?", "apply now to company forms only", "ok but only remote jobs", "yes but change it to 5", "ok what do you mean?", "sure, what about 5 jobs", "go for remote only", "yes?", "ok?",
    "okay wait", "sige pero remote lang", "start with 3", "yes except remote boards", "hmm okay", "ok, is that everything?",
])
def test_anything_more_than_yes_never_sends_real_applications(planned, text):
    assert not assistant.plain_yes(text), text
    assert say(planned, text)["action"] != "start_apply"
    assert planned.db.get_json(f"apply_request:{OWNER}")                               # still waiting for you


def test_asking_what_test_does_doesnt_start_a_test(planned):
    assert say(planned, "what does test do?")["action"] == "none"


def test_saying_test_still_starts_a_test(planned):
    for text in ("test", "test it first", "just do a dry run"):
        planned.db.set_json(f"apply_request:{OWNER}", {"count": 3, "sites": None})
        r = say(planned, text)
        assert r["action"] == "start_apply" and r["request"]["dry_run"] is True


# ----------------------------------------------------------------- no, and changes
@pytest.mark.parametrize("text", ["no", "nope", "not now", "hindi", "never mind", "no thanks", "no don't apply"])
def test_a_clear_no_cancels(planned, text):
    assert "won't apply" in say(planned, text)["reply"]
    assert planned.db.get_json(f"apply_request:{OWNER}") is None


def test_no_with_a_change_is_a_change_not_a_cancel(planned):
    r = say(planned, "no, make it 5 jobs")
    assert "up to 5" in r["reply"] and r["reply"].startswith("Got it")
    assert planned.db.get_json(f"apply_request:{OWNER}")["count"] == 5


def test_a_change_acknowledges_and_shows_the_new_plan(planned):
    r = say(planned, "only remote jobs")
    assert r["reply"].startswith("Got it") and planned.prefs["work_setup"] == ["Remote"] and "Work setup:** Remote" in r["reply"]


def test_can_you_change_it_is_a_change_but_can_you_explain_isnt():
    assert not assistant.just_asking("can you make it remote only?")
    assert not assistant.just_asking("please make it 5 jobs")
    assert assistant.just_asking("can you explain the experience part?")
    assert assistant.just_asking("Experience 0 - 1 year is what you mean right")
    assert assistant.just_asking("is it only remote?")
    assert assistant.just_asking("ano ibig sabihin nito")


def test_small_talk_doesnt_repeat_the_plan(planned, monkeypatch):
    async def fake_ai(prompt, **_k):
        return "No worries, take your time!" if "Reply briefly" in prompt else None
    monkeypatch.setattr(assistant.ai, "complete", fake_ai)
    r = say(planned, "hmm let me think")
    assert "No worries" in r["reply"] and "Job titles" not in r["reply"] and r["action"] == "none"


# ----------------------------------------------------------------- the plan itself
def test_the_plan_is_clear_about_experience(planned):
    r = say(planned, "apply to 5 jobs")
    assert "0 to 1 year, including no-experience jobs" in r["reply"] and "year(s)" not in r["reply"]


def test_no_number_gets_a_short_nudge_after_a_question(me):
    say(me, "apply for me")
    assert "how many" in say(me, "is it remote?")["reply"]


# ----------------------------------------------------------------- cold email drafts
def drafts(session):
    session.db.set_json(f"outreach_pending:{OWNER}", [{"email": "hr@acme.ph", "subject": "Hi", "body": "Hello"}])


@pytest.mark.parametrize("text", ["ok, what about the second one?", "yes but change the subject", "ok what does it say?", "sure, who is that?"])
def test_your_cold_emails_only_go_out_on_a_clear_yes(me, text):
    drafts(me)
    assert say(me, text)["action"] != "send_outreach"
    assert me.db.get_json(f"outreach_pending:{OWNER}")


def test_a_clear_yes_sends_your_cold_emails(me):
    drafts(me)
    assert say(me, "yes send them")["action"] == "send_outreach"


def test_a_question_about_drafts_gets_an_answer_and_a_reminder(me, monkeypatch):
    drafts(me)

    async def fake_ai(prompt, **_k):
        assert "hr@acme.ph" in prompt and "Nothing has been sent" in prompt
        return "That one goes to Acme's HR team."
    monkeypatch.setattr(assistant.ai, "complete", fake_ai)
    r = say(me, "who is this going to?")
    assert "Acme's HR" in r["reply"] and "still waiting" in r["reply"] and r["action"] == "none"


# ----------------------------------------------------------------- her voice
def test_she_is_told_to_talk_like_a_person(me, monkeypatch):
    seen = []

    async def fake_ai(prompt, **_k):
        seen.append(prompt)
        return "Hey!"
    monkeypatch.setattr(assistant.ai, "complete", fake_ai)
    asyncio.run(assistant.chat(me, "hello"))
    assert "easygoing friend" in seen[0] and "Reply in English" in seen[0] and "Never invent" in seen[0]


def test_members_are_treated_the_same_and_stay_on_their_own_plan(vera, me):
    guest = vera.user(GUEST)
    guest.update_prefs({"positions": ["Web Developer"], "reject_mandatory_experience_over": 3})
    say(guest, "apply to 3 jobs")
    answer = say(guest, "what experience is that?")["reply"]
    assert "0 to 3 years" in answer and not rounds.is_owner(GUEST)
    assert outreach.history(GUEST) == []


def test_she_asks_for_proceed(planned):
    assert "**proceed**" in say(planned, "apply to 5 jobs")["reply"]
    assert "**proceed**" in say(planned, "is it remote?")["reply"]


def test_proceed_sends_your_cold_emails(me):
    drafts(me)
    assert say(me, "proceed")["action"] == "send_outreach"


def test_job_age_doesnt_stop_her_applying(vera, me, monkeypatch):
    seen = {}

    async def nothing(*_a, **_k):
        return {}

    def fake_rank(_db, _uid, prefs, _profile, **_k):
        seen.update(prefs)
        return []
    monkeypatch.setattr(rounds.scout, "refresh_sources", nothing)
    monkeypatch.setattr(rounds.scout, "rank", fake_rank)
    assert me.prefs["max_age_days"] == 7
    asyncio.run(rounds.board_urls(me))
    assert seen["max_age_days"] is None
    assert me.prefs["max_age_days"] == 7             # /find-jobs keeps its setting
