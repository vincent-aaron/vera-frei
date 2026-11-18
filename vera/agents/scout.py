"""Scout agent: finds jobs on the enabled sources using the CURRENT preferences,
removes duplicates, scores them and returns the best new matches."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from vera import config
from vera.agents.evaluator import evaluate
from vera.db import DB
from vera.sources import SOURCES
from vera.sources.base import http_client

log = logging.getLogger("vera.scout")

CLOSED_RE = re.compile(r"no longer (available|accepting)|position (has been )?filled|job (has )?expired|"
                       r"this job (is|has been) (closed|removed)|job not found|listing (has )?expired", re.I)


def split_query(query: Optional[str]) -> List[str]:
    """'software developer or ai automation' -> ['software developer', 'ai automation']."""
    if not query:
        return []
    parts = re.split(r"\s+or\s+|,|/|;|\|", query, flags=re.I)
    return [p.strip() for p in parts if p.strip()][:5]


async def refresh_sources(db: DB, prefs: Dict[str, Any], query: Optional[str] = None,
                          force: bool = False) -> Dict[str, str]:
    """Fetch from each enabled source (unless cached recently). Returns per-source status."""
    queries = split_query(query) or (prefs.get("positions") or ["developer"])[:4]
    country = next((l.get("country") for l in prefs.get("locations", []) if l.get("country")), "")
    status: Dict[str, str] = {}
    async with http_client() as client:
        async def run(src, q):
            age = db.cache_age_hours(src.name, f"{q.lower()}|{country.lower()}")
            if not force and age is not None and age < src.min_refresh_hours:
                return src.name, q, None, "cached"
            try:
                jobs = await src.fetch(client, q, country)
                return src.name, q, jobs, None
            except Exception as e:
                return src.name, q, None, f"error: {type(e).__name__}"
        tasks = [run(SOURCES[s], q) for s in prefs.get("sources", []) if s in SOURCES for q in queries]
        for name, q, jobs, err in await asyncio.gather(*tasks):
            if jobs is None:
                status[name] = status.get(name) if status.get(name, "").startswith("+") else err
                continue
            new = 0
            for j in jobs:
                if not j.get("url") or not j.get("title"):
                    continue
                dup = db.find_by_fingerprint(j["fingerprint"])
                if dup and dup["id"] != j["id"] and dup.get("source") != "manual":     # a hand-added job never hides the real one
                    continue          # same job already stored from another board
                new += db.upsert_job(j)
            db.mark_fetched(name, f"{q.lower()}|{country.lower()}")
            prev = int(status.get(name, "+0").lstrip("+").split()[0]) if status.get(name, "").startswith("+") else 0
            status[name] = f"+{prev + new} new"
    return status


def days_since_posted(job: Dict[str, Any]) -> Optional[float]:
    """Age of the posting in days (None if the source gave no date)."""
    raw = (job.get("posted_at") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)


def rank(db: DB, uid: int, prefs: Dict[str, Any], profile: Dict[str, Any], query: Optional[str] = None,
         include_seen: bool = False, save: bool = True) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Score every stored job from enabled sources (+ your manual ones) and sort best-first."""
    jobs = db.jobs_for_sources(list(prefs.get("sources", [])) + ["manual"])
    alternatives = [a.lower().split() for a in split_query(query)]
    max_age = prefs.get("max_age_days")
    out = []
    for job in jobs:
        # A job added by hand belongs to whoever added it (they get a "posted" state then). Anyone else's link
        # never reaches this person's list or runs: it could point at a site built to collect their details.
        if job.get("source") == "manual":
            owners = [int(x) for x in db.get_json(f"manual_by:{job['id']}") or []]
            if (int(uid) not in owners) if owners else db.state(job["id"], uid) is None:
                continue
        if max_age and job.get("source") != "manual":
            age = days_since_posted(job)
            if age is None or age > float(max_age):
                continue          # too old (or undated): never recommended
        if alternatives:
            text = f"{job['title']} {' '.join(job.get('tags') or [])} {job.get('description', '')[:3000]}".lower()
            if not any(all(w in text for w in alt) for alt in alternatives):
                continue
        if not include_seen and db.state(job["id"], uid) in ("skipped", "saved", "applying", "applied", "posted"):
            continue
        ev = evaluate(job, prefs, profile)
        if save:
            db.save_evaluation(job["id"], uid, ev["score"], ev)
        if ev["recommended"]:
            out.append((job, ev))
    out.sort(key=lambda p: p[1]["score"], reverse=True)
    return out


CHECK_BYTES = 2_000_000


async def check_available(db: DB, job: Dict[str, Any]) -> Optional[bool]:
    """True = open, False = closed (and marked unavailable), None = couldn't tell."""
    if job.get("source") == "manual":
        return None
    from types import SimpleNamespace
    from urllib.parse import urljoin, urlparse
    from vera.agents import web
    url, status, body = job["url"], None, b""
    try:
        # Public internet only, checked on every connection; plain (uncompressed) and at most 2 MB, so a page built
        # to be huge can't fill the laptop's memory.
        async with web.safe_client(timeout=20, follow_redirects=False,
                                   headers={"User-Agent": config.USER_AGENT, "Accept-Encoding": "identity"}) as c:
            for _ in range(5):
                if urlparse(url).scheme not in ("http", "https"):
                    return None
                async with c.stream("GET", url) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    status = r.status_code
                    if r.headers.get("content-encoding", "identity").lower() not in ("identity", ""):
                        return None              # asked for plain; a compressed reply could unpack into gigabytes
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > CHECK_BYTES:
                            break
                    break
    except httpx.HTTPError:
        return None
    if status is None:
        return None
    r = SimpleNamespace(status_code=status, text=body[:CHECK_BYTES].decode("utf-8", errors="replace"))
    if r.status_code in (404, 410):
        closed = True
    elif r.status_code >= 400:
        return None               # blocked / rate-limited: unknown, not closed
    else:
        closed = bool(CLOSED_RE.search(r.text[:200000]))
    if closed:
        db.execute("UPDATE jobs SET available=0 WHERE id=?", (job["id"],))
        return False
    return True


async def find_jobs(db: DB, uid: int, query: Optional[str] = None, limit: Optional[int] = None,
                    force: bool = False) -> Dict[str, Any]:
    prefs, profile = db.get_prefs(uid), db.get_profile(uid)
    status = await refresh_sources(db, prefs, query, force)
    ranked = rank(db, uid, prefs, profile, query)
    want = limit or int(prefs.get("results_per_search", 5))
    picks = []
    for job, ev in ranked:
        if len(picks) >= want:
            break
        avail = await check_available(db, job)
        if avail is False:
            continue
        ev["available"] = avail
        picks.append((job, ev))
    return {"status": status, "total_matches": len(ranked), "picks": picks}
