"""Command-line entry point.

  python -m vera.apply check                show missing profile fields
  python -m vera.apply apply --queue-file F apply to the job links in F (a JSON list from Vera's job APIs)
  python -m vera.apply apply --dry-run      fill forms but stop before Submit
  python -m vera.apply apply --url URL      apply to specific job URLs (repeatable)
  python -m vera.apply apply --background   keep the browser window off-screen
  python -m vera.apply log                  show recent results
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import logging
import random
import re
import shutil
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from vera.apply import config, filters, netguard
from vera.apply.answers import SENSITIVE_RE, SKIP, AnswerMemory, Field, rule_answer
from vera.apply.form_engine import SUBMIT_UNCONFIRMED, run_application
from vera.apply.sites import SiteBlocked, check_blocked, site_for

log = logging.getLogger("agent")
LOG_FIELDS = ["time", "site", "status", "title", "company", "url", "reason", "unanswered", "setup", "location"]
COOLDOWN_HOURS = 6     # leave a site alone this long after a bot check
REPEAT_COOLDOWN_HOURS = 24      # a second check within a day: leave it for a full day
MAX_PER_COMPANY = 2    # applications to one company per run, so one advertiser's many ads can't use up a whole run
MAX_RETRIES = 3        # attempts for a job that keeps failing or waiting for an answer
SAME_FAILURE_LIMIT = 2  # a job that failed this many times the same way isn't opened again
MAX_ANSWER_TRIES = MAX_RETRIES + 2   # a job waiting for your answers gets a couple more tries once you've answered
WAITING = "still waiting for your answer, so I didn't open it again"   # logged for a held-back job; not an attempt
# A promise that no AI helped: Vera can't honestly make it, so those jobs are left to the person.
PLEDGE_RE = re.compile(r"own words|plagiari|use of ai|\bai\b.{0,40}(generated|disqualif)|generated content|without (the use of )?ai|chatgpt", re.I)
# When answers started drawing on the person's earlier answers: jobs waiting since before then get one more try.
ANSWERS_IMPROVED = "2026-11-07T16:30:00"


def round_robin(lists: List[List[str]]) -> List[str]:
    """One from each list in turn, so a total is spread across sources instead of used up by the first."""
    out: List[str] = []
    for i in range(max((len(x) for x in lists), default=0)):
        out += [x[i] for x in lists if i < len(x)]
    return list(dict.fromkeys(out))


def block_key(site, url: str) -> str:
    """Remote boards are separate websites, so a bot check on one mustn't rest the others."""
    return urlparse(url).netloc.removeprefix("www.") if site.name == "remote_board" else site.name


def job_summary(job: Dict[str, str]) -> Dict[str, str]:
    return {k: job.get(k, "") for k in ("title", "company", "location", "url")}


def company_key(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip()


ANONYMOUS_COMPANIES = {"private advertiser", "confidential", "confidential company", "anonymous"}


def title_key(title: str) -> str:
    """Titles match even if a site puts an invisible space in one: the same ad got applied to twice because of a non-breaking space."""
    return " ".join(unicodedata.normalize("NFKC", title or "").lower().split())


def too_many(job: Dict[str, str], applied: Dict[str, int], seen: set, cap: int = MAX_PER_COMPANY) -> str:
    """Same ad seen again, or this person's limit for one company reached (cap 0 = no limit)."""
    company = company_key(job.get("company", ""))
    if not company:
        return ""
    title = title_key(job.get("title", ""))
    # "Private Advertiser" is many employers, so a short common title from one could be a different job. A long, specific one is the same ad.
    if (company, title) in seen and (company not in ANONYMOUS_COMPANIES or len(title) >= 25):
        return "same job already seen"
    if cap and applied.get(company, 0) >= cap:
        return f"already sent {cap} applications to {job.get('company')} in this run"
    return ""


def save_leads(job: Dict[str, str]) -> None:
    """Hiring emails published in a posting, for optional outreach later."""
    found = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", job.get("description", ""))
    if not found:
        return
    path = config.DATA_DIR / "leads.csv"
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["email", "company", "title", "url"])
        if new:
            w.writeheader()
        for email in dict.fromkeys(e.strip(".").lower() for e in found):
            w.writerow({"email": email, "company": job.get("company", ""), "title": job.get("title", ""), "url": job.get("url", "")})


