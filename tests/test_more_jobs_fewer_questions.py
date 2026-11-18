"""The report only asks what Vera can't answer from saved answers (even in other words) or the profile,
and jobs that keep failing the same way stop being retried."""

import asyncio
import json

import pytest

from vera.agents import rounds
from vera.apply import __main__ as engine
from vera.apply import answers
from vera.apply.answers import AnswerMemory, Field, rule_answer

PROFILE = {"expected_salary_usd_monthly": 800, "expected_salary_monthly": 28000}
REMOTE_JOB = {"url": "https://jobs.example/1", "location": "Remote"}


# ----------------------------------------------------------------- reading on past the cached pages
@pytest.mark.parametrize("question,answer", [
    ("* What is your expected hourly rate in USD?", "5 USD per hour"),
    ("Please specify your expected gross salary (monthly or annually) in USD or EUR.", "800 USD per month"),
    ("What is your current salary in SAR ?", None),
    ("* How much was your last monthly compensation package (in PESO)?", None),
    ("Expected salary (EUR)", None),
])
def test_salary_questions_use_their_own_figure(question, answer):
    assert rule_answer(Field("text", question, [], True), PROFILE, REMOTE_JOB) == answer


def test_an_hourly_number_field_gets_a_number():
    assert rule_answer(Field("number", "Expected hourly rate (USD)", [], True), PROFILE, REMOTE_JOB) == 5


# ----------------------------------------------------------------- answers given in other words
@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(answers, "ANSWERS_PATH", tmp_path / "answers.json")
    m = AnswerMemory()
    m.data = {"which languages do you speak": "English, Filipino", "do you have customer service experience": "No",
              "describe your experience with linux and open source": "None",
              "what is your expected salary": "28000",
              "_sources": {"which languages do you speak": "user", "do you have customer service experience": "user",
                           "describe your experience with linux and open source": "user", "what is your expected salary": "user"}}
    return m


def test_the_same_question_in_other_words_reuses_the_answer(memory):
    assert memory.similar(Field("text", "What languages do you speak fluently?", [], True)) == "English, Filipino"


def test_a_different_topic_is_not_taken_for_the_same_question(memory):
    assert memory.similar(Field("text", "Describe your experience with Kubernetes", [], True)) is None
    assert memory.similar(Field("text", "Describe your experience with Python and Bash", [], True)) is None


def test_sensitive_questions_never_borrow_a_similar_answer(memory):
    assert memory.similar(Field("text", "What is your expected monthly salary in USD?", [], True)) is None


def test_the_ai_gets_their_earlier_answers_as_facts(memory, monkeypatch):
    seen = {}

    async def complete(prompt, temperature=0.1):
        seen["prompt"] = prompt
        return '{"answer": "I haven\'t held a customer-facing role yet."}'
    from vera.apply import llm
    monkeypatch.setattr(llm, "complete", complete)
    field = Field("textarea", "Describe your experience within customer-facing roles", [], True)
    out = asyncio.run(answers.decide(field, {"name": "Juan"}, memory, {"title": "Dev", "company": "Acme"}))
    assert out and "do you have customer service experience" in seen["prompt"] and '"their_answer": "No"' in seen["prompt"]


# ----------------------------------------------------------------- the report asks only what's left, once
def test_the_report_skips_answered_questions_and_merges_lookalikes(tmp_path, monkeypatch):
    monkeypatch.setattr(rounds, "user_dir", lambda uid: tmp_path)
    (tmp_path / "answers.json").write_text(json.dumps({"which languages do you speak": "English, Filipino",
                                                       "_sources": {"which languages do you speak": "user"}}), encoding="utf-8")
    rows = [{"status": "needs_answer", "unanswered": " | ".join([
        "* What is your expected hourly rate in USD?", "What languages do you speak fluently?",
        "Describe your experience within customer-facing roles, or with managing customer relationships",
        "Describe your experience with customer-facing roles and managing customer relationships"])}]
    asked = rounds.open_questions(rows, 1, PROFILE)
    assert asked == ["Describe your experience within customer-facing roles, or with managing customer relationships"]


def test_the_report_says_what_it_already_used():
    text = rounds.report({"rows": [], "dry_run": False, "questions": ["Q1"], "blocked": [], "tail": ""})
    assert "saved answers" in text and "1. Q1" in text


