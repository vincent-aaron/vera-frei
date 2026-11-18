"""Today's real problems: filling a profile page, tabs side by side, channels interfering, emailing when only asked about it, and stiff chat."""

import asyncio
import csv
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from vera import config
from vera.agents import assistant, rounds, export_parse, exports, outreach
from vera.apply import __main__ as engine
from vera.apply import form_engine
from vera.apply.answers import SKIP, Field, rule_answer
from vera.bot.main import VeraBot
from vera.db import DB
from vera.services import Vera

OWNER, GUEST, HERE, THERE = 111, 222, 1000, 2000


@pytest.fixture
def vera(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESUME_DIR", tmp_path / "resumes")
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    monkeypatch.setattr(config, "OUTREACH_EMAIL", "me@gmail.com")
    monkeypatch.setattr(config, "OUTREACH_APP_PASSWORD", "x")
    for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.setattr(config, key, "")
    monkeypatch.setattr(rounds, "_active", [])
    monkeypatch.setattr(rounds, "_waiting", [])
    return Vera(DB(tmp_path / "t.db"))


@pytest.fixture
def me(vera, tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF")
    s = vera.user(OWNER)
    s.update_profile({"name": "Juan Cruz", "email": "j@example.com", "phone": "0900", "location": "Manila, Philippines", "resumes": [{"name": "CV", "path": str(cv), "tags": []}],
                      "application": {"expected_salary_monthly": 28000, "notice_period": "2 weeks", "work_authorized": True}})
    s.update_prefs({"positions": ["Software Developer"], "reject_mandatory_experience_over": 1})
    return s


def say(session, text, channel_id=None, channel_name=None, sites=None):
    return asyncio.run(assistant.handle(session, text, sites, channel_id, channel_name))


# ----------------------------------------------------------------- never fill anything but an application
@pytest.mark.parametrize("url", ["https://jobs.example.com/profile/edit", "https://jobs.example.com/profile/edit/overview", "https://jobs.example.ph/profile/me", "https://x.com/settings", "https://x.com/account/details?a=1"])
def test_profile_pages_are_never_filled(url):
    assert form_engine.not_an_application(url)


@pytest.mark.parametrize("url", ["https://jobs.example.ph/job/95/apply/profile", "https://jobs.example.ph/job/95/apply/review",
                                 "https://job-boards.greenhouse.io/acme/jobs/1", "https://jobs.lever.co/acme/1/apply", "https://jobs.ashbyhq.com/acme/1/application"])
def test_real_application_forms_are_still_filled(url):
    assert not form_engine.not_an_application(url)


def test_she_stops_and_touches_nothing_on_a_profile_page():
    class Page:
        url = "https://jobs.example.com/profile/edit"

        async def wait_for_load_state(self, _s):
            return None

        def locator(self, _s):
            raise AssertionError("she read the page instead of stopping")
    original = asyncio.sleep

    async def fast(_s):
        await original(0)
    result = asyncio.run(_with_fast_sleep(form_engine.run_application(Page(), {}, None, {}), fast))
    assert result.status == "skipped" and "profile page" in result.reason


async def _with_fast_sleep(coroutine, fast):
    real = form_engine.asyncio.sleep
    form_engine.asyncio.sleep = fast
    try:
        return await coroutine
    finally:
        form_engine.asyncio.sleep = real


@pytest.mark.parametrize("label", ["South Asian (including Bangladeshi, Bhutanese, Indian)", "Southeast Asian (including Filipino)", "Black / African-American", "Hispanic or Latino/a/x",
                                   "White", "Middle Eastern", "Self-describe", "Pacific Islander", "Native American or Alaskan Native"])
def test_identity_boxes_are_never_ticked(label):
    field = Field(kind="checkbox", question=label, options=[], required=False)
    assert rule_answer(field, {"first_name": "Juan"}) is SKIP


def test_only_sources_that_need_no_login():
    from vera import preferences
    assert "sites" not in preferences.defaults()["rounds"]
    assert {s for sites in config.CHANNEL_SITES.values() for s in sites} <= {"remote_boards", "company_forms"}


# ----------------------------------------------------------------- several tabs at once
class Tab:
    def __init__(self, browser):
        self.browser, self.url, self.handlers, self.closed = browser, "", [], False

    def on(self, _event, handler):
        self.handlers.append(handler)

    def opened(self, tab):
        for handler in self.handlers:
            handler(tab)

    async def goto(self, url, **_):
        self.url = url
        self.browser.note()

    async def close(self):
        self.closed = True
        self.browser.pages.remove(self)

    async def screenshot(self, **_):
        pass


class Browser:
    def __init__(self):
        self.pages = [Tab(self)]
        self.peak, self.closed, self.by_host = 0, False, {}
        self.host_peak = {}

    def note(self):
        self.peak = max(self.peak, len(self.pages))
        now = {}
        for p in self.pages:
            host = p.url.split("/")[2] if "://" in p.url else ""
            now[host] = now.get(host, 0) + 1
        for host, n in now.items():
            self.host_peak[host] = max(self.host_peak.get(host, 0), n)

    async def new_page(self):
        tab = Tab(self)
        self.pages.append(tab)
        return tab

    async def close(self):
        self.closed = True


def run_engine(monkeypatch, tmp_path, urls, tabs, per_site, budget=99, fill=None):
    import playwright.async_api as pa
    browser = Browser()
    base = browser.pages[0]
    logged = []

    class PW:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_):
            return False

    class Site:
        def __init__(self, name):
            self.name = name

        async def job_info(self, page):
            await asyncio.sleep(0)
            return {"title": f"Dev {page.url[-3:]}", "company": page.url, "location": "", "description": ""}

        async def open_apply(self, page):
            tab = await browser.new_page()
            page.opened(tab)
            tab.url = page.url + "/apply"
            return tab

    async def default_fill(work, *_a, **_k):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return SimpleNamespace(status="submitted", reason="ok", unanswered=[])

    async def nothing(*_a, **_k):
        await original_sleep(0)

    async def open_browser(*_a, **_k):
        return browser
    original_sleep = asyncio.sleep
    monkeypatch.setattr(pa, "async_playwright", lambda: PW())
    monkeypatch.setattr(engine, "open_browser", open_browser)
    monkeypatch.setattr(engine, "site_for", lambda url: Site(url.split("/")[2]))
    monkeypatch.setattr(engine, "block_key", lambda site, _url: site.name)
    monkeypatch.setattr(engine, "check_blocked", nothing)
    monkeypatch.setattr(engine, "run_application", fill or default_fill)
    monkeypatch.setattr(engine, "read_log", lambda: [])
    monkeypatch.setattr(engine, "write_log", lambda row: logged.append((row, len(browser.pages))))
    monkeypatch.setattr(engine, "save_leads", lambda _job: None)
    monkeypatch.setattr(engine, "load_state", lambda: {})
    monkeypatch.setattr(engine, "save_state", lambda _s: None)
    monkeypatch.setattr(engine, "AnswerMemory", lambda: None)
    monkeypatch.setattr(engine, "claim_job", lambda *_a: True)
    monkeypatch.setattr(engine, "release_job", lambda *_a: None)
    monkeypatch.setattr(engine.filters, "rejects", lambda *_a: None)
    monkeypatch.setattr(engine.config, "load_profile", lambda: {
        "limits": {"max_per_run": 99, "max_per_day": 99, "delay_seconds": [0, 0], "max_per_company": 0, "tabs": tabs, "tabs_per_site": per_site},
        "allow_job_sites": True, "search": {}})
    monkeypatch.setattr(engine.config, "missing_fields", lambda _p: [])
    monkeypatch.setattr(engine.asyncio, "sleep", nothing)
    queue = tmp_path / "q.json"
    queue.write_text(json.dumps(urls), encoding="utf-8")
    args = SimpleNamespace(queue_file=str(queue), url=None, site=None, max=budget, dry_run=False, headless=True, background=True, debug=False, recommend=False)
    asyncio.run(engine.cmd_apply(args))
    return browser, base, logged


URLS = [f"https://site{(i % 3) + 1}.test/{i:03}" for i in range(30)]


def test_several_tabs_run_side_by_side_and_all_close(monkeypatch, tmp_path):
    browser, base, logged = run_engine(monkeypatch, tmp_path, URLS, tabs=10, per_site=4)
    assert len(logged) == 30
    assert browser.peak > 3                     # really at the same time
    assert browser.pages == [base] and browser.closed      # every tab is gone at the end, then the browser


def test_boards_get_only_their_share_of_tabs(monkeypatch, tmp_path):
    busy = {}
    peak = {}

    async def fill(work, *_a, **_k):
        host = work.url.split("/")[2]
        busy[host] = busy.get(host, 0) + 1
        peak[host] = max(peak.get(host, 0), busy[host])
        for _ in range(3):
            await asyncio.sleep(0)
        busy[host] -= 1
        return SimpleNamespace(status="submitted", reason="ok", unanswered=[])
    run_engine(monkeypatch, tmp_path, URLS, tabs=10, per_site=2, fill=fill)
    assert peak and max(peak.values()) <= 2


def test_it_never_sends_more_than_you_asked_for(monkeypatch, tmp_path):
    _b, _base, logged = run_engine(monkeypatch, tmp_path, URLS, tabs=10, per_site=10, budget=5)
    assert sum(r["status"] == "submitted" for r, _n in logged) == 5


def test_finishing_one_job_leaves_other_jobs_tabs_open(monkeypatch, tmp_path):
    closed_early = []

    async def fill(work, *_a, **_k):
        for _ in range(4):
            await asyncio.sleep(0)
        if work.closed:
            closed_early.append(work.url)
        return SimpleNamespace(status="submitted", reason="ok", unanswered=[])
    run_engine(monkeypatch, tmp_path, URLS, tabs=10, per_site=10, fill=fill)
    assert not closed_early


def test_lines_from_side_by_side_jobs_cant_mix(monkeypatch, tmp_path, capsys):
    run_engine(monkeypatch, tmp_path, URLS[:9], tabs=9, per_site=9)
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if line.startswith("["):
            assert "where:" in lines[i + 1] and "info:" in lines[i + 2] and "->" in lines[i + 3]


def test_a_rested_board_is_announced(monkeypatch, capsys):
    monkeypatch.setattr(engine, "save_state", lambda _s: None)
    monkeypatch.setattr(engine, "load_state", lambda: {})
    state = {}
    engine.rest(state, "company_form")
    assert f"RESTING company_form {state['company_form']}" in capsys.readouterr().out


def test_you_get_ten_tabs_and_members_get_one(vera, me):
    mine = rounds.build_profile(me)["limits"]
    theirs = rounds.build_profile(vera.user(GUEST))["limits"]
    assert mine["tabs"] == 10 and mine["tabs_per_site"] == 3 and theirs["tabs"] == 1 and theirs["tabs_per_site"] == 1


@pytest.mark.parametrize("text,field,number", [("use 20 tabs", "tabs", 20), ("increase it to 15 tabs", "tabs", 15), ("make it 5 tabs per board", "tabs_per_site", 5), ("set 99 tabs", "tabs", 30)])
def test_changing_tabs_in_chat(me, text, field, number):
    r = say(me, text)
    assert me.prefs["rounds"][field] == number and r["learned"]


def test_members_cant_change_tabs_and_mentioning_tabs_isnt_a_change(vera, me):
    guest = vera.user(GUEST)
    say(guest, "use 20 tabs")
    assert not guest.prefs["rounds"].get("tabs")
    say(me, "you left 3 tabs open")
    assert not me.prefs["rounds"].get("tabs")


# ----------------------------------------------------------------- channels don't interfere
def test_a_plan_belongs_to_the_channel_it_was_made_in(me):
    say(me, "apply to 3 jobs", HERE, "vera-company-site-application", ["company_forms"])
    say(me, "apply to 50 jobs", THERE, "vera-multiboards-application")
    first = say(me, "proceed", HERE, "vera-company-site-application", ["company_forms"])
    assert first["action"] == "start_apply" and first["request"]["sites"] == ["company_forms"] and first["request"]["count"] == 3
    assert say(me, "proceed", THERE, "vera-multiboards-application")["request"]["count"] == 50


def test_proceed_with_no_plan_here_says_so(me):
    r = say(me, "proceed", HERE, "vera-company-site-application")
    assert r["action"] == "none" and "no plan waiting" in r["reply"]


def bot():
    launched = []
    b = SimpleNamespace()
    b._launch = launched.append
    b.launched = launched
    return b


def item(channel_id, uid=OWNER, mention="#x"):
    return {"uid": uid, "dry_run": False, "count": 3, "sites": None, "channel": SimpleNamespace(mention=mention), "channel_id": channel_id}


def copies_ready(monkeypatch):
    real = rounds.browser_folder

    def fake(uid, slot):
        folder = real(uid, slot)
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    monkeypatch.setattr(rounds, "browser_folder", fake)


def test_two_channels_run_side_by_side(vera, monkeypatch):
    copies_ready(monkeypatch)
    b = bot()
    rounds.start_run(item(HERE))
    assert VeraBot.start_or_wait(b, item(THERE)) is None and len(b.launched) == 1


def test_each_side_by_side_run_gets_its_own_browser_and_profile(vera, monkeypatch):
    copies_ready(monkeypatch)
    first, second = rounds.start_run(item(HERE)), rounds.start_run(item(THERE))
    assert first["slot"] != second["slot"]
    assert rounds.browser_folder(OWNER, first["slot"]) != rounds.browser_folder(OWNER, second["slot"])
    env = config.engine_env("x", rounds.browser_folder(OWNER, second["slot"]), "p.json")
    assert env["VERA_BROWSER_DIR"].endswith("browser-2") and env["VERA_PROFILE_PATH"] == "p.json"


def test_a_second_run_in_the_same_channel_waits(vera, monkeypatch):
    copies_ready(monkeypatch)
    b = bot()
    rounds.start_run(item(HERE))
    reply = VeraBot.start_or_wait(b, item(HERE))
    assert "in line" in reply and "this channel" in reply and not b.launched and rounds.waiting_for(OWNER, HERE)


def test_too_many_runs_wait_and_dont_name_other_peoples_channels(vera, monkeypatch):
    copies_ready(monkeypatch)
    monkeypatch.setattr(rounds, "MAX_RUNS", 1)
    b = bot()
    rounds.start_run(item(HERE, uid=GUEST, mention="#their-channel"))
    reply = VeraBot.start_or_wait(b, item(THERE))
    assert "in line" in reply and "#their-channel" not in reply


def test_members_browsers_never_copy_yours(vera):
    assert rounds.browser_folder(GUEST, 2) != rounds.browser_folder(OWNER, 2)
    assert rounds.user_dir(OWNER) not in rounds.browser_folder(GUEST, 2).parents


def test_two_runs_cant_apply_to_the_same_job(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.config, "DATA_DIR", tmp_path)
    job = {"company": "Acme", "title": "Web Developer"}
    assert engine.claim_job("https://x/1", job)
    assert not engine.claim_job("https://x/1", job)                                   # same link
    assert not engine.claim_job("https://x/2", job)                                   # same ad on another link
    assert engine.claim_job("https://x/3", {"company": "Acme", "title": "Backend Developer"})
    engine.release_job("https://x/1", job)
    assert engine.claim_job("https://x/1", job)                                       # not sent, so it can be tried again


def test_a_failed_claim_leaves_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.config, "DATA_DIR", tmp_path)
    assert engine.claim_job("https://x/1", {"company": "Acme", "title": "Web Developer"})
    assert not engine.claim_job("https://x/9", {"company": "Acme", "title": "Web Developer"})
    assert engine.claim_job("https://x/9", {"company": "Other", "title": "Designer"})


