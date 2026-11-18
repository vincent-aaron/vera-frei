"""More free job sources: no key and no account.

CompanyForms: companies' own public job lists on Greenhouse, Lever and Ashby. These links go straight
to forms Vera can fill.
HNHiring: Hacker News' monthly "Who is hiring?" thread, with many remote jobs and published hiring emails.
"""

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from vera.sources.base import Source, clean_html

# Companies whose public lists had remote technical jobs when checked on 5 Nov 2026.
# turing (Greenhouse) and coins (Lever, Coins.ph in Taguig) added 7 Nov 2026: they had developer jobs open to the Philippines.
GREENHOUSE = ("gitlab", "canonical", "datadog", "twilio", "dropbox", "grafanalabs", "discord", "reddit", "pinterest", "instacart", "affirm", "bitwarden", "turing")
ASHBY = ("ramp", "notion", "linear", "posthog", "supabase", "replit", "zapier", "openai", "cohere", "perplexity", "render", "airbyte", "resend")
LEVER = ("coins",)
# Workable's public job search (jobs.workable.com), no key: hundreds of jobs in the Philippines, remote and in Metro Manila,
# each with a form on Workable that Vera can fill.
WORKABLE_SEARCH = "https://jobs.workable.com/api/v1/jobs"
WORKABLE_PAGES = 6        # its public search, no account: more pages means more Philippine and remote jobs
LEVEL_WORDS = {"junior", "jr", "entry", "level", "fresh", "graduate", "associate", "trainee", "intern", "remote", "senior", "sr", "lead"}
CACHE_SECONDS = 3600
_lists: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


def query_words(query: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9+#]+", query.lower().replace("full stack", "fullstack").replace("full-stack", "fullstack")) if w not in LEVEL_WORDS]


def title_fits(title: str, words: List[str]) -> bool:
    have = set(re.findall(r"[a-z0-9+#]+", title.lower().replace("full stack", "fullstack").replace("full-stack", "fullstack")))
    return all(w in have for w in words)


def _name(slug: str) -> str:
    return slug.replace("grafanalabs", "Grafana Labs").replace("-", " ").title() if slug == "grafanalabs" else slug.replace("-", " ").title()


async def _greenhouse(client: httpx.AsyncClient, slug: str) -> List[Dict[str, Any]]:
    r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    r.raise_for_status()
    return [{"id": f"gh-{slug}-{j['id']}", "title": j.get("title"), "company": _name(slug), "location": (j.get("location") or {}).get("name") or "",
             "url": j.get("absolute_url"), "description": j.get("content") or "", "posted_at": j.get("updated_at") or ""} for j in r.json().get("jobs", [])]


async def _ashby(client: httpx.AsyncClient, slug: str) -> List[Dict[str, Any]]:
    r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    r.raise_for_status()
    return [{"id": f"as-{slug}-{j.get('id')}", "title": j.get("title"), "company": _name(slug),
             "location": ("Remote " if j.get("isRemote") else "") + (j.get("location") or ""), "url": j.get("jobUrl"),
             "description": j.get("descriptionPlain") or j.get("descriptionHtml") or "", "posted_at": j.get("publishedAt") or ""} for j in r.json().get("jobs", [])]


async def _lever(client: httpx.AsyncClient, slug: str) -> List[Dict[str, Any]]:
    r = await client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        where = ", ".join(dict.fromkeys(cats.get("allLocations") or [cats.get("location") or ""]))
        setup = {"remote": "Remote ", "hybrid": "Hybrid "}.get(j.get("workplaceType") or "", "")
        posted = datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).isoformat(timespec="seconds") if j.get("createdAt") else ""
        out.append({"id": f"lv-{slug}-{j.get('id')}", "title": j.get("text"), "company": _name(slug), "location": (setup + where).strip(),
                    "url": j.get("hostedUrl"), "description": j.get("descriptionPlain") or j.get("description") or "", "posted_at": posted})
    return out


