"""Site adapters: open a job's application form so the form engine can fill it.

Supported: application forms companies host on Greenhouse, Lever, Ashby and Workable
(no account or login needed), reached directly or through a listing on one of the free
remote-job boards Vera reads through their public APIs.

Vera never signs in to job boards, never searches them in a browser, and never bypasses
bot checks or CAPTCHAs: a site that shows one is left alone for a while.
"""

import asyncio
import logging
import re
from typing import Dict, Optional
from urllib.parse import urlparse

from playwright.async_api import Page

logger = logging.getLogger(__name__)


BLOCK_TITLE = re.compile(r"just a moment|blocked|access denied|attention required|verify you are human|captcha|security check", re.I)


class SiteBlocked(RuntimeError):
    """The site showed a bot check / block page. The agent does not bypass these."""


CHALLENGE_TEXT = re.compile(r"humans only|verify you are human|verifying you are human|checking your browser|are you a robot|"
                            r"unusual traffic|needs to review the security of your connection", re.I)


async def is_challenge(page: Page) -> bool:
    """A full-page bot check. These don't always say so in the page title, so the page text is checked too."""
    if "__cf_chl" in page.url or BLOCK_TITLE.search(await page.title()):
        return True
    try:
        return bool(CHALLENGE_TEXT.search((await page.locator("body").inner_text(timeout=3000))[:1500]))
    except Exception:
        return False


async def check_blocked(page: Page, site_name: str) -> None:
    title = await page.title()
    if await is_challenge(page):
        raise SiteBlocked(f"{site_name} showed a bot check ('{title[:40]}'). Not bypassed: that site is left alone for a while.")


class Site:
    name = ""
    domains: tuple = ()

    def matches(self, url: str) -> bool:
        """The real site or one of its subdomains. "greenhouse.io.example.net" and "greenhouse.io@10.0.0.1" don't count."""
        host = (urlparse(url).hostname or "").lower().rstrip(".")     # lower(): RemoteOK's links say "remoteOK.com"
        return any(host == d or host.endswith("." + d) for d in self.domains)

    async def job_info(self, page: Page) -> Dict[str, str]:
        title = await _first_text(page, self.title_selectors) or (await page.title())
        company = await _first_text(page, self.company_selectors)
        location = await _first_text(page, self.location_selectors)
        extra = [await _first_text(page, (s,)) for s in self.meta_selectors]
        description = await _first_text(page, self.description_selectors + ("main", "body"))
        return {"title": title.strip()[:150], "company": (company or "").strip()[:100], "url": page.url,
                "location": (location or "").strip()[:150],
                "description": "\n".join(x for x in extra + [description] if x)[:8000]}

    async def open_apply(self, page: Page) -> Optional[str]:
        """Open the application form. Return None on success or a skip reason."""
        raise NotImplementedError

    title_selectors: tuple = ("h1",)
    company_selectors: tuple = ()
    location_selectors: tuple = ()
    meta_selectors: tuple = ()
    description_selectors: tuple = ()


async def _first_text(page: Page, selectors) -> str:
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count():
                t = (await loc.inner_text(timeout=2000)).strip()
                if t:
                    return t
        except Exception:
            continue
    return ""


POPUP_CLOSE = ("[role=dialog] button[aria-label*='close' i]", "[aria-modal=true] button[aria-label*='close' i]",
               "[class*='modal' i] button[aria-label*='close' i]", "[class*='modal' i] button[class*='close' i]",
               "button[aria-label='Close']", "button[aria-label='Cancel']", "[data-test*='close' i]")


async def dismiss_popups(page: Page) -> int:
    """Ads and promos can cover the page and swallow clicks. Never touches a form's fields."""
    closed = 0
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.5)
    for selector in POPUP_CLOSE:
        button = page.locator(selector).first
        try:
            if await button.count() and await button.is_visible():
                await button.click(timeout=2000)
                closed += 1
                await asyncio.sleep(0.5)
        except Exception:
            continue
    return closed


FORM_FIELDS = "input[type=email]:visible, input[type=file], input[name*=name i]:visible, input[id*=name i]:visible"
COOKIE_DECLINE = re.compile(r"^\s*(decline all|reject all|decline|reject|only necessary|necessary only|accept only necessary|"
                            r"use necessary cookies only|allow necessary only|continue without accepting)\s*$", re.I)


async def decline_cookies(page: Page) -> bool:
    """A cookie banner can cover the Apply button (Workable's does). Turn the optional cookies down, which also closes it."""
    for button in (page.get_by_role("button", name=COOKIE_DECLINE).first, page.get_by_role("link", name=COOKIE_DECLINE).first):
        try:
            if await button.count() and await button.is_visible():
                await button.click(timeout=3000)
                await asyncio.sleep(0.5)
                return True
        except Exception:
            continue
    return False