def error_kind(reason: str) -> str:
    """'stuck on a step: Missing entry...' and 'gave up after 15 steps (screenshot: x.png)' -> what kind of failure it was."""
    text = re.sub(r"\(screenshot:[^)]*\)", "", (reason or "").strip()).splitlines()[0] if (reason or "").strip() else ""
    return re.sub(r"\d+", "#", text.split(":")[0]).strip().lower()[:60]


def plain_error(e: Exception) -> str:
    """An error in words a person can read, instead of the browser's call log."""
    text = str(e)
    if type(e).__name__ == "TimeoutError" and "click" in text:
        return "TimeoutError: the form's Next/Submit button never became clickable (something covered it, or it stayed greyed out)"
    if type(e).__name__ == "TimeoutError" and "goto" in text:
        return "TimeoutError: the page took too long to load"
    if "Target page, context or browser has been closed" in text:
        return "the page closed before the form was finished"
    return f"{type(e).__name__}: {text.splitlines()[0][:200] if text else ''}"


def held_back(rows: List[dict], answerable) -> Optional[tuple]:
    """Why a job isn't worth opening again, from its earlier attempts: (status, reason, questions), or None to try it.
    answerable(question) says whether there's an answer now, saved by you or from your profile."""
    tries = [r for r in rows if r.get("status") in ("failed", "needs_answer") and not (r.get("reason") or "").startswith(WAITING)]
    if not tries:
        return None
    if any(SUBMIT_UNCONFIRMED in (r.get("reason") or "") for r in tries):
        return ("skipped", "Submit was already clicked on an earlier try and I couldn't confirm it went through, "
                           "so I won't send it again. Check your email or the company's site", [])
    last = tries[-1]
    if last["status"] == "needs_answer":
        questions = [q.strip() for q in (last.get("unanswered") or "").split(" | ") if q.strip()]
        missing = [q for q in questions if not answerable(q)]
        if any(PLEDGE_RE.search(q) for q in missing):
            # "I promise I wrote this myself, without AI": Vera's answers are AI-assisted, so she can't honestly agree.
            return ("skipped", "the form asks you to promise you wrote every answer yourself, without AI. My answers are "
                               "AI-assisted, so I can't honestly agree. Apply to this one yourself if you want it", [])
        if missing and last.get("time", "") < ANSWERS_IMPROVED and len(tries) < MAX_ANSWER_TRIES and \
                not any(SENSITIVE_RE.search(q.lower()) for q in missing):
            return None        # one more try: answers can now build on what you've said before
        if missing:
            return ("needs_answer", WAITING, missing)
        if len(tries) >= MAX_ANSWER_TRIES:
            return ("skipped", f"tried {len(tries)} times and the form still wasn't accepted, so I stopped retrying it", [])
        return None
    kinds: Dict[str, int] = {}
    for r in tries:
        if r["status"] == "failed":
            kinds[error_kind(r.get("reason", ""))] = kinds.get(error_kind(r.get("reason", "")), 0) + 1
    kind, times = max(kinds.items(), key=lambda kv: kv[1])
    if times >= SAME_FAILURE_LIMIT:
        return ("skipped", f"failed the same way {times} times ({kind}), so I stopped retrying it", [])
    if len(tries) >= MAX_RETRIES:
        return ("skipped", f"failed {len(tries)} times, so I stopped retrying it", [])
    return None


def sort_out(urls: List[str], held: Dict[str, tuple], state: Dict[str, str], blocked: List[str]):
    """Split lined-up jobs into the ones she'll really open and the ones left out before opening anything, by reason.
    So "found 30 jobs" means 30 she'll check, not 30 of which most are dropped unseen."""
    queue: List[str] = []
    left_out: Dict[str, List[str]] = {}
    resting: List[str] = []
    for url in urls:
        why = "held" if url in held else None
        site = site_for(url) if not why else None
        if not why and not site:
            why = "unsupported"
        elif not why:
            key = block_key(site, url)
            if key in blocked or cooling(state, key):
                why = "resting"
                resting.append(key)
        if why:
            left_out.setdefault(why, []).append(url)
        else:
            queue.append(url)
    return queue, left_out, list(dict.fromkeys(resting))


