"""Tests for the auto-apply agent (no network, no real job sites).

The end-to-end test drives a local multi-step application form mock
(tests/agent_fixtures/quick_apply.html) with a real headless browser.
"""

import asyncio
from pathlib import Path

import pytest

from vera.apply import answers
from vera.apply.answers import SKIP, AnswerMemory, Field, choose_option, rule_answer

FIXTURE = Path(__file__).parent / "apply_fixtures" / "quick_apply.html"

PROFILE = {
    "first_name": "Juan", "last_name": "Dela Cruz", "email": "juan@example.com", "phone": "+63 900 000 0000",
    "city": "Hagonoy", "country": "Philippines", "years_experience": 0,
    "expected_salary_monthly": 25000, "notice_period": "Immediately available",
    "work_authorized": True, "requires_sponsorship": False,
    "resume_path": str(FIXTURE),  # any existing file works for the upload
}


# ---------------------------------------------------------------- unit tests
def test_choose_option_ranges_and_yes_no():
    opts = ["No experience", "Less than 1 year", "1-2 years", "3+ years"]
    assert choose_option(opts, 0) == "No experience"
    assert choose_option(opts, 1.5) == "1-2 years"
    assert choose_option(opts, 7) == "3+ years"
    assert choose_option(["Yes", "No"], True) == "Yes"
    assert choose_option(["Yes, I am", "No"], "Yes") == "Yes, I am"


def test_rules_map_profile_fields():
    f = lambda q, kind="text", options=None: Field(kind, q, options or [])
    assert rule_answer(f("First name"), PROFILE) == "Juan"
    assert rule_answer(f("Email address", "email"), PROFILE) == "juan@example.com"
    assert rule_answer(f("What's your expected monthly basic salary?"), PROFILE) == 25000
    assert rule_answer(f("Are you willing to relocate to Manila?", "radio", ["Yes", "No"]), PROFILE) is None
    assert rule_answer(f("Are you legally authorised to work in the Philippines?", "radio", ["Yes", "No"]), PROFILE) == "Yes"


def test_cover_letter_is_never_included():
    opts = ["Upload a cover letter", "Write a cover letter", "Don't include a cover letter"]
    assert rule_answer(Field("radio", "Cover letter", opts), PROFILE) == "Don't include a cover letter"
    assert rule_answer(Field("textarea", "Cover letter (optional)", []), PROFILE) == SKIP
    assert rule_answer(Field("file", "Upload cover letter", []), PROFILE) == SKIP


def test_resume_choice_prefers_saved_then_upload():
    opts = ["Select a resumé", "Upload a resumé", "Don't include a resumé"]
    assert rule_answer(Field("radio", "Resumé", opts), PROFILE) == "Select a resumé"
    assert rule_answer(Field("radio", "Resumé", opts[1:]), PROFILE) == "Upload a resumé"


def test_cover_letter_written_when_enabled():
    on = {**PROFILE, "cover_letter": {"enabled": True}}
    opts = ["Upload a cover letter", "Write a cover letter", "Don't include a cover letter"]
    assert rule_answer(Field("radio", "Cover letter", opts), on) == "Write a cover letter"
    assert rule_answer(Field("textarea", "Cover letter", []), on) == answers.COVER_TEXT
    assert rule_answer(Field("file", "Cover Letter", []), on) == answers.COVER_FILE


OVERSEAS = {"url": "https://job-boards.greenhouse.io/acme/jobs/1", "location": "Remote, United States", "company": "Acme"}


def test_work_rights_are_honest_per_country():
    p = {**PROFILE, "work_authorized_countries": ["Philippines"]}
    ask = lambda q, job=None: rule_answer(Field("radio", q, ["Yes", "No"]), p, job)
    assert ask("Are you legally authorized to work in the United States?", OVERSEAS) == "No"
    assert ask("Are you legally authorised to work in the Philippines?", OVERSEAS) == "Yes"
    assert ask("Will you require visa sponsorship to work in the United States?", OVERSEAS) == "Yes"
    assert ask("Are you authorized to work for us?", OVERSEAS) is None      # "us" isn't the USA; unclear, so it waits
    assert ask("Do you require sponsorship to remain in your current location?", OVERSEAS) == "No"


