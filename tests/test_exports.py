"""Applications as an Excel sheet, a Word document and a PDF. No network."""

import asyncio
import csv
import socket
from types import SimpleNamespace

import docx
import openpyxl
import pytest
from pypdf import PdfReader

from vera import config
from vera.agents import assistant, rounds, exports
from vera.bot.main import VeraBot
from vera.db import DB
from vera.services import Vera

OWNER, GUEST, BOT = 111, 222, 999
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


def row(url, status, title="Web Developer", company="Acme Corp", site="company_form", time="2026-11-04T10:00:00", reason="", unanswered=""):
    return {"time": time, "site": site, "url": url, "title": title, "company": company, "status": status, "reason": reason, "unanswered": unanswered}


SAMPLE = [row("https://x.test/old", "failed", time="2026-11-01T10:00:00"),
          row("https://x.test/old", "submitted", time="2026-11-02T10:00:00"),
          row("https://x.test/new", "submitted", "Software Engineer", "Beta Inc", "remote_board", "2026-11-05T09:00:00"),
          row("https://x.test/test", "dry-run"), row("https://x.test/no", "filtered", reason="too senior"),
          row("https://x.test/gone", "skipped", reason="site I can't fill"),
          row("https://x.test/stuck", "needs_answer", "PHP Developer", "Gamma", unanswered="Do you have a visa?"),
          row("https://x.test/bad", "failed", "QA", "Delta", reason="Timeout (screenshot: a.png)")]


# ----------------------------------------------------------------- what goes in
def test_only_real_applications_count_as_sent(vera):
    history(OWNER, SAMPLE)
    rows = exports.collect(vera.user(OWNER))
    assert [r["link"] for r in rows["sent"]] == ["https://x.test/new", "https://x.test/old"]      # newest first; a test run or filtered job isn't sent
    assert rows["sent"][0]["board"] == "Remote board" and rows["sent"][0]["status"] == "Sent"
    assert {r["company"] for r in rows["unfinished"]} == {"Gamma", "Delta"}                        # the one that later succeeded isn't listed here


def test_reasons_are_tidy_and_questions_are_shown(vera):
    history(OWNER, SAMPLE)
    unfinished = {r["company"]: r for r in exports.collect(vera.user(OWNER))["unfinished"]}
    assert unfinished["Gamma"]["note"] == "Do you have a visa?" and unfinished["Gamma"]["status"] == "Needs your answer"
    assert unfinished["Delta"]["note"] == "Timeout"


def test_a_long_reason_doesnt_show_a_cut_off_screenshot_name(vera):
    history(OWNER, [row("https://x.test/long", "failed", reason="stuck " * 60 + "(screenshot: 20261104-1.png)")])
    note = exports.collect(vera.user(OWNER))["unfinished"][0]["note"]
    assert "screenshot" not in note and len(note) <= 300


def test_the_latest_status_comes_from_your_history(vera):
    history(OWNER, [row("https://x.test/1", "submitted")])
    job = vera.add_manual_job("https://x.test/1", "Web Developer", "Acme Corp")
    vera.db.update_application(vera.db.create_application(job, OWNER), status="interview")
    manual = vera.add_manual_job("https://x.test/2", "Designer", "Own Search")
    vera.db.update_application(vera.db.create_application(manual, OWNER), status="applied")
    rows = {r["link"]: r for r in exports.collect(vera.user(OWNER))["sent"]}
    assert rows["https://x.test/1"]["status"] == "Interview"
    assert rows["https://x.test/2"]["company"] == "Own Search"       # applied by hand


def test_a_member_only_gets_their_own(vera):
    history(OWNER, [row("https://x.test/mine", "submitted", company="Owner Secret Co")])
    history(GUEST, [row("https://x.test/theirs", "submitted", company="Guest Co")])
    rows = exports.collect(vera.user(GUEST))
    assert [r["company"] for r in rows["sent"]] == ["Guest Co"]