def left_out_lines(lined_up: List[str], left_out: Dict[str, List[str]], held: Dict[str, tuple], resting: List[str]) -> List[str]:
    """Say what was left out and why, instead of dropping jobs silently."""
    if not left_out:
        return []
    waiting = [u for u in left_out.get("held", []) if held[u][0][0] == "needs_answer"]
    stopped = [u for u in left_out.get("held", []) if held[u][0][0] != "needs_answer"]
    parts = [f"{len(left_out[k])} on sites resting after a bot check ({', '.join(resting)})" if k == "resting" else
             f"{len(left_out[k])} on sites I can't fill" for k in ("resting", "unsupported") if left_out.get(k)]
    if waiting:
        parts.append(f"{len(waiting)} still waiting for your answers")
    if stopped:
        parts.append(f"{len(stopped)} that kept failing")
    lines = ["LEFT-OUT " + json.dumps({"lined_up": len(lined_up), **{k: len(v) for k, v in left_out.items()}}),
             f"Of {len(lined_up)} job(s) lined up, I left out " + "; ".join(parts) + "."]
    for url in left_out.get("held", []):
        (status, reason, questions), last = held[url]
        lines.append(f"  - {last.get('title') or url} ({last.get('company') or '?'}): {reason}"
                     + (f". Waiting on: {' | '.join(q[:100] for q in questions[:3])}" if questions else ""))
    return lines


def load_state() -> Dict[str, str]:
    try:
        return json.loads(config.STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: Dict[str, str]) -> None:
    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def cool_down(state: Dict[str, str], site: str, hours: float = COOLDOWN_HOURS) -> None:
    state[site] = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    save_state(state)


def rest(state: Dict[str, str], site: str) -> None:
    """A bot check: leave this site alone for a while, and say so, so nobody thinks she's just slow.
    A second rest within a day means a full day."""
    state.update(load_state())     # another run may have added to it
    last = state.get(f"_rested:{site}")
    again = bool(last) and datetime.now() - datetime.fromisoformat(last) < timedelta(hours=24)
    state[f"_rested:{site}"] = datetime.now().isoformat(timespec="seconds")
    cool_down(state, site, REPEAT_COOLDOWN_HOURS if again else COOLDOWN_HOURS)
    print(f"RESTING {site} {state[site]}", flush=True)


def cooling(state: Dict[str, str], site: str) -> bool:
    return bool(state.get(site)) and datetime.fromisoformat(state[site]) > datetime.now()


# --------------------------------------------------------------------------- #
# Log (CSV) helpers
# --------------------------------------------------------------------------- #
def read_log() -> List[dict]:
    if not config.LOG_PATH.exists():
        return []
    with open(config.LOG_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def upgrade_log() -> None:
    """Older logs don't have the newest columns. Adding to one without fixing its header would put values under the wrong names."""
    if not config.LOG_PATH.exists():
        return
    with open(config.LOG_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == LOG_FIELDS or not reader.fieldnames:
            return
        rows = list(reader)
    backup = config.LOG_PATH.with_suffix(".before-upgrade.csv")
    if not backup.exists():
        shutil.copy2(config.LOG_PATH, backup)
    temp = config.LOG_PATH.with_suffix(".upgrading")
    with open(temp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in LOG_FIELDS})
    temp.replace(config.LOG_PATH)


def write_log(row: dict) -> None:
    upgrade_log()
    new = not config.LOG_PATH.exists()
    with open(config.LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in LOG_FIELDS})


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #
async def open_browser(pw, headless: bool, background: bool = False):
    """Persistent profile so logins survive between runs. Uses the Microsoft
    Edge installed on Windows (free); falls back to Playwright's Chromium.
    background=True places the window off-screen so it doesn't get in your way."""
    config.ensure_dirs()
    # Every connection goes through Vera's filtering proxy (public internet only, checked per connection), so a
    # redirect, pop-up, websocket or restored tab can't reach this laptop or the home network.
    proxy = netguard.FilterProxy()
    server = await proxy.start()
    kwargs = dict(user_data_dir=str(config.BROWSER_PROFILE_DIR), headless=headless,
                  viewport={"width": 1280, "height": 900}, locale="en-US",
                  chromium_sandbox=True,    # Playwright turns the browser's sandbox off unless told otherwise
                  proxy={"server": server},
                  args=(["--window-position=-32000,-32000"] if background else []) + netguard.BROWSER_ARGS)
    # No per-request interception (context.route): stepping into every request can break a form's final submit.
    # The proxy alone filters every connection, including service workers and redirects, and job links are checked
    # before they're opened.
    try:
        context = await pw.chromium.launch_persistent_context(channel="msedge", **kwargs)
    except Exception as e:
        log.info(f"Edge not available ({e.__class__.__name__}); using Playwright Chromium")
        context = await pw.chromium.launch_persistent_context(**kwargs)
    context._vera_proxy = proxy               # lives as long as the browser
    if background:
        _to_agents_desktop()
    return context


