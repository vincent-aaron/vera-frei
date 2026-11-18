"""Application round tests: no network, no browser, no Discord."""

import asyncio
import csv
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from vera import ai, config
from vera.agents import rounds
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


def complete(vera, uid, tmp_path, name="Juan Dela Cruz"):
    cv = tmp_path / f"cv{uid}.pdf"
    cv.write_bytes(b"%PDF")
    me = vera.user(uid)
    me.update_profile({"name": name, "email": f"u{uid}@example.com", "phone": "+63 900 000 0000", "location": "Hagonoy, Bulacan, Philippines",
                       "resumes": [{"name": "CV", "path": str(cv), "tags": []}],
                       "application": {"expected_salary_monthly": 28000, "notice_period": "2 weeks' notice", "work_authorized": True}})
    return me


def test_nothing_ever_runs_on_a_schedule(vera):
    """Rounds start only from an approved plan: there's no switch, timer or scheduler that applies by itself."""
    from vera.bot import core
    from vera.hermes import discord_runtime
    assert "enabled" not in vera.user(OWNER).prefs["rounds"] and "interval_hours" not in vera.user(OWNER).prefs["rounds"]
    assert not hasattr(rounds, "is_due") and not hasattr(rounds, "next_due")
    assert not hasattr(core.VeraCore, "rounds_tick")
    assert all("rounds" not in name for name, _every in discord_runtime.LOOPS)


def test_preferences_become_the_job_spec(vera, tmp_path):
    me = complete(vera, OWNER, tmp_path)
    me.update_prefs({
        "positions": ["QA Tester"], "job_levels": ["Junior"], "work_setup": ["Remote"], "employment_types": ["Contract"],
        "reject_mandatory_experience_over": 1, "salary": {"min": 40000},
        "exclude": {"companies": ["BadCo"], "keywords": ["night shift"]},
        "locations": [{"city": "Hagonoy", "province": "Bulacan", "country": "Philippines"}],
        "rounds": {"max_per_run": 3, "max_per_day": 7, "cover_letters": False},
    })
    profile = rounds.write_profile(me)
    s = profile["search"]
    assert s["keywords"] == ["QA Tester"] and s["levels"] == ["Junior"] and s["work_setup"] == ["Remote"]
    assert s["job_types"] == ["Contract"] and s["max_years_required"] == 1 and s["min_salary_monthly"] == 40000
    assert s["exclude_companies"] == ["BadCo"] and s["exclude_keywords"] == ["night shift"] and "sites" not in s
    assert s["location"] == ""      # remote jobs aren't tied to a town
    assert profile["cover_letter"] == {"enabled": False}
    assert profile["limits"]["max_per_run"] == 3 and profile["limits"]["max_per_day"] == 7
    assert (profile["first_name"], profile["last_name"]) == ("Juan Dela", "Cruz")
    assert (profile["city"], profile["province"], profile["country"]) == ("Hagonoy", "Bulacan", "Philippines")
    assert json.loads((rounds.user_dir(OWNER) / "profile.json").read_text(encoding="utf-8"))["search"]["keywords"] == ["QA Tester"]


def test_each_person_has_a_separate_folder_and_resume(vera, tmp_path):
    a, b = complete(vera, OWNER, tmp_path, "Owner Person"), complete(vera, GUEST, tmp_path, "Guest Person")
    pa, pb = rounds.write_profile(a), rounds.write_profile(b)
    assert rounds.user_dir(OWNER) != rounds.user_dir(GUEST)
    assert pa["resume_path"] != pb["resume_path"] and pb["email"] == f"u{GUEST}@example.com"
    rounds.save_answer(GUEST, "Do you hold a visa?", "No")
    assert not (rounds.user_dir(OWNER) / "answers.json").exists()
    assert json.loads((rounds.user_dir(GUEST) / "answers.json").read_text(encoding="utf-8"))["do you hold a visa"] == "No"


def test_other_people_have_capped_rounds(vera, tmp_path):
    guest = complete(vera, GUEST, tmp_path)
    guest.update_prefs({"rounds": {"max_per_run": 50, "max_per_day": 200}})
    p = rounds.write_profile(guest)
    assert p["limits"]["max_per_run"] == config.SHARED_MAX_PER_RUN and p["limits"]["max_per_day"] == config.SHARED_MAX_PER_DAY


def test_no_resume_or_details_means_no_run(vera, monkeypatch):
    async def must_not_run(*_a, **_k):
        raise AssertionError("the engine must not start")
    monkeypatch.setattr(rounds, "_run_engine", must_not_run)
    res = asyncio.run(rounds.run_once(vera, GUEST))
    assert "résumé" in res["error"] and "salary" in res["error"]
    assert not rounds.is_running()