# ----------------------------------------------------------------- the files
RISKY = {"sent": [{"date": "2026-11-05", "title": '=HYPERLINK("http://evil.test","click") <img src="http://evil.test/x.png"/> <b>bold</b> & co ✨', "company": "+cmd|calc", "board": "Company form",
                   "status": "Sent", "link": "https://x.test/1", "note": ""},
                  {"date": "2026-11-05", "title": exports.clean("Dev\x00\x07 Ñoño"), "company": "@SUM(1)", "board": "Remote board", "status": "Sent", "link": exports.web_link("javascript:alert(1)"), "note": ""}],
         "unfinished": [{"date": "2026-11-04", "title": "QA", "company": "Delta", "board": "Remote board", "status": "Failed", "link": "", "note": "<script>"}]}


def test_excel_keeps_web_text_as_plain_text(tmp_path):
    path = exports.write_all(RISKY, ["xlsx"], tmp_path)[0]
    book = openpyxl.load_workbook(path)
    sheet = book["Applications"]
    assert [c.value for c in sheet[1]] == ["Date", "Job", "Company", "Setup", "Location", "Board", "Status", "Link"]
    assert all(c.data_type == "s" for line in sheet.iter_rows() for c in line if c.value)      # no formulas anywhere
    assert sheet["B2"].value.startswith("=HYPERLINK") and sheet["C2"].value == "+cmd|calc"
    assert sheet["H2"].hyperlink.target == "https://x.test/1" and sheet["H3"].value is None    # javascript: never becomes a link
    assert book["Not finished"]["F2"].value == "<script>"


def test_word_document_reads_back(tmp_path):
    path = exports.write_all(RISKY, ["docx"], tmp_path)[0]
    doc = docx.Document(path)
    cells = [c.text for t in doc.tables for line in t.rows for c in line.cells]
    assert "Job applications" in [p.text for p in doc.paragraphs] and "Ñoño" in " ".join(cells) and "+cmd|calc" in cells
    assert doc.sections[0].page_width > doc.sections[0].page_height      # landscape, so the table fits


def test_pdf_shows_web_text_as_words(tmp_path):
    path = exports.write_all(RISKY, ["pdf"], tmp_path)[0]
    text = " ".join(page.extract_text() for page in PdfReader(path).pages)
    assert "Job applications" in text and "evil.test/x.png" in text and "<b>bold</b>" in text and "Open job" in text and "<script>" in text


def test_a_long_list_flows_onto_more_pages(tmp_path):
    many = {"sent": [{"date": "2026-11-05", "title": f"Job {i}", "company": "Acme", "board": "Remote board", "status": "Sent", "link": "https://x.test", "note": ""} for i in range(200)], "unfinished": []}
    path = exports.write_all(many, ["pdf"], tmp_path)[0]
    assert len(PdfReader(path).pages) > 1