def stop(monkeypatch, run_channel, asked_in, text="stop", waiting=(), also=()):
    stopped = []
    monkeypatch.setattr(rounds, "stop_run", lambda run=None: stopped.append(run["channel_id"]))
    monkeypatch.setattr(rounds, "_active", [item(run_channel, mention="#the-other-channel"), *also])
    for w in waiting:
        rounds.enqueue(w)
    return VeraBot.stop_for(None, OWNER, asked_in, text), stopped


def test_stop_ends_only_this_channels_run(vera, monkeypatch):
    reply, stopped = stop(monkeypatch, HERE, HERE, also=[item(THERE)])
    assert stopped == [HERE] and "Stopping" in reply


def test_stop_elsewhere_leaves_the_run_alone_and_says_where_it_is(vera, monkeypatch):
    reply, stopped = stop(monkeypatch, THERE, HERE)
    assert not stopped and "#the-other-channel" in reply and "stop all" in reply


def test_stop_cancels_only_the_run_waiting_here(vera, monkeypatch):
    reply, stopped = stop(monkeypatch, THERE, HERE, waiting=[item(HERE)])
    assert not stopped and "cancelled" in reply and not rounds.waiting_for(OWNER, HERE)


def test_stop_all_ends_everything_of_yours(vera, monkeypatch):
    reply, stopped = stop(monkeypatch, THERE, HERE, "stop all", waiting=[item(HERE), item(THERE)], also=[item(HERE)])
    assert sorted(stopped) == sorted([THERE, HERE]) and not rounds.waiting_for(OWNER)


