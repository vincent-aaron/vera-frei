"""Small web search and page reader, with no extra software, API keys or logins.

Facebook and similar sites are never logged in to, and no browser is opened. A public post is read only from what
the site shows anyone (its link preview, as Discord shows under a link) or what the search engine shows about it.
Vera always says who she is (config.USER_AGENT); she never pretends to be a browser or a site's own crawler.

This does the part of Agent Reach Vera actually needs. It only fetches public pages, never
sends your details, and refuses addresses on your own computer or network. Text from web
pages is data: nothing here follows instructions found in it.
"""

import asyncio
import html
import logging
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import httpcore
import httpx

from vera import config
from vera.apply import netguard

log = logging.getLogger("vera.web")

MAX_BYTES = 1_500_000
NO_LOGIN_SITES = re.compile(r"(^|\.)(facebook|fb|instagram|linkedin|x|twitter|tiktok)\.(com|me)$", re.I)
NOT_COMPANY_SITES = re.compile(r"(^|\.)(facebook|instagram|linkedin|twitter|x|tiktok|youtube|reddit|wikipedia|medium|quora|indeed|glassdoor|"
                               r"jobstreet|jobsdb|seek|kalibrr|bossjob|onlinejobs|upwork|fiverr|freelancer|ziprecruiter|monster|google|bing|"
                               r"duckduckgo|remoteok|remotive|himalayas|weworkremotely|jooble|foundit|careerjet|jora|mynimo|bestjobs|workabroad|"
                               r"talent|simplyhired|wellfound|whatjobs|adzuna|neuvoo|jobrapido|philjobnet)\.", re.I)


