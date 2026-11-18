"""Application rounds: apply to jobs that fit, only after the person approves the plan.

A round starts only from an approved plan (the person's own plain "proceed", or "test" to fill
forms and send nothing). Nothing runs on a schedule. Each round, for one person:
  1. writes that person's profile and job spec into their own folder
  2. gets matching jobs from the free job APIs (company forms and remote boards)
  3. runs Vera's form engine (vera/apply) in its own process: it filters by the spec, writes a
     cover letter per job if wanted, fills the company's form (Greenhouse, Lever, Ashby, Workable)
     and submits it
  4. records what happened and returns a report

Everyone in the server can use it, but people's data never mixes: each has a folder with
their own profile, answers, log and letters. Vera never signs in to job boards.

Only an on-screen confirmation from the site counts as "submitted". Bot checks are never
bypassed: that site rests and the person is told.
"""

import asyncio
import csv
import json
import logging
import re
import subprocess
import sys
import tempfile
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from vera import config
from vera.agents import applier, scout

log = logging.getLogger("vera.rounds")

RUN_TIMEOUT_MINUTES = 120
NO_LIMIT = 100000      # "until I say stop"
MAX_RUNS = 3            # at the same time. Each has its own browser, and each browser uses real memory
_active: List[Dict[str, Any]] = []       # runs going now: who, which channel, its browser, its engine
_waiting: List[Dict[str, Any]] = []      # runs waiting for room
_this_run: ContextVar[Optional[Dict[str, Any]]] = ContextVar("vera_this_run", default=None)


def active_runs() -> List[Dict[str, Any]]:
    return list(_active)


def runs_for(uid: int) -> List[Dict[str, Any]]:
    return [r for r in _active if r["uid"] == int(uid)]


def browser_folder(uid: int, slot: int) -> Path:
    return user_dir(uid) / ("browser" if slot == 1 else f"browser-{slot}")


def _free_slot(uid: int) -> int:
    used = {r.get("slot") for r in runs_for(uid)}
    return next(n for n in range(1, MAX_RUNS + 2) if n not in used)


def has_room(uid: int, channel_id: Optional[int] = None) -> bool:
    """One run per channel, and a few at most at the same time."""
    return not (len(_active) >= MAX_RUNS or any(r["uid"] == int(uid) and r.get("channel_id") == channel_id for r in _active))


def start_run(item: Dict[str, Any]) -> Dict[str, Any]:
    item["slot"] = _free_slot(item["uid"])
    item["stopped"] = False
    _active.append(item)
    return item


def finish_run(item: Dict[str, Any]) -> None:
    if item in _active:
        _active.remove(item)


def enqueue(item: Dict[str, Any]) -> int:
    _waiting.append(item)
    return len(_waiting)


def next_waiting() -> Optional[Dict[str, Any]]:
    """The first waiting run that has room now."""
    for item in _waiting:
        if has_room(item["uid"], item.get("channel_id")):
            _waiting.remove(item)
            return item
    return None


def waiting_for(uid: int, channel_id: Optional[int] = None) -> List[Dict[str, Any]]:
    return [w for w in _waiting if w["uid"] == int(uid) and (channel_id is None or w.get("channel_id") == channel_id)]


def cancel_waiting(uid: int, channel_id: Optional[int] = None) -> int:
    gone = waiting_for(uid, channel_id)
    for item in gone:
        _waiting.remove(item)
    return len(gone)