def test_salary_never_uses_pesos_on_foreign_forms():
    f = lambda q, kind="text": Field(kind, q, [])
    assert rule_answer(f("Expected salary"), PROFILE, OVERSEAS) is None
    usd = {**PROFILE, "expected_salary_usd_monthly": 1500}
    assert rule_answer(f("Expected salary (USD)"), usd, OVERSEAS) == "1500 USD"
    assert rule_answer(f("Expected annual salary", "number"), usd, OVERSEAS) == 18000
    assert rule_answer(f("Expected monthly salary"), usd, {"url": "https://jobs.example.ph/job/1"}) == 25000


def test_diversity_questions_are_declined():
    opts = ["Woman", "Man", "Prefer not to disclose"]
    assert rule_answer(Field("radio", "Gender", opts), PROFILE) == "Prefer not to disclose"
    assert rule_answer(Field("combobox", "Veteran Status", []), PROFILE) == SKIP


def test_sensitive_questions_never_reach_ai(tmp_path, monkeypatch):
    calls = []

    async def spy(*_a, **_k):
        calls.append(1)
        return "Yes"
    monkeypatch.setattr(answers, "ai_answer", spy)
    monkeypatch.setattr(answers, "ANSWERS_PATH", tmp_path / "a.json")
    mem = AnswerMemory()
    for q in ("Have you ever been convicted of a crime?",
              "We ask this to prove you are not a bot: decode the secret", "Do you hold a security clearance?"):
        assert asyncio.run(answers.decide(Field("text", q, [], True), {**PROFILE, "skills": ["Laravel"]}, mem, OVERSEAS)) is None
    assert not calls


def test_written_ai_answers_are_not_reused(tmp_path, monkeypatch):
    async def fake(*_a, **_k):
        return "I admire Acme."
    monkeypatch.setattr(answers, "ai_answer", fake)
    monkeypatch.setattr(answers, "ANSWERS_PATH", tmp_path / "a.json")
    mem = AnswerMemory()
    asyncio.run(answers.decide(Field("textarea", "Why this company?", []), PROFILE, mem, OVERSEAS))
    assert mem.get(Field("textarea", "Why this company?", [])) is None


def test_cover_letter_fact_check_removes_unsupported_claims():
    from vera.apply import cover_letter
    p = {"skills": ["PHP", "Laravel"], "summary": "Recent graduate."}
    clean, removed = cover_letter.scrub("I build apps with Laravel. I am expert in Kubernetes. I have 5 years in PHP.", p)
    assert "Laravel" in clean and "Kubernetes" not in clean and "5 years" not in clean
    assert "kubernetes" in removed


def test_cut_off_cover_letter_is_replaced_by_template(tmp_path, monkeypatch):
    from vera.apply import config, cover_letter, llm

    async def cut_off(*_a, **_k):
        return " ".join(["I build web applications with PHP."] * 15) + " I also designed real"
    monkeypatch.setattr(llm, "complete", cut_off)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    letter = asyncio.run(cover_letter.build({"skills": ["PHP"]}, {"title": "Dev", "company": "Acme"}))
    assert "designed real" not in letter and "I am applying for the Dev role at Acme." in letter


def test_cover_letters_only_use_gemini(tmp_path, monkeypatch):
    import httpx
    from vera.apply import config, cover_letter, llm
    llm._cooldown.clear()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(llm, "_env", lambda: {"GEMINI_API_KEY": "g", "NVIDIA_API_KEY": "n", "OPENROUTER_API_KEY": "o"})
    hits = []

    def handler(request):
        hits.append(request.url.host)
        return httpx.Response(503, json={})
    real = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **k: real(transport=httpx.MockTransport(handler), **k))
    letter = asyncio.run(cover_letter.build({"skills": ["PHP"]}, {"title": "Dev", "company": "Acme"}))
    assert set(hits) == {"generativelanguage.googleapis.com"}      # Gemini down: nobody else writes it
    assert "I am applying for the Dev role at Acme." in letter


