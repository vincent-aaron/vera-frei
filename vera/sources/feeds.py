"""Free public job APIs (no key, no payment)."""

import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Tuple

import httpx

from vera.sources.base import Source

TYPE_MAP = {"full_time": "Full-time", "full-time": "Full-time", "fulltime": "Full-time", "part_time": "Part-time",
            "part-time": "Part-time", "contract": "Contract", "contractor": "Contract", "freelance": "Freelance",
            "internship": "Internship", "intern": "Internship", "temporary": "Temporary", "temp": "Temporary"}


def _etype(value) -> str:
    vals = value if isinstance(value, list) else [value]
    out = [TYPE_MAP.get(str(v).strip().lower().replace(" ", "_"), str(v).strip()) for v in vals if v]
    return ", ".join(dict.fromkeys(o for o in out if o))


def _ts(value) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    return str(value or "")


class Remotive(Source):
    """https://remotive.com/api-documentation : max ~4 calls/day, credit + link back."""
    name = "remotive"
    min_refresh_hours = 6.0

    async def fetch(self, client: httpx.AsyncClient, query: str, country: str = "") -> List[Dict[str, Any]]:
        r = await client.get("https://remotive.com/api/remote-jobs", params={"search": query, "limit": 100})
        r.raise_for_status()
        return [self.job("remotive", j["id"], title=j.get("title"), company=j.get("company_name"),
                         location=j.get("candidate_required_location") or "Remote", remote=1,
                         employment_type=_etype(j.get("job_type")), level="",
                         salary_text=j.get("salary") or "", description=j.get("description", ""),
                         url=j.get("url"), tags=j.get("tags") or [], posted_at=j.get("publication_date"))
                for j in r.json().get("jobs", [])]


class ArbeitNow(Source):
    """https://www.arbeitnow.com/api : free board API (mostly Europe)."""
    name = "arbeitnow"

    async def fetch(self, client, query, country=""):
        r = await client.get("https://www.arbeitnow.com/api/job-board-api")
        r.raise_for_status()
        words = [w for w in query.lower().split() if len(w) > 2]
        out = []
        for j in r.json().get("data", []):
            hay = f"{j.get('title', '')} {' '.join(j.get('tags') or [])}".lower()
            if words and not any(w in hay for w in words):
                continue
            out.append(self.job("arbeitnow", j["slug"], title=j.get("title"), company=j.get("company_name"),
                                location=j.get("location") or "", remote=int(bool(j.get("remote"))),
                                employment_type=_etype(j.get("job_types")), level="",
                                description=j.get("description", ""), url=j.get("url"),
                                tags=j.get("tags") or [], posted_at=_ts(j.get("created_at"))))
        return out


class Jobicy(Source):
    """https://jobi.cy/apidocs : credit Jobicy, apply buttons go to the original URL."""
    name = "jobicy"

    async def fetch(self, client, query, country=""):
        params = {"count": 50}
        if country:
            params["geo"] = country.lower()   # e.g. philippines -> jobs open there (incl. Anywhere/APAC)
        else:
            tag = query.strip().split()[-1] if query.strip() else ""
            if tag:
                params["tag"] = tag
        r = await client.get("https://jobicy.com/api/v2/remote-jobs", params=params)
        r.raise_for_status()
        return [self.job("jobicy", j["id"], title=j.get("jobTitle"), company=j.get("companyName"),
                         location=j.get("jobGeo") or "Remote", remote=1, employment_type=_etype(j.get("jobType")),
                         level=j.get("jobLevel") or "", salary_min=j.get("salaryMin"), salary_max=j.get("salaryMax"),
                         salary_currency=j.get("salaryCurrency"), salary_period=j.get("salaryPeriod"),
                         description=j.get("jobDescription") or j.get("jobExcerpt", ""), url=j.get("url"),
                         tags=j.get("jobIndustry") or [], posted_at=j.get("pubDate"))
                for j in r.json().get("jobs", [])]


