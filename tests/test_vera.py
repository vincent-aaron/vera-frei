"""Vera tests: no network, no Discord connection, AI disabled."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from vera import config, preferences as P
from vera.agents import evaluator
from vera.db import DB
from vera.services import Vera
from vera.sources import SOURCES

FIX = Path(__file__).parent / "fixtures"
ALICE, BOB = 111, 222


@pytest.fixture
def vera(tmp_path, monkeypatch):
    for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.setattr(config, key, "")                        # AI off -> template answers
    monkeypatch.setattr(config, "RESUME_DIR", tmp_path / "resumes")
    monkeypatch.setattr(config, "OWNER_ID", ALICE)
    f = Vera(DB(tmp_path / "t.db"))
    f.user(ALICE).update_profile({
        "name": "Juan Dela Cruz", "years_experience": 0,
        "skills": ["Python", "JavaScript", "PHP", "Laravel (CRUD operations)", "MySQL", "React.js"],
        "education": [{"degree": "BS Information Technology", "school": "BulSU", "year": "2026", "honors": "Cum Laude"}],
    })
    return f


@pytest.fixture
def alice(vera):
    return vera.user(ALICE)


def job(**kw):
    base = {"id": "manual:x", "source": "manual", "title": "Junior Web Developer", "company": "Acme", "location": "Makati, Philippines",
            "remote": 0, "employment_type": "Full-time", "level": "", "salary_min": None, "salary_max": None, "salary_currency": None,
            "salary_period": None, "description": "We use PHP, Laravel and MySQL. Bachelor's degree in IT. Fresh graduates welcome.",
            "url": "https://example.com/job", "tags": []}
    base.update(kw)
    return base


# ------------------------------------------------------------------ evaluator
def test_good_match_scores_high(alice):
    ev = evaluator.evaluate(job(), alice.prefs, alice.profile)
    assert ev["score"] >= 80, ev
    assert ev["recommended"]
    assert set(ev["components"]["skills"]["matched"]) >= {"php", "laravel", "mysql"}


def test_mandatory_vs_preferred_experience(alice):
    d = "Requirements: at least 5 years of experience with Java. 7 years experience in Kubernetes is a plus."
    assert evaluator.experience_required(d) == (5, 7)
    ev = evaluator.evaluate(job(title="Java Developer", description=d), alice.prefs, alice.profile)
    assert ev["hard_reject"] and not ev["recommended"]
    ev2 = evaluator.evaluate(job(description="3+ years of experience with React is preferred. Fresh graduates may apply."),
                             alice.prefs, alice.profile)
    assert ev2["components"]["experience"]["mandatory_years"] is None
    assert ev2["components"]["experience"]["preferred_years"] == 3 and not ev2["hard_reject"]


def test_preferences_are_not_hardcoded(alice):
    senior = job(title="Senior Software Engineer", description="Senior role.")
    assert evaluator.evaluate(senior, alice.prefs, alice.profile)["components"]["level"]["score"] <= 10
    alice.update_prefs({"job_levels": ["Senior", "Lead"]})
    assert evaluator.evaluate(senior, alice.prefs, alice.profile)["components"]["level"]["score"] == 100


def test_exclusions_location_salary(alice):
    alice.update_prefs({"exclude": {"keywords": ["call center"], "companies": ["BadCo"]},
                        "salary": {"min": 30000, "currency": "PHP", "period": "monthly", "negotiable": False}})
    p, pr = alice.prefs, alice.profile
    assert evaluator.evaluate(job(company="BadCo"), p, pr)["hard_reject"]
    assert evaluator.evaluate(job(description="Call center support"), p, pr)["hard_reject"]
    assert evaluator.evaluate(job(location="Berlin, Germany"), p, pr)["components"]["location"]["score"] <= 20
    assert evaluator.evaluate(job(location="USA only", remote=1), p, pr)["components"]["location"]["score"] < 50
    low = job(salary_min=15000, salary_max=18000, salary_currency="PHP", salary_period="monthly")
    assert evaluator.evaluate(low, p, pr)["components"]["salary"]["score"] == 10


def test_resume_text_counts_as_skills():
    prof = P.merge(P.DEFAULT_PROFILE, {"resume_text": "Built apps with Django and PostgreSQL. BS Computer Science."})
    ev = evaluator.evaluate(job(description="Python, Django, SQL. Bachelor's degree required."), P.defaults(), prof)
    assert {"django", "python", "sql"} <= set(ev["components"]["skills"]["matched"])
    assert ev["components"]["education"]["score"] == 100


def test_preference_parsing():
    assert P.split_list("a, b; c\nd") == ["a", "b", "c", "d"]
    locs = P.parse_locations("Hagonoy, Bulacan, Central Luzon, Philippines")
    assert locs[0] == {"city": "Hagonoy", "province": "Bulacan", "region": "Central Luzon", "country": "Philippines"}
    assert P.fill_country([{"city": "Bulacan"}])[0]["country"] == "Philippines"
    assert P.parse_money("25k") == 25000 and P.parse_money("₱30,000") == 30000 and P.parse_money("") is None


# ------------------------------------------------------------------ sources (real API samples)
@pytest.mark.parametrize("name", ["remotive", "arbeitnow", "jobicy", "himalayas", "themuse"])
def test_source_parsers(name):
    payload = json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))

    async def go():
        async with httpx.AsyncClient(transport=transport) as c:
            return await SOURCES[name].fetch(c, "", "")
    jobs = asyncio.run(go())
    assert jobs, name
    for j in jobs:
        assert j["id"].startswith(name + ":") and j["title"] and j["url"].startswith("http")
        assert "<" not in j["description"][:200]


# ------------------------------------------------------------------ multi-user
def test_users_have_separate_data(vera):
    a, b = vera.user(ALICE), vera.user(BOB)
    a.update_prefs({"positions": ["QA Tester"]})
    assert b.prefs["positions"] != ["QA Tester"]
    assert b.profile["name"] == "" and a.profile["name"] == "Juan Dela Cruz"
    j = vera.add_manual_job("https://x.com/1", "Junior PHP Developer")
    a.set_job_state(j["id"], "saved")
    assert vera.db.state(j["id"], ALICE) == "saved" and vera.db.state(j["id"], BOB) is None
    assert set(vera.db.users()) == {ALICE}            # Bob hasn't saved anything yet
    b.update_prefs({"positions": ["Designer"]})
    assert set(vera.db.users()) == {ALICE, BOB}


def test_only_owner_can_approve_and_auto_submit_is_owner_only(vera, monkeypatch):
    from vera.agents import applier
    assert applier.can_auto_submit("https://jobs.lever.co/acme/1", ALICE)
    assert not applier.can_auto_submit("https://jobs.lever.co/acme/1", BOB)
    a = vera.user(ALICE)
    j = vera.add_manual_job("https://www.linkedin.com/jobs/view/1", "Junior PHP Developer", "Acme", "Hagonoy, Bulacan",
                            "Laravel and MySQL. Fresh graduates welcome. Bachelor's degree.", "PHP 25,000", "Full-time")
    prep = asyncio.run(a.start_application(j))
    assert prep["user_id"] == ALICE and prep["cover_letter"] is None
    assert "Juan Dela Cruz" in prep["answers"]["Tell us about yourself."]
    assert asyncio.run(vera.user(BOB).approve(prep["application_id"]))["detail"] == "not your application"
    res = asyncio.run(a.approve(prep["application_id"]))
    assert res["status"] == "manual" and res["manual_url"] == j["url"]
    a.mark(prep["application_id"], "applied")
    assert "already applied" in asyncio.run(a.start_application(j))["error"]
    assert a.statistics()["sent"] == 1 and vera.user(BOB).statistics()["sent"] == 0


def test_fact_check_removes_unsupported_claims(alice):
    from vera.agents.writer import scrub_claims
    text = ("I know Python and MySQL well. I also have experience with Git and Docker in production. "
            "However, I have not yet accumulated 2+ years of professional experience. I'm eager to learn Kubernetes.")
    clean, removed = scrub_claims(text, alice.profile)
    assert "Python and MySQL" in clean and "Git" not in clean and "2+ years" not in clean
    assert "eager to learn Kubernetes" in clean          # learning intent is fine
    assert "git" in removed and "docker" in removed


def test_public_profile_hides_contact(alice):
    from vera.bot import embeds as E
    alice.update_profile({"email": "juan.delacruz@gmail.com", "phone": "+63 917 123 4567"})
    public = E.profile_card(alice.profile, private=False).fields[0].value
    assert "juan.delacruz@" not in public and "917 123" not in public
    assert "juan.delacruz@gmail.com" in E.profile_card(alice.profile, private=True).fields[0].value


def test_import_resume_without_ai(vera, tmp_path):
    import zipfile
    docx = tmp_path / "cv.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:t>Maria Santos</w:t></w:p>"
                                        "<w:p><w:t>Skills: React, Node.js, MongoDB. BS Computer Science</w:t></w:p></w:document>")
    bob = vera.user(BOB)
    res = asyncio.run(bob.import_resume(docx))
    assert res["ok"] and not res["ai_used"]
    assert {"react", "node.js", "mongodb"} <= set(bob.profile["skills"])
    assert "Maria Santos" in bob.profile["resume_text"]


# ------------------------------------------------------------------ search, age filter, plain language
def test_find_jobs_uses_cache_and_ranks(alice, monkeypatch):
    payload = json.loads((FIX / "himalayas.json").read_text(encoding="utf-8"))
    for j in payload["jobs"]:
        j["pubDate"] = int(datetime.now(timezone.utc).timestamp()) - 3600     # fresh
    calls = {"n": 0}
    real = httpx.AsyncClient

    def fake_client(*a, **k):
        def handler(req):
            calls["n"] += 1
            return httpx.Response(200, json=payload) if "himalayas" in str(req.url) else httpx.Response(404)
        return real(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("vera.agents.scout.http_client", fake_client)
    monkeypatch.setattr("vera.agents.scout.httpx.AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))))
    alice.update_prefs({"sources": ["himalayas"], "positions": ["Developer"], "min_match": 0, "job_levels": ["Any Level"],
                        "locations": [{"country": ""}], "work_setup": ["Remote", "Hybrid", "On-site"]})
    r1 = asyncio.run(alice.find_jobs())
    n = calls["n"]
    r2 = asyncio.run(alice.find_jobs())
    assert r1["status"]["himalayas"].startswith("+") and r2["status"]["himalayas"] == "cached" and calls["n"] == n
    assert r1["picks"]


def test_old_jobs_are_never_recommended(vera, alice):
    from vera.agents import scout
    alice.update_prefs({"sources": ["remotive"], "min_match": 0, "job_levels": ["Any Level"], "positions": ["Web Developer"]})
    now = datetime.now(timezone.utc)
    for i, days in enumerate((2, 6, 9, None)):
        vera.db.upsert_job({**job(), "id": f"remotive:{i}", "source": "remotive", "title": f"Junior Web Developer {i}",
                            "posted_at": (now - timedelta(days=days)).isoformat() if days is not None else "", "fingerprint": f"fp{i}"})
    titles = sorted(j["title"] for j, _ in scout.rank(vera.db, ALICE, alice.prefs, alice.profile))
    assert titles == ["Junior Web Developer 0", "Junior Web Developer 1"]


USER_MSG = "suggest only jobs thatvreuire no experience and are either remote or from this location bulacan pampanga bataan manila"


def test_rule_parser_understands_user_request(alice):
    from vera.agents import assistant
    r = assistant.rule_parse(USER_MSG, alice.prefs)
    patch = assistant.sanitize(r["patch"], alice.prefs)
    assert r["action"] == "search" and patch["reject_mandatory_experience_over"] == 0
    assert set(patch["work_setup"]) == {"Remote", "Hybrid", "On-site"}
    assert {l["province"] for l in patch["locations"]} == {"Bulacan", "Pampanga", "Bataan", "Metro Manila"}
    d = assistant.rule_parse("post 10-20 jobs every morning at 7am", alice.prefs)["patch"]
    assert d["digest_count"] == 20 and d["daily_digest"] == {"enabled": True, "hour": 7}


def test_questions_become_chat_without_ai(alice):
    from vera.agents import assistant
    r = asyncio.run(assistant.handle(alice, "how do I prepare for a technical interview?"))
    assert r["action"] == "chat" and not r["patch"]
    assert "busy" in r["reply"]                     # honest message when the free AI is unavailable


def test_sanitize_rejects_bad_values(alice):
    from vera.agents import assistant
    p = assistant.sanitize({"job_levels": ["Senior", "Wizard"], "min_match": 500, "evil": 1, "digest_count": 99}, alice.prefs)
    assert p == {"job_levels": ["Senior"], "min_match": 100, "digest_count": 20}


# ------------------------------------------------------------------ Discord layer (no connection)
def test_bot_commands_and_views_build(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bot.db")
    from vera.bot import embeds as E, main, views as V

    async def go():
        bot = main.VeraBot()
        main.register_commands(bot.tree)
        names = {c.name for c in bot.tree.get_commands()}
        assert {"find-jobs", "preferences", "profile", "saved", "apply", "applications", "statistics",
                "reset-preferences", "add-job", "ask", "status", "help", "resume"} <= names
        assert [c.custom_id for c in V.job_buttons(7).children] == ["vera:job:apply:7", "vera:job:save:7", "vera:job:skip:7", "vera:job:details:7"]
        assert [c.custom_id for c in V.app_buttons(3).children] == ["vera:app:approve:3", "vera:app:edit:3", "vera:app:cancel:3"]
        V.PrefsPanel(1), V.ProfilePanel(1), V.ConfirmReset(1)
        prefs = bot.vera.user(1).prefs
        for modal in (V.SalaryModal(prefs), V.ExclusionsModal(prefs), V.RequirementsModal(prefs), V.SearchSettingsModal(prefs)):
            assert len(modal.children) <= 5
        card = E.job_card(job(), evaluator.evaluate(job(), prefs, bot.vera.user(1).profile))
        assert len(card) <= 6000 and card.fields and len(E.prefs_card(prefs)) <= 6000
        await bot.close()
    asyncio.run(go())


# ------------------------------------------------------------------ résumé requests + "only these jobs"
def test_resume_requests_are_detected(alice):
    from vera.agents import assistant
    for msg in ("can i see my resume", "send me my CV please", "show my résumé", "pls give me my resume file"):
        assert assistant.rule_parse(msg, alice.prefs)["action"] == "send_resume", msg
    r = asyncio.run(assistant.handle(alice, "can i see my resume?"))
    assert r["action"] == "send_resume" and not r["patch"]


def test_only_these_positions(vera, alice):
    from vera.agents import assistant
    msg = "these are the jobs i want: web developer, QA tester and IT support. suggest only this, no experience, remote or bulacan"
    r = assistant.rule_parse(msg, alice.prefs)
    patch = assistant.sanitize(r["patch"], alice.prefs)
    assert patch["positions"] == ["Web Developer", "QA Tester", "IT Support"]
    assert patch["only_listed_positions"] is True and patch["reject_mandatory_experience_over"] == 0
    r2 = assistant.rule_parse("suggest only web developer and software engineer jobs", alice.prefs)["patch"]
    assert r2["positions"] == ["Web Developer", "Software Engineer"] and r2["only_listed_positions"] is True
    alice.update_prefs(patch)
    p, pr = alice.prefs, alice.profile
    assert not evaluator.evaluate(job(title="Junior Web Developer", location="Hagonoy, Bulacan"), p, pr)["hard_reject"]
    assert not evaluator.evaluate(job(title="QA Tester (Entry Level)", location="Remote", remote=1), p, pr)["hard_reject"]
    assert evaluator.evaluate(job(title="Data Analyst", location="Remote", remote=1), p, pr)["hard_reject"]
    assert evaluator.evaluate(job(title="Web Designer", location="Remote", remote=1), p, pr)["hard_reject"]
    # each person keeps their own list
    assert vera.user(BOB).prefs["only_listed_positions"] is False


# ------------------------------------------------------------------ fixes: AI guard, OR queries, soft board labels
def test_ai_cannot_change_settings_you_did_not_mention():
    from vera.agents.assistant import guard
    ai_patch = {"work_setup": ["Hybrid", "On-site"], "job_levels": ["Entry Level"], "positions": ["Software Developer"]}
    assert guard(ai_patch, "find more software developer jobs") == {"positions": ["Software Developer"]}
    assert "work_setup" in guard(ai_patch, "only on-site or hybrid please")


def test_or_queries_match_either():
    from vera.agents.scout import split_query
    assert split_query("software developer or ai automation") == ["software developer", "ai automation"]
    assert split_query(None) == []


def test_job_board_level_tag_is_only_a_soft_signal(alice):
    tagged = job(title="Web Developer", level="Mid-level", description="Build websites with PHP and Laravel.",
                 location="Remote", remote=1)
    ev = evaluator.evaluate(tagged, alice.prefs, alice.profile)
    assert ev["components"]["level"]["score"] == 45 and "tagged" in ev["components"]["level"]["note"]
    assert ev["score"] > 45                                   # not capped like a real "Senior" title
    senior = evaluator.evaluate(job(title="Senior Web Developer"), alice.prefs, alice.profile)
    assert senior["components"]["level"]["score"] == 10


def test_questions_about_resume_are_not_send_requests(alice):
    from vera.agents import assistant
    for msg in ("where do you get my data, is it from my resume?", "how do I improve my resume?", "is my cv good enough?"):
        assert assistant.rule_parse(msg, alice.prefs)["action"] != "send_resume", msg