# ----------------------------------------------------------------- held jobs: the "no AI" pledge, and one more try
def _row(time, unanswered):
    return {"time": time, "site": "company_form", "status": "needs_answer", "reason": "required question(s) with no honest answer",
            "unanswered": unanswered, "url": "https://x/1", "title": "Engineer", "company": "Acme"}


def test_a_form_demanding_a_no_ai_pledge_is_skipped_for_good():
    pledge = "During this application process I agree to use only my own words. The use of AI will disqualify"
    status, reason, questions = engine.held_back([_row("2026-11-08T10:00:00", pledge)], lambda _q: False)
    assert status == "skipped" and "without AI" in reason and questions == []


def test_a_job_waiting_since_before_the_better_answers_gets_one_more_try():
    essay = "Describe your experience within customer-facing roles"
    assert engine.held_back([_row("2026-11-07T10:00:00", essay)], lambda _q: False) is None
    assert engine.held_back([_row("2026-11-08T10:00:00", essay)], lambda _q: False)[0] == "needs_answer"
    pay = "How much was your last monthly compensation package (in PESO)?"          # sensitive: still theirs to answer
    assert engine.held_back([_row("2026-11-07T10:00:00", pay)], lambda _q: False)[0] == "needs_answer"


def test_questions_are_only_put_to_you_once(tmp_path, monkeypatch):
    from vera.db import DB
    from vera.services import Vera
    vera = Vera(DB(tmp_path / "t.db"))
    rows = [{"status": "needs_answer", "unanswered": "Q one about kubernetes clusters | Q two about embedded rust"}]
    assert rounds.remember_questions(vera, 1, rows, ["Q one about kubernetes clusters", "Q two about embedded rust"]) == [1, 2]
    assert rounds.remember_questions(vera, 1, rows, ["Q one about kubernetes clusters", "Q two about embedded rust", "Q three about salsa"]) == [3]
    text = rounds.report({"rows": [], "dry_run": False, "questions": ["A", "B", "C"], "new_questions": [3], "waiting_jobs": 9, "blocked": [], "tail": ""})
    assert "3. C" in text and "1. A" not in text and "2 question(s) I asked before are still open, holding 9 job(s)" in text


# ----------------------------------------------------------------- retries
def _js(time, status, reason, url="https://jobs.lever.co/acme/1"):
    return {"time": time, "site": "company_form", "status": status, "reason": reason, "unanswered": "", "url": url, "title": "Dev", "company": "Acme"}


def test_a_real_repeat_failure_stops_the_retries():
    stuck = [_js("2026-11-08T10:00:00", "failed", "stuck on a step: Missing entry"), _js("2026-11-08T11:00:00", "failed", "stuck on a step: Missing entry")]
    assert engine.held_back(stuck, lambda _q: False)[0] == "skipped"
    assert engine.held_back(stuck[:1], lambda _q: False) is None                 # one failure: worth another try


def test_browser_errors_are_put_in_plain_words():
    class TimeoutError(Exception):
        pass
    msg = engine.plain_error(TimeoutError('Locator.click: Timeout 30000ms exceeded.\nCall log:\n  - waiting for locator("button:visible")'))
    assert "Next/Submit button never became clickable" in msg and "Call log" not in msg


def test_the_last_run_breakdown_counts_real_reasons(tmp_path, monkeypatch):
    from vera.db import DB
    from vera.services import Vera
    vera = Vera(DB(tmp_path / "t.db"))
    rows = [_js("2026-11-07T17:20:00", "filtered", "title 'SEO Manager' is not one of your job titles"),
            _js("2026-11-07T17:21:00", "filtered", "title 'Business Analyst' is not one of your job titles"),
            _js("2026-11-07T17:22:00", "submitted", "")]
    monkeypatch.setattr(rounds, "read_results", lambda uid, start: rows)
    vera.db.set_json("rounds_last_start:1", "2026-11-07T17:14:00")
    out = rounds.last_run_breakdown(vera, 1)
    assert out["by_outcome"] == {"filtered": 2, "submitted": 1} and out["duplicates_skipped"] == 0
    assert out["top_reasons"] == {"filtered: title '…' is not one of your job titles": 2}