class Himalayas(Source):
    """https://himalayas.app/api : free remote-jobs API."""
    name = "himalayas"

    async def fetch(self, client, query, country=""):
        params = {"q": query, "page": 1}
        if country:
            params["country"] = country
        r = await client.get("https://himalayas.app/jobs/api/search", params=params)
        r.raise_for_status()
        out = []
        for j in r.json().get("jobs", []):
            locs = j.get("locationRestrictions") or []
            out.append(self.job("himalayas", j.get("guid") or j.get("applicationLink"), title=j.get("title"),
                                company=j.get("companyName"), location=", ".join(locs) or "Remote (worldwide)", remote=1,
                                employment_type=_etype(j.get("employmentType")), level=", ".join(j.get("seniority") or []),
                                salary_min=j.get("minSalary"), salary_max=j.get("maxSalary"),
                                salary_currency=j.get("currency"), salary_period=j.get("salaryPeriod"),
                                description=j.get("description") or j.get("excerpt", ""),
                                url=j.get("applicationLink") or j.get("guid"), tags=j.get("categories") or [],
                                posted_at=_ts(j.get("pubDate"))))
        return out


class TheMuse(Source):
    """https://www.themuse.com/developers/api/v2 : public jobs API (mostly US)."""
    name = "themuse"

    async def fetch(self, client, query, country=""):
        params = [("page", 0), ("category", "Software Engineering"), ("category", "Computer and IT")]
        for lvl in ("Entry Level", "Mid Level", "Internship", "Senior Level"):
            params.append(("level", lvl))
        r = await client.get("https://www.themuse.com/api/public/jobs", params=params)
        r.raise_for_status()
        words = [w for w in query.lower().split() if len(w) > 2]
        out = []
        for j in r.json().get("results", []):
            if words and not any(w in j.get("name", "").lower() for w in words):
                continue
            locs = [l.get("name") for l in j.get("locations") or []]
            out.append(self.job("themuse", j["id"], title=j.get("name"), company=(j.get("company") or {}).get("name"),
                                location="; ".join(locs), remote=int(any("remote" in (l or "").lower() for l in locs)),
                                employment_type="", level=", ".join(l.get("name") for l in j.get("levels") or []),
                                description=j.get("contents", ""), url=(j.get("refs") or {}).get("landing_page"),
                                tags=[c.get("name") for c in j.get("categories") or []], posted_at=j.get("publication_date")))
        return out


class RemoteOK(Source):
    """https://remoteok.com/api : free; must link back to Remote OK and credit it."""
    name = "remoteok"
    min_refresh_hours = 6.0

    async def fetch(self, client, query, country=""):
        r = await client.get("https://remoteok.com/api")
        r.raise_for_status()
        words = [w for w in query.lower().split() if len(w) > 2]
        out = []
        for j in r.json()[1:]:                         # first item is the legal notice
            hay = f"{j.get('position', '')} {' '.join(j.get('tags') or [])}".lower()
            if words and not any(w in hay for w in words):
                continue
            out.append(self.job("remoteok", j.get("id"), title=j.get("position"), company=j.get("company"),
                                location=j.get("location") or "Remote (worldwide)", remote=1, employment_type="",
                                level="", salary_min=j.get("salary_min") or None, salary_max=j.get("salary_max") or None,
                                salary_currency="USD" if j.get("salary_min") else None,
                                salary_period="yearly" if j.get("salary_min") else None,
                                description=j.get("description", ""), url=j.get("url"), tags=j.get("tags") or [],
                                posted_at=j.get("date")))
        return out


class WeWorkRemotely(Source):
    """https://weworkremotely.com public RSS feeds (programming categories)."""
    name = "weworkremotely"
    FEEDS = ("remote-full-stack-programming-jobs", "remote-back-end-programming-jobs",
             "remote-front-end-programming-jobs", "remote-programming-jobs")

    async def fetch(self, client, query, country=""):
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        words = [w for w in query.lower().split() if len(w) > 2]
        out, seen = [], set()
        for feed in self.FEEDS:
            r = await client.get(f"https://weworkremotely.com/categories/{feed}.rss")
            if r.status_code != 200:
                continue
            for it in ET.fromstring(r.content).iter("item"):
                raw_title = (it.findtext("title") or "").strip()
                company, _, title = raw_title.partition(": ") if ": " in raw_title else ("", "", raw_title)
                link = (it.findtext("link") or "").strip()
                if not link or link in seen or (words and not any(w in title.lower() for w in words)):
                    continue
                seen.add(link)
                try:
                    posted = parsedate_to_datetime(it.findtext("pubDate")).isoformat()
                except (TypeError, ValueError):
                    posted = ""
                out.append(self.job("weworkremotely", link, title=title, company=company,
                                    location=(it.findtext("region") or "Remote").strip(), remote=1,
                                    employment_type=_etype(it.findtext("type") or ""), level="",
                                    description=it.findtext("description") or "", url=link,
                                    tags=[(it.findtext("category") or "").strip()], posted_at=posted))
        return out