_desktop_watch = None


def _to_agents_desktop() -> None:
    """The owner's wish (13 Nov 2026): agents' browsers go on their own Windows virtual desktop ("Agents"), not his.
    Shared helper in hermes\\tools\\agent-desktop (standard library, no install). If it fails, the window just stays
    off-screen as before. AGENT_DESKTOP=off turns it off."""
    global _desktop_watch
    if _desktop_watch is not None or os.name != "nt":
        return
    try:
        import importlib.util
        path = Path(__file__).resolve().parents[5] / "tools" / "agent-desktop" / "agent_desktop.py"
        spec = importlib.util.spec_from_file_location("agent_desktop", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _desktop_watch = mod.watch(os.getpid()) or False
    except Exception as e:
        _desktop_watch = False
        log.info(f"Agents desktop not used ({e.__class__.__name__}); the browser stays off-screen")


CLAIM_HOURS = 3


class _Done(Exception):
    """This job is finished before its page was opened (e.g. another run has it)."""


def _claim_names(url: str, job: Dict[str, str]) -> List[str]:
    keys = [url]
    company, title = company_key(job.get("company", "")), title_key(job.get("title", ""))
    if company and (company not in ANONYMOUS_COMPANIES or len(title) >= 25):
        keys.append(f"{company}|{title}")
    return [hashlib.sha256(k.encode("utf-8")).hexdigest()[:32] for k in keys]


def claim_job(url: str, job: Dict[str, str], have_link: bool = False) -> bool:
    """Runs side by side share these, so two runs can't both apply to the same job, or to the same ad on two links.
    have_link: this run already claimed the link itself (before opening it), so only the ad is claimed now."""
    folder = config.DATA_DIR / "claims"
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now().timestamp()
    made = []
    for name in _claim_names(url, job)[1 if have_link else 0:]:
        path = folder / name
        try:
            if path.exists() and now - path.stat().st_mtime > CLAIM_HOURS * 3600:
                path.unlink(missing_ok=True)      # left by a run that was stopped. A sent job is in the log by then anyway
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            made.append(path)
        except FileExistsError:
            for other in made:
                other.unlink(missing_ok=True)
            return False
    return True


def release_job(url: str, job: Dict[str, str]) -> None:
    """Not sent, so another run may try it. Sent jobs stay claimed."""
    for name in _claim_names(url, job):
        (config.DATA_DIR / "claims" / name).unlink(missing_ok=True)


def watch_tabs(page, tabs: list) -> None:
    """Keep track of this job's tab and any pop-ups it opens, so finishing one job can't close another job's tabs."""
    tabs.append(page)
    page.on("popup", lambda opened: watch_tabs(opened, tabs))


async def close_own(tabs: list) -> None:
    for tab in list(tabs):
        try:
            await tab.close()
        except Exception:
            pass       # it may have closed itself already
    tabs.clear()


async def close_tabs(ctx, keep) -> None:
    """Close every tab except `keep`, a blank one, because the browser quits when its last tab closes."""
    for tab in list(ctx.pages):
        if tab is not keep:
            try:
                await tab.close()
            except Exception:
                pass       # it may have closed itself already


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_check(_args):
    profile = config.load_profile()
    missing = config.missing_fields(profile)
    from vera.apply import llm
    print("Profile:", config.PROFILE_PATH)
    print("AI providers:", ", ".join(llm.configured()) or "NONE - cover letters use a template; unknown questions wait for you")
    print("Search:", json.dumps(profile.get("search", {})))
    print("Limits:", json.dumps(profile["limits"]))
    if missing:
        print("\nMissing or invalid fields (forms that require them will be skipped):")
        for m in missing:
            print("  -", m)
    else:
        print("\nAll important fields are filled in.")


async def cmd_apply(args):
    from playwright.async_api import async_playwright

    profile = config.load_profile()
    missing = config.missing_fields(profile)
    if any(m.startswith("resume_path") for m in missing):
        sys.exit("No résumé file found. Add one with /resume add.")
    limits = profile["limits"]
    history = read_log()
    state = load_state()
    # Changing your filters gives previously filtered jobs another look.
    spec = hashlib.sha256(json.dumps(profile.get("search", {}), sort_keys=True).encode()).hexdigest()[:12]
    if state.get("_spec") != spec:
        state.update(_spec=spec, _spec_time=datetime.now().isoformat(timespec="seconds"))
        save_state(state)
    done_urls = {r["url"] for r in history
                 if r["status"] in ("submitted", "skipped")
                 or (r["status"] == "filtered" and r["time"] >= state["_spec_time"] and not re.search("no location listed|another run", r.get("reason") or ""))}
    memory = AnswerMemory()

    def answerable(question: str) -> bool:
        """A saved answer, or one the profile gives (the rules can change, e.g. a new question they now cover)."""
        if memory is not None and memory.has(question):
            return True
        return any(rule_answer(Field(kind, question[:300], [], True), profile) not in (None, SKIP, "") for kind in ("text", "checkbox"))
    # Jobs that keep failing the same way, or wait for answers only you can give, aren't opened again until something changes.
    attempts: Dict[str, List[dict]] = {}
    for r in history:
        if r.get("url") and r["url"] not in done_urls:
            attempts.setdefault(r["url"], []).append(r)
    held: Dict[str, tuple] = {}
    for url, rows in attempts.items():
        verdict = held_back(rows, answerable)
        if verdict:
            held[url] = (verdict, rows[-1])
    today = date.today().isoformat()
    sent_today = sum(1 for r in history if r["status"] == "submitted" and r["time"].startswith(today))
    budget = min(args.max or limits["max_per_run"], max(0, limits["max_per_day"] - sent_today))
    if budget <= 0 and not args.dry_run:
        print(f"Daily limit reached ({limits['max_per_day']}). Change limits.max_per_day to send more.")
        return

    search = profile.get("search", {})
    explicit = list(args.url or [])    # jobs you picked yourself skip the filters and cooldowns
    blocked_now: List[str] = []

    async with async_playwright() as pw:
        ctx = await open_browser(pw, headless=args.headless, background=args.background)
        # Each job gets its own tab, closed when it's done. This blank one just keeps the browser open.
        base = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await close_tabs(ctx, base)      # anything restored from last time
        await base.goto("about:blank")

        # 1) The jobs to check: links from Vera's free job APIs (company forms and remote boards), handed over in a file.
        boards: List[List[str]] = []
        if args.queue_file:
            boards.append(json.loads(Path(args.queue_file).read_text(encoding="utf-8")))
        lined_up = [u for u in round_robin([[u for u in b if u not in done_urls] for b in boards]) if u not in explicit]
        queue, left_out, resting = sort_out(lined_up, held, state, blocked_now)
        queue = explicit + queue
        for url in left_out.get("held", []):
            (status, reason, questions), last = held[url]
            write_log({"time": datetime.now().isoformat(timespec="seconds"), "site": last.get("site", ""), "status": status, "reason": reason,
                       "title": last.get("title", ""), "company": last.get("company", ""), "url": url, "unanswered": " | ".join(questions),
                       "setup": last.get("setup", ""), "location": last.get("location", "")})
        for line in left_out_lines(lined_up, left_out, held, resting):
            print(line, flush=True)
        if not queue:
            print("No new jobs to apply to.")
            print("SITE-STATUS " + json.dumps({"blocked": list(dict.fromkeys(blocked_now + resting))}))
            await ctx.close()
            return

        # Vera posts this, so a long run doesn't look stuck. Only jobs she'll really open count.
        print(f"QUEUE {len(queue)}", flush=True)
        # 2) Apply: a few jobs at once, each in its own tab, a few per site at most.
        sent = 0
        applied_to: Dict[str, int] = {}
        # Jobs sent in earlier runs count too, so an ad posted again doesn't get a second application.
        seen_jobs: set = {(company_key(r.get("company", "")), title_key(r.get("title", ""))) for r in history if r.get("status") == "submitted"}
        try:
            known_locations = json.loads((config.DATA_DIR / "known_locations.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known_locations = {}
        max_tabs = max(1, int(limits.get("tabs") or 1))
        tabs_per_site = max(1, int(limits.get("tabs_per_site") or max_tabs))

        async def apply_to(i: int, url: str, site, key: str) -> None:
            nonlocal sent
            row = {"time": datetime.now().isoformat(timespec="seconds"), "site": site.name, "url": url}
            own_tabs: list = []
            claimed = None
            page = work = None
            tried_form = False
            heading: List[str] = []
            url_claimed = False
            try:
                # Another run side by side may have this link open already: check before opening it, not after.
                if not netguard.url_is_safe(url):
                    row.update(status="filtered", reason="the link isn't a public website (it points at this computer or a home network)")
                    raise _Done
                if not claim_job(url, {}):
                    row.update(status="filtered", reason="another run is already handling this job")
                    raise _Done
                url_claimed = True
                page = work = await ctx.new_page()
                watch_tabs(page, own_tabs)
                await page.goto(url, wait_until="domcontentloaded")
                await asyncio.sleep(2.5)
                await check_blocked(page, site.name)
                job = await site.job_info(page)
                job["location"] = job.get("location") or known_locations.get(url, "")     # some forms don't show one, but the job list had it
                row.update(title=job["title"], company=job["company"], location=job.get("location", ""), setup=filters.detect_setup(job) or "On-site")
                heading = [f"\n[{i + 1}/{len(queue)}] {site.name}: {job['title']} - {job['company']}",
                           f"  where: {' · '.join(x for x in (row['setup'], ' '.join(row['location'].split())) if x)}"]
                why_not = None if url in explicit else (filters.rejects(job, search) or too_many(job, applied_to, seen_jobs, limits.get("max_per_company", MAX_PER_COMPANY)))
                seen_jobs.add((company_key(job.get("company", "")), title_key(job.get("title", ""))))
                if why_not:
                    row.update(status="filtered", reason=why_not)
                elif not claim_job(url, job, url_claimed):
                    row.update(status="filtered", reason="another run is already handling this job")
                else:
                    claimed = job
                    save_leads(job)
                    opened = await site.open_apply(page)
                    if isinstance(opened, str):
                        # "Not found" may just mean the site changed its layout, so it stays
                        # retryable. A job that truly goes elsewhere is never revisited.
                        row.update(status="failed" if "found" in opened else "skipped", reason=opened)
                    else:
                        tried_form = True
                        work = opened or page
                        result = await run_application(work, profile, memory, job, dry_run=args.dry_run)
                        row.update(status=result.status, reason=result.reason, unanswered=" | ".join(result.unanswered))
                        # A test counts forms it finished, so it stops at the same limit as a real run.
                        if result.status == "submitted" or (args.dry_run and result.status == "ready"):
                            sent += 1
                            applied_to[company_key(job.get("company", ""))] = applied_to.get(company_key(job.get("company", "")), 0) + 1
                        elif result.status == "blocked":
                            rest(state, key)
                            blocked_now.append(key)
            except _Done:
                pass
            except SiteBlocked as e:
                row.update(status="blocked", reason=str(e))
                rest(state, key)
                blocked_now.append(key)
            except Exception as e:
                row.update(status="failed", reason=plain_error(e))
            if work is not None and (args.debug or row.get("status") in ("failed",)):
                shot = config.SCREENSHOT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{site.name}.png"
                try:
                    await work.screenshot(path=str(shot), full_page=True)
                    row["reason"] = f"{row.get('reason', '')} (screenshot: {shot.name})"
                except Exception:
                    pass
            await close_own(own_tabs)
            if (claimed is not None or url_claimed) and row.get("status") != "submitted":
                release_job(url, claimed or {})
            if args.dry_run and row.get("status") == "ready":
                row["status"] = "dry-run"
            write_log(row)
            # Printed together with no pause, so lines from jobs running side by side can't mix.
            # The info line gives Vera the pieces for her per-job post (title, company, setup, location; never the link).
            info = {k: " ".join(str(row.get(k) or "").split()) for k in ("title", "company", "setup", "location")}
            lines = heading + ["  info: " + json.dumps(info, ensure_ascii=False), f"  -> {row['status'].upper()}: {row.get('reason', '')}"]
            if row.get("unanswered"):
                lines.append(f"     unanswered: {row['unanswered']}")
            print("\n".join(lines), flush=True)
            if sent < budget:
                lo, hi = limits["delay_seconds"] if tried_form else (3, 8)
                await asyncio.sleep(random.uniform(lo, hi))

        waiting = list(enumerate(queue))
        running: Dict[asyncio.Task, str] = {}
        while True:
            state.update(load_state())     # another run may have hit a bot check
            for item in list(waiting):
                if len(running) >= max_tabs or sent + len(running) >= budget:
                    break
                i, url = item
                site = site_for(url)
                if not site:
                    print(f"Skipping unsupported site: {url}")
                    waiting.remove(item)
                    continue
                key = block_key(site, url)
                if url not in explicit and (key in blocked_now or cooling(state, key)):
                    waiting.remove(item)
                    continue       # retried on a later run, once the site has rested
                if sum(1 for k in running.values() if k == key) >= tabs_per_site:
                    continue       # this site is busy, so give another one a turn
                waiting.remove(item)
                running[asyncio.create_task(apply_to(i, url, site, key))] = key
            if not running:
                break
            finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                running.pop(task)
                if task.exception():
                    print(f"A job stopped unexpectedly: {task.exception()}")

        await ctx.close()
    print("SITE-STATUS " + json.dumps({"blocked": list(dict.fromkeys(blocked_now + resting))}))
    print(f"\nDone. Submitted {sent} application(s). Full log: {config.LOG_PATH}")


def cmd_log(args):
    rows = read_log()[-args.n:]
    if not rows:
        print("No applications logged yet.")
    for r in rows:
        print(f"{r['time']}  {r['status']:<9} {r['site']:<9} {r['title'][:45]:<45}  {r['company'][:25]:<25} {r['reason'][:60]}")


def main():
    p = argparse.ArgumentParser(prog="python -m vera.apply", description="Vera's form engine: fills company application forms (Greenhouse, Lever, Ashby, Workable)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="check your profile for missing fields")
    a = sub.add_parser("apply", help="apply to the jobs Vera lined up")
    a.add_argument("--url", action="append", help="apply to this job URL (repeatable)")
    a.add_argument("--max", type=int, help="max applications this run")
    a.add_argument("--dry-run", action="store_true", help="fill forms but do NOT submit")
    a.add_argument("--headless", action="store_true", help="hide the browser window")
    a.add_argument("--background", action="store_true", help="keep the browser window off-screen")
    a.add_argument("--queue-file", help="JSON list of job URLs to try (used by Vera)")
    a.add_argument("--debug", action="store_true", help="save a screenshot for every job")
    lg = sub.add_parser("log", help="show recent results")
    lg.add_argument("-n", type=int, default=25)
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    for stream in (sys.stdout, sys.stderr):      # job titles can contain any character
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        if args.cmd == "check":
            cmd_check(args)
        elif args.cmd == "apply":
            asyncio.run(cmd_apply(args))
        elif args.cmd == "log":
            cmd_log(args)
    except config.ProfileError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