def test_one_year_limit_keeps_one_year_jobs():
    from vera.apply.filters import rejects
    spec = {"max_years_required": 1}
    job = lambda d: {"title": "Web Developer", "description": d}
    assert rejects(job("At least 1 year of experience required."), spec) is None
    assert rejects(job("Fresh graduates welcome."), spec) is None
    assert rejects(job("0-1 years experience."), spec) is None
    assert "2+ years" in rejects(job("Minimum 2 years of relevant experience."), spec)


def test_cover_letter_docx_is_a_real_word_file(tmp_path, monkeypatch):
    import zipfile
    from vera.apply import config, cover_letter
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    path = cover_letter.save_docx("Dear Team & friends,\n\nHello.", {"title": "Dev", "company": "Acme"})
    with zipfile.ZipFile(path) as z:
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= set(z.namelist())
        assert "Team &amp; friends" in z.read("word/document.xml").decode()


def test_llm_skips_answers_that_were_cut_off(monkeypatch):
    import httpx
    from vera.apply import llm
    llm._cooldown.clear()
    monkeypatch.setattr(llm, "_env", lambda: {"NVIDIA_API_KEY": "n"})
    replies = iter([{"finish_reason": "length", "message": {"content": "half a sent"}},
                    {"finish_reason": "stop", "message": {"content": "A whole answer."}}])
    real = httpx.AsyncClient
    handler = lambda _req: httpx.Response(200, json={"choices": [next(replies)]})
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **k: real(transport=httpx.MockTransport(handler), **k))
    assert asyncio.run(llm.complete("hi")) == "A whole answer."


def test_cover_letter_falls_back_to_template(tmp_path, monkeypatch):
    from vera.apply import config, cover_letter, llm

    async def down(*_a, **_k):
        return None
    monkeypatch.setattr(llm, "complete", down)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    p = {"first_name": "Juan", "last_name": "Cruz", "skills": ["PHP"], "education": "BSIT", "email": "j@example.com"}
    job = {"title": "Web Developer", "company": "Acme", "description": "We use PHP."}
    letter = asyncio.run(cover_letter.build(p, job))
    assert "Dear Hiring Team at Acme" in letter and "PHP" in letter and "Juan Cruz" in letter
    assert list(tmp_path.glob("cover_letters/*.txt"))
    assert cover_letter.save_docx(letter, job).suffix == ".docx"


SPEC = {"keywords": ["Web Developer", "Full Stack Developer"], "levels": ["Fresh Graduate", "Entry Level", "Junior"],
        "work_setup": ["Remote"], "job_types": ["Full-time"], "max_years_required": 2, "exclude_companies": ["BadCo"],
        "min_salary_monthly": 30000}


def test_job_spec_filters():
    from vera.apply.filters import rejects
    good = {"title": "Junior Web Developer (Remote)", "company": "Acme", "location": "Remote", "description": "Full-time. PHP."}
    assert rejects(good, SPEC) is None
    assert rejects({**good, "title": "Fullstack Developer - Remote"}, SPEC) is None
    assert "not one of" in rejects({**good, "title": "Real Estate Agent (Remote)"}, SPEC)
    assert "level" in rejects({**good, "title": "Senior Web Developer (Remote)"}, SPEC)
    assert "work setup" in rejects({**good, "title": "Web Developer", "location": "Makati"}, SPEC)
    assert "Hybrid" in rejects({**good, "location": "Hybrid - Makati", "title": "Web Developer"}, SPEC)
    assert "excluded company" in rejects({**good, "company": "BadCo Inc"}, SPEC)
    assert "job type" in rejects({**good, "description": "This is a part-time role."}, SPEC)
    assert "5+ years" in rejects({**good, "description": "Full-time. At least 5 years of experience required."}, SPEC)
    assert "pay tops out" in rejects({**good, "description": "Full-time. PHP 20,000 - PHP 25,000 monthly"}, SPEC)


def test_places_only_limit_jobs_you_must_travel_to():
    from vera.apply.filters import rejects
    spec = {"work_setup": ["Remote", "Hybrid", "On-site"], "places": ["Bulacan", "Metro Manila"]}
    assert "outside your locations" in rejects({"title": "Web Developer", "location": "Cebu City"}, spec)
    assert rejects({"title": "Web Developer", "location": "Malolos, Bulacan"}, spec) is None
    assert rejects({"title": "Web Developer (Remote)", "location": "Cebu City"}, spec) is None