class WorkingNomads(Source):
    """https://www.workingnomads.com public jobs API."""
    name = "workingnomads"

    async def fetch(self, client, query, country=""):
        r = await client.get("https://www.workingnomads.com/api/exposed_jobs/")
        r.raise_for_status()
        words = [w for w in query.lower().split() if len(w) > 2]
        out = []
        for j in r.json():
            if words and not any(w in f"{j.get('title', '')} {j.get('tags', '')}".lower() for w in words):
                continue
            out.append(self.job("workingnomads", j.get("url"), title=j.get("title"), company=j.get("company_name"),
                                location=j.get("location") or "Remote", remote=1, employment_type="", level="",
                                description=j.get("description", ""), url=j.get("url"),
                                tags=[t.strip() for t in (j.get("tags") or "").split(",") if t.strip()],
                                posted_at=j.get("pub_date")))
        return out


def rss_items(text: str) -> List[Dict[str, str]]:
    """Items from an RSS feed, read leniently: some job feeds have small XML mistakes that a strict parser rejects."""
    items = []
    for block in re.findall(r"<item\b[^>]*>(.*?)</item>", text, re.S | re.I):     # what's inside each item, not the item tag itself
        fields: Dict[str, str] = {}
        for tag, value in re.findall(r"<([\w:.-]+)(?:\s[^>]*)?>(.*?)</\1>", block, re.S):
            value = re.sub(r"^\s*<!\[CDATA\[(.*)\]\]>\s*$", r"\1", value, flags=re.S)
            fields.setdefault(tag.split(":")[-1].lower(), html.unescape(value).strip())
        items.append(fields)
    return items


class RssBoard(Source):
    """A remote job board's public RSS feed. Each listing links back to the board."""
    min_refresh_hours = 6.0
    feed = ""

    def split_title(self, raw: str) -> Tuple[str, str]:
        """(title, company). Boards write these differently, and many leave the company out."""
        found = re.match(r"(.+?)\s+(?:at|@)\s+(.+)$", raw)
        return (found.group(1), found.group(2)) if found else (raw, "")

    async def fetch(self, client, query, country=""):
        r = await client.get(self.feed)
        r.raise_for_status()
        words = [w for w in query.lower().split() if len(w) > 2]
        out = []
        for it in rss_items(r.text):
            title, company = self.split_title(it.get("title", ""))
            link = it.get("link", "")
            if not link or not title or (words and not any(w in title.lower() for w in words)):
                continue
            try:
                posted = parsedate_to_datetime(it.get("pubdate", "")).isoformat()
            except (TypeError, ValueError):
                posted = ""
            company = re.sub(r"<[^>]+>|[^\w&.,'() -]", " ", company or it.get("creator", "")).split("  ")[0].strip()      # some feeds put markup here
            out.append(self.job(self.name, link, title=title, company=company, location="Remote", remote=1, employment_type="", level="",
                                description=it.get("encoded") or it.get("description", ""), url=link, tags=[], posted_at=posted))
        return out


class Jobspresso(RssBoard):
    name = "jobspresso"
    feed = "https://jobspresso.co/feed/?post_type=job_listing"


class RealWorkFromAnywhere(RssBoard):
    """Remote jobs open to people anywhere, which suits working from the Philippines."""
    name = "realworkfromanywhere"
    feed = "https://www.realworkfromanywhere.com/rss.xml"


class LaraJobs(RssBoard):
    """Laravel and PHP jobs."""
    name = "larajobs"
    feed = "https://larajobs.com/feed"
