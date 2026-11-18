"""Tagging her role instead of the bot, and reports sorted by setup."""

import asyncio
import csv
from types import SimpleNamespace

import discord
import openpyxl
import pytest

from vera import config
from vera.agents import assistant, rounds, export_parse, exports
from vera.apply import __main__ as engine
from vera.bot.main import VeraBot
from vera.db import DB
from vera.services import Vera

OWNER, BOT_ID, ROLE_ID = 111, 999, 555


@pytest.fixture
def vera(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESUME_DIR", tmp_path / "resumes")
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.setattr(config, key, "")
    return Vera(DB(tmp_path / "t.db"))


class FakeMessage:
    def __init__(self, content, mentions=(), role_mentions=(), fetched=None, guild=True, reference=None):
        self.author = SimpleNamespace(bot=False, id=OWNER)
        self.content, self.mentions, self.role_mentions, self.attachments = content, list(mentions), list(role_mentions), []
        self.id, self.reference = 1, reference
        self.replies = []
        self.role = SimpleNamespace(id=ROLE_ID)
        self.guild = SimpleNamespace(self_role=self.role) if guild else None
        self.fetched = fetched
        outer = self

        class Channel:
            name = "vera-company-site-application"
            id = 77

            async def fetch_message(self, _id):
                return SimpleNamespace(content=outer.fetched)

            def typing(self):
                class T:
                    async def __aenter__(self):
                        return None

                    async def __aexit__(self, *_):
                        return False
                return T()

            async def send(self, text, **_k):
                outer.replies.append(text)
        self.channel = Channel()

    async def reply(self, text, **_k):
        self.replies.append(text)


def bot(vera):
    me = SimpleNamespace(id=BOT_ID)

    async def note_new_rules(_result):
        return None
    return SimpleNamespace(user=me, vera=vera, note_new_rules=note_new_rules), me


def handle(vera, monkeypatch, make):
    seen = []

    async def fake_handle(_session, text, *_a):
        seen.append(text)
        return {"action": "none", "reply": "ok", "patch": {}, "changes": []}
    monkeypatch.setattr(assistant, "handle", fake_handle)
    b, me = bot(vera)
    message = make(me)
    asyncio.run(VeraBot.on_message(b, message))
    return seen, message


def test_your_message_tagging_the_role_now_works(vera, monkeypatch):
    def make(_me):
        m = FakeMessage(f"<@&{ROLE_ID}> apply me to atleast 100 jobs")
        m.role_mentions = [m.role]
        return m
    seen, _m = handle(vera, monkeypatch, make)
    assert seen == ["apply me to atleast 100 jobs"]


def test_tagging_the_bot_still_works(vera, monkeypatch):
    seen, _m = handle(vera, monkeypatch, lambda me: FakeMessage(f"<@{BOT_ID}> hello", mentions=[me]))
    assert seen == ["hello"]


def test_if_discord_hides_the_words_she_asks_for_the_message(vera, monkeypatch):
    def make(_me):
        m = FakeMessage("", fetched=f"<@&{ROLE_ID}> apply to 5 jobs")
        m.role_mentions = [m.role]
        return m
    seen, _m = handle(vera, monkeypatch, make)
    assert seen == ["apply to 5 jobs"]


def test_if_the_words_stay_hidden_she_says_how_to_fix_it_instead_of_going_silent(vera, monkeypatch):
    def make(_me):
        m = FakeMessage("", fetched="")
        m.role_mentions = [m.role]
        return m
    seen, m = handle(vera, monkeypatch, make)
    assert not seen and "marked **APP**" in m.replies[0]


def test_other_peoples_roles_and_ordinary_chat_are_ignored(vera, monkeypatch):
    def other_role(_me):
        return FakeMessage("<@&777> hello everyone", role_mentions=[SimpleNamespace(id=777)])
    assert handle(vera, monkeypatch, other_role)[0] == []
    assert handle(vera, monkeypatch, lambda _me: FakeMessage("hello everyone"))[0] == []


def test_replying_to_her_counts_even_without_a_ping(vera, monkeypatch):
    def make(me):
        hers = discord.Message.__new__(discord.Message)
        object.__setattr__(hers, "author", me)
        return FakeMessage("yes", reference=SimpleNamespace(resolved=hers))
    seen, _m = handle(vera, monkeypatch, make)
    assert seen == ["yes"]


def test_a_dm_needs_no_tag(vera, monkeypatch):
    seen, _m = handle(vera, monkeypatch, lambda _me: FakeMessage("hello", guild=False))
    assert seen == ["hello"]


# ----------------------------------------------------------------- the report
def report(rows):
    return rounds.report({"rows": rows, "dry_run": False})


SENT = [{"status": "submitted", "title": "Dev A", "company": "Acme", "site": "company_form", "url": "https://x/1", "setup": "Remote", "location": "Davao City (Remote)"},
        {"status": "submitted", "title": "Dev B", "company": "Beta", "site": "remote_board", "url": "https://x/2", "setup": "On-site", "location": "Makati City"},
        {"status": "submitted", "title": "Dev C", "company": "Gamma", "site": "company_form", "url": "https://x/3", "setup": "Hybrid", "location": "BGC, Taguig"},
        {"status": "submitted", "title": "Dev D", "company": "Delta", "site": "company_form", "url": "https://x/4", "setup": "Remote", "location": "Remote"}]


def test_the_report_is_sorted_by_setup_with_counts_and_locations():
    text = report(SENT)
    assert "🏠 Remote (2)" in text and "🔀 Hybrid (1)" in text and "🏢 On-site (1)" in text
    assert text.index("Remote (2)") < text.index("Hybrid (1)") < text.index("On-site (1)")
    assert text.index("Dev D") < text.index("Hybrid (1)") and "company form · BGC, Taguig" in text
    assert text.startswith("🤖 **Application round:** 4 sent")


def test_older_rows_without_a_setup_still_work():
    text = report([{"status": "submitted", "title": "Old", "company": "Acme", "site": "company_form", "url": "https://x/1"}])
    assert "✅ **Old** at Acme (company form): <https://x/1>" in text and "Setup not recorded" not in text
    mixed = report(SENT[:1] + [{"status": "submitted", "title": "Old", "company": "Acme", "site": "company_form", "url": "https://x/9"}])
    assert "Setup not recorded (1)" in mixed


# ----------------------------------------------------------------- your log gets the new columns safely
OLD_FIELDS = ["time", "site", "status", "title", "company", "url", "reason", "unanswered"]


def test_an_old_log_is_upgraded_with_nothing_lost_and_a_backup(tmp_path, monkeypatch):
    log = tmp_path / "applications.csv"
    with open(log, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OLD_FIELDS)
        w.writeheader()
        w.writerow({"time": "2026-11-04T10:00:00", "site": "company_form", "status": "submitted", "title": "Ñoño Dev, Senior", "company": "Acme", "url": "https://x/1", "reason": "clicked", "unanswered": ""})
    monkeypatch.setattr(engine.config, "LOG_PATH", log)
    engine.write_log({"time": "2026-11-05T10:00:00", "site": "remote_board", "status": "submitted", "title": "New", "company": "Beta", "url": "https://x/2", "setup": "Hybrid", "location": "Makati"})
    with open(log, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["title"] for r in rows] == ["Ñoño Dev, Senior", "New"] and rows[0]["reason"] == "clicked" and rows[0]["setup"] == ""
    assert rows[1]["setup"] == "Hybrid" and rows[1]["location"] == "Makati"
    assert "Ñoño Dev, Senior" in log.with_suffix(".before-upgrade.csv").read_text(encoding="utf-8")


def test_a_new_log_just_works(tmp_path, monkeypatch):
    log = tmp_path / "applications.csv"
    monkeypatch.setattr(engine.config, "LOG_PATH", log)
    engine.write_log({"time": "t", "site": "s", "status": "submitted", "setup": "Remote"})
    engine.write_log({"time": "t2", "site": "s", "status": "submitted", "setup": "Hybrid"})
    with open(log, newline="", encoding="utf-8") as f:
        assert [r["setup"] for r in csv.DictReader(f)] == ["Remote", "Hybrid"]
    assert not log.with_suffix(".before-upgrade.csv").exists()


# ----------------------------------------------------------------- sheets and tables
def history(rows):
    folder = rounds.user_dir(OWNER)
    folder.mkdir(parents=True, exist_ok=True)
    fields = OLD_FIELDS + ["setup", "location"]
    with open(folder / "applications.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({"time": "2026-11-05T10:00:00", "reason": "", "unanswered": "", **r})


def test_the_sheet_shows_setup_and_location(vera, tmp_path):
    history(SENT)
    path = exports.write_all(exports.collect(vera.user(OWNER)), ["xlsx"], tmp_path / "out")[0]
    sheet = openpyxl.load_workbook(path)["Applications"]
    values = {c.value for line in sheet.iter_rows() for c in line}
    assert {"Setup", "Location", "Hybrid", "BGC, Taguig", "On-site"} <= values


@pytest.mark.parametrize("text,setups", [
    ("make a table of my remote applications", ["Remote"]), ("table of my applications, only hybrid", ["Hybrid"]), ("table of my on-site applications", ["On-site"]),
    ("table of my applications by setup hybrid, onsite, remote", []),
])
def test_asking_for_one_setup(text, setups):
    assert export_parse.plan(text)["setups"] == setups


def test_only_that_setup_is_included(vera):
    history(SENT)
    rows = exports.narrow(exports.collect(vera.user(OWNER)), {"setups": ["Remote"]})
    assert sorted(r["company"] for r in rows["sent"]) == ["Acme", "Delta"]


def test_a_table_can_be_sorted_by_setup(vera):
    history(SENT)
    plan = export_parse.plan("make a table of my applications categorized by setup")
    assert plan["group"] is True
    text = "\n".join(exports.render_chat(exports.collect(vera.user(OWNER)), plan))
    assert text.index("Remote (2)") < text.index("Hybrid (1)") < text.index("On-site (1)")


def test_the_summary_counts_by_setup(vera):
    history(SENT)
    text = "\n".join(exports.render_chat(exports.collect(vera.user(OWNER)), {"chat": "summary"}))
    assert "By setup" in text and "Remote: **2**" in text


def test_your_exact_request_starts_a_plan_for_100(vera, tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF")
    me = vera.user(OWNER)
    me.update_profile({"name": "Juan Cruz", "email": "j@example.com", "phone": "0900", "location": "Manila, Philippines", "resumes": [{"name": "CV", "path": str(cv), "tags": []}],
                       "application": {"expected_salary_monthly": 1, "notice_period": "2 weeks", "work_authorized": True}})
    me.update_prefs({"positions": ["Web Developer"]})
    r = asyncio.run(assistant.handle(me, "apply me to atleast 100 jobs through different job boards in youre reports include the setup or categorized them by setup hybrid, onsite remote"))
    assert r["action"] == "none" and "up to 100" in r["reply"] and me.db.get_json(f"apply_request:{OWNER}")["count"] == 100
    assert sorted(me.prefs["work_setup"]) == ["Hybrid", "On-site", "Remote"]