async def _workable_search(client: httpx.AsyncClient, query: str, country: str) -> List[Dict[str, Any]]:
    out, token = [], None
    for _ in range(WORKABLE_PAGES):
        params = {"query": query, "location": country}
        if token:
            params["pageToken"] = token
        r = await client.get(WORKABLE_SEARCH, params=params)
        r.raise_for_status()
        data = r.json()
        for j in data.get("jobs") or []:
            places = [("Remote" if p == "TELECOMMUTE" else p) for p in j.get("locations") or []]
            setup = {"remote": "Remote", "hybrid": "Hybrid"}.get(j.get("workplace") or "", "")
            where = ", ".join(dict.fromkeys(p for p in [setup] + places if p))
            out.append({"id": f"wk-{j.get('id')}", "title": j.get("title"), "company": (j.get("company") or {}).get("title") or "",
                        "location": where, "url": j.get("url"), "description": j.get("description") or "", "posted_at": j.get("created") or ""})
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def _near_home(location: str, country: str) -> bool:
    """A job in your country, including ones that only name the city ("Taguig City")."""
    if not country:
        return False
    if country.lower() in location.lower():
        return True
    from vera.apply.filters import PLACE_AREAS, _plain_place
    return country.lower() == "philippines" and any(re.search(rf"\b(?:{rx})\b", _plain_place(location)) for rx in PLACE_AREAS.values())


class CompanyForms(Source):
    name = "companyforms"
    min_refresh_hours = 6.0

    async def _all(self, client: httpx.AsyncClient, query: str = "", country: str = "") -> List[Dict[str, Any]]:
        """Each list once an hour, however many job titles you search for."""
        limit = asyncio.Semaphore(6)

        async def one(fetch, *args):
            key = f"{fetch.__name__}:{'|'.join(args)}"
            cached = _lists.get(key)
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                return cached[1]
            async with limit:
                try:
                    jobs = await fetch(client, *args)
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    return []        # one company's list being down shouldn't stop the others
            _lists[key] = (time.monotonic(), jobs)
            return jobs
        searches = [one(_workable_search, query, country)] if query and country else []
        groups = await asyncio.gather(*[one(_greenhouse, s) for s in GREENHOUSE], *[one(_ashby, s) for s in ASHBY],
                                      *[one(_lever, s) for s in LEVER], *searches)
        return [j for g in groups for j in g]

    async def fetch(self, client, query, country=""):
        words = query_words(query)
        out = []
        for j in await self._all(client, query, country):
            j["location"] = j.get("location") or ""
            remote = re.search(r"remote|anywhere|worldwide", j["location"], re.I)
            local = _near_home(j["location"], country)
            if j.get("url") and j.get("title") and (remote or local) and title_fits(j["title"], words):
                out.append(self.job("companyforms", j["id"], title=j["title"], company=j["company"], location=j["location"], remote=int(bool(remote)),
                                    employment_type="", level="", description=j["description"], url=j["url"], tags=[], posted_at=j["posted_at"]))
        return out


FORM_LINK = re.compile(r"https?://(?:[\w.-]+\.)?(?:greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com)/[^\s\"'<>]+", re.I)
ROLE = re.compile(r"\b(developer|engineer|programmer|devops|sre|full.?stack|front.?end|back.?end|automation)\b", re.I)


def parse_post(text: str) -> Optional[Dict[str, str]]:
    """'Acme | Web Developer | REMOTE | Full-time' -> company, title and location."""
    plain = clean_html(text)
    first = plain.split("\n", 1)[0]
    parts = [p.strip() for p in re.split(r"\s*\|\s*", first) if p.strip()]
    if len(parts) < 2:
        return None
    title = next((p for p in parts[1:] if ROLE.search(p) and len(p) < 90), None)
    if not title:
        return None
    place = next((p for p in parts[1:] if re.search(r"remote|onsite|on-site|hybrid", p, re.I)), "")
    return {"company": parts[0][:70], "title": title, "location": place[:90], "plain": plain}


class HNHiring(Source):
    name = "hnhiring"
    min_refresh_hours = 12.0

    async def fetch(self, client, query, country=""):
        found = await client.get("https://hn.algolia.com/api/v1/search_by_date", params={"tags": "story,author_whoishiring", "query": "who is hiring"})
        found.raise_for_status()
        story = next((h for h in found.json().get("hits", []) if h.get("title", "").lower().startswith("ask hn: who is hiring")), None)
        if not story:
            return []
        item = await client.get(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
        item.raise_for_status()
        words = query_words(query)
        out = []
        for post in item.json().get("children", []):
            parsed = parse_post(post.get("text") or "")
            if not parsed or not re.search(r"\bremote\b", parsed["location"], re.I) or not title_fits(parsed["title"], words):
                continue
            form = FORM_LINK.search(post.get("text") or "")
            url = form.group(0) if form else f"https://news.ycombinator.com/item?id={post['id']}"
            out.append(self.job("hnhiring", post["id"], title=parsed["title"], company=parsed["company"], location=parsed["location"], remote=1,
                                employment_type="", level="", description=post.get("text") or "", url=url, tags=[],
                                posted_at=post.get("created_at") or datetime.now(timezone.utc).isoformat()))
        return out
