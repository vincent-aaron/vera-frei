"""Generic multi-step application form filler.

Works on any page: finds visible fields, works out each field's question from
its label, fills it via answers.decide(), then clicks Continue/Next until it
reaches Submit. Site-specific code (sites.py) only gets us to the form.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Dict, List

from playwright.async_api import Page, Frame

from vera.apply import config
from vera.apply.answers import SKIP, AnswerMemory, Field, decide, saved_resume_option
from vera.apply.sites import is_challenge

logger = logging.getLogger(__name__)

SUBMIT_RE = re.compile(r"^\s*(submit( (your |my )?application)?|send( my)? application|apply( now)?|finish|complete application)\s*$", re.I)
NEXT_RE = re.compile(r"^\s*(continue|next|review( (your |my )?application)?|proceed|save (and|&) continue|continue to .+|next step)\s*$", re.I)
SUCCESS_RE = re.compile(
    # Ashby says "Your application was successfully submitted." Missing that once sent the same application four times.
    r"(application (has been |was |is )?(successfully )?(submitted|sent|received|complete)|thanks? (you )?for applying|"
    r"you('ve| have) (successfully )?applied|your application is on its way|we('ve| have) (successfully )?received your application)",
    re.I,
)
# Marks a try where Submit was clicked but no confirmation was seen. Such a job is never tried again: it may have gone through.
SUBMIT_UNCONFIRMED = "clicked Submit but couldn't confirm it went through"
# Only challenges a person would have to solve. Many forms carry an invisible reCAPTCHA
# badge that asks nothing of you; those aren't a reason to skip. Challenges are never
# solved or bypassed: the job is reported so you can apply yourself.
CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha'][src*='bframe']:visible", "iframe[src*='recaptcha'][src*='size=normal']:visible",
    "iframe[src*='hcaptcha']:visible", "iframe[src*='challenges.cloudflare.com']:visible", "#challenge-form",
]
LOGIN_URL_RE = re.compile(r"/(login|signin|sign-in|oauth|account/login|auth)\b", re.I)

# Runs in the page: returns every visible, editable field with its question text.
COLLECT_JS = r"""
() => {
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0 || el.type === 'file') && s.visibility !== 'hidden' && s.display !== 'none'; };
  const text = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const labelFor = el => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\s+/).map(id => text(document.getElementById(id))).join(' ').trim(); if (t) return t; }
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && text(l)) return text(l); }
    const wrap = el.closest('label'); if (wrap && text(wrap)) return text(wrap);
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg && text(lg)) return text(lg); }
    let n = el.parentElement;
    for (let i = 0; n && i < 4; i++, n = n.parentElement) {
      const t = text(n); if (t && t.length < 300) return t;
    }
    return el.getAttribute('placeholder') || el.name || '';
  };
  const groupQuestion = el => {
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg && text(lg)) return text(lg); }
    const rg = el.closest('[role=radiogroup],[role=group]');
    if (rg) { const by = rg.getAttribute('aria-labelledby'); if (by) { const t = text(document.getElementById(by)); if (t) return t; }
      if (rg.getAttribute('aria-label')) return rg.getAttribute('aria-label'); }
    let n = el.closest('div'); for (let i = 0; n && i < 5; i++, n = n.parentElement) {
      // The question, not the text of one of its options.
      const labs = [...n.querySelectorAll('[class*=label], label:not(:has(input)), legend, h2, h3, h4, p, span')];
      const lab = labs.find(l => !l.contains(el) && !l.closest('label:has(input)') && !l.querySelector('input') &&
                                 text(l).replace(/[^\p{L}\p{N}]/gu, '').length > 2);
      if (lab) return text(lab);
    }
    return el.name || '';
  };
  // Helper inputs behind custom dropdowns and captchas aren't questions.
  const isHelper = el => el.getAttribute('aria-hidden') === 'true' || /captcha/i.test(el.name || el.id || '') ||
    (el.tabIndex === -1 && el.type !== 'file' && el.type !== 'radio' && el.type !== 'checkbox' && !el.getAttribute('role'));
  // Upload buttons are often labelled just "Attach".
  const fileQuestion = el => {
    const own = labelFor(el);
    const generic = /^(attach|upload|choose( a)?( file)?|browse|select( a)?( file)?|drop.*)\W*$/i;
    if (own && !generic.test(own) && own.length < 80) return own;
    const g = groupQuestion(el);
    return generic.test(g) ? (el.id || el.name || '').replace(/[_-]/g, ' ') : g;
  };
  // Re-tag from scratch on every scan so ids never collide with an earlier step's.
  document.querySelectorAll('[data-aa]').forEach(e => e.removeAttribute('data-aa'));
  const out = []; const seenGroups = new Set(); let idx = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden','submit','button','image','reset','search','password'].includes(type)) continue;
    // Custom-styled radios/checkboxes hide the input but show the label.
    const shown = e => visible(e) || [...(e.labels || [])].some(visible);
    if (el.disabled || el.readOnly || !shown(el) || isHelper(el)) continue;
    const required = el.required || el.getAttribute('aria-required') === 'true';
    if (type === 'radio' || (type === 'checkbox' && el.name && document.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`).length > 1)) {
      const key = type + ':' + (el.name || idx);
      if (seenGroups.has(key)) continue; seenGroups.add(key);
      const members = (el.name ? [...document.querySelectorAll(`input[type=${type}][name="${CSS.escape(el.name)}"]`)] : [el])
        .filter(m => !m.disabled && shown(m));
      const tag = 'aa-' + (idx++);
      members.forEach((m, i) => m.setAttribute('data-aa', tag + '-' + i));
      out.push({ id: tag, kind: type, question: groupQuestion(el), required: required || members.some(m => m.required),
                 options: members.map(m => labelFor(m)), checked: members.some(m => m.checked) });
      continue;
    }
    const tag = 'aa-' + (idx++); el.setAttribute('data-aa', tag);
    const options = el.tagName === 'SELECT' ? [...el.options].filter(o => o.value !== '' && !/^(select|choose|--|please)/i.test(o.text.trim())).map(o => o.text.trim()) : [];
    const kind = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' :
                 el.getAttribute('role') === 'combobox' ? 'combobox' : type;
    const filled = kind === 'file' ? el.files.length > 0 : kind === 'checkbox' ? el.checked :
                   kind === 'select' ? (el.selectedIndex > 0 && el.value !== '') :
                   kind === 'combobox' ? (el.value.trim() !== '' || !!el.closest('[class*=select]')?.querySelector('[class*=single-value],[class*=singleValue]')) :
                   el.value.trim() !== '';
    out.push({ id: tag, kind, question: kind === 'file' ? fileQuestion(el) : labelFor(el), required, options, filled });
  }
  // Yes/No answered with toggle buttons instead of inputs (Ashby: "Are you over the age of 18?" [Yes] [No]).
  for (const box of document.querySelectorAll('*')) {
    const members = [...box.children].filter(b => b.tagName === 'BUTTON' && b.hasAttribute('aria-pressed') && visible(b) && !b.disabled);
    // Icon-only toggles (a text editor's Bold / Italic) aren't answers.
    if (members.length < 2 || members.length > 6 || !members.every(m => text(m) && text(m).length <= 40)) continue;
    let q = '', req = false;
    for (let n = box.parentElement, i = 0; n && i < 3 && !q; i++, n = n.parentElement) {
      const lab = [...n.querySelectorAll('label, legend, [class*=label], [class*=title], [class*=question]')]
        .find(l => !box.contains(l) && !l.contains(box) && text(l).replace(/[^\p{L}\p{N}]/gu, '').length > 2);
      if (lab) { q = text(lab); req = /required/i.test(lab.className || '') || /\*\s*$/.test(q); }
    }
    const hidden = box.querySelector('input'); if (hidden && hidden.required) req = true;
    const tag = 'aa-' + (idx++);
    members.forEach((m, i) => m.setAttribute('data-aa', tag + '-' + i));
    out.push({ id: tag, kind: 'buttons', question: q, required: req, options: members.map(m => text(m)),
               checked: members.some(m => m.getAttribute('aria-pressed') === 'true') });
  }
  return out;
}
"""


def clean_label(text: str) -> str:
    """Sites hide invisible characters in button text ('Continue\\u2060')."""
    return re.sub(r"[​-‏⁠﻿­]", "", text or "").strip()


def questions_from_errors(errors: List[str]) -> List[str]:
    """'How many years...? - Please make a selection' -> the question, so it can be asked instead of failing."""
    found = []
    for text in errors:
        for line in text.splitlines():
            # Ashby: "Missing entry for required field: Are you over the age of 18?"
            named = re.search(r"(?:missing entry for|please (?:fill in|complete|answer)) (?:the )?required (?:field|question)s?\s*:\s*(.{4,300})", line, re.I)
            if named:
                found.append(named.group(1).strip())
                continue
            m = re.match(r"\s*(.{8,300}?)\s*[-–:]\s*(please|this (field|question)|required|select|make a selection)\b", line, re.I)
            if m and not re.match(r"before you can|please address", m.group(1), re.I):
                found.append(m.group(1).strip())
    return list(dict.fromkeys(found))


def needs_filling(f: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """Empty fields, plus a cover-letter box the site pre-filled with a saved generic letter."""
    if not (f.get("filled") or f.get("checked")):
        return True
    # The site preselects its default résumé, which may be an old one.
    if f["kind"] == "radio" and re.search(r"resum|\bcv\b", f.get("question") or "", re.I) and saved_resume_option(f.get("options") or [], profile):
        return True
    return (f["kind"] == "textarea" and bool((profile.get("cover_letter") or {}).get("enabled"))
            and re.search(r"cover(ing)? letter", f.get("question") or "", re.I) is not None)


@dataclass
class Result:
    status: str                     # submitted | ready (dry run) | skipped | failed
    reason: str = ""
    unanswered: List[str] = dc_field(default_factory=list)


async def _find_frame(page: Page) -> Frame:
    """Application forms are sometimes inside an iframe; use the one with the most fields."""
    best, best_n = page.main_frame, -1
    for fr in page.frames:
        try:
            n = await fr.locator("input:visible, textarea:visible, select:visible").count()
        except Exception:
            continue
        if n > best_n:
            best, best_n = fr, n
    return best


async def _blocked(page: Page) -> str:
    if await is_challenge(page):
        return "CAPTCHA / bot check shown (not bypassed)"
    for fr in page.frames:
        for sel in CAPTCHA_SELECTORS:
            try:
                if await fr.locator(sel).count():
                    return "CAPTCHA / bot check shown (not bypassed)"
            except Exception:
                continue
    if await page.locator("input[type=password]:visible").count() or LOGIN_URL_RE.search(page.url):
        return "login required: this form needs an account, and I never sign in for you (skipped)"
    return ""


async def _combobox_options(frame: Frame, f: Dict[str, Any]) -> List[str]:
    """Searchable dropdowns only list their options once opened."""
    el = frame.locator(f'[data-aa="{f["id"]}"]').first
    await el.click(timeout=4000)
    await asyncio.sleep(0.4)
    options = await frame.locator("[role=option]:visible").all_inner_texts()
    await el.press("Escape")
    return [o.strip() for o in options if o.strip()]


async def _pick_place(frame: Frame, el, value: str) -> None:
    """Some location boxes only accept a suggestion from their list, and they only search
    on real keystrokes. If your town isn't listed, the country is still true."""
    suggestion = frame.locator("[role=option]:visible, .dropdown-results > div:visible")
    for query in (value, "Philippines"):
        await el.fill("")
        await el.press_sequentially(query, delay=40, timeout=8000)
        await asyncio.sleep(2)
        if await suggestion.count():
            await suggestion.first.click(timeout=3000)
            return
    await el.fill(value)


async def _fill(frame: Frame, f: Dict[str, Any], value: Any) -> None:
    kind, sel = f["kind"], f'[data-aa="{f["id"]}"]'
    if kind == "buttons":         # toggle buttons: press the one that says the answer
        await frame.locator(f'[data-aa="{f["id"]}-{f["options"].index(value)}"]').click(timeout=4000)
        return
    if kind in ("radio", "checkbox") and f["options"] and not f.get("single"):
        wanted = value if isinstance(value, list) else [value]
        for i, opt in enumerate(f["options"]):
            if opt in wanted:
                el = frame.locator(f'[data-aa="{f["id"]}-{i}"]')
                try:
                    await el.check(timeout=4000)
                except Exception:      # custom-styled inputs: click their label
                    await el.evaluate("e => (e.labels && e.labels[0] ? e.labels[0] : e).click()")
                if kind == "radio":    # options can share a name (two résumés), and only the first is meant
                    break
        return
    el = frame.locator(sel).first
    if kind == "file":
        await el.set_input_files(str(value), timeout=8000)
    elif kind == "select":
        await el.select_option(label=str(value), timeout=8000)
    elif kind == "combobox":
        await el.click(timeout=4000)
        if not f["options"]:        # type-to-search (e.g. a location box)
            await el.fill(str(value), timeout=4000)
            await asyncio.sleep(1.2)
        option = frame.locator("[role=option]:visible")
        if f["options"]:
            option = option.filter(has_text=re.compile(rf"^\s*{re.escape(str(value))}\s*$"))
        await option.first.click(timeout=4000)
    elif kind == "checkbox":
        try:
            await (el.check(timeout=4000) if value else el.uncheck(timeout=4000))
        except Exception:  # custom-styled checkbox: click its label
            await el.evaluate("e => (e.labels && e.labels[0] ? e.labels[0] : e).click()")
    else:
        await el.fill(str(value), timeout=8000)
        if re.search(r"location|\bcity\b", f["question"], re.I):
            await _pick_place(frame, el, str(value))
        try:
            await el.dispatch_event("blur", timeout=1500)
        except Exception:
            pass      # the form redrew itself after the text went in, so the field is already filled


async def _click_text(frame: Frame, regex: re.Pattern) -> str:
    """Click the last visible button/link whose text matches; return its text."""
    buttons = frame.locator("button:visible, a[role=button]:visible, input[type=submit]:visible, [role=button]:visible")
    n = await buttons.count()
    for i in range(n - 1, -1, -1):
        b = buttons.nth(i)
        try:
            label = clean_label((await b.inner_text()) or (await b.get_attribute("value") or "") or (await b.get_attribute("aria-label") or ""))
        except Exception:
            continue
        if regex.match(label) and await b.is_enabled():
            await b.click()
            return label
    return ""


def not_an_application(url: str) -> bool:
    """A profile or settings page is not an application form, and filling one changes the person's real account.
    An Apply button can lead to a profile editor. A form's own apply steps (/apply/profile) are fine."""
    path = re.sub(r"^[a-z]+://[^/]+", "", url or "", flags=re.I).split("?")[0].lower()
    return "/apply" not in path and bool(re.search(r"/(profile|settings|account|preferences|dashboard)(/|$)", path))


async def run_application(page: Page, profile: Dict[str, Any], memory: AnswerMemory,
                          job: Dict[str, str], dry_run: bool = False, max_steps: int = 15) -> Result:
    """Fill every step of the form and submit. Assumes the form is already open."""
    last_signature, repeats = None, 0
    submit_clicked = False
    for step in range(max_steps):
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1.2)
        if not_an_application(getattr(page, "url", "")):
            return Result("skipped", "the Apply button opened a profile page, not a form, so I left it alone")
        body = await page.locator("body").inner_text()
        if SUCCESS_RE.search(body):
            # Before anything was sent, this wording means an earlier application or page
            # text. It must never be counted as an application sent now.
            if step == 0:
                return Result("skipped", "already applied, or the page already says so")
            return Result("submitted", f"confirmed after {step} step(s)")
        why = await _blocked(page)
        if why:
            return Result("blocked" if "CAPTCHA" in why else "needs_login", why)

        frame = await _find_frame(page)
        unanswered, handled = [], set()
        # Several passes: picking an option can reveal new fields (e.g. "Upload a resume").
        for _ in range(3):
            fields = await frame.evaluate(COLLECT_JS)
            todo = [f for f in fields if needs_filling(f, profile) and (f["kind"], f["question"]) not in handled]
            if not todo:
                break
            for f in todo:
                handled.add((f["kind"], f["question"]))
                if f["kind"] == "combobox":
                    try:
                        f["options"] = await _combobox_options(frame, f)
                    except Exception:
                        f["options"] = []
                fld = Field(kind=f["kind"], question=f["question"][:300], options=f["options"], required=f["required"])
                if f["kind"] == "checkbox" and len(f["options"]) <= 1:   # single checkbox
                    f["single"] = True
                    fld.options = []
                value = await decide(fld, profile, memory, job)
                if f["kind"] == "file" and value not in (None, SKIP, "") and not config.allowed_upload(value, profile):
                    print(f"  refused upload: not their résumé or cover letter ({Path(str(value)).name[:60]})")
                    value = None                 # only their own résumé or this run's cover letter ever leaves the laptop
                if value in (None, SKIP, "") or (value is False and f["kind"] != "checkbox"):
                    if f["required"] and value != SKIP:
                        unanswered.append(fld.question)
                    continue
                try:
                    await _fill(frame, f, value)
                except Exception:
                    # Filling one field can redraw the form and lose track of the rest, so find it again once.
                    try:
                        fresh = await frame.evaluate(COLLECT_JS)
                        again = next(x for x in fresh if x["kind"] == f["kind"] and x["question"] == f["question"])
                        again["options"], again["single"] = f["options"] or again["options"], f.get("single")
                        await _fill(frame, again, value)
                    except Exception as e:
                        logger.warning(f"Could not fill '{fld.question[:60]}': {type(e).__name__}")
                        if f["required"]:
                            unanswered.append(fld.question)
            await asyncio.sleep(0.6)
        if unanswered:
            return Result("needs_answer", "required question(s) with no honest answer", unanswered)

        # Detect being stuck on the same step (e.g. validation errors).
        signature = (page.url, tuple(f["question"] for f in fields))
        repeats = repeats + 1 if signature == last_signature else 0
        last_signature = signature
        if repeats >= 2:
            errors = await frame.locator("[role=alert]:visible, [aria-invalid=true], .error:visible").all_inner_texts()
            questions = questions_from_errors(errors)
            if questions:
                return Result("needs_answer", "the form needs an answer I don't have", questions)
            return Result("failed", "stuck on a step: " + "; ".join(e.strip() for e in errors if e.strip())[:300])

        # Final step?
        submit = frame.locator("button:visible, input[type=submit]:visible")
        texts = [clean_label((await submit.nth(i).inner_text()) or (await submit.nth(i).get_attribute("value") or ""))
                 for i in range(await submit.count())]
        if any(SUBMIT_RE.match(t) for t in texts) and not any(NEXT_RE.match(t) for t in texts):
            if dry_run:
                return Result("ready", "dry run: stopped before Submit")
            clicked = await _click_text(frame, SUBMIT_RE)
            submit_clicked = submit_clicked or bool(clicked)
            await asyncio.sleep(3)
            await page.wait_for_load_state("domcontentloaded")
            body = await page.locator("body").inner_text()
            if SUCCESS_RE.search(body):
                return Result("submitted", f"clicked '{clicked}'")
            continue  # some flows show one more confirmation step

        clicked = await _click_text(frame, NEXT_RE)
        if not clicked:
            if submit_clicked:
                # The form is gone after Submit, with wording I don't recognise: it most likely went through.
                return Result("failed", SUBMIT_UNCONFIRMED + ", so I won't try it again")
            return Result("failed", "no Continue / Submit button found")
    return Result("failed", f"gave up after {max_steps} steps")