def test_experience_only_counts_your_automation_work():
    p = {**PROFILE, "skills": ["Laravel", "PHP"], "skill_years": {"automation": 0.5, "ai agent": 0.5}, "years_experience": 0.5}
    ask = lambda q, kind="text", opts=None: rule_answer(Field(kind, q, opts or []), p)
    assert ask("How many years of Software Support experience do you have?") == 0
    assert ask("How many years of Laravel do you have?") == 0
    assert ask("How many years of experience do you have with PHP?") == 0
    assert ask("How many years of experience do you have as a web developer?") == 0
    assert ask("How many years of automation experience do you have?") == 0.5
    assert ask("How many years of experience do you have with AI agents?") == 0.5
    assert ask("How many years of total work experience do you have?") == 0.5
    assert choose_option(["No experience", "Less than 1 year", "1-2 years"], ask("How many years of Application Software Support do you have?")) == "No experience"
    assert "maintain" not in p["skill_years"] and ask("How many years of experience with email marketing?") == 0     # "ai" inside "email" doesn't count


def test_full_page_bot_checks_are_recognised(tmp_path):
    from playwright.async_api import async_playwright
    from vera.apply.sites import SiteBlocked, check_blocked, is_challenge
    wall = tmp_path / "wall.html"
    wall.write_text("<title>Example Board</title><h1>Humans only</h1><p>We use advanced security systems</p>", encoding="utf-8")
    job = tmp_path / "job.html"
    job.write_text("<title>Web Developer</title><h1>Web Developer</h1><p>Humans only is not in the first part of this job</p>".replace("Humans only is not in the first part of this job", "Build websites"), encoding="utf-8")

    async def go():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job.as_uri())
            clean = await is_challenge(page)
            await page.goto(wall.as_uri())
            walled = await is_challenge(page)
            with pytest.raises(SiteBlocked):
                await check_blocked(page, "remote_board")
            await browser.close()
            return clean, walled
    assert asyncio.run(go()) == (False, True)


def test_stuck_form_becomes_a_question():
    from vera.apply.form_engine import questions_from_errors
    errors = ["Before you can continue with the application, please address the following issues:\nHow many years of Software Support experience do you have? - Please make a selection\nDo you have a car? - Please make a selection"]
    assert questions_from_errors(errors) == ["How many years of Software Support experience do you have?", "Do you have a car?"]
    assert questions_from_errors(["Something went wrong"]) == []


def test_similar_titles_pass_and_others_dont():
    from vera.apply.filters import rejects
    spec = {"keywords": ["Web Developer", "AI Automation Specialist"], "similar_titles": True, "levels": ["Junior"]}
    for good in ("Front-End Developer || WFH", "FULL-STACK ENGINEER", "React, Typescript Developer - Homebased", "Solutions Developer", "DevOps Engineer", "AI/Automation Specialist - Web Development Company"):
        assert rejects({"title": good}, spec) is None, good
    for bad in ("Civil Engineer", "Sales Engineer", "Real Estate Developer", "Business Developer", "Website Designer Remote"):
        assert "not one of" in rejects({"title": bad}, spec), bad
    assert "level" in rejects({"title": "Lead Backend Developer"}, spec)
    assert "not one of" in rejects({"title": "Frontend Developer"}, {**spec, "similar_titles": False})


class FakeTab:
    def __init__(self, ctx):
        self.ctx, self.url, self.handlers = ctx, "", []

    def on(self, _event, handler):
        self.handlers.append(handler)

    def opened(self, tab):
        for handler in self.handlers:
            handler(tab)

    async def goto(self, url, **_):
        self.url = url
        if "broken" in url:
            raise RuntimeError("page failed")

    async def close(self):
        self.ctx.pages.remove(self)

    async def screenshot(self, **_):
        pass


class FakeBrowser:
    def __init__(self, leftover=0):
        self.pages = []
        for _ in range(leftover + 1):
            self.pages.append(FakeTab(self))
        self.closed, self.peak = False, 0

    async def new_page(self):
        tab = FakeTab(self)
        self.pages.append(tab)
        self.peak = max(self.peak, len(self.pages))
        return tab

    async def close(self):
        self.closed = True