def resting_boards(uid: int) -> Dict[str, datetime]:
    """Sites she's leaving alone after a bot check, and until when."""
    try:
        state = json.loads((user_dir(uid) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now, out = datetime.now(), {}
    for key, value in state.items():
        if key.startswith("_"):
            continue
        try:
            until = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        if until > now:
            out[key.replace('_', ' ')] = until
    return out


def resting_text(uid: int) -> str:
    resting = resting_boards(uid)
    return ", ".join(f"{name} until {until:%I:%M %p}".replace(" 0", " ") for name, until in sorted(resting.items(), key=lambda x: x[1])) if resting else ""


def status_now(session) -> str:
    """What's really happening right now, in plain words."""
    uid = session.uid
    lines = []
    mine = runs_for(uid)
    if mine:
        rows = read_results(uid, session.db.get_json(f"apply_started:{uid}") or "")
        sent = sum(1 for r in rows if r.get("status") == "submitted")
        going = "Still going" if len(mine) == 1 else f"**{len(mine)}** runs going"
        lines.append(f"{going}: **{sent}** sent so far, out of {len(rows)} jobs checked. Say **stop** in a run's channel to end it.")
    elif is_running():
        lines.append("Someone else's run is going right now.")
    else:
        lines.append("Nothing is running right now.")
    queued = waiting_for(uid)
    if queued:
        lines.append(f"You have {len(queued)} more waiting for a turn.")
    resting = resting_text(uid)
    if resting:
        lines.append(f"⏸ Resting after a bot check: {resting}. I skip those until then.")
    return " ".join(lines)


def is_running() -> bool:
    return bool(_active)


def running_for(uid: int) -> bool:
    return bool(runs_for(uid))


def stop_run(run: Optional[Dict[str, Any]] = None) -> bool:
    """End a run, including its browser. Applications already sent stay sent. With no run named, the one this code belongs to, or every run."""
    run = run or _this_run.get()
    stopped = False
    for r in ([run] if run else list(_active)):
        proc = r.get("proc")
        r["stopped"] = True
        if proc is not None and proc.returncode is None:
            kill_tree(proc.pid)
            stopped = True
    return stopped


def kill_tree(pid: int) -> None:
    """End the engine and the browser it started, so no hidden Edge stays open."""
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def is_owner(uid: int) -> bool:
    return bool(config.OWNER_ID) and int(uid) == config.OWNER_ID


def user_dir(uid: int) -> Path:
    """Each person's private folder. int() keeps anything but a number out of the path."""
    return config.DATA_DIR / "apply" / ("owner" if is_owner(uid) else str(int(uid)))


def engine_python() -> Path:
    """Her own venv's python.exe: the form engine needs its Playwright, even when this code runs inside Hermes's
    Python. Not pythonw.exe, which has no output to read."""
    own = config.ROOT / ".venv" / "Scripts" / "python.exe"
    if own.exists():
        return own
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return console if console.exists() else exe


def limits_for(uid: int, auto: Dict[str, Any]) -> Dict[str, int]:
    """Other people's runs share the owner's PC and connection, so they have caps."""
    per_run, per_day = int(auto.get("max_per_run") or NO_LIMIT), int(auto.get("max_per_day") or NO_LIMIT)
    if not is_owner(uid):
        per_run, per_day = min(per_run, config.SHARED_MAX_PER_RUN), min(per_day, config.SHARED_MAX_PER_DAY)
    return {"max_per_run": per_run, "max_per_day": per_day}


def spec_from_prefs(prefs: Dict[str, Any], uid: int) -> Dict[str, Any]:
    """Vera's preferences -> the job spec the engine filters by."""
    ex = prefs.get("exclude") or {}
    setups = prefs.get("work_setup") or []
    salary = prefs.get("salary") or {}
    place = next((l.get("city") or l.get("province") for l in prefs.get("locations") or []
                  if l.get("city") or l.get("province")), "")
    return {
        "keywords": prefs.get("positions") or [],
        "similar_titles": not prefs.get("only_listed_positions"),
        "home_country": next((l.get("country") for l in prefs.get("locations") or [] if l.get("country")), "Philippines"),
        "location": "" if "Remote" in setups else place,   # remote jobs aren't tied to your town
        "levels": list(prefs.get("job_levels") or []) + list(prefs.get("custom_levels") or []),
        "work_setup": setups,
        "places": [p for l in prefs.get("locations") or [] for p in (l.get("city"), l.get("province")) if p],
        "job_types": prefs.get("employment_types") or [],
        "max_years_required": prefs.get("reject_mandatory_experience_over"),
        "min_salary_monthly": salary.get("min") if salary.get("currency") == "PHP" and salary.get("period") == "monthly" else None,
        "exclude_titles": ex.get("positions") or [],
        "exclude_companies": ex.get("companies") or [],
        "exclude_keywords": ex.get("keywords") or [],
        "exclude_locations": ex.get("locations") or [],
        "avoid_onsite": ex.get("onsite_locations") or [],
    }


def resume_path(profile: Dict[str, Any]) -> Optional[str]:
    return next((r["path"] for r in profile.get("resumes") or [] if Path(r.get("path", "")).is_file()), None)


def build_profile(session) -> Dict[str, Any]:
    """Everything the engine needs for one person, from Vera's own records."""
    p, prefs = session.profile, session.prefs
    app = p.get("application") or {}
    auto = prefs["rounds"]
    name = (p.get("name") or "").split()
    places = [x.strip() for x in (p.get("location") or "").split(",") if x.strip()]
    links = p.get("links") or {}
    education = "; ".join(" ".join(str(v) for v in (e.get("degree"), e.get("school"), e.get("honors")) if v)
                          for e in p.get("education") or [])
    return {
        "first_name": app.get("first_name") or " ".join(name[:-1]) or "".join(name),
        "last_name": app.get("last_name") or (name[-1] if len(name) > 1 else ""),
        "email": p.get("email"), "phone": p.get("phone"),
        "address": app.get("address") or "", "postal_code": app.get("postal_code") or "",
        "city": places[0] if places else "",
        "province": places[1] if len(places) > 2 else "",
        "country": places[-1] if len(places) > 1 else "",
        "linkedin": links.get("linkedin") or "", "github": links.get("github") or "", "portfolio": links.get("portfolio") or "",
        "resume_path": resume_path(p),
        "education": education,
        "current_title": app.get("current_title") or "", "current_company": app.get("current_company") or "",
        "years_experience": p.get("years_experience") or 0,
        "skill_years": app.get("skill_years") or {},
        "expected_salary_monthly": app.get("expected_salary_monthly"),
        "expected_salary_usd_monthly": app.get("expected_salary_usd_monthly"),
        "notice_period": app.get("notice_period"),
        "work_authorized": app.get("work_authorized"),
        "work_authorized_countries": app.get("work_authorized_countries") or (["Philippines"] if app.get("work_authorized") else []),
        "requires_sponsorship": bool(app.get("requires_sponsorship")),
        "willing_to_relocate": app.get("willing_to_relocate"),
        "willing_to_work_onsite": app.get("willing_to_work_onsite"),
        "summary": p.get("summary") or "",
        "skills": p.get("skills") or [], "experience": p.get("experience") or [],
        "certifications": p.get("certifications") or [], "achievements": p.get("achievements") or [],
        "search": spec_from_prefs(prefs, session.uid),
        "cover_letter": {"enabled": bool(auto.get("cover_letters", True))},
        "limits": {**limits_for(session.uid, auto), "delay_seconds": [25, 60],
                   # Other people's runs use the owner's PC, so they get one tab.
                   "tabs": int(auto.get("tabs") or 10) if is_owner(session.uid) else 1,
                   "tabs_per_site": int(auto.get("tabs_per_site") or 3) if is_owner(session.uid) else 1,
                   "max_per_company": 0 if is_owner(session.uid) else 2},      # 0 = no limit
    }


def write_profile(session, count: Optional[int] = None) -> Dict[str, Any]:
    """count is for one requested run ("apply to 5 jobs")."""
    folder = user_dir(session.uid)
    folder.mkdir(parents=True, exist_ok=True)
    profile = build_profile(session)
    if count:
        profile["limits"]["max_per_run"] = min(int(count), profile["limits"]["max_per_day"])
    (folder / "profile.json").write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    return profile


REQUIRED = {"first_name": "your name", "email": "email", "phone": "phone", "resume_path": "a résumé (`/resume add`)",
            "expected_salary_monthly": "expected monthly salary in pesos", "notice_period": "when you can start"}
OPTIONAL = {"expected_salary_usd_monthly": "expected monthly salary for overseas jobs (USD)", "linkedin": "LinkedIn link",
            "work_authorized": "whether you can legally work in the Philippines"}


def blockers(profile: Dict[str, Any]) -> List[str]:
    """Details no application can go out without."""
    return [label for key, label in REQUIRED.items() if profile.get(key) in (None, "")]


def missing_details(profile: Dict[str, Any]) -> List[str]:
    return blockers(profile) + [label for key, label in OPTIONAL.items() if profile.get(key) in (None, "")]


REMOTE_SOURCES = ("remotive", "jobicy", "himalayas", "remoteok", "weworkremotely", "workingnomads", "companyforms", "hnhiring", "jobspresso", "realworkfromanywhere", "larajobs")


async def board_urls(session, limit: int = 40, remote_only: bool = False, only_sources: Optional[tuple] = None) -> List[str]:
    """Matching jobs from the free job APIs (scored by your preferences), taking turns between boards.
    only_sources keeps just those sources: a company-forms run gets company forms, not every feed."""
    prefs = session.prefs
    try:
        await scout.refresh_sources(session.db, prefs)
    except Exception:
        log.exception("job APIs failed; continuing without them")
    # Age doesn't matter for applying: if the posting still takes applications, it's worth trying. /find-jobs keeps its own limit.
    # Off the main thread: scoring every stored job takes long enough to freeze her chat.
    ranked = await asyncio.to_thread(scout.rank, session.db, session.uid, {**prefs, "max_age_days": None}, session.profile, include_seen=True, save=False)
    from vera.apply import filters
    search = build_profile(session)["search"]
    per_board: Dict[str, List[str]] = {}
    known: Dict[str, str] = {}
    for j, _ in ranked:
        if session.db.state(j["id"], session.uid) in ("applied", "skipped") or (remote_only and j.get("source") not in REMOTE_SOURCES) \
                or (only_sources and j.get("source") not in only_sources):
            continue
        # The list already says enough to rule out most: "Remote San Francisco" needs no visit, which saves minutes per run.
        if filters.rejects({"title": j.get("title") or "", "company": j.get("company") or "", "location": j.get("location") or "", "url": j.get("url") or "", "description": j.get("description") or ""}, search):
            continue
        per_board.setdefault(j.get("source") or "", []).append(j["url"])
        if j.get("location"):
            known[j["url"]] = j["location"]
    folder = user_dir(session.uid)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "known_locations.json"
    try:
        known = {**json.loads(path.read_text(encoding="utf-8")), **known}      # another run may be using this
    except (OSError, ValueError):
        pass
    path.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
    urls: List[str] = []
    for i in range(max((len(v) for v in per_board.values()), default=0)):
        urls += [v[i] for v in per_board.values() if i < len(v)]
    return urls[:limit]


def read_results(uid: int, since: str) -> List[Dict[str, str]]:
    path = user_dir(uid) / "applications.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("time", "") >= since]


def record(vera, uid: int, rows: List[Dict[str, str]]) -> None:
    """Sent applications go in Vera's history, so /applications and reminders include them."""
    session = vera.user(uid)
    for r in rows:
        if r.get("status") != "submitted":
            continue
        job = vera.job_by_key(r["url"]) or vera.add_manual_job(r["url"], r.get("title") or "Job", r.get("company") or "", added_by=uid)
        existing = vera.db.application_for_job(job["id"], uid)
        app_id = existing["id"] if existing else vera.db.create_application(job, uid)
        vera.db.update_application(app_id, status="submitted", notes=f"Application round ({r.get('site')}): {r.get('reason', '')}")
        session.set_job_state(job["id"], "applied")


async def _run_engine(uid: int, queue: List[str], budget: int, dry_run: bool, on_line=None) -> str:
    run = _this_run.get() or {}
    folder = user_dir(uid)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(queue, f)
    args = [str(engine_python()), "-m", "vera.apply", "apply", "--background", "--queue-file", f.name, "--max", str(budget)]
    if dry_run:
        args.append("--dry-run")
    slot = run.get("slot") or 1
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(config.ROOT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=config.engine_env(folder, browser_folder(uid, slot), run.get("profile_file")),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    run["proc"] = proc
    lines: List[str] = []

    async def read():
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            lines.append(line)
            if on_line:
                try:
                    await on_line(line)
                except Exception:
                    log.exception("couldn't send a progress update")
        await proc.wait()

    try:
        # Long enough for the requested total; a very large one runs until you say stop.
        await asyncio.wait_for(read(), timeout=None if budget >= NO_LIMIT else max(RUN_TIMEOUT_MINUTES, budget * 6) * 60)
    except asyncio.TimeoutError:
        stop_run(run or None)
        return "TIMEOUT"
    finally:
        run["proc"] = None
        Path(f.name).unlink(missing_ok=True)
    return "\n".join(lines)


def jobs_asking_for_email(uid: int, rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Postings in this run that give an email address. She only tells the person about these: emailing is their call."""
    path = user_dir(uid) / "leads.csv"
    if not path.exists():
        return []
    open_jobs = {r.get("url") for r in rows if r.get("status") != "submitted"}
    with open(path, newline="", encoding="utf-8") as f:
        return list({r["url"]: r for r in csv.DictReader(f) if r.get("url") in open_jobs}.values())


async def run_once(vera, uid: int, dry_run: bool = False, count: Optional[int] = None,
                   sites: Optional[List[str]] = None, on_line=None, run: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One full cycle for one person. Returns a report; never raises for ordinary failures."""
    if run is None:
        if _active:
            return {"error": "another application round is in progress. They run one at a time from here, so try again in a while."}
        run = start_run({"uid": int(uid), "channel_id": None, "channel": None})
    token = _this_run.set(run)
    try:
        session = vera.user(uid)
        profile = write_profile(session, count)
        run["profile_file"] = user_dir(uid) / f"profile-run{run['slot']}.json"     # so another run starting can't change this one's plan
        run["profile_file"].write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        missing = blockers(profile)
        if missing:
            return {"error": "I can't apply for you yet. Still needed: " + ", ".join(missing) + ". Add them with `/apply-details`."}
        start = datetime.now().isoformat(timespec="seconds")
        # Many listings lead to sites she can't fill, so line up plenty.
        only = ("companyforms",) if sites and set(sites) == {"company_forms"} else None
        queue = await board_urls(session, limit=max(40, (count or 0) * 8), remote_only=bool(sites) and set(sites) == {"remote_boards"},
                                 only_sources=only)
        vera.db.set_json(f"rounds_last_start:{uid}", start)
        output = await _run_engine(uid, queue, profile["limits"]["max_per_run"], dry_run, on_line)
        rows = read_results(uid, start)
        if not dry_run:
            record(vera, uid, rows)
        questions = open_questions(rows, uid, profile)
        new_numbers = remember_questions(vera, uid, rows, questions)
        waiting = len({r.get("url") for r in rows if r.get("status") == "needs_answer"})
        vera.db.set_json(f"rounds_last:{uid}", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        status = re.search(r"SITE-STATUS (\{.*\})", output)
        tail = output.strip().splitlines()[-1][:300] if output.strip() else ""
        return {"rows": rows, "dry_run": dry_run, "timed_out": output == "TIMEOUT", "owner": is_owner(uid), "email_jobs": jobs_asking_for_email(uid, rows),
                "stopped": run["stopped"], "ended_early": tail if not status and output != "TIMEOUT" and not run["stopped"] else "",
                "resume": Path(profile.get("resume_path") or "").name,
                "blocked": json.loads(status.group(1))["blocked"] if status else [],
                "tail": output.strip().splitlines()[-1][:300] if output.strip() else "",
                "missing": missing_details(profile), "boards_checked": len(queue),
                "questions": questions[:20], "new_questions": new_numbers, "waiting_jobs": waiting}
    except Exception as e:
        log.exception("application round failed")
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    finally:
        _this_run.reset(token)
        finish_run(run)


async def recommend(vera, uid: int, count: int = 10, sites: Optional[List[str]] = None) -> Dict[str, Any]:
    """Jobs that fit, from the free job APIs, applying to nothing."""
    if not has_room(uid, "recommend"):
        return {"error": "I'm busy with other runs. Say **stop** to end one, or ask again in a while."}
    run = start_run({"uid": int(uid), "channel_id": "recommend", "channel": None})
    token = _this_run.set(run)
    try:
        session = vera.user(uid)
        await scout.refresh_sources(session.db, session.prefs)
        ranked = await asyncio.to_thread(scout.rank, session.db, uid, session.prefs, session.profile, include_seen=True, save=False)
        picks = [{"title": j["title"], "company": j.get("company") or "", "location": j.get("location") or "", "url": j["url"], "score": ev["score"]}
                 for j, ev in ranked if session.db.state(j["id"], uid) not in ("applied", "skipped")
                 and (not sites or "remote_boards" not in sites or j.get("source") in REMOTE_SOURCES)][:count]
        return {"picks": picks[:count], "blocked": []}
    except Exception as e:
        log.exception("recommendations failed")
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    finally:
        _this_run.reset(token)
        finish_run(run)


def recommend_report(res: Dict[str, Any]) -> str:
    if res.get("error"):
        return f"⚠️ {res['error']}"
    picks = res.get("picks") or []
    if not picks:
        return "I didn't find jobs that fit your filters right now. Tell me what to loosen, like *include hybrid jobs*."
    lines = [f"🔎 **{len(picks)} job(s) that fit you:**"]
    for i, p in enumerate(picks, 1):
        where = f" · {p['location'][:40]}" if p.get("location") else ""
        lines.append(f"{i}. **{p['title'][:80]}** at {p.get('company') or '?'}{where}\n<{p['url']}>")
    if res.get("blocked"):
        lines.append(f"🛑 Bot check on {', '.join(res['blocked'])}, so those are missing from this list.")
    lines.append("Nothing was sent. Say *apply to 10 jobs* when you want me to apply.")
    return "\n".join(lines)


def _answer_memory(uid: Optional[int]):
    """This person's saved answers, read the way the form engine reads them."""
    from vera.apply.answers import AnswerMemory
    memory = AnswerMemory.__new__(AnswerMemory)
    try:
        memory.data = json.loads((user_dir(uid) / "answers.json").read_text(encoding="utf-8")) if uid is not None else {}
    except (OSError, ValueError):
        memory.data = {}
    return memory


def open_questions(rows: List[Dict[str, str]], uid: Optional[int] = None, profile: Optional[Dict[str, Any]] = None) -> List[str]:
    """Questions still waiting for them: not ones answered since (saved, said in other words, or in their profile),
    and each question once, even when forms word it differently."""
    from vera.apply.answers import Field, SKIP, _content_words, SIMILAR_ENOUGH, rule_answer
    memory = _answer_memory(uid)
    out: List[str] = []
    seen: List[set] = []
    for q in dict.fromkeys(q.strip() for r in rows if r.get("status") == "needs_answer"
                           for q in (r.get("unanswered") or "").split(" | ") if q.strip()):
        if memory.has(q) or (profile and rule_answer(Field("text", q[:300], [], True), profile) not in (None, SKIP, "")):
            continue
        words = _content_words(q)
        if any(len(words & s) >= 2 and len(words & s) / len(words | s) >= SIMILAR_ENOUGH for s in seen):
            continue                      # the same question asked in other words: once is enough
        seen.append(words)
        out.append(q)
    return out


def last_run_breakdown(vera, uid: int) -> Optional[Dict[str, Any]]:
    """What really happened in their latest run (or the one going now): counts by outcome and the commonest reasons.
    So Vera explains a run with facts, not guesses."""
    start = vera.db.get_json(f"rounds_last_start:{uid}")
    if not start:
        return None
    rows = read_results(uid, start)
    by_status: Dict[str, int] = {}
    reasons: Dict[str, int] = {}
    for r in rows:
        by_status[r.get("status") or "?"] = by_status.get(r.get("status") or "?", 0) + 1
        if r.get("status") != "submitted":
            why = re.sub(r"\(screenshot:.*", "", re.sub(r"'[^']*'", "'…'", (r.get("reason") or "")))[:90].strip()
            key = f"{r.get('status')}: {re.sub(r'[0-9]+', 'N', why)}"
            reasons[key] = reasons.get(key, 0) + 1
    return {"started": start, "jobs_checked": len(rows), "by_outcome": by_status,
            "top_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:8]),
            "duplicates_skipped": sum(n for k, n in reasons.items() if "already" in k or "another run" in k)}


def remember_questions(vera, uid: int, rows: List[Dict[str, str]], questions: Optional[List[str]] = None) -> List[int]:
    """So you can answer in chat by number, e.g. "1: 0 years". Returns the numbers (1-based) of questions never asked
    before: a job waiting on answers comes back every run, but its questions are only put to you once."""
    if questions is None:
        questions = open_questions(rows, uid, build_profile(vera.user(uid)))
    questions = questions[:20]
    vera.db.set_json(f"open_questions:{uid}", questions)
    asked = vera.db.get_json(f"asked_questions:{uid}") or {}
    from vera.apply.answers import norm
    new = [i for i, q in enumerate(questions, 1) if norm(q) not in asked]
    today = datetime.now().date().isoformat()
    asked.update({norm(q): asked.get(norm(q), today) for q in questions})
    vera.db.set_json(f"asked_questions:{uid}", dict(list(asked.items())[-500:]))
    return new


def report(res: Dict[str, Any]) -> str:
    """The message a person gets after a run."""
    if res.get("error"):
        return f"⚠️ Application round: {res['error']}"
    rows = res["rows"]
    by = lambda s: [r for r in rows if r.get("status") == s]
    sent, dry = by("submitted"), by("dry-run")
    lines = [f"🤖 **Application round{' test (nothing was sent)' if res['dry_run'] else ''}:** "
             f"{len(sent)} sent · {len(dry)} ready · {len(by('filtered'))} didn't fit your filters · "
             f"{len(by('skipped'))} skipped · {len(by('needs_answer'))} need your answer · {len(by('failed'))} failed"]
    done = sent + dry

    def job_line(r: Dict[str, str]) -> str:
        where = " · ".join(x for x in ((r.get("site") or "").replace("_", " "), r.get("location")) if x)
        return f"✅ **{r.get('title')}** at {r.get('company') or '?'} ({where}): <{r.get('url')}>"

    if any(r.get("setup") for r in done):
        for setup, icon in (("Remote", "🏠"), ("Hybrid", "🔀"), ("On-site", "🏢"), ("", "❔")):
            group = [r for r in done if (r.get("setup") or "") == setup]
            if group:
                lines.append(f"\n**{icon} {setup or 'Setup not recorded'} ({len(group)})**")
                lines += [job_line(r) for r in group]
    else:
        lines += [job_line(r) for r in done]
    if res.get("resume") and (sent or dry):
        lines.append(f"📄 Résumé: **{res['resume']}**")
    if res.get("stopped"):
        lines.append("🛑 You stopped this run. Anything listed above as sent has already gone out.")
    if res.get("ended_early"):
        lines.append(f"⚠️ **The run ended early, so these results may be incomplete:** {res['ended_early']}")
    if res.get("timed_out"):
        lines.append(f"⏱️ The run was stopped after {RUN_TIMEOUT_MINUTES} minutes.")
    if res.get("blocked"):
        lines.append(f"🛑 **Bot check on {', '.join(res['blocked'])}.** I don't bypass these, so that site rests for 6 hours and I retry after.")
    if by("needs_login"):
        lines.append("🔑 **Some forms asked for a login.** I never sign in for you, so I skipped those. Apply to them yourself if you want them.")
    if res.get("email_jobs"):
        lines.append("📧 **These ask you to email them.** I didn't email anyone. To send one, ask me in the social outreach channel:")
        lines += [f"• **{j.get('title')}** at {j.get('company') or '?'}: {j.get('email')} <{j.get('url')}>" for j in res["email_jobs"][:10]]
    questions = res.get("questions") if res.get("questions") is not None else open_questions(rows)
    # Only questions never put to you before. Numbers stay the same as in the full list, so "3: yes" still works.
    new = res.get("new_questions") if res.get("new_questions") is not None else list(range(1, len(questions) + 1))
    shown = [(i, questions[i - 1]) for i in new if 0 < i <= len(questions)][:8]
    if shown:
        lines.append("❓ **New questions only you can answer** (I already used your saved answers, ones you gave in other words, and your profile). "
                     "Reply like `1: 0 years`, and I'll use it on any form asking the same thing:")
        lines += [f"{i}. {q[:150]}" for i, q in shown]
    older = len(questions) - len(shown)
    if older > 0:
        lines.append(f"⏳ {older} question(s) I asked before are still open"
                     + (f", holding {res['waiting_jobs']} job(s)" if res.get("waiting_jobs") else "")
                     + ". Say *show waiting questions* to see them again, or just ignore them.")
    for r in by("failed")[:3]:
        lines.append(f"❌ {r.get('title') or r.get('url')}: {(r.get('reason') or '')[:150]}")
    if res.get("missing"):
        lines.append("📝 **Missing from your details:** " + ", ".join(res["missing"]))
    if not rows and not res.get("blocked") and not res.get("ended_early"):
        lines.append(f"No new jobs fit your filters this time. {res.get('tail', '')}".strip())
    return "\n".join(lines)


def save_answer(uid: int, question: str, answer: str) -> None:
    """Saved answers beat AI and are reused for that exact question on every form."""
    applier.share_answers(uid, {question: answer})


def status_text(vera, uid: int) -> str:
    session = vera.user(uid)
    auto = session.prefs["rounds"]
    caps = limits_for(uid, auto)
    per_day = "no daily limit" if caps["max_per_day"] >= NO_LIMIT else f"{caps['max_per_day']} per day"
    per_run = "no limit per run" if caps["max_per_run"] >= NO_LIMIT else f"up to {caps['max_per_run']} per run"
    last: Optional[str] = vera.db.get_json(f"rounds_last:{uid}")
    missing = missing_details(build_profile(session))
    return (f"**Application rounds**{' (one is running now)' if running_for(uid) else ''}: only when you ask and approve the plan. "
            "Nothing runs on a schedule.\n"
            f"{per_run}, {per_day}\n"
            "Applies through: company forms (Greenhouse, Lever, Ashby, Workable) from the free job APIs\n"
            f"Cover letters: {'on' if auto.get('cover_letters', True) else 'off'} · last run: {(last or 'never')[:16]}\n"
            + (f"Still needed: {', '.join(missing)} (use `/apply-details`)\n" if missing else "")
            + "Change which jobs it applies to with `/preferences`, or tell me in plain words.\n"
            + ("Try it safely: ask me to apply, then answer the plan with **test**. It fills forms but sends nothing." if last is None else ""))


CONSENT_TEXT = ("**Before I apply for you:** after you approve a plan, I'll submit real applications in your name, using the résumé and "
                "details you give me. Those are stored on the bot owner's PC, where the owner can see them, and your work history and skills go "
                "to free AI services that write your cover letters. Only use your own details. Say **stop** any time to end a round.")