def test_someone_elses_stop_cant_touch_your_run(vera, monkeypatch):
    stopped = []
    monkeypatch.setattr(rounds, "_active", [{"uid": OWNER, "channel_id": HERE}])
    monkeypatch.setattr(rounds, "stop_run", lambda run=None: stopped.append(True))
    assert say(vera.user(GUEST), "stop", HERE, "vera-company-site-application")["action"] == "none" and not stopped


def test_the_next_waiting_run_starts_by_itself(vera):
    started = []

    class B:
        async def run_rounds(self, *_a, **_k):
            return None

        def _launch(self, next_item):
            started.append(next_item)
            rounds.start_run(next_item)
    first = rounds.start_run(item(HERE))
    rounds.enqueue({**item(HERE), "channel": None})
    asyncio.run(VeraBot._run_then_next(B(), first))
    assert started and started[0]["channel_id"] == HERE


def test_results_stay_in_the_channel_you_asked_in():
    import pathlib
    source = (pathlib.Path(config.ROOT) / "vera/bot/main.py").read_text(encoding="utf-8")
    assert "ch = message.channel      # results stay where you asked" in source


def test_asking_for_a_cover_letter_shows_a_real_one(me):
    folder = rounds.user_dir(OWNER) / "cover_letters"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "20261104_Acme_Software_Developer.txt").write_text("Dear Acme team, real letter.", encoding="utf-8")
    (folder / "20261105_Beta_Web_Designer.txt").write_text("Dear Beta team, other letter.", encoding="utf-8")
    r = say(me, "show me the cover letter you use on job applications", HERE, "vera-company-site-application")
    assert r["action"] == "none" and "really used" in r["reply"] and "Dear" in r["reply"]
    assert "Dear Acme" in say(me, "show me a sample cover letter for software developer", HERE, "vera-company-site-application")["reply"]


