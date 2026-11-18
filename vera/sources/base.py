"""Common source helpers."""

import html
import re
from typing import Any, Dict, List

import httpx

from vera import config


def clean_html(text: str, limit: int = 8000) -> str:
    """HTML description -> readable plain text."""
    if not text:
        return ""
    if "&lt;" in text[:2000]:      # some feeds send HTML that was escaped twice
        text = html.unescape(text)
    # Tags are matched only up to the next "<" and 4,000 characters, so odd markup can't make this crawl.
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", text[:500_000])
    t = re.sub(r"(?i)<li[^<>]{0,4000}>", "• ", t)
    t = re.sub(r"<[^<>]{0,4000}>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()[:limit]


def fingerprint(company: str, title: str) -> str:
    """Same job posted on several boards -> same fingerprint."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"{norm(company)}|{norm(title)}"


class Source:
    name = ""
    min_refresh_hours = 3.0     # respect providers: don't call more often than this per query

    async def fetch(self, client: httpx.AsyncClient, query: str, country: str = "") -> List[Dict[str, Any]]:
        """country: from your location preferences; sources that support it only return jobs open there."""
        raise NotImplementedError

    @staticmethod
    def job(source: str, ext_id: Any, **kw) -> Dict[str, Any]:
        j = {"id": f"{source}:{ext_id}", "source": source, **kw}
        j["description"] = clean_html(j.get("description", ""))
        salary_text = j.pop("salary_text", "")
        if salary_text:  # free-text salary (Remotive): keep it visible for evaluation
            j["description"] = f"Salary: {salary_text}\n\n{j['description']}"
        j["fingerprint"] = fingerprint(j.get("company"), j.get("title"))
        return j


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": config.USER_AGENT})