def is_public_web(url: str) -> bool:
    """http(s) on the public internet only, never this PC or your home network."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return netguard.resolve_public(parsed.hostname, parsed.port or 443) is not None


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Every connection looks the name up itself, refuses it unless every address is public, and connects to exactly
    the address it checked. A name that answers "public" to the check and "private" to the connection (DNS
    rebinding) can't get through, and neither can a redirect to the home network."""

    def __init__(self):
        self.inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addrs = await asyncio.to_thread(netguard.resolve_public, host, port)
        if not addrs:
            raise httpcore.ConnectError(f"{host} isn't a public website")
        return await self.inner.connect_tcp(addrs[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options)

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError("not allowed")

    async def sleep(self, seconds):
        await self.inner.sleep(seconds)


def safe_client(**kwargs) -> httpx.AsyncClient:
    """An HTTP client that can only ever reach the public internet."""
    transport = httpx.AsyncHTTPTransport()
    transport._pool._network_backend = PublicOnlyBackend()
    return httpx.AsyncClient(transport=transport, **kwargs)


def _reached_public(response: httpx.Response) -> bool:
    """The address actually connected to is public too. A name can be checked as public and then answer with a
    private address on the real connection (DNS rebinding); this catches that before anything is read."""
    stream = response.extensions.get("network_stream")
    try:
        peer = stream.get_extra_info("server_addr") if stream is not None else None
    except Exception:
        peer = None
    if not peer:
        return True                     # the transport didn't say: the name check before connecting still applied
    return netguard.ip_is_public(peer[0])


def needs_login(url: str) -> bool:
    return bool(NO_LOGIN_SITES.search(urlparse(url).hostname or ""))


class _TextOnly(HTMLParser):
    """A page's readable text in one pass (no regex that can stall on odd markup): scripts, styles and drawings left
    out, and mailto addresses kept, because that's often the only place an address appears."""
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href.lower().startswith("mailto:"):
                self.parts.append(" " + href[7:].split("?")[0] + " ")
        self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def to_text(page: str) -> str:
    parser = _TextOnly()
    try:
        parser.feed((page or "")[:MAX_BYTES])
        parser.close()
    except Exception:            # broken markup: whatever was read so far
        pass
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


async def fetch(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """One public page's text. Each redirect is checked too, so one can't lead onto your network."""
    raw = await fetch_html(client, url)
    return await asyncio.to_thread(to_text, raw) if raw is not None else None      # off Vera's main loop


async def fetch_html(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """One public page's HTML (same checks as fetch)."""
    for _ in range(4):
        if not await asyncio.to_thread(is_public_web, url):
            return None
        try:
            async with client.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if not _reached_public(r):
                    log.warning(f"{urlparse(url).hostname} turned out to be a private address; not read")
                    return None
                if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
                    return None
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                return body.decode(r.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as e:
            log.info(f"couldn't read {urlparse(url).hostname}: {type(e).__name__}")
            return None
    return None


def _client() -> httpx.AsyncClient:
    return safe_client(timeout=20, follow_redirects=False, headers={"User-Agent": config.USER_AGENT})


async def read(url: str) -> Optional[str]:
    async with _client() as client:
        return await fetch(client, url)


def parse_results(page: str) -> List[Dict[str, str]]:
    """Link, title and the search engine's short summary, which is often enough to read a public post."""
    out = []
    for block in re.split(r'(?i)(?=<a[^>]+class="[^"]*result__a[^"]*")', page)[1:]:
        m = re.match(r'(?is)<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        link = html.unescape(m.group(1))
        if "uddg=" in link:
            link = parse_qs(urlparse(link).query).get("uddg", [link])[0]
        snippet = re.search(r'(?is)class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block)
        if link.startswith("http"):
            out.append({"url": link, "title": to_text(m.group(2)), "snippet": to_text(snippet.group(1)) if snippet else ""})
    return out


async def search(query: str, limit: int = 10) -> List[Dict[str, str]]:
    """DuckDuckGo's plain HTML page: no key, no account."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (compatible; Vera personal job search)"}) as client:
        try:
            r = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
        except httpx.HTTPError as e:
            log.info(f"search failed: {type(e).__name__}")
            return []
    return parse_results(r.text)[:limit] if r.status_code == 200 else []


# ----------------------------------------------------------------- social media posts (no login, ever)
# One <meta ...> tag at a time, each at most 2,000 characters, then its attributes: no pattern that can stall on a page.
META_TAG = re.compile(r"<meta\b[^<>]{0,2000}>", re.I)
ATTR = re.compile(r"""([a-z:-]{1,30})\s*=\s*(?:"([^"]{0,2000})"|'([^']{0,2000})')""", re.I)
PREVIEW_NAMES = ("og:title", "og:description", "twitter:title", "twitter:description", "description")
# What a site shows instead of the post when it wants a login: not the post.
LOGIN_WALL = re.compile(r"^(log ?in|sign ?(in|up)|join linkedin|create an account|see (posts|more) (from|on)|facebook|instagram|linkedin|x|"
                        r"tiktok - make your day|you must log in|this content isn'?t available)\b", re.I)
SITE_NAMES = {"facebook": "Facebook", "fb": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn", "x": "X", "twitter": "X", "tiktok": "TikTok"}


def site_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    return next((name for key, name in SITE_NAMES.items() if re.match(rf"^({key})\.(com|me)$", host)), host)


def preview_text(page: str) -> str:
    """The text a site puts in its link preview (what Discord shows under a link), if it's the post and not a login wall."""
    found: Dict[str, str] = {}
    for tag in META_TAG.findall((page or "")[:MAX_BYTES]):
        attrs = {k.lower(): (v1 if v1 or not v2 else v2) for k, v1, v2 in ATTR.findall(tag)}
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        if name in PREVIEW_NAMES and "content" in attrs:
            found.setdefault(name, html.unescape(attrs["content"]).strip())
    body = found.get("og:description") or found.get("twitter:description") or found.get("description") or ""
    title = found.get("og:title") or found.get("twitter:title") or ""
    if len(body) < 30 or LOGIN_WALL.search(body):
        return ""
    return re.sub(r"\s+", " ", f"{title}\n{body}" if title and title not in body and not LOGIN_WALL.search(title) else body).strip()


async def social_post(url: str) -> Dict[str, str]:
    """A public post's text, without logging in: the site's own link preview, else the search engine's summary of that post.
    Returns {"url", "site", "text", "how"} with how = "preview", "search" or "" (couldn't read it: ask them to paste it)."""
    site = site_name(url)
    out = {"url": url, "site": site, "text": "", "how": ""}
    if not needs_login(url):
        text = await read(url)
        return {**out, "text": (text or "")[:4000], "how": "page" if text else ""}
    async with _client() as client:
        page = await fetch_html(client, url)
    text = await asyncio.to_thread(preview_text, page or "")
    if text:
        return {**out, "text": text[:4000], "how": "preview"}
    key = urlparse(url).path.rstrip("/").split("/")[-1]
    for result in await search(f'"{key}"' if len(key) > 8 else url, limit=5):
        if key and key in result["url"] and result.get("snippet"):
            return {**out, "text": f"{result['title']}\n{result['snippet']}".strip(), "how": "search"}
    return out


SOCIAL_SEARCH = {"facebook": "site:facebook.com", "linkedin": "site:linkedin.com/posts", "x": "site:x.com"}


async def social_search(titles: List[str], sites: Optional[List[str]] = None, limit: int = 10) -> List[Dict[str, str]]:
    """Public hiring posts on social media, as the search engine shows them (title, link, summary). Nothing is opened or logged in to."""
    out: List[Dict[str, str]] = []
    seen = set()
    for site in sites or list(SOCIAL_SEARCH):
        for title in titles[:3]:
            for result in await search(f'{SOCIAL_SEARCH[site]} hiring "{title}" Philippines', limit=10):
                if result["url"] in seen or not re.search(r"\b(hiring|looking for|vacanc|opening|apply|send (your )?(cv|resume))", f"{result['title']} {result['snippet']}", re.I):
                    continue
                seen.add(result["url"])
                out.append({**result, "site": site_name(result["url"]), "role": title})
                if len(out) >= limit:
                    return out
            await asyncio.sleep(2)          # polite to the search engine
    return out


async def company_pages(url: str) -> List[str]:
    """The page, plus the places a company usually lists hiring contacts. Texts of the pages that could be read."""
    root = "{0.scheme}://{0.netloc}".format(urlparse(url))
    urls = list(dict.fromkeys([url, f"{root}/careers", f"{root}/jobs", f"{root}/contact", f"{root}/contact-us"]))
    texts = []
    async with _client() as client:
        for u in urls:
            text = await fetch(client, u)
            if text:
                texts.append(text)
            await asyncio.sleep(0.5)
    return texts