def test_no_letters_yet_says_so(vera):
    assert "haven't written" in say(vera.user(GUEST), "show me a sample cover letter")["reply"]


# ----------------------------------------------------------------- your email rule
OUT = "vera-social-outreach"


@pytest.mark.parametrize("text", ["can you send emails for me?", "can you send email", "i just want to ask you not make you if you could send email not actually send make sure you only send email when ask on vera job mail channel and vera social outreach"])
def test_asking_about_email_gets_an_answer_not_drafts(me, text):
    for channel in (OUT, "vera-self-improvemnt"):
        r = say(me, text, HERE, channel)
        assert r["action"] == "none" and "Yes, I can" in r["reply"] and "proceed" in r["reply"]
        assert me.db.get_json(f"outreach_pending:{OWNER}") is None


def test_email_only_works_in_the_two_channels_you_named(me):
    for channel in ("vera-self-improvemnt", "vera-multiboards-application", "vera-owner"):
        r = say(me, "email hr@acme.ph about openings", HERE, channel)
        assert "I only send email from" in r["reply"] and me.db.get_json(f"outreach_pending:{OWNER}") is None
    for channel in (OUT, "vera-job-mail"):
        r = say(me, "email hr@acme.ph about openings", HERE, channel)
        assert me.db.get_json(f"outreach_pending:{OWNER}"), channel
        me.db.set_json(f"outreach_pending:{OWNER}", None)