def test_tabs_close_after_every_job(monkeypatch, tmp_path):
    """Tabs left from last time, pop-ups and failed jobs must not pile up during a long run."""
    import json
    from types import SimpleNamespace

    import playwright.async_api as pa

    from vera.apply import __main__ as engine

    browser = FakeBrowser(leftover=3)
    base = browser.pages[0]
    after_each = []

    class PW:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_):
            return False

    class Site:
        name = "test_site"

        async def job_info(self, page):
            return {"title": "Developer", "company": page.url, "location": "", "description": ""}

        async def open_apply(self, page):
            tab = await browser.new_page()       # an application opening its own tab
            page.opened(tab)
            return tab

    async def fill(work, *_a, **_k):
        work.opened(await browser.new_page())      # a pop-up on the form
        return SimpleNamespace(status="ready", reason="", unanswered=[])

    async def nothing(*_a, **_k):
        return None

    async def open_browser(*_a, **_k):
        return browser

    monkeypatch.setattr(pa, "async_playwright", lambda: PW())
    monkeypatch.setattr(engine, "open_browser", open_browser)
    monkeypatch.setattr(engine, "site_for", lambda _url: Site())
    monkeypatch.setattr(engine, "check_blocked", nothing)
    monkeypatch.setattr(engine, "run_application", fill)
    monkeypatch.setattr(engine, "read_log", lambda: [])
    monkeypatch.setattr(engine, "write_log", lambda row: after_each.append((row["status"], len(browser.pages))))
    monkeypatch.setattr(engine, "save_leads", lambda _job: None)
    monkeypatch.setattr(engine, "load_state", lambda: {})
    monkeypatch.setattr(engine, "save_state", lambda _s: None)
    monkeypatch.setattr(engine, "AnswerMemory", lambda: None)
    monkeypatch.setattr(engine, "claim_job", lambda *_a: True)
    monkeypatch.setattr(engine, "release_job", lambda *_a: None)
    monkeypatch.setattr(engine.filters, "rejects", lambda *_a: None)
    monkeypatch.setattr(engine.config, "load_profile", lambda: {
        "limits": {"max_per_run": 9, "max_per_day": 9, "delay_seconds": [0, 0], "max_per_company": 0}, "search": {}})
    monkeypatch.setattr(engine.config, "missing_fields", lambda _p: [])
    monkeypatch.setattr(engine.asyncio, "sleep", nothing)
    queue = tmp_path / "q.json"
    queue.write_text(json.dumps([f"https://x.test/{i}" for i in range(3)] + ["https://x.test/broken"]), encoding="utf-8")
    args = SimpleNamespace(queue_file=str(queue), url=None, max=9, dry_run=True, headless=True, background=True, debug=False)
    asyncio.run(engine.cmd_apply(args))

    assert browser.pages == [base] and browser.closed      # only the blank tab, and then the browser closes too
    assert base.url == "about:blank"
    assert all(count == 1 for _status, count in after_each)
    assert [s for s, _ in after_each] == ["dry-run", "dry-run", "dry-run", "failed"]
    assert browser.peak == 4                           # blank + job + its own tab + a pop-up, never more


def test_one_advertiser_cant_use_up_a_run():
    from vera.apply.__main__ import MAX_PER_COMPANY, company_key, too_many
    job = {"company": "DataAnnotation", "title": "Frontend Developer - AI Trainer"}
    seen = {(company_key(job["company"]), job["title"].lower())}
    assert "already seen" in too_many(job, {}, seen)
    other = {**job, "title": "Java Developer - AI Trainer"}
    assert too_many(other, {}, seen) == ""
    assert "already sent" in too_many(other, {company_key(job["company"]): MAX_PER_COMPANY}, seen)
    assert too_many({"company": "", "title": "x"}, {}, set()) == ""
    assert too_many(other, {company_key(job["company"]): 50}, seen, cap=0) == ""       # the owner has no company limit
    assert "already seen" in too_many(job, {}, seen, cap=0)                            # but the same ad is never sent twice


def test_total_is_spread_across_boards_evenly():
    from vera.apply.__main__ import round_robin
    assert round_robin([["a1", "a2", "a3"], ["b1"], ["c1", "c2"]]) == ["a1", "b1", "c1", "a2", "c2", "a3"]
    assert round_robin([["same", "a"], ["same", "b"]]) == ["same", "a", "b"]
    assert round_robin([]) == []