class ATS(Site):
    """Application forms companies host on Greenhouse, Lever, Ashby and Workable.
    They need no login, and many overseas remote jobs end up here."""
    name = "company_form"
    domains = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com")
    title_selectors = ("h1", "h2")
    location_selectors = (".job__location", ".location", ".posting-categories .location", "[class*=location i]")
    description_selectors = (".job__description", ".posting-page", "[class*=description i]")

    async def job_info(self, page):
        info = await super().job_info(page)
        if "//jobs.workable.com/" in page.url:
            # Workable's shared job search: the page title is "Title | Company | Jobs by Workable".
            parts = [p.strip() for p in (await page.title()).split("|")]
            info["company"] = info["company"] or (parts[1] if len(parts) >= 3 else "")
            return info
        m = re.search(r"(?:greenhouse\.io|lever\.co)/(?:embed/job_app\?for=)?([^/?#]+)|//([^.]+)\.workable\.com|ashbyhq\.com/([^/?#]+)", page.url)
        slug = next((g for g in (m.groups() if m else ()) if g), "")
        info["company"] = info["company"] or slug.replace("-", " ").title()
        return info

    async def open_apply(self, page):
        await decline_cookies(page)
        if await page.locator(FORM_FIELDS).count():
            return None
        url = page.url.split("?")[0].rstrip("/")
        if "lever.co" in url and not url.endswith("/apply"):
            await page.goto(url + "/apply", wait_until="domcontentloaded")
        elif "ashbyhq.com" in url and not url.endswith("/application"):
            await page.goto(url + "/application", wait_until="domcontentloaded")
        else:
            easy = re.compile(r"^\s*apply( for this job| now| to this job)?\s*$", re.I)
            for loc in (page.get_by_role("button", name=easy).first, page.get_by_role("link", name=easy).first):
                if await loc.count() and await loc.is_visible():
                    await loc.click()
                    break
        await asyncio.sleep(2.5)
        return None if await page.locator(FORM_FIELDS).count() else "no application form found on this page"


_ATS = ATS()


class RemoteBoard(Site):
    """Listings from the free remote-job APIs Vera reads. These pages only link onward, so
    follow the link and continue if it reaches a company form we support."""
    name = "remote_board"
    domains = ("remotive.com", "jobicy.com", "himalayas.app", "remoteok.com", "weworkremotely.com",
               "workingnomads.com", "arbeitnow.com", "themuse.com", "jobspresso.co", "realworkfromanywhere.com", "larajobs.com")
    company_selectors = ("[class*=company i]",)
    location_selectors = ("[class*=location i]",)

    async def open_apply(self, page):
        """Find where the board's apply button really goes: a plain link, a redirect through the board, or a new tab."""
        await dismiss_popups(page)
        here = urlparse(page.url).netloc
        hrefs = await page.eval_on_selector_all(
            "a[href]", """els => els.filter(e => /\\bapply\\b/i.test(e.innerText || e.getAttribute('aria-label') || '') || /\\/(apply|out|redirect|click)\\b/i.test(e.href))
                                    .map(e => e.href).filter(h => h.startsWith('http'))""")
        direct = next((h for h in hrefs if _ATS.matches(h)), None)
        outside = [h for h in hrefs if urlparse(h).netloc != here]
        landed = direct or (outside[0] if outside else (hrefs[0] if hrefs else None))
        if landed:
            await page.goto(landed, wait_until="domcontentloaded")
        else:
            button = page.get_by_role("button", name=re.compile(r"\bapply\b", re.I)).first
            if not await button.count() or not await button.is_visible():
                return "no apply link found on this board's page"
            try:
                async with page.expect_popup(timeout=6000) as new_tab:
                    await button.click()
                tab = await new_tab.value
                await tab.wait_for_load_state("domcontentloaded")
                landed = tab.url
                await tab.close()
                await page.goto(landed, wait_until="domcontentloaded")
            except Exception:
                return "its apply button didn't lead anywhere I can follow"
        await asyncio.sleep(2)
        await check_blocked(page, self.name)
        if not _ATS.matches(page.url):
            return f"goes to {urlparse(page.url).netloc}, a site I can't fill (skipped)"
        return await _ATS.open_apply(page)


ALL_SITES = [_ATS, RemoteBoard()]


def site_for(url: str) -> Optional[Site]:
    return next((s for s in ALL_SITES if s.matches(url)), None)