def _write_rows(uid, rows):
    fields = ["time", "site", "status", "title", "company", "url", "reason", "unanswered"]
    folder = rounds.user_dir(uid)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "applications.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def test_run_records_only_confirmed_submissions_and_reports(vera, tmp_path, monkeypatch):
    now = datetime.now()
    later = (now + timedelta(seconds=5)).isoformat(timespec="seconds")
    old = (now - timedelta(days=2)).isoformat(timespec="seconds")

    async def fake_engine(uid, queue, budget, dry_run, on_line=None):
        _write_rows(uid, [
            {"time": old, "status": "submitted", "url": "https://x/old", "title": "Old"},
            {"time": later, "site": "company_form", "status": "submitted", "url": "https://jobs.lever.co/acme/1", "title": "Web Dev", "company": "Acme"},
            {"time": later, "site": "company_form", "status": "failed", "url": "https://x/2", "title": "Failed", "reason": "stuck"},
            {"time": later, "site": "company_form", "status": "needs_answer", "url": "https://x/3", "unanswered": "Do you have a visa?"},
            {"time": later, "site": "company_form", "status": "needs_login", "url": "https://x/4"},
            {"time": later, "site": "remote_board", "status": "filtered", "url": "https://x/5"},
        ])
        return 'SITE-STATUS {"blocked": ["himalayas.app"]}\nDone.'

    async def no_boards(_session, **_k):
        return []
    monkeypatch.setattr(rounds, "_run_engine", fake_engine)
    monkeypatch.setattr(rounds, "board_urls", no_boards)
    me = complete(vera, OWNER, tmp_path)
    res = asyncio.run(rounds.run_once(vera, OWNER))
    apps = vera.db.applications(OWNER, limit=10)
    assert [a["position"] for a in apps] == ["Web Dev"]      # not the failed one, not last week's row
    assert apps[0]["status"] == "submitted" and apps[0]["applied_at"]
    text = rounds.report(res)
    assert "1 sent" in text and "Web Dev" in text and "Do you have a visa?" in text
    assert "Bot check on himalayas.app" in text and "asked for a login" in text and "never sign in" in text and "stuck" in text
    assert "LinkedIn" in text        # tells you what's missing from your details
    assert me.db.get_json(f"rounds_last:{OWNER}")
    assert not rounds.is_running()


def test_dry_run_records_nothing(vera, tmp_path, monkeypatch):
    later = (datetime.now() + timedelta(seconds=5)).isoformat(timespec="seconds")

    async def fake_engine(uid, queue, budget, dry_run, on_line=None):
        assert dry_run
        _write_rows(uid, [{"time": later, "status": "dry-run", "url": "https://x/1", "title": "Web Dev"}])
        return "Done."

    async def no_boards(_session, **_k):
        return []
    monkeypatch.setattr(rounds, "_run_engine", fake_engine)
    monkeypatch.setattr(rounds, "board_urls", no_boards)
    complete(vera, OWNER, tmp_path)
    res = asyncio.run(rounds.run_once(vera, OWNER, dry_run=True))
    assert vera.db.applications(OWNER, limit=10) == []
    assert "nothing was sent" in rounds.report(res)


def test_only_one_run_at_a_time(vera, monkeypatch):
    monkeypatch.setattr(rounds, "_active", [{"uid": OWNER, "channel_id": None}])
    assert "one at a time" in asyncio.run(rounds.run_once(vera, GUEST))["error"]


def test_a_failed_run_reports_instead_of_crashing(vera, tmp_path, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("no browser")
    monkeypatch.setattr(rounds, "board_urls", boom)
    complete(vera, OWNER, tmp_path)
    res = asyncio.run(rounds.run_once(vera, OWNER))
    assert "no browser" in rounds.report(res)
    assert not rounds.is_running()


def test_guide_fits_discord_and_covers_every_command():
    import re
    from pathlib import Path
    from vera.bot.guide import MESSAGES, guide_hash
    assert all(len(m) <= 2000 for m in MESSAGES)
    source = (Path(config.ROOT) / "vera" / "bot" / "main.py").read_text(encoding="utf-8")
    text = "\n".join(MESSAGES)
    for name in re.findall(r'tree\.command\(name="([a-z-]+)"', source):
        assert f"`/{name}`" in text, f"{name} is missing from the guide"
    for banned in ("examplecorp", "jane.colleague", "john.colleague"):
        assert banned not in text.lower()
    assert len(guide_hash()) == 16


def test_status_points_new_users_to_the_test(vera, tmp_path):
    complete(vera, OWNER, tmp_path)
    assert "answer the plan with **test**" in rounds.status_text(vera, OWNER)
    vera.db.set_json(f"rounds_last:{OWNER}", datetime.now(timezone.utc).isoformat())
    assert "answer the plan with **test**" not in rounds.status_text(vera, OWNER)


def test_openrouter_is_limited_to_free_models(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_MODEL", "openai/gpt-paid")
    monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODELS", ["google/gemma:free", "anthropic/paid", "openrouter/free"])
    assert ai.free_openrouter_models() == ["google/gemma:free", "openrouter/free"]
    monkeypatch.setattr(config, "OPENROUTER_FALLBACK_MODELS", ["anthropic/paid"])
    assert ai.free_openrouter_models() == ["openrouter/free"]


def test_ai_fails_over_from_gemini_to_openrouter(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g")
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "o")
    ai._cooldown.clear()
    hits = []

    def handler(request):
        hits.append(request.url.host)
        if "googleapis" in request.url.host:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
    real = httpx.AsyncClient
    monkeypatch.setattr(ai.httpx, "AsyncClient", lambda **k: real(transport=httpx.MockTransport(handler), **k))
    assert ai.providers() == ["gemini", "openrouter"]
    assert asyncio.run(ai.complete("hello")) == "hi"
    assert hits == ["generativelanguage.googleapis.com", "openrouter.ai"]
    assert "gemini" in ai._cooldown
    ai._cooldown.clear()