def test_a_bot_check_rests_one_remote_board_not_all():
    from vera.apply.__main__ import block_key
    from vera.apply.sites import site_for
    a, b = "https://himalayas.app/x", "https://www.remotive.com/y"
    assert block_key(site_for(a), a) == "himalayas.app" and block_key(site_for(b), b) == "remotive.com"
    form = "https://jobs.lever.co/acme/1"
    assert block_key(site_for(form), form) == "company_form"


def test_board_addresses_match_whatever_their_capitals():
    from vera.apply.sites import site_for
    assert site_for("https://remoteOK.com/remote-jobs/1").name == "remote_board"
    assert site_for("https://www.example-jobboard.com/job/1") is None      # sites that need an account aren't supported
    assert site_for("https://jobs.lever.co/acme/1").name == "company_form"
    assert site_for("https://example.com/jobs/remoteok.com") is None      # only the site itself counts, not words in the path


def test_a_form_that_redraws_itself_still_gets_filled(tmp_path, monkeypatch):
    """Filling the first field wipes the tags on the others, as Ashby's forms do."""
    from playwright.async_api import async_playwright
    from vera.apply import answers as A
    from vera.apply.form_engine import run_application
    monkeypatch.setattr(A, "ANSWERS_PATH", tmp_path / "a.json")
    html = tmp_path / "f.html"
    html.write_text("""<label for=a>First name</label><input id=a required oninput="document.querySelectorAll('[data-aa]').forEach(e => e.removeAttribute('data-aa'))">
      <label for=b>Email</label><input id=b type=email required>
      <button onclick="window.saved=[a.value,b.value]; document.body.innerText='Thanks for applying';">Submit application</button>""", encoding="utf-8")

    async def go():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(html.as_uri())
            r = await run_application(page, PROFILE, AnswerMemory(), {"title": "Dev", "company": "X"})
            saved = await page.evaluate("window.saved")
            await browser.close()
            return r, saved
    result, saved = asyncio.run(go())
    assert result.status == "submitted" and saved == ["Juan", "juan@example.com"]


def test_same_named_resumes_tick_only_the_first(tmp_path):
    """Two saved résumés with one name: the newest is listed first, and must stay selected."""
    from playwright.async_api import async_playwright
    from vera.apply.form_engine import COLLECT_JS, _fill
    html = tmp_path / "f.html"
    html.write_text("""<fieldset><legend>Resumé</legend>
      <label><input type=radio name=r value=new> Same_Name.pdf</label>
      <label><input type=radio name=r value=old checked> Same_Name.pdf</label>
      <label><input type=radio name=r value=none> Don't include</label></fieldset>""", encoding="utf-8")

    async def go():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(html.as_uri())
            field = (await page.evaluate(COLLECT_JS))[0]
            await _fill(page.main_frame, field, "Same_Name.pdf")
            chosen = await page.evaluate("document.querySelector('input:checked').value")
            await browser.close()
            return chosen
    assert asyncio.run(go()) == "new"


def test_your_resume_is_picked_over_the_sites_default():
    from vera.apply.form_engine import needs_filling
    me = {**PROFILE, "resume_path": "C:/Docs/My_New_Resume.docx"}
    opts = ["Default Old_Resume.pdf Added 1 month ago", "My_New_Resume.pdf Added today", "Don't include a resumé"]
    assert rule_answer(Field("radio", "Resumé", opts), me) == opts[1]
    assert needs_filling({"kind": "radio", "question": "Resumé", "options": opts, "checked": True}, me)
    assert not needs_filling({"kind": "radio", "question": "Resumé", "options": opts[:1], "checked": True}, me)      # not uploaded there: keep default


def test_buttons_with_invisible_characters_still_match():
    from vera.apply.form_engine import NEXT_RE, SUBMIT_RE, clean_label
    assert NEXT_RE.match(clean_label("Continue⁠"))
    assert SUBMIT_RE.match(clean_label("​Submit application⁠"))
    assert not SUBMIT_RE.match(clean_label("Review and submit"))      # a form's step label, not the send button


