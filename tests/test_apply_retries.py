"""7 Nov 2026 fixes: jobs that kept failing or waiting for answers, honest queue counts, answers from the profile,
Ashby's Yes/No buttons and success message, and the wider company-form sources. No network."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from vera.apply import __main__ as engine
from vera.apply import answers
from vera.apply.answers import SKIP, AnswerMemory, Field, choose_option, is_adult, rule_answer
from vera.apply.form_engine import SUBMIT_UNCONFIRMED, SUCCESS_RE, questions_from_errors

ME = {"first_name": "Juan", "last_name": "Cruz", "city": "Hagonoy", "province": "Bulacan", "country": "Philippines",
      "education": "Bachelor of Science in Information Technology, Bulacan State University (graduated June 2026, Cum Laude)",
      "experience": [{"company": "Example Corp"}], "work_authorized": True, "work_authorized_countries": ["Philippines"]}


def row(status, reason="", unanswered="", url="https://jobs.ashbyhq.com/acme/1", **kw):
    return {"time": "2026-11-08T22:00:00", "site": "company_form", "status": status, "reason": reason, "unanswered": unanswered,
            "url": url, "title": "Software Engineer", "company": "Acme", **kw}


never = lambda _q: False
always = lambda _q: True


# ----------------------------------------------------------------- jobs that keep failing
def test_a_job_that_failed_twice_the_same_way_is_left_alone():
    rows = [row("failed", "no Continue / Submit button found (screenshot: 1.png)"), row("failed", "no Continue / Submit button found (screenshot: 2.png)")]
    status, reason, _q = engine.held_back(rows, never)
    assert status == "skipped" and "same way 2 times" in reason and "no continue / submit button found" in reason


def test_one_failure_or_two_different_ones_get_another_try():
    assert engine.held_back([row("failed", "TimeoutError: Page.goto: Timeout 30000ms exceeded")], never) is None
    assert engine.held_back([row("failed", "stuck on a step: Missing entry"), row("failed", "gave up after 15 steps")], never) is None


def test_another_runs_job_or_a_filter_doesnt_excuse_a_job_from_the_limit():
    """Spark was retried five times because one old 'filtered' row exempted it from the retry limit."""
    rows = [row("filtered", "on-site/hybrid with no location listed, so it could be too far")] + \
           [row("failed", f"no Continue / Submit button found (screenshot: {i}.png)") for i in range(2)]
    assert engine.held_back(rows, never)[0] == "skipped"
    assert engine.held_back([row("filtered", "another run is already handling this job")], never) is None


def test_failure_kinds_ignore_numbers_and_details():
    assert engine.error_kind("gave up after 15 steps (screenshot: x.png)") == engine.error_kind("gave up after 9 steps")
    assert engine.error_kind("stuck on a step: Your form needs corrections\n\nMissing entry") == "stuck on a step"
    assert engine.error_kind("TargetClosedError: Locator.inner_text: Target page closed") == "targetclosederror"


def test_a_job_waits_for_your_answers_instead_of_being_opened_again():
    rows = [row("needs_answer", "required question(s) with no honest answer", "Describe your experience with Kubernetes | Are you over 18?*")]
    status, reason, questions = engine.held_back(rows, lambda q: "18" in q)
    assert status == "needs_answer" and reason == engine.WAITING and questions == ["Describe your experience with Kubernetes"]
    assert engine.held_back(rows, always) is None                  # answered: try again


def test_waiting_rows_arent_attempts_and_answered_jobs_still_stop_eventually():
    waiting = row("needs_answer", engine.WAITING, "Q1")
    rows = [row("needs_answer", "required question(s) with no honest answer", "Q1")] + [waiting] * 6
    assert engine.held_back(rows, always) is None
    tried = [row("needs_answer", "required question(s) with no honest answer", "Q1")] * engine.MAX_ANSWER_TRIES
    assert engine.held_back(tried, always)[0] == "skipped"


def test_a_job_where_submit_was_clicked_is_never_sent_again():
    rows = [row("failed", SUBMIT_UNCONFIRMED + ", so I won't try it again")]
    status, reason, _q = engine.held_back(rows, always)
    assert status == "skipped" and "won't send it again" in reason


# ----------------------------------------------------------------- honest "found N jobs"
def test_jobs_left_out_before_opening_are_counted_and_explained(monkeypatch):
    held = {"https://jobs.ashbyhq.com/acme/1": (("needs_answer", engine.WAITING, ["Describe your Linux experience"]), row("needs_answer"))}
    state = {"himalayas.app": "2999-01-01T00:00:00"}
    urls = ["https://jobs.ashbyhq.com/acme/1", "https://news.ycombinator.com/item?id=1", "https://himalayas.app/companies/x/jobs/y",
            "https://jobs.lever.co/acme/2"]
    queue, left_out, resting = engine.sort_out(urls, held, state, [])
    assert queue == ["https://jobs.lever.co/acme/2"]
    assert {k: len(v) for k, v in left_out.items()} == {"held": 1, "unsupported": 1, "resting": 1}
    assert resting == ["himalayas.app"]
    lines = engine.left_out_lines(urls, left_out, held, resting)
    assert json.loads(lines[0].removeprefix("LEFT-OUT "))["lined_up"] == 4
    assert "1 still waiting for your answers" in lines[1] and "himalayas.app" in lines[1] and "sites I can't fill" in lines[1]
    assert "Describe your Linux experience" in lines[2]


def _engine(monkeypatch, tmp_path, urls, history, opened):
    import playwright.async_api as pa

    class Tab:
        url = ""

        def on(self, *_a):
            pass

        async def goto(self, url, **_k):
            self.url = url
            if url != "about:blank":
                opened.append(url)

        async def close(self):
            pass

        async def screenshot(self, **_k):
            pass

    class Browser:
        pages = [Tab()]

        async def new_page(self):
            return Tab()

        async def close(self):
            pass

    class PW:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_):
            return False

    class FakeSite:
        name = "company_form"

        async def job_info(self, page):
            return {"title": "Dev", "company": page.url, "location": "", "description": ""}

        async def open_apply(self, page):
            return None

    async def fill(*_a, **_k):
        return SimpleNamespace(status="submitted", reason="ok", unanswered=[])

    async def nothing(*_a, **_k):
        return None

    async def open_browser(*_a, **_k):
        return Browser()
    logged = []
    monkeypatch.setattr(pa, "async_playwright", lambda: PW())
    monkeypatch.setattr(engine, "open_browser", open_browser)
    monkeypatch.setattr(engine, "site_for", lambda url: FakeSite() if "unsupported" not in url else None)
    monkeypatch.setattr(engine, "check_blocked", nothing)
    monkeypatch.setattr(engine, "run_application", fill)
    monkeypatch.setattr(engine, "read_log", lambda: history)
    monkeypatch.setattr(engine, "write_log", logged.append)
    monkeypatch.setattr(engine, "save_leads", lambda _job: None)
    monkeypatch.setattr(engine, "load_state", lambda: {"_spec_time": "2000-01-01"})
    monkeypatch.setattr(engine, "save_state", lambda _s: None)
    monkeypatch.setattr(engine, "AnswerMemory", lambda: None)
    monkeypatch.setattr(engine, "claim_job", lambda *_a: True)
    monkeypatch.setattr(engine, "release_job", lambda *_a: None)
    monkeypatch.setattr(engine.filters, "rejects", lambda *_a: None)
    monkeypatch.setattr(engine.config, "load_profile", lambda: {
        "limits": {"max_per_run": 9, "max_per_day": 9, "delay_seconds": [0, 0], "max_per_company": 0, "tabs": 2},
        "search": {}, "country": "Philippines"})
    monkeypatch.setattr(engine.config, "missing_fields", lambda _p: [])
    monkeypatch.setattr(engine.asyncio, "sleep", nothing)
    queue = tmp_path / "q.json"
    queue.write_text(json.dumps(urls), encoding="utf-8")
    args = SimpleNamespace(queue_file=str(queue), url=None, site=None, max=9, dry_run=False, headless=True, background=True, debug=False, recommend=False)
    asyncio.run(engine.cmd_apply(args))
    return logged


def test_a_run_skips_held_jobs_says_so_and_counts_only_real_ones(monkeypatch, tmp_path, capsys):
    stuck, waiting, fresh = "https://a.test/stuck", "https://a.test/waiting", "https://a.test/fresh"
    history = [row("failed", "no Continue / Submit button found", url=stuck), row("failed", "no Continue / Submit button found", url=stuck),
               row("needs_answer", "required question(s) with no honest answer", "Describe your best personal project", url=waiting)]
    opened = []
    logged = _engine(monkeypatch, tmp_path, [stuck, waiting, fresh, "https://unsupported.test/1"], history, opened)
    out = capsys.readouterr().out
    assert opened == [fresh]
    assert "QUEUE 1" in out and "Of 4 job(s) lined up" in out
    by_url = {r["url"]: r for r in logged}
    assert by_url[stuck]["status"] == "skipped" and "same way" in by_url[stuck]["reason"]
    assert by_url[waiting]["status"] == "needs_answer" and by_url[waiting]["unanswered"] == "Describe your best personal project"
    assert by_url[fresh]["status"] == "submitted"


def test_a_question_the_profile_now_answers_lets_the_job_be_tried_again(monkeypatch, tmp_path):
    job = "https://a.test/canonical"
    history = [row("needs_answer", "required question(s) with no honest answer", "In which country do you currently work?*", url=job)]
    opened = []
    _engine(monkeypatch, tmp_path, [job], history, opened)
    assert opened == [job]


# ----------------------------------------------------------------- two runs at once
def test_a_link_is_claimed_before_its_page_is_opened(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.config, "DATA_DIR", tmp_path)
    url, job = "https://jobs.ashbyhq.com/acme/1", {"company": "Acme", "title": "Software Engineer"}
    assert engine.claim_job(url, {})                         # before opening the page
    assert not engine.claim_job(url, {})                     # another run can't open it now
    assert engine.claim_job(url, job, True)                  # after reading it, this run adds the ad itself
    assert not engine.claim_job("https://other.test/2", job)  # the same ad on another link is taken too
    engine.release_job(url, job)
    assert not list((tmp_path / "claims").iterdir())
    import inspect
    assert "claim_job(url, {})" in inspect.getsource(engine.cmd_apply) and "claim_job(url, job, url_claimed)" in inspect.getsource(engine.cmd_apply)


# ----------------------------------------------------------------- answers from the profile
def test_age_is_answered_only_when_the_profile_makes_it_certain():
    q = Field("buttons", "Are you over the age of 18?", ["Yes", "No"], True)
    assert rule_answer(q, ME) == "Yes"
    assert rule_answer(Field("checkbox", "I confirm I am at least 18 years old", [], True), ME) is True
    unknown = {**ME, "education": "BS Information Technology (expected 2027)"}
    assert rule_answer(q, unknown) is None
    assert rule_answer(q, {**unknown, "birth_date": "2000-02-29"}) == "Yes"
    assert rule_answer(q, {**unknown, "birth_date": "2015-01-01"}) is None
    # Mixed with work rights: that's for the work-rights rules, not the age rule.
    assert rule_answer(Field("radio", "Are you 18 or older and authorized to work in the United States?", ["Yes", "No"]), ME) != "Yes"


def test_is_adult_reads_lists_of_degrees_too():
    assert is_adult({"education": [{"degree": "Bachelor of Science in IT", "honors": "Cum Laude", "year": "2026"}]})
    assert not is_adult({"education": "Senior High School"}) and not is_adult({})


def test_country_where_you_work_comes_from_the_profile():
    countries = ["Afghanistan", "Lebanon", "Norway", "Philippines"]
    f = Field("combobox", "In which country do you currently work?*", countries, True)
    assert rule_answer(f, ME, {"company": "Acme"}) == "Philippines"
    assert asyncio.run(answers.decide(f, ME, _Memory(), {"company": "Acme"})) == "Philippines"
    assert rule_answer(Field("text", "Country of residence", []), ME) == "Philippines"


def test_previously_worked_here_still_works_but_currently_employed_isnt_a_guess():
    assert rule_answer(Field("radio", "Have you previously worked for Acme?", ["Yes", "No"]), ME, {"company": "Acme"}) == "No"
    assert rule_answer(Field("radio", "Are you currently employed?", ["Yes", "No"]), ME, {"company": "Acme"}) is None


def test_a_question_that_mentions_a_degree_isnt_answered_with_your_degree():
    q = "In the past ten years, looking only at the time since you graduated your first undergraduate degree, how many companies have you worked for?*"
    assert rule_answer(Field("text", q, []), ME) is None
    assert rule_answer(Field("text", "Highest degree", []), ME) == ME["education"]


def test_no_doesnt_pick_lebanon():
    assert choose_option(["Afghanistan", "Lebanon", "Norway"], "No") is None
    assert choose_option(["Philippines +63", "Lebanon +961"], "Philippines") == "Philippines +63"
    assert choose_option(["Bachelor's Degree (BS)", "Master's"], "Bachelor's Degree") == "Bachelor's Degree (BS)"


@pytest.mark.parametrize("kind,options,expected", [
    ("combobox", ["Acknowledge/Confirm"], "Acknowledge/Confirm"),
    ("radio", ["I do not agree", "I agree"], "I agree"),
    ("checkbox", [], True),
])
def test_privacy_notices_are_acknowledged(kind, options, expected):
    q = "Please confirm that you have read and agree to Acme's Recruitment Privacy Notice and Privacy Policy.*"
    assert rule_answer(Field(kind, q, options, True), ME) == expected


def test_promises_about_yourself_are_yours_to_make(tmp_path, monkeypatch):
    pledge = "During this application process I agree to use only my own words. I understand that plagiarism, the use of AI or other generated content will disqualify my application.*"
    assert rule_answer(Field("combobox", pledge, ["Yes", "No"], True), ME) is None
    assert rule_answer(Field("checkbox", pledge, [], True), ME) is None
    assert not rule_answer(Field("checkbox", "I confirm I have 5 years of experience with React", [], True), ME)
    # An AI guess saved earlier for it isn't used either.
    path = tmp_path / "answers.json"
    key = answers.norm(pledge)
    path.write_text(json.dumps({key: "Yes", "_sources": {key: "ai"}}), encoding="utf-8")
    monkeypatch.setattr(answers, "ANSWERS_PATH", path)
    memory = AnswerMemory()
    assert memory.get(Field("combobox", pledge, ["Yes", "No"])) is None and not memory.has(pledge)
    path.write_text(json.dumps({key: "Yes", "_sources": {}}), encoding="utf-8")       # your own answer is
    assert AnswerMemory().has(pledge)


class _Memory:
    def get(self, _f):
        return None


def test_identity_questions_still_arent_answered():
    assert rule_answer(Field("combobox", "Which gender do you identify as?*", ["Male", "Female"], True), ME) is None
    assert rule_answer(Field("combobox", "Please indicate your race or ethnicity:*", ["Asian", "White"], True), ME) is None
    assert rule_answer(Field("checkbox", "Southeast Asian (including Filipino)", [], False), ME) is SKIP


# ----------------------------------------------------------------- the form engine
def test_ashbys_success_message_counts_as_sent():
    assert SUCCESS_RE.search("Success\nYour application was successfully submitted.")
    assert SUCCESS_RE.search("We've successfully received your application")
    assert not SUCCESS_RE.search("Submit Application")


def test_a_named_missing_field_becomes_a_question():
    errors = ["Your form needs corrections\n\nMissing entry for required field: Are you over the age of 18?"]
    assert questions_from_errors(errors) == ["Are you over the age of 18?"]


ASHBY_LIKE = """
<div class="ashby-application-form-field-entry"><label class="question-title _required_x" for="n">Name</label><input id="n" name="name" required></div>
<div class="ashby-application-form-field-entry"><label class="question-title _required_x" for="age">Are you over the age of 18?</label>
  <div class="_yesno"><button type="button" aria-pressed="false" onclick="press(this)">Yes</button><button type="button" aria-pressed="false" onclick="press(this)">No</button>
  <input type="checkbox" tabindex="-1" name="age" style="display:none"></div></div>