def test_making_files_never_touches_the_network(tmp_path, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    paths = exports.write_all(RISKY, list(exports.FORMATS), tmp_path)
    assert sorted(p.suffix for p in paths) == [".docx", ".pdf", ".xlsx"] and all(p.stat().st_size > 0 for p in paths)


# ----------------------------------------------------------------- asking for it
@pytest.mark.parametrize("text,formats", [
    ("create a sheet for my applications", ["xlsx"]), ("make an excel of the jobs I applied to", ["xlsx"]), ("can you make a spreadsheet", ["xlsx"]),
    ("give me a word document of my applications", ["docx"]), ("send me my application list as a doc", ["docx"]),
    ("make a pdf of my applications", ["pdf"]), ("export my job applications to pdf and excel", ["xlsx", "pdf"]),
    ("gawa ka ng sheet ng applications ko", ["xlsx"]),
    ("make my applications in all formats", ["xlsx", "docx", "pdf"]),
])
def test_ways_of_asking(text, formats):
    assert assistant.export_request(text) == formats


@pytest.mark.parametrize("text", [
    "apply to 5 jobs", "send me my resume as pdf", "make a pdf of my resume", "write a cover letter document", "did you use a pdf for my application?",
    "what document did you attach to my applications?", "how do I make a sheet?", "apply to 5 jobs and send my applications", "only remote jobs", "any replies?",
    "I need a job that uses excel",
])
def test_other_messages_arent_mistaken_for_it(text):
    assert assistant.export_request(text) is None


@pytest.mark.parametrize("text", ["make a pdf", "create a word document"])
def test_she_asks_what_you_mean_if_its_unclear(vera, text):
    assert assistant.export_request(text) == "ask"
    r = asyncio.run(assistant.handle(vera.user(OWNER), text))
    assert r["action"] == "none" and "applications" in r["reply"]


def test_chat_starts_it(vera):
    r = asyncio.run(assistant.handle(vera.user(OWNER), "create a sheet for my applications"))
    assert r["action"] == "send_export" and r["request"]["formats"] == ["xlsx"]


def test_it_doesnt_disturb_an_application_you_were_about_to_start(vera):
    me = vera.user(OWNER)
    me.db.set_json(f"apply_request:{OWNER}", {"count": 5, "sites": None})
    asyncio.run(assistant.handle(me, "make a sheet of my applications"))
    assert me.db.get_json(f"apply_request:{OWNER}") == {"count": 5, "sites": None}


# ----------------------------------------------------------------- where it goes
class Sink:
    def __init__(self, name):
        self.name, self.sent, self.mention = name, [], f"#{name}"

    async def send(self, text=None, **kw):
        self.sent.append((text, kw))


def bot(vera, files=None):
    async def owner_channel(which):
        return {"files": files}.get(which)
    return SimpleNamespace(vera=vera, owner_channel=owner_channel, user=SimpleNamespace(id=BOT))


def public_channel():
    ch = Sink("vera-company-site-application")
    ch.guild = SimpleNamespace(default_role=object())
    ch.category, ch.overwrites = SimpleNamespace(name="vera"), {}
    ch.permissions_for = lambda _r: SimpleNamespace(view_channel=True)
    return ch


def test_your_file_goes_to_your_private_channel_when_asked_in_public(vera):
    history(OWNER, SAMPLE)
    files, here = Sink("vera-owner-files"), public_channel()
    asyncio.run(VeraBot.send_export(bot(vera, files), SimpleNamespace(id=OWNER), ["xlsx", "pdf"], here))
    assert [f.filename.rsplit(".")[-1] for f in files.sent[0][1]["files"]] == ["xlsx", "pdf"]
    assert "#vera-owner-files" in here.sent[0][0] and not here.sent[0][1].get("files")


def test_your_file_stays_where_you_asked_in_a_dm(vera):
    history(OWNER, SAMPLE)
    here, files = Sink("dm"), Sink("vera-owner-files")
    here.guild = None
    asyncio.run(VeraBot.send_export(bot(vera, files), SimpleNamespace(id=OWNER), ["docx"], here))
    assert here.sent[0][1]["files"] and not files.sent


def test_a_members_file_comes_by_dm_and_never_reaches_your_channel(vera):
    history(GUEST, [row("https://x.test/theirs", "submitted", company="Guest Co")])
    dm, files, here = Sink("dm"), Sink("vera-owner-files"), public_channel()
    member = SimpleNamespace(id=GUEST, send=dm.send)
    asyncio.run(VeraBot.send_export(bot(vera, files), member, ["xlsx"], here))
    assert dm.sent[0][1]["files"] and not files.sent and not here.sent[0][1].get("files")


def test_nothing_is_made_with_no_applications(vera):
    here = Sink("dm")
    here.guild = None
    asyncio.run(VeraBot.send_export(bot(vera), SimpleNamespace(id=OWNER), ["pdf"], here))
    assert "don't have any" in here.sent[0][0] and not here.sent[0][1].get("files")