def test_prefilled_generic_cover_letter_gets_replaced_only_when_letters_are_on():
    from vera.apply.form_engine import needs_filling
    box = {"kind": "textarea", "question": "Write a cover letter", "filled": True}
    assert needs_filling(box, {"cover_letter": {"enabled": True}})
    assert not needs_filling(box, {"cover_letter": {"enabled": False}})
    assert not needs_filling({"kind": "textarea", "question": "Anything else?", "filled": True}, {"cover_letter": {"enabled": True}})
    assert not needs_filling({"kind": "radio", "question": "Cover letter", "checked": True}, {"cover_letter": {"enabled": True}})
    assert needs_filling({"kind": "text", "question": "City", "filled": False}, {})


def test_llm_only_uses_free_models():
    from vera.apply import llm
    by_name = {p["name"]: p for p in llm.PROVIDERS}
    env = {"OPENROUTER_MODEL": "openai/gpt-paid", "OPENROUTER_FALLBACK_MODELS": "google/gemma:free, anthropic/paid, openrouter/free"}
    assert llm._models(by_name["openrouter"], env) == ["google/gemma:free", "openrouter/free"]
    assert llm._models(by_name["openrouter"], {"OPENROUTER_MODEL": "openai/gpt-paid"}) == ["openrouter/free"]
    assert llm._models(by_name["gemini"], env) == llm.GEMINI_FREE
    assert llm._models(by_name["groq"], env) == llm.GROQ_FREE and llm._models(by_name["mistral"], env) == llm.MISTRAL_FREE
    assert [p["name"] for p in llm.PROVIDERS][:3] == ["gemini", "groq", "nvidia"] and llm.PROVIDERS[-1]["name"] == "openrouter"


def test_llm_fails_over_to_next_provider(monkeypatch):
    import httpx
    from vera.apply import llm
    llm._cooldown.clear()
    env = {"GEMINI_API_KEY": "g", "OPENROUTER_API_KEY": "o", "OPENROUTER_MODEL": "free-x:free"}
    monkeypatch.setattr(llm, "_env", lambda: env)
    hits = []

    def handler(request):
        hits.append(request.url.host)
        if "googleapis" in request.url.host:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})
    real = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **k: real(transport=httpx.MockTransport(handler), **k))
    assert asyncio.run(llm.complete("hi")) == "hello"
    assert hits == ["generativelanguage.googleapis.com", "openrouter.ai"]
    assert "gemini" in llm._cooldown           # rate-limited provider rests


# ------------------------------------------------------------ end-to-end test
@pytest.fixture
def no_ai(monkeypatch, tmp_path):
    async def _none(*_a, **_k):
        return None
    monkeypatch.setattr(answers, "ai_answer", _none)
    monkeypatch.setattr(answers, "ANSWERS_PATH", tmp_path / "answers.json")


def _run(profile, dry_run=False):
    from playwright.async_api import async_playwright
    from vera.apply.form_engine import run_application

    async def go():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(FIXTURE.as_uri())
            result = await run_application(page, profile, AnswerMemory(), {"title": "Web Developer", "company": "Mock Corp"},
                                           dry_run=dry_run)
            captured = await page.evaluate("window.captured")
            await browser.close()
            return result, captured
    return asyncio.run(go())


def test_end_to_end_submits_without_cover_letter(no_ai):
    result, captured = _run(PROFILE)
    assert result.status == "submitted", result
    assert captured["submitted"] is True
    assert captured["coverLetter"] == "none"
    assert captured["resume"] == FIXTURE.name
    assert captured["years"] == "No experience"
    assert captured["auth"] == "y"
    assert captured["salary"] == "25000"
    assert captured["name"] == "Juan Dela Cruz"


def test_dry_run_stops_before_submit(no_ai):
    result, captured = _run(PROFILE, dry_run=True)
    assert result.status == "ready"
    assert not captured.get("submitted")


def test_skips_job_when_required_answer_unknown(no_ai):
    profile = {**PROFILE, "expected_salary_monthly": None}
    result, captured = _run(profile)
    assert result.status == "needs_answer"
    assert any("salary" in q.lower() for q in result.unanswered)
    assert not captured.get("submitted")
