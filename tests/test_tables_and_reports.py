"""Asking for tables and summaries in your own words, one report per run, and no second application to a reposted ad."""

import asyncio
import csv
from datetime import date
from types import SimpleNamespace

import openpyxl
import pytest

from vera import config
from vera.agents import assistant, rounds, export_parse, exports
from vera.apply.__main__ import company_key, title_key, too_many
from vera.bot.main import VeraBot, split_message
from vera.db import DB
from vera.services import Vera

OWNER, GUEST, BOT = 111, 222, 999
TODAY = date(2026, 11, 5)      # a Thursday
FIELDS = ["time", "site", "url", "title", "company", "status", "reason", "unanswered"]


@pytest.fixture
def vera(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESUME_DIR", tmp_path / "resumes")
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.setattr(config, key, "")
    return Vera(DB(tmp_path / "t.db"))


def history(uid, rows):
    folder = rounds.user_dir(uid)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "applications.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def row(day, company, status="submitted", site="company_form", title="Web Developer", reason=""):
    return {"time": f"{day}T10:00:00", "site": site, "url": f"https://x.test/{company}", "title": title, "company": company, "status": status, "reason": reason, "unanswered": ""}


SAMPLE = [row("2026-10-20", "October Co"), row("2026-11-01", "Last Week Co", site="remote_board"), row("2026-11-03", "Tuesday Co"), row("2026-11-05", "Today Co"),
          row("2026-11-04", "Broken Co", "failed", reason="no button")]


# ----------------------------------------------------------------- the dates
@pytest.mark.parametrize("text,since,until", [
    ("this whole week", "2026-11-02", "2026-11-05"), ("this week", "2026-11-02", "2026-11-05"), ("for the week", "2026-11-02", "2026-11-05"),
    ("last 3 days", "2026-11-03", "2026-11-05"), ("the past three days", "2026-11-03", "2026-11-05"), ("last few days", "2026-11-03", "2026-11-05"),
    ("last week", "2026-10-26", "2026-11-01"), ("past week", "2026-10-30", "2026-11-05"), ("last 2 weeks", "2026-10-23", "2026-11-05"),
    ("today", "2026-11-05", "2026-11-05"), ("yesterday", "2026-11-04", "2026-11-04"), ("kahapon", "2026-11-04", "2026-11-04"), ("2 days ago", "2026-11-03", "2026-11-03"),
    ("this month", "2026-11-01", "2026-11-05"), ("last month", "2026-10-01", "2026-10-31"), ("in october", "2026-10-01", "2026-10-31"), ("november", "2026-11-01", "2026-11-05"),
    ("december", "2025-12-01", "2025-12-31"),      # hasn't happened this year, so it's last year's
    ("Nov 1 to Nov 3", "2026-11-01", "2026-11-03"), ("from october 28 to november 2", "2026-10-28", "2026-11-02"), ("nov 1 - 3", "2026-11-01", "2026-11-03"),
    ("since Nov 2", "2026-11-02", "2026-11-05"), ("on Nov 3", "2026-11-03", "2026-11-03"), ("2026-11-01 to 2026-11-02", "2026-11-01", "2026-11-02"),
])
def test_dates_in_your_own_words(text, since, until):
    found = export_parse.parse_range(text, TODAY)
    assert (found[0], found[1]) == (since, until)


@pytest.mark.parametrize("text", ["my applications", "may you make a table", "make a table", "the job is great"])
def test_no_date_means_everything(text):
    assert export_parse.parse_range(text, TODAY) is None


def test_the_label_says_the_real_dates():
    assert export_parse.parse_range("last 3 days", TODAY)[2] == "Nov 3 to Nov 5, 2026"
    assert export_parse.parse_range("today", TODAY)[2] == "Nov 5, 2026"


# ----------------------------------------------------------------- asking for it
@pytest.mark.parametrize("text,chat,formats,since", [
    ("vera create me a tbale or summary or whaevert for my job application this whole week", "table", [], "2026-11-02"),
    ("create me a summary of my job applications for the last 3 days", "summary", [], "2026-11-03"),
    ("make a table of my applications last 3 days include date link etc", "table", [], "2026-11-03"),
    ("create a sheet of my applicaitons this week", None, ["xlsx"], "2026-11-02"),
    ("give me a pdf of my applications last week", None, ["pdf"], "2026-10-26"),
    ("show my applications today", "table", [], "2026-11-05"),
    ("list my applications", "table", [], None),
    ("what did i apply to this week?", "table", [], "2026-11-02"),
    ("how many applications did i send this week?", "summary", [], "2026-11-02"),
    ("gawa ka ng table ng applications ko kahapon", "table", [], "2026-11-04"),
    ("make a report of my applications", "table", [], None),
])
def test_asking_in_your_own_words(text, chat, formats, since):
    found = export_parse.plan(text, TODAY)
    assert (found["chat"], found["formats"], found["since"]) == (chat, formats, since)


@pytest.mark.parametrize("text", [
    "apply to 5 jobs this week", "send my applications this week", "recommend jobs this week", "what document did you attach to my applications?",
    "make a pdf of my resume", "only remote jobs", "give me a list of jobs", "any replies?", "how many jobs are there?", "how do I make a table?",
])
def test_other_messages_arent_mistaken_for_it(text):
    assert export_parse.plan(text, TODAY) is None


def test_a_summary_of_nothing_in_particular_gets_a_question(vera):
    assert export_parse.plan("give me a summary", TODAY) == "ask"
    assert "applications" in asyncio.run(assistant.handle(vera.user(OWNER), "give me a summary"))["reply"]


@pytest.mark.parametrize("text,columns", [
    ("make a table of my applications include date link etc", None),      # those are in the table anyway
    ("table of my applications with only company and link", ["title", "company", "link"]),
    ("table of my applications without link", ["date", "title", "company", "setup", "location", "board", "status"]),
    ("table of my job applications, no board or status this week", ["date", "title", "company", "setup", "location", "link"]),
    ("only date and link in a table of my applications", ["date", "title", "link"]),   # the job always stays, or the table means nothing
])
def test_choosing_columns(text, columns):
    assert export_parse.plan(text, TODAY)["columns"] == columns


def test_filters(vera):
    found = export_parse.plan("table of my applications from company forms, only interviews", TODAY)
    assert found["boards"] == ["Company form"] and found["statuses"] == ["Interview"]
    assert export_parse.plan("table of my failed applications", TODAY)["section"] == "unfinished"
    assert export_parse.plan("table of the applications i sent", TODAY)["section"] == "sent"


def test_chat_passes_the_details_to_the_bot(vera):
    r = asyncio.run(assistant.handle(vera.user(OWNER), "make a table of my applications last 3 days"))
    assert r["action"] == "send_export" and r["request"]["options"]["chat"] == "table" and r["request"]["options"]["since"]
    assert "together for" in r["reply"]


# ----------------------------------------------------------------- what comes out
OPTIONS = {"since": "2026-11-02", "until": "2026-11-05", "label": "Nov 2 to Nov 5, 2026"}


def test_only_the_dates_you_asked_for_are_included(vera):
    history(OWNER, SAMPLE)
    rows = exports.narrow(exports.collect(vera.user(OWNER)), OPTIONS)
    assert sorted(r["company"] for r in rows["sent"]) == ["Today Co", "Tuesday Co"]
    assert [r["company"] for r in rows["unfinished"]] == ["Broken Co"]


def test_filters_narrow_the_list(vera):
    history(OWNER, SAMPLE)
    everything = exports.collect(vera.user(OWNER))
    assert [r["company"] for r in exports.narrow(everything, {"boards": ["Remote board"]})["sent"]] == ["Last Week Co"]
    assert not exports.narrow(everything, {"section": "unfinished"})["sent"]
    assert not exports.narrow(everything, {"section": "sent"})["unfinished"]


def test_the_sheet_has_only_the_columns_you_chose(vera, tmp_path):
    history(OWNER, SAMPLE)
    options = {**OPTIONS, "columns": ["title", "company", "link"]}
    rows = exports.narrow(exports.collect(vera.user(OWNER)), options)
    path = exports.write_all(rows, ["xlsx", "docx", "pdf"], tmp_path / "out", options)[0]
    assert "2026-11-02_to_2026-11-05" in path.name
    book = openpyxl.load_workbook(path)
    assert [c.value for c in book["Applications"][1]] == ["Job", "Company", "Link"]
    assert [c.value for c in book["Not finished"][1]] == ["Job", "Company", "Why"]


def test_a_table_with_links_has_clickable_links_and_no_previews(vera):
    history(OWNER, SAMPLE)
    rows = exports.narrow(exports.collect(vera.user(OWNER)), OPTIONS)
    text = "\n".join(exports.render_chat(rows, {**OPTIONS, "chat": "table"}))
    assert "Nov 2 to Nov 5, 2026" in text and "<https://x.test/Today Co>" in text and "October Co" not in text and "Not finished" in text


def test_a_table_without_links_lines_up_in_a_block(vera):
    history(OWNER, SAMPLE)
    rows = exports.narrow(exports.collect(vera.user(OWNER)), OPTIONS)
    messages = exports.render_chat(rows, {**OPTIONS, "chat": "table", "columns": ["date", "title", "company"]})
    block = next(m for m in messages if m.startswith("```"))
    assert block.endswith("```") and "Date" in block and "Today Co" in block and "https" not in block


def test_a_summary_counts_things(vera):
    history(OWNER, SAMPLE)
    rows = exports.collect(vera.user(OWNER))
    text = "\n".join(exports.render_chat(rows, {"chat": "summary"}))
    assert "By status" in text and "Company form: **3**" in text and "Remote board: **1**" in text and "2026-11-05: **1**" in text


def test_a_long_table_splits_cleanly():
    many = {"sent": [{"date": "2026-11-05", "title": f"Job {i}", "company": "Acme", "board": "Remote board", "status": "Sent", "link": f"https://x.test/{i}", "note": ""} for i in range(300)], "unfinished": []}
    for options in ({"chat": "table"}, {"chat": "table", "columns": ["date", "title"]}):
        messages = exports.render_chat(many, options)
        assert len(messages) > 1 and all(len(m) <= 2000 for m in messages)
        if "columns" in options:
            assert all(m.count("```") in (0, 2) for m in messages)     # every block is closed


def test_job_text_cant_ping_or_break_the_table():
    rows = {"sent": [{"date": "2026-11-05", "title": "@everyone **Dev** ```", "company": "`x`", "board": "Remote board", "status": "Sent", "link": "", "note": ""}], "unfinished": []}
    linked, block = exports.render_chat(rows, {"chat": "table"}), exports.render_chat(rows, {"chat": "table", "columns": ["title"]})
    assert "@everyone" not in "\n".join(linked) and "**Dev**" not in "\n".join(linked)
    assert all(m.count("```") in (0, 2) for m in block)


class Sink:
    def __init__(self, name):
        self.name, self.sent, self.mention = name, [], f"#{name}"

    async def send(self, text=None, **kw):
        self.sent.append((text, kw))


def fake_bot(vera, files=None, reports=None):
    async def owner_channel(which):
        return {"files": files, "reports": reports}.get(which)
    told = []

    async def tell_user(uid, text):
        told.append(text)
    return SimpleNamespace(vera=vera, owner_channel=owner_channel, user=SimpleNamespace(id=BOT), tell_user=tell_user, told=told)


def public_channel():
    ch = Sink("vera-company-site-application")
    ch.guild = SimpleNamespace(default_role=object())
    ch.category, ch.overwrites = SimpleNamespace(name="vera"), {}
    ch.permissions_for = lambda _r: SimpleNamespace(view_channel=True)
    return ch


def test_your_table_comes_here_and_a_file_stays_private(vera):
    history(OWNER, SAMPLE)
    files, here = Sink("vera-owner-files"), public_channel()
    asyncio.run(VeraBot.send_export(fake_bot(vera, files), SimpleNamespace(id=OWNER), ["xlsx"], here, {**OPTIONS, "chat": "table"}))
    assert any("Today Co" in text for text, _kw in here.sent) and not any(kw.get("files") for _t, kw in here.sent)
    assert files.sent[-1][1]["files"] and "Today Co" not in files.sent[-1][0]


def test_nothing_in_those_dates_says_so(vera):
    history(OWNER, SAMPLE)
    here = Sink("dm")
    here.guild = None
    asyncio.run(VeraBot.send_export(fake_bot(vera), SimpleNamespace(id=OWNER), [], here, {"since": "2026-01-01", "until": "2026-01-02", "label": "Jan 1 to Jan 2, 2026", "chat": "table"}))
    assert "no applications" in here.sent[0][0] and "Jan 1 to Jan 2" in here.sent[0][0]


# ----------------------------------------------------------------- one report per run
def run(vera, monkeypatch, uid, reports, asked, report="THE REPORT"):
    seen = {}

    async def run_once(*_a, **kw):
        seen.update(kw)
        return {"rows": []}
    monkeypatch.setattr(rounds, "run_once", run_once)
    monkeypatch.setattr(rounds, "report", lambda _r: report)
    b = fake_bot(vera, reports=reports)
    asyncio.run(VeraBot.run_rounds(b, uid, channel=asked))
    return seen, b


def lines_during_a_run(vera, monkeypatch, uid, reports, asked, lines):
    async def run_once(*_a, on_line=None, **_k):
        for line in lines:
            await on_line(line)
        return {"rows": []}
    monkeypatch.setattr(rounds, "run_once", run_once)
    monkeypatch.setattr(rounds, "report", lambda _r: "THE REPORT")
    b = fake_bot(vera, reports=reports)
    asyncio.run(VeraBot.run_rounds(b, uid, channel=asked))
    return b


LINES = ["[1/9] company_form: Web Dev - Acme", "  where: Remote · Davao City (Remote)", "  -> SUBMITTED: clicked", "[2/9] remote_board: Dev II - Beta", "  where: On-site", "  -> FILTERED: too senior",
         "[3/9] company_form: QA - Gamma", "  where: Hybrid · BGC, Taguig", "  -> DRY-RUN: stopped before Submit"]


def test_each_application_is_posted_where_you_asked_then_the_full_report(vera, monkeypatch):
    reports, asked = Sink("vera-owner-reports"), Sink("vera-company-site-application")
    lines_during_a_run(vera, monkeypatch, OWNER, reports, asked, LINES)
    posts = [text for text, _k in asked.sent]
    assert posts[0] == "✅ Sent: **Web Dev - Acme** (Company form · Remote)"
    assert posts[1] == "🧪 Filled, not sent: **QA - Gamma** (Company form · Hybrid)"
    assert "report is in" in posts[2] and len(posts) == 3                    # jobs that didn't fit aren't posted
    assert [t for t, _k in reports.sent] == ["THE REPORT"]


def test_a_run_without_a_channel_posts_to_your_reports_channel(vera, monkeypatch):
    reports = Sink("vera-owner-reports")
    lines_during_a_run(vera, monkeypatch, OWNER, reports, None, LINES)
    assert reports.sent[0][0].startswith("✅ Sent") and reports.sent[-1][0] == "THE REPORT"


def test_a_members_posts_stay_private(vera, monkeypatch):
    reports = Sink("vera-owner-reports")
    b = lines_during_a_run(vera, monkeypatch, GUEST, reports, None, LINES)
    assert not reports.sent and b.told[0].startswith("✅ Sent") and b.told[-1] == "THE REPORT"


def test_job_text_in_a_post_cant_ping_anyone(vera, monkeypatch):
    asked = Sink("vera-company-site-application")
    lines_during_a_run(vera, monkeypatch, OWNER, Sink("r"), asked, ["[1/1] company_form: @everyone Dev - Acme", "  -> SUBMITTED: ok"])
    assert asked.sent[0][1]["allowed_mentions"].everyone is False


def test_one_short_report_is_one_message(vera, monkeypatch):
    reports = Sink("vera-owner-reports")
    run(vera, monkeypatch, OWNER, reports, Sink("vera-company-site-application"))
    assert [t for t, _k in reports.sent] == ["THE REPORT"]


def test_a_very_long_report_keeps_each_job_and_link_together():
    lines = [f"✅ **Job {i}** at Acme (company form): <https://jobs.lever.co/acme/{i}>" for i in range(200)]
    parts = split_message("\n".join(lines))
    assert len(parts) > 1 and all(len(p) <= 1900 for p in parts)
    assert "\n".join(parts).split("\n") == lines          # nothing lost, and no line was cut


def test_she_says_what_shell_post(vera):
    me = vera.user(OWNER)
    me.db.set_json(f"apply_request:{OWNER}", {"count": 5, "sites": None})
    me.update_prefs({"positions": ["Web Developer"]})
    cv = config.DATA_DIR / "cv.pdf"
    cv.write_bytes(b"%PDF")
    me.update_profile({"name": "Juan Cruz", "email": "j@example.com", "phone": "0900", "location": "Manila, Philippines", "resumes": [{"name": "CV", "path": str(cv), "tags": []}],
                       "application": {"expected_salary_monthly": 1, "notice_period": "2 weeks", "work_authorized": True}})
    r = asyncio.run(assistant.handle(me, "yes"))
    assert r["action"] == "start_apply" and "post each application" in r["reply"] and "full report" in r["reply"]


# ----------------------------------------------------------------- the same ad twice
SAME = "AI/Automation Specialist - Web Development Company"


def test_an_invisible_space_doesnt_make_it_a_different_job():
    assert title_key("AI/Automation Specialist\xa0- Web Development Company") == title_key(SAME)
    seen = {(company_key("Private Advertiser"), title_key(SAME))}
    assert "already seen" in too_many({"company": "Private Advertiser", "title": "AI/Automation Specialist\xa0- Web Development Company"}, {}, seen, cap=0)


def test_private_advertisers_with_a_common_short_title_may_be_different_employers():
    seen = {(company_key("Private Advertiser"), title_key("Web Developer"))}
    assert too_many({"company": "Private Advertiser", "title": "Web Developer"}, {}, seen, cap=0) == ""


def test_named_companies_dont_get_the_same_job_twice_but_can_get_different_ones():
    seen = {(company_key("Acme"), title_key("Web Developer"))}
    assert "already seen" in too_many({"company": "Acme", "title": "Web  Developer"}, {}, seen, cap=0)
    assert too_many({"company": "Acme", "title": "Backend Developer"}, {}, seen, cap=0) == ""


def test_a_long_run_says_what_it_found_and_keeps_saying_it_is_going(vera, monkeypatch):
    reports, asked = Sink("vera-owner-reports"), Sink("vera-multiboards-application")
    lines = ["QUEUE 25"] + [f"[{i}/25] company_form: Job {i} - Co\n  -> FILTERED: too far" for i in range(1, 21)]
    lines = [part for line in lines for part in line.split("\n")]
    lines_during_a_run(vera, monkeypatch, OWNER, reports, asked, lines)
    posts = [text for text, _k in asked.sent]
    assert posts[0].startswith("🔎 Found **25** jobs to check")
    assert posts[1] == "⏳ Still going: checked **10** of 25 jobs, **0** sent so far."
    assert posts[2] == "⏳ Still going: checked **20** of 25 jobs, **0** sent so far."


def test_each_sent_job_shows_title_company_board_setup_and_place_but_no_link(vera, monkeypatch):
    reports, asked = Sink("vera-owner-reports"), Sink("vera-multiboards-application")
    info = '{"title": "Forward Deployed Engineer - Work From Home", "company": "Example Solutions, Inc", "setup": "Remote", "location": "Metro Manila (Remote)"}'
    lines = ["[1/3] company_form: Forward Deployed Engineer - Work From Home - Example Solutions, Inc", "  where: Remote · Metro Manila (Remote)",
             "  info: " + info, "  -> SUBMITTED: clicked",
             "[2/3] remote_board: @everyone Dev - Acme", "  where: On-site", '  info: {"title": "@everyone Dev", "company": "Acme", "setup": "On-site", "location": "Makati"}', "  -> SUBMITTED: ok"]
    lines_during_a_run(vera, monkeypatch, OWNER, reports, asked, lines)
    posts = [text for text, _k in asked.sent]
    assert posts[0] == "✅ Sent: **Forward Deployed Engineer - Work From Home** - Example Solutions, Inc (Company form · Remote · Metro Manila (Remote))"
    assert posts[1] == "✅ Sent: **everyone Dev** - Acme (Remote board · On-site · Makati)"
    assert not any("http" in p for p in posts[:2])


def test_a_bot_check_rest_is_posted(vera, monkeypatch):
    reports, asked = Sink("vera-owner-reports"), Sink("vera-multiboards-application")
    lines_during_a_run(vera, monkeypatch, OWNER, reports, asked, ["RESTING himalayas.app 2999-01-01T10:00:00"])
    posts = [text for text, _k in asked.sent]
    assert posts[0].startswith("⏸ **himalayas.app** showed a bot check") and "10:00 AM" in posts[0]