<div class="toolbar"><button type="button" aria-pressed="false" aria-label="Bold"><b></b></button><button type="button" aria-pressed="false" aria-label="Italic"><i></i></button></div>
<button type="button" id="send" onclick="send()">Submit Application</button>
<script>
function press(b){ [...b.parentElement.querySelectorAll('button')].forEach(x => x.setAttribute('aria-pressed', 'false')); b.setAttribute('aria-pressed', 'true'); window.age = b.innerText; }
function send(){ if (!window.age) { document.getElementById('err')?.remove();
    document.body.insertAdjacentHTML('afterbegin', '<div id="err" role="alert">Missing entry for required field: Are you over the age of 18?</div>'); return; }
  window.sent = {name: document.getElementById('n').value, age: window.age}; document.body.innerHTML = window.done || '<p>Your application was successfully submitted.</p>'; }
</script>"""


def _run_form(tmp_path, monkeypatch, profile, done=None):
    from playwright.async_api import async_playwright
    from vera.apply.form_engine import run_application

    async def none(*_a, **_k):
        return None
    monkeypatch.setattr(answers, "ai_answer", none)
    monkeypatch.setattr(answers, "ANSWERS_PATH", tmp_path / "answers.json")
    html = tmp_path / "ashby.html"
    html.write_text(ASHBY_LIKE.replace("window.done ||", f"{json.dumps(done)} ||" if done else "window.done ||"), encoding="utf-8")

    async def go():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(html.as_uri())
            result = await run_application(page, profile, AnswerMemory(), {"title": "Software Engineer", "company": "Acme"})
            sent = await page.evaluate("window.sent")
            await browser.close()
            return result, sent
    return asyncio.run(go())


def test_yes_no_buttons_are_pressed_and_the_application_goes(tmp_path, monkeypatch):
    result, sent = _run_form(tmp_path, monkeypatch, ME)
    assert result.status == "submitted", result
    assert sent == {"name": "Juan Cruz", "age": "Yes"}


def test_without_a_certain_age_it_asks_you_instead_of_looping(tmp_path, monkeypatch):
    result, sent = _run_form(tmp_path, monkeypatch, {**ME, "education": ""})
    assert result.status == "needs_answer" and result.unanswered == ["Are you over the age of 18?"] and not sent


def test_an_unrecognised_thank_you_after_submit_is_never_retried(tmp_path, monkeypatch):
    result, sent = _run_form(tmp_path, monkeypatch, ME, done="<p>All done, talk soon!</p>")
    assert sent and result.status == "failed" and SUBMIT_UNCONFIRMED in result.reason
    assert engine.held_back([row("failed", result.reason)], always)[0] == "skipped"


def test_a_cookie_banner_doesnt_hide_the_apply_button(tmp_path):
    from playwright.async_api import async_playwright
    from vera.apply.sites import ATS
    html = tmp_path / "workable.html"
    html.write_text("""<h1>Full Stack Developer</h1><button onclick="document.getElementById('f').style.display='block'">Apply now</button>
      <form id="f" style="display:none"><input name="firstname"><input type="email"></form>
      <div id="c" style="position:fixed;inset:0;background:rgba(0,0,0,.5)"><button onclick="document.getElementById('c').remove()">Decline all</button></div>""",
                    encoding="utf-8")

    async def go():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(html.as_uri())
            result = await ATS().open_apply(page)
            await browser.close()
            return result
    assert asyncio.run(go()) is None


# ----------------------------------------------------------------- wider company-form sources
def test_company_forms_include_workable_and_lever_jobs_near_you():
    from vera.sources.extra import CompanyForms, _lists, _near_home
    _lists.clear()

    def handler(request):
        if request.url.host == "jobs.workable.com":
            assert request.url.params["location"] == "Philippines"
            return httpx.Response(200, json={"jobs": [
                {"id": "w1", "title": "Junior Web Developer", "url": "https://jobs.workable.com/view/w1/x", "locations": ["TELECOMMUTE", "Philippines"],
                 "workplace": "remote", "company": {"title": "Kasama"}, "description": "<p>Build sites</p>", "created": "2026-11-01"},
                {"id": "w2", "title": "Web Developer", "url": "https://jobs.workable.com/view/w2/x", "locations": ["Pasig, Metro Manila, Philippines"],
                 "workplace": "on_site", "company": {"title": "Questronix"}, "description": "", "created": "2026-11-01"},
                {"id": "w3", "title": "Accountant", "url": "https://jobs.workable.com/view/w3/x", "locations": ["Philippines"],
                 "workplace": "on_site", "company": {"title": "X"}, "description": "", "created": "2026-11-01"}]})
        if request.url.host == "api.lever.co" and "coins" in request.url.path:
            return httpx.Response(200, json=[{"id": "l1", "text": "Web Developer", "hostedUrl": "https://jobs.lever.co/coins/l1", "workplaceType": "hybrid",
                                              "categories": {"location": "Taguig City", "allLocations": ["Taguig City"]}, "createdAt": 1793843386000, "descriptionPlain": "x"}])
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await CompanyForms().fetch(c, "Web Developer", "Philippines")
    jobs = asyncio.run(go())
    assert sorted(j["url"] for j in jobs) == ["https://jobs.lever.co/coins/l1", "https://jobs.workable.com/view/w1/x", "https://jobs.workable.com/view/w2/x"]
    w1 = next(j for j in jobs if j["url"].endswith("w1/x"))
    assert w1["company"] == "Kasama" and w1["location"] == "Remote, Philippines" and w1["remote"] == 1 and w1["description"] == "Build sites"
    assert _near_home("Hybrid Taguig City", "Philippines") and not _near_home("Cebu City", "Philippines") and not _near_home("Taguig", "")


def test_workable_search_pages_are_a_form_she_can_fill():
    from vera.apply.sites import site_for
    assert site_for("https://jobs.workable.com/view/qrFn/remote-full-stack-developer").name == "company_form"