def test_drafts_cant_be_sent_from_another_channel(me):
    me.db.set_json(f"outreach_pending:{OWNER}", [{"email": "hr@acme.ph", "subject": "s", "body": "b"}])
    assert say(me, "proceed", HERE, "vera-multiboards-application")["action"] != "send_outreach"
    assert say(me, "proceed", HERE, OUT)["action"] == "send_outreach"


def test_a_real_request_with_a_number_or_address_still_works(me):
    assert not assistant.asking_about_email("can you cold email 3 companies", [])
    assert not assistant.asking_about_email("can you email hr@acme.ph", ["hr@acme.ph"])


@pytest.mark.parametrize("address", ["eeocompliance@spacex.com", "compliance@x.com", "accommodations@x.com", "press@x.com", "privacy@x.com"])
def test_addresses_that_arent_for_hiring_are_dropped(address):
    assert not outreach.usable(address)


def test_jobs_you_would_never_apply_to_arent_emailed(vera, me):
    me.update_prefs({"job_levels": ["Junior"]})
    vera.add_manual_job("https://x/1", "Senior Backend Software Engineer", "KSC", description="Email hiring@ksc.com")
    vera.add_manual_job("https://x/2", "Software Developer", "Good Co", description="Email hiring@goodco.com")
    found = {l["email"] for l in outreach.find_leads(me)}
    assert "hiring@goodco.com" in found and "hiring@ksc.com" not in found


def test_a_job_asking_for_email_is_mentioned_and_nothing_is_sent(vera, me):
    folder = rounds.user_dir(OWNER)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "leads.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["email", "company", "title", "url"])
        w.writeheader()
        w.writerow({"email": "hr@acme.ph", "company": "Acme", "title": "Web Dev", "url": "https://x/1"})
        w.writerow({"email": "hr@sent.ph", "company": "Sent", "title": "Web Dev", "url": "https://x/2"})
    rows = [{"status": "skipped", "url": "https://x/1"}, {"status": "submitted", "url": "https://x/2", "title": "Web Dev", "company": "Sent", "site": "company_form"}]
    jobs = rounds.jobs_asking_for_email(OWNER, rows)
    assert [j["email"] for j in jobs] == ["hr@acme.ph"]
    text = rounds.report({"rows": rows, "dry_run": False, "email_jobs": jobs})
    assert "I didn't email anyone" in text and "hr@acme.ph" in text


# ----------------------------------------------------------------- less stiff, and honest
def resting(uid, **boards):
    folder = rounds.user_dir(uid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(json.dumps({"_spec": "x", **boards}), encoding="utf-8")


def soon(hours=3):
    return (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")


def test_the_plan_warns_you_before_you_start(me):
    resting(OWNER, **{"himalayas.app": soon(), "company_form": soon(1), "remotive.com": "2020-01-01T00:00:00"})
    reply = say(me, "apply to 5 jobs")["reply"]
    assert "Resting after a bot check" in reply and "himalayas.app" in reply and "company form" in reply and "remotive.com until" not in reply


@pytest.mark.parametrize("text", ["how's it going?", "hows the application going", "how is the remote application going", "progress?", "are you done"])
def test_asking_how_its_going_gets_the_truth(me, monkeypatch, text):
    resting(OWNER, **{"himalayas.app": soon()})
    r = say(me, text)
    assert "Nothing is running" in r["reply"] and "himalayas.app" in r["reply"]


def test_how_many_gets_just_the_number_here(vera, me):
    folder = rounds.user_dir(OWNER)
    folder.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    with open(folder / "applications.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["time", "site", "status", "title", "company", "url"])
        w.writeheader()
        for i in range(3):
            w.writerow({"time": f"{today}T10:00:00", "site": "company_form", "status": "submitted", "title": "Dev", "company": f"C{i}", "url": f"https://x/{i}"})
    for text in ("how many jobs i applied today", "job number like how many jobs i sent my application today"):
        plan = export_parse.plan(text)
        assert plan["count"] and plan["formats"] == [] and plan["since"] == today, text
        assert exports.render_chat(exports.narrow(exports.collect(me), plan), plan)[0].startswith("**3** applications sent for")


def test_a_table_is_told_here_and_only_a_named_file_goes_private(vera, me):
    sent = []

    class Here:
        guild = SimpleNamespace()

        async def send(self, text, **kw):
            sent.append((text, kw))
    folder = rounds.user_dir(OWNER)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "applications.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["time", "site", "status", "title", "company", "url"])
        w.writeheader()
        w.writerow({"time": "2026-11-05T10:00:00", "site": "company_form", "status": "submitted", "title": "Dev", "company": "Acme", "url": "https://x/1"})
    plan = export_parse.plan("make a table of my applications")
    assert plan["chat"] == "table" and plan["formats"] == []
    b = SimpleNamespace(vera=vera, user=SimpleNamespace(id=999))
    asyncio.run(VeraBot.send_export(b, SimpleNamespace(id=OWNER), plan["formats"], Here(), plan))
    assert any("Acme" in text for text, _k in sent) and not any(k.get("files") for _t, k in sent)


def test_a_go_ahead_with_extras_never_lets_the_ai_claim_it_started(me, monkeypatch):
    async def lying_ai(prompt, **_k):
        return "Got it, starting the application run now!"
    say(me, "apply to 3 jobs", HERE, "vera-company-site-application", ["company_forms"])
    monkeypatch.setattr(assistant.ai, "complete", lying_ai)
    r = say(me, "proceed if youre block again tell me", HERE, "vera-company-site-application", ["company_forms"])
    assert r["action"] == "none" and "haven't started" in r["reply"] and "starting the application" not in r["reply"]


def test_she_remembers_the_conversation_per_channel(me, monkeypatch):
    prompts = []

    async def fake_ai(prompt, **_k):
        prompts.append(prompt)
        return "Sure thing."
    monkeypatch.setattr(assistant.ai, "complete", fake_ai)
    say(me, "tell me about interviews", HERE, "vera-company-site-application")
    say(me, "no i mean the second one", HERE, "vera-company-site-application")
    assert "tell me about interviews" in prompts[-1]
    say(me, "something else entirely", THERE, "vera-remote")
    assert "tell me about interviews" not in prompts[-1]


def test_she_knows_your_real_numbers(me):
    facts = assistant._live_facts(me)
    assert facts["applications_sent_total"] == 0 and "Nothing is running" in facts["run_status"]
    assert "right_now" in assistant._context(me) and "cold email" in assistant._context(me)


# ----------------------------------------------------------------- "don't use X"
@pytest.mark.parametrize("text", ["dont use remote boards on this channel", "no dont use workable, exclude it", "not greenhouse"])
def test_ruling_a_source_out(text):
    out = assistant.excluded_sites(text)
    assert out and not set(out) & set(assistant.parse_sites(text) or [])


@pytest.mark.parametrize("text,sites", [("apply on company forms", ["company_forms"]), ("apply on greenhouse and remote boards", ["company_forms", "remote_boards"]),
                                        ("don't use remote boards", None), ("exclude remote boards, use company forms", ["company_forms"])])
def test_naming_a_source_to_rule_it_out_isnt_choosing_it(text, sites):
    assert assistant.parse_sites(text) == sites


def test_leaving_out_a_source_keeps_the_other(me):
    r = say(me, "don't use remote boards, apply to 5 jobs", HERE, "vera-multiboards-application")
    where = next(l for l in r["reply"].split("\n") if "Where:" in l)
    assert "remote boards" not in where and "company forms" in where
    assert say(me, "proceed", HERE, "vera-multiboards-application")["request"]["sites"] == ["company_forms"]


def test_the_rule_stays_in_that_channel_only(me):
    say(me, "don't use remote boards", HERE, "vera-multiboards-application")
    later = say(me, "apply to 5 jobs", HERE, "vera-multiboards-application")["reply"]
    assert "remote boards" not in next(l for l in later.split("\n") if "Where:" in l)
    elsewhere = say(me, "apply to 5 jobs", THERE, "vera-multiboards-application")["reply"]
    assert "remote boards" in next(l for l in elsewhere.split("\n") if "Where:" in l)


def test_you_can_bring_a_source_back(me):
    say(me, "don't use remote boards", HERE, "vera-multiboards-application")
    r = say(me, "use remote boards again", HERE, "vera-multiboards-application")
    assert "again" in r["reply"] and not me.db.get_json(f"channel_skip:{HERE}")


def test_saying_it_with_no_plan_gets_a_clear_answer_and_a_rule_post(me):
    r = say(me, "don't use workable here", HERE, "vera-multiboards-application")
    assert "won't use company forms" in r["reply"] and "company forms" in r["learned"]


def test_a_source_never_becomes_a_blocked_keyword(me, monkeypatch):
    async def fake_interpret(_text, _current):
        return {"patch": {"exclude": {"keywords": ["csr", "workable"], "companies": ["Remotive", "BadCo"]}}, "action": "none", "query": None, "reply": None}
    monkeypatch.setattr(assistant, "interpret", fake_interpret)
    say(me, "apply to 3 jobs and exclude workable", HERE, "vera-multiboards-application")
    exclude = me.prefs["exclude"]
    assert "workable" not in exclude["keywords"] and "Remotive" not in exclude["companies"]


def test_saying_nothing_new_isnt_called_an_update(me):
    say(me, "apply to 5 jobs", HERE, "vera-company-site-application")
    r = say(me, "5", HERE, "vera-company-site-application")
    assert not r["reply"].startswith("Got it, updated")


# ----------------------------------------------------------------- the job list saves opening pages
def _list_job(vera, number, title, location, remote=1):
    from vera.sources.base import Source
    vera.db.upsert_job(Source.job("companyforms", number, title=title, company=f"Co{number}", description="Build web apps.", url=f"https://jobs.ashbyhq.com/co/{number}",
                                  location=location, remote=remote, employment_type="", level="", tags=[], posted_at="2026-11-01"))


def test_jobs_the_list_rules_out_are_never_opened(vera, me, monkeypatch):
    async def nothing(*_a, **_k):
        return {}
    monkeypatch.setattr(rounds.scout, "refresh_sources", nothing)
    me.update_prefs({"sources": ["companyforms"], "job_levels": ["Junior"]})
    _list_job(vera, 1, "Software Developer", "Remote San Francisco")
    _list_job(vera, 2, "Software Developer", "Remote, Global")
    _list_job(vera, 3, "Senior Software Developer", "Remote, Global")
    urls = asyncio.run(rounds.board_urls(me))
    assert urls == ["https://jobs.ashbyhq.com/co/2"]
    known = json.loads((rounds.user_dir(OWNER) / "known_locations.json").read_text(encoding="utf-8"))
    assert known["https://jobs.ashbyhq.com/co/2"] == "Remote, Global"


def test_a_page_with_no_location_uses_the_job_lists(monkeypatch, tmp_path):
    seen = []
    (tmp_path / "known_locations.json").write_text(json.dumps({URLS[0]: "Remote, Global"}), encoding="utf-8")
    monkeypatch.setattr(engine.config, "DATA_DIR", tmp_path)
    real = engine.filters.detect_setup

    def spy(job):
        seen.append(job.get("location"))
        return real(job)
    monkeypatch.setattr(engine.filters, "detect_setup", spy)
    run_engine(monkeypatch, tmp_path, URLS[:1], tabs=1, per_site=1)
    assert seen and seen[0] == "Remote, Global"


def test_leaving_a_source_out_stays_in_that_channel(me):
    r = say(me, "don't use remote boards", HERE, "vera-remote")
    assert "this channel" in r["reply"] and me.db.get_json(f"channel_skip:{HERE}") == ["remote_boards"]
