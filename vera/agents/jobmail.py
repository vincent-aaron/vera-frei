"""Tracks employers' responses from your Gmail, and posts them to your private email channel.

Read-only: the mailbox is opened read-only and messages are fetched without marking them as read.
Nothing is deleted, moved or answered. Automatic notices ("your application was submitted") are
counted but not posted, because the end-of-run report already covers those. Other mail is ignored
and not stored. Message text is never sent to AI and is never treated as instructions, because
anyone can write anything in an email.

Owner only. Uses the same Gmail app password as outreach.
"""

import csv
import email
import email.policy
import imaplib
import logging
import re
from datetime import datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vera import config
from vera.agents import outreach

log = logging.getLogger("vera.jobmail")

FIRST_LOOK_DAYS = 7
SNIPPET = 400
# reply_to: where an answer goes (job boards pass employers' mail on from their own no-reply address, with the
# employer's address as Reply-To). counts: "1" when this mail was judged a real response from an employer (9 Nov 2026).
LOG_FIELDS = ["time", "from", "subject", "kind", "company", "title", "url", "gmail", "message_id", "folder", "reply_to", "counts", "display"]

ATS_DOMAINS = re.compile(r"(^|\.)(greenhouse\.io|greenhouse-mail\.io|lever\.co|hire\.lever\.co|ashbyhq\.com|workable\.com|workablemail\.com|smartrecruiters\.com|teamtailor-mail\.com|"
                         r"myworkday\.com|jobstreet\.com|seek\.com|s\.seek\.com|indeed\.com|indeedemail\.com|glassdoor\.com|bamboohr\.com|"
                         r"breezy\.hr|teamtailor\.com|recruitee\.com|jobvite\.com|icims\.com)$", re.I)
JOB_WORDS = re.compile(r"\b(your application|application (for|to|received|status|update)|applied|applying|interview|shortlist\w*|assessment|"
                       r"take[- ]home|coding (test|challenge)|job offer|offer letter|your resume|your résumé|your cv|regret to inform|"
                       r"next steps?|hiring (team|manager)|recruiter|talent acquisition)\b", re.I)
NEWSLETTER = re.compile(r"\b(job alert|jobs? for you|recommended jobs?|new jobs?|jobs? you may|jobs? matching|similar jobs?|weekly digest|newsletter|"
                        r"people (are )?viewing|still hiring|job hunting\?|who'?s hiring|salary insights|top jobs|career advice|profile views?|complete your profile)\b", re.I)
# "Thanks for applying" mail often mentions interviews or not being selected without any of that having happened.
CONFIRMATION = re.compile(r"\b(thank(s| you) for (applying|your (application|interest))|received your application|we got your application|"
                          r"application (was |has been )?(successfully )?(received|submitted|sent)|"
                          r"your application (to|for|at) .{0,80}(was|has been|is) (successfully )?(received|submitted|sent))\b", re.I)
REAL_NEXT_STEP = re.compile(r"\b(invit(e|ing) you|like to (invite|interview|schedule|speak|talk|meet)|schedule (a|an|your)|book a (time|slot|call)|calendly|"
                            r"confirm (your )?availability|video (call|interview)|(send|share) (us )?(a copy of )?(your )?(most )?(updated |latest )?(cv|resume|résumé|portfolio)|"
                            r"are you available|(complete|take|answer) (a|the|this|your|an|our) [a-z -]{0,30}(assessment|test|exam|challenge|questionnaire)|"
                            r"(pre-?)?screening (test|exam|bot|questions?|questionnaire)|hackerrank|codility)\b", re.I)
# "We don't have an open position that matches your qualifications, but we'll keep your résumé on file" is a no.
# ("We'll keep your résumé on file" alone isn't one: confirmations say it too, as in "if you're not selected".)
NO_OPENING = (r"(do not|don'?t|no longer) have an? (open |current |suitable )?(position|opening|role|vacancy)|"
              r"(has|have) (decided|chosen) (to )?(move|proceed|go) forward with other|not been shortlisted")
REAL_REJECTION = re.compile(rf"\b(unfortunately|regret to inform|not (be )?moving forward|won'?t be moving|decided to pursue|position has been filled|{NO_OPENING})\b", re.I)
AUTOMATED_SENDER = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|alerts?|mailer)\b|@(.+\.)?(jobstreet|seek|indeed|indeedemail|glassdoor)\.", re.I)
EMPLOYER_MESSAGE = re.compile(r"\b(sent you a message|new message|message from|replied to|responded to)\b", re.I)
KINDS: List[Tuple[str, str, re.Pattern]] = [
    ("bounce", "📭 Couldn't deliver", re.compile(r"delivery status notification|undeliver|mail delivery (failed|subsystem)|address not found|returned mail", re.I)),
    ("offer", "🎉 Job offer", re.compile(r"\b(job offer|offer letter|pleased to offer|we('d| would) like to offer|offer of employment)\b", re.I)),
    ("rejected", "😔 Not moving forward", re.compile(r"\b(unfortunately|regret to inform|not (be )?moving forward|other candidates|not selected|won'?t be moving|"
                                                     rf"decided to pursue|position has been filled|not a (good )?fit|{NO_OPENING})\b", re.I)),
    ("interview", "📅 Interview", re.compile(r"\b(interview|schedule a (call|chat|meeting)|availability|available (for|to) (a )?(call|chat|talk)|calendly|meet with)\b", re.I)),
    ("assessment", "📝 Assessment", re.compile(r"\b(assessment|take[- ]home|coding (test|challenge)|technical (test|exam)|hackerrank|codility|skills? test|"
                                               r"(pre-?)?screening (test|exam|bot|questions?|questionnaire)|(online|written|aptitude|qualifying|initial) (test|exam))\b", re.I)),
    ("received", "📨 Application received", CONFIRMATION),
    ("viewed", "👀 Employer activity", re.compile(r"\b(viewed your (application|resume|résumé|profile)|new activity in jobs)\b", re.I)),
]
OPT_OUT = re.compile(r"\b(unsubscribe|remove me|take me off|stop (e-?mailing|contacting|sending)|do not (e-?mail|contact)|don'?t (e-?mail|contact)|no more e-?mails?)\b", re.I)


# A person at the company wrote this, passed on by the job board ("X has sent you a message", "X has responded to your application").
RELAYED = re.compile(r"\b(has sent you a (new )?message|has responded to your application|replied to your application|message from the (employer|hiring))\b", re.I)
# Subjects the boards use, which name the company and the job.
BOARD_SUBJECTS = [re.compile(p, re.I) for p in (
    r"^(?P<company>.+?) has (responded to|viewed|replied to) your application for (?P<title>.+?)\.?$",
    r"^(?P<company>.+?) (has )?sent you a (new )?message( about (?P<title>.+?))?\.?$",
    r"^application update for (?P<title>.+?) at (?P<company>.+?)\.?$",
    # Company forms (Workable, Greenhouse...) send the employer's own message as "Job title - Company".
    r"^(?:re:\s*)?(?P<title>[^|]{4,120}?) [-–] (?P<company>[^-–|]{3,80}?)\.?$",
)]
# "Re: Software Developer - Juan Dela Cruz": someone answering an email he sent them.
REPLY_PREFIX = re.compile(r"^\s*(re|aw|sv|antw)\s*:", re.I)


def answers_him(subject: str, his_name: str) -> bool:
    """A reply to an email he sent: "Re:" and his name in the subject (every email Vera writes for him ends its subject with it)."""
    name = _words(his_name)
    return bool(REPLY_PREFIX.match(subject or "")) and len(name) >= 5 and name in _words(subject)


def is_relayed(subject: str, body: str) -> bool:
    return bool(RELAYED.search(subject or "") or RELAYED.search((body or "")[:600]))


def classify(subject: str, body: str, sender: str) -> Tuple[str, str]:
    text = f"{subject}\n{body[:3000]}"
    if re.match(r"(mailer-daemon|postmaster)@", sender, re.I):
        return KINDS[0][0], KINDS[0][1]
    # "Thank you for your interest in applying" from a real person is still a real message: their next step counts.
    confirmation = bool(CONFIRMATION.search(text)) and not is_relayed(subject, body)
    for kind, label, rx in KINDS[1:]:
        # "Pre-screening test for AV Design Engineer" in the subject is the point of the email, whatever thank-you comes first.
        named_in_subject = kind in ("interview", "assessment") and bool(rx.search(subject or "")) and not CONFIRMATION.search(subject or "")
        if confirmation and not named_in_subject and ((kind in ("interview", "assessment") and not REAL_NEXT_STEP.search(text))
                                                      or (kind == "rejected" and not REAL_REJECTION.search(text))):
            continue
        if rx.search(text):
            return kind, label
    return "reply", "💬 Reply"


def is_response(item: Dict[str, Any]) -> bool:
    """Did someone actually respond? Confirmations, "viewed" notices and vague automatic mail are left out."""
    if str(item.get("counts") or "") in ("1", "0"):     # decided when it was read (since 9 Nov 2026)
        return str(item["counts"]) == "1"
    if item["kind"] == "bounce":
        return True
    if item.get("answers_him"):            # a person answering his own email, even if it only says they got it
        return True
    if item["kind"] in ("received", "viewed"):
        return False
    if item.get("employer_wrote"):
        return True
    if is_relayed(item.get("subject") or "", item.get("body") or ""):
        return True
    if item["kind"] == "reply" and AUTOMATED_SENDER.search(item.get("from") or "") and not EMPLOYER_MESSAGE.search(item.get("subject") or ""):
        return False
    return True


def company_key(name: str) -> str:
    return re.sub(r"\b(inc|llc|ltd|corp|corporation|company|co|pty|opc|solutions|technologies|technology|global|group)\b", " ",
                  re.sub(r"[^a-z0-9]+", " ", (name or "").lower())).strip()


GENERIC_NAMES = {"tech", "software", "digital", "data", "systems", "system", "web", "online", "services", "service", "consulting", "media",
                 "labs", "lab", "soft", "it", "ai", "app", "apps", "cloud", "info", "net", "business", "international", "philippines", "ph"}


def match_application(apps: List[Dict[str, Any]], sender: str, subject: str, display: str) -> Optional[Dict[str, Any]]:
    """The application this mail is from, judged by who sent it. A subject line alone proves nothing, and neither does a common word:
    "Tech Solutions" must not match every newsletter that says "tech"."""
    who_sent = re.sub(r"[^a-z0-9]+", " ", f"{sender.split('@')[-1]} {display}".lower())
    joined = who_sent.replace(" ", "")
    best = None
    for a in apps:
        full = re.sub(r"[^a-z0-9]+", " ", (a.get("company") or "").lower()).strip()
        key = company_key(a.get("company") or "")
        words = key.split()
        # Short or generic names only count in full ("tech solutions"), never reduced to one common word.
        candidate = full if not words or all(w in GENERIC_NAMES for w in words) else key
        if len(candidate) < 4 or set(candidate.split()) <= GENERIC_NAMES:
            continue
        # Run-together matching ("techsolutions.ph") is only safe for longer names.
        if re.search(rf"\b{re.escape(candidate)}\b", who_sent) or (len(candidate.replace(" ", "")) >= 7 and candidate.replace(" ", "") in joined):
            best = a if best is None or (a.get("applied_at") or "") > (best.get("applied_at") or "") else best
    return best


def _words(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def match_board_subject(apps: List[Dict[str, Any]], sender: str, subject: str) -> Optional[Dict[str, Any]]:
    """Job boards send employers' messages from their own address, but name the company and the job in the subject."""
    if not ATS_DOMAINS.search(outreach.domain(sender)):
        return None
    for rx in BOARD_SUBJECTS:
        m = rx.search((subject or "").strip())
        if not m:
            continue
        company, title = company_key(m.group("company")), _words(m.groupdict().get("title") or "")
        if len(company) < 4:
            return None
        # Whole words only: "Northwind" is "Northwind Information Technology, Inc.", but "AI" isn't every company with "ai" in it.
        within = lambda short, long: len(short) >= 4 and bool(re.search(rf"\b{re.escape(short)}\b", long))
        found = [a for a in apps if (lambda key: key and (within(key, company) or within(company, key)))(company_key(a.get("company") or ""))]
        if title:
            same_job = [a for a in found if _words(a.get("position")) and (title.startswith(_words(a.get("position"))) or _words(a.get("position")).startswith(title))]
            found = same_job or found
        return max(found, key=lambda a: a.get("applied_at") or "") if found else None
    return None


def match_mentioned(apps: List[Dict[str, Any]], subject: str, body: str) -> Optional[Dict[str, Any]]:
    """An employer writing from an address that doesn't carry their name (a recruiter's own Gmail, an agency, a parent company),
    but naming the company in the mail. Only distinctive names count, and only in full."""
    text = _words(f"{subject} {(body or '')[:2000]}")
    best = None
    for a in apps:
        key = company_key(a.get("company") or "")
        if len(key) < 5 or set(key.split()) <= GENERIC_NAMES or key in ("private advertiser", "confidential"):
            continue
        if re.search(rf"\b{re.escape(key)}\b", text):
            best = a if best is None or (a.get("applied_at") or "") > (best.get("applied_at") or "") else best
    return best


def gmail_link(message_id: str, anywhere: bool = False) -> str:
    """Opens that email in Gmail, for whoever is signed in to the inbox. Gmail's search leaves out Spam unless asked."""
    from urllib.parse import quote
    mid = (message_id or "").strip().strip("<>")
    where = "in%3Aanywhere%20" if anywhere else ""
    return f"https://mail.google.com/mail/u/0/#search/{where}rfc822msgid%3A{quote(mid, safe='')}" if mid else ""


# A person at a company writing to him about a job directly, not as a reply to one of his applications.
DIRECT_EMPLOYER = re.compile(r"\b(we (have )?(reviewed|received|came across|saw|found|got) your (application|resume|résumé|cv|profile)|"
                             r"your (application|resume|résumé|cv|profile) (on|from|via|through|in) (jobstreet|indeed|linkedin|glassdoor|kalibrr|our)|"
                             r"(for|regarding|about|re:?) the (position|role|vacancy|opening) (of|for|as)|initial (interview|screening|call)|"
                             r"invit(e|ation|ing) (you )?(to|for) (an? |our )?(interview|exam|assessment|screening)|"
                             r"we('d| would) like to (invite|schedule|offer|move forward|proceed with your application)|shortlisted|"
                             r"job offer|offer letter)\b", re.I)


def is_direct(sender: str, subject: str, body: str, bulk: bool = False) -> bool:
    """Written to him by someone hiring: not a mailing list, not a board's robot."""
    text = f"{subject} {(body or '')[:2000]}"
    return not bulk and not AUTOMATED_SENDER.search(sender) and bool(DIRECT_EMPLOYER.search(text) or REAL_NEXT_STEP.search(text))


def is_relevant(sender: str, subject: str, body: str, emailed: set, app: Optional[Dict[str, Any]], bulk: bool = False, spam: bool = False,
                answers: bool = False) -> bool:
    """bulk: sent to a mailing list (it has an unsubscribe header). spam: found in the Spam folder, so it needs clear signs it's real.
    answers: a person replying to an email he sent ("Re: ... - Juan Dela Cruz"), even one he sent himself from Gmail."""
    domain = outreach.domain(sender)
    if outreach.who(sender) in emailed:
        return True
    if answers and not bulk and not AUTOMATED_SENDER.search(sender):
        return True
    if re.match(r"(mailer-daemon|postmaster)@", sender, re.I):
        return bool(emailed) and any(d in body.lower() for d in emailed)
    if NEWSLETTER.search(subject):
        return False
    if spam:
        return (app is not None and not bulk) or is_direct(sender, subject, body, bulk)
    if app:
        return True
    if ATS_DOMAINS.search(domain):
        return bool(JOB_WORDS.search(f"{subject} {body[:1500]}"))
    return bool(JOB_WORDS.search(subject)) or is_direct(sender, subject, body, bulk)


def body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError, AttributeError):
        return ""
    if part.get_content_type() == "text/html":
        from vera.agents.web import to_text
        text = to_text(text)
    # Leave out the quoted email being replied to.
    text = re.split(r"\n\s*(On .{5,80} wrote:|-{2,}\s*Original Message|From: )", text, maxsplit=1)[0]
    return re.sub(r"\s+", " ", text).strip()


INBOX, SPAM = "INBOX", "spam"


def spam_folder(box) -> str:
    """Gmail's Spam folder, whatever it's called in the account's language: the one marked \\Junk."""
    try:
        _, rows = box.list()
    except (imaplib.IMAP4.error, OSError):
        rows = []
    for raw in rows or []:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if "\\Junk" in line:
            m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if m:
                return m.group(1)
    return "[Gmail]/Spam"


def fetch_new(last_uid: int, since_days: Optional[int] = None, folder: str = INBOX) -> Tuple[List[Dict[str, str]], int]:
    """New messages since last_uid, or everything from the last since_days days, from the inbox or the Spam folder.
    Opens the mailbox read-only. UIDs are counted per folder, so each folder keeps its own last_uid."""
    box = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=60)
    try:
        box.login(config.OUTREACH_EMAIL, config.OUTREACH_APP_PASSWORD)
        name = INBOX if folder == INBOX else spam_folder(box)
        status, _ = box.select(f'"{name}"' if re.search(r'[\s\[\]"]', name) else name, readonly=True)
        if status != "OK":                  # Spam hidden from IMAP in Gmail's settings
            log.warning(f"couldn't open the {folder} folder")
            return [], last_uid
        if since_days:
            since = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
            _, data = box.uid("search", None, f"SINCE {since}")
        elif last_uid:
            _, data = box.uid("search", None, f"UID {last_uid + 1}:*")
        else:
            since = (datetime.now() - timedelta(days=FIRST_LOOK_DAYS)).strftime("%d-%b-%Y")
            _, data = box.uid("search", None, f"SINCE {since}")
        uids = [int(u) for u in (data[0] or b"").split() if since_days or int(u) > last_uid][-200:]
        out = []
        for uid in uids:
            # PEEK keeps the message unread. The size limit skips large attachments.
            _, parts = box.uid("fetch", str(uid), "(BODY.PEEK[]<0.200000>)")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            display, sender = parseaddr(str(msg.get("From", "")))
            _, reply_to = parseaddr(str(msg.get("Reply-To", "")))
            reply_to = reply_to.strip().lower() if outreach.EMAIL_RE.fullmatch(reply_to.strip()) and reply_to.lower() != sender.lower() else ""
            bulk = bool(msg.get("List-Unsubscribe")) or str(msg.get("Precedence", "")).lower() in ("bulk", "list", "junk")
            out.append({"uid": uid, "from": sender, "display": display, "reply_to": reply_to, "subject": str(msg.get("Subject", ""))[:200],
                        "date": str(msg.get("Date", ""))[:40], "body": body_text(msg), "message_id": str(msg.get("Message-ID", "")).strip()[:300],
                        "folder": folder, "bulk": bulk})
        return out, max(uids + [last_uid])
    finally:
        try:
            box.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def headers_of(message_id: str, folder: str = INBOX) -> Dict[str, str]:
    """From, display name and Reply-To of one email, looked up by its Message-ID. Read-only, headers only (for mail logged
    before Reply-To was kept)."""
    mid = (message_id or "").strip()
    if not re.fullmatch(r"<[^<>\s\"]{3,250}>", mid):
        return {}
    box = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=60)
    try:
        box.login(config.OUTREACH_EMAIL, config.OUTREACH_APP_PASSWORD)
        name = INBOX if folder != SPAM else spam_folder(box)
        if box.select(f'"{name}"' if re.search(r'[\s\[\]"]', name) else name, readonly=True)[0] != "OK":
            return {}
        _, data = box.uid("search", None, "HEADER", "Message-ID", f'"{mid}"')
        uids = (data[0] or b"").split()
        if not uids:
            return {}
        _, parts = box.uid("fetch", uids[-1], "(BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO SUBJECT)])")
        raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        display, sender = parseaddr(str(msg.get("From", "")))
        _, reply_to = parseaddr(str(msg.get("Reply-To", "")))
        return {"from": sender, "display": display, "reply_to": reply_to.strip().lower() if outreach.EMAIL_RE.fullmatch(reply_to.strip()) else ""}
    finally:
        try:
            box.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def logged(uid: int) -> List[Dict[str, str]]:
    path = outreach.folder(uid) / "mail.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def log_mail(uid: int, row: Dict[str, str]) -> None:
    path = outreach.folder(uid) / "mail.csv"
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != LOG_FIELDS:                 # written before links were kept: same rows, new columns
            old = logged(uid)
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
                w.writeheader()
                for r in old:
                    w.writerow({k: r.get(k) or "" for k in LOG_FIELDS})
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in LOG_FIELDS})


def whose(session) -> str:
    """The person whose mailbox this is, by name (plain text, never a ping)."""
    return (session.profile.get("name") or "").strip() or "the owner"


def track(session, messages: List[Dict[str, str]], recheck: bool = False) -> List[Dict[str, Any]]:
    """Which messages matter, what they are, and what to update. Applications only ever move forward to a clear outcome.
    recheck=True looks again at mail already seen: only mail that now counts as a response, and wasn't one before, comes back."""
    uid = session.uid
    seen = {(r.get("from"), r.get("subject")): r for r in logged(uid)} if recheck else {}
    emailed = {r["email"].lower() for r in outreach.history(uid) if r["status"] == "sent"}
    emailed |= {outreach.who(e) for e in emailed}
    apps = session.db.applications(uid, limit=500)
    found = []
    for m in messages:
        if m["from"].lower() == (config.OUTREACH_EMAIL or "").lower():
            continue
        spam, bulk = m.get("folder") == SPAM, bool(m.get("bulk"))
        # The sending address is the evidence. A display name ("Acme Corp <someone@gmail.com>") can say anything,
        # so a match on it alone is shown with a warning and never changes an application by itself.
        app = match_application(apps, m["from"], m["subject"], "") or match_board_subject(apps, m["from"], m["subject"])
        by_name_only = app is None and match_application(apps, m["from"], m["subject"], m["display"]) is not None
        if by_name_only:
            app = match_application(apps, m["from"], m["subject"], m["display"])
        mentioned = by_name_only
        if app is None and not bulk and not ATS_DOMAINS.search(outreach.domain(m["from"])) and JOB_WORDS.search(f"{m['subject']} {m['body'][:2000]}"):
            app = match_mentioned(apps, m["subject"], m["body"])          # their own address, but they name the company
            mentioned = app is not None
        answers = answers_him(m["subject"], session.profile.get("name") or "")
        if not is_relevant(m["from"], m["subject"], m["body"], emailed, app, bulk=bulk, spam=spam, answers=answers):
            continue
        kind, label = classify(m["subject"], m["body"], m["from"])
        item = {**m, "kind": kind, "label": label, "company": (app or {}).get("company") or "", "note": "", "for": whose(session),
                "title": (app or {}).get("position") or "", "url": (app or {}).get("url") or "", "gmail": gmail_link(m.get("message_id") or "", anywhere=spam),
                "answers_him": answers and not AUTOMATED_SENDER.search(m["from"]),
                # A company form passing on the employer's own message ("Job title - Company"), matched to his application.
                "employer_wrote": bool(app) and not bulk and ATS_DOMAINS.search(outreach.domain(m["from"])) is not None
                                  and kind == "reply" and not NEWSLETTER.search(m["subject"])}
        if not item["company"] and item["answers_him"]:
            item["company"] = outreach.company_of(m["from"]) or _quote(m.get("display") or "")[:60]
        item["counts"] = "1" if is_response({k: v for k, v in item.items() if k != "counts"}) else "0"
        warnings = []
        if spam:
            warnings.append("📥 This was in your **Spam** folder. Open it and mark it *Not spam*, so their next email reaches your inbox.")
        if by_name_only:
            warnings.append(f"⚠️ The sender's name says {_quote(m['display'])[:60]}, but the address isn't theirs. It could be a fake: "
                            "check it before you reply or send personal details.")
        if app is None and outreach.who(m["from"]) not in emailed and kind not in ("bounce", "received", "viewed"):
            warnings.append("⚠️ It doesn't match any job you applied to. Check the company is real before you reply or send personal details.")
        before = seen.get((m["from"], m["subject"]))
        if before is not None and (is_response(before) or not is_response(item)):
            continue                     # already seen, and nothing new to say about it
        from_outreach = outreach.who(m["from"]) in emailed
        if from_outreach and OPT_OUT.search(m["body"][:1500] + " " + m["subject"]):
            outreach.block(uid, m["from"])
            item["note"] = "They asked not to be contacted, so I added them to your do-not-contact list."
        if kind == "bounce":
            for address in outreach.EMAIL_RE.findall(m["body"][:3000]):
                if address.lower() in emailed:
                    outreach.block(uid, address)
                    item["note"] = f"I won't try {address.lower()} again."
                    break
        if app and kind in ("interview", "offer", "rejected") and app.get("status") in ("submitted", "applied", "interview"):
            new = {"interview": "interview", "offer": "offer", "rejected": "rejected"}[kind]
            if app["status"] != new and (mentioned or spam):     # a weaker match: he decides
                item["note"] = f"If this is about your {app.get('company')} application, tell me and I'll mark it as **{new}**."
            elif app["status"] != new:
                session.db.update_application(app["id"], status=new)
                item["note"] = f"I marked your {app.get('company')} application as **{new}**."
        item["note"] = "\n".join(x for x in [item["note"]] + warnings if x)
        log_mail(uid, {"time": datetime.now().isoformat(timespec="seconds"), "from": m["from"], "subject": m["subject"], "kind": kind, "company": item["company"],
                       "title": item["title"], "url": item["url"], "gmail": item["gmail"], "message_id": m.get("message_id") or "",
                       "folder": m.get("folder") or INBOX, "reply_to": m.get("reply_to") or "", "counts": item["counts"],
                       "display": _quote(m.get("display") or "")[:80]})
        found.append(item)
    return found


def _quote(text: str) -> str:
    """Show text from an email as plain text, so it can't ping anyone or pretend to be formatting."""
    return re.sub(r"[`*_~|>@]", "", text).replace("\n", " ").strip()


def _address(text: str) -> str:
    """The sender's email address, kept readable. In a code span, it can't ping anyone or pretend to be formatting."""
    found = outreach.EMAIL_RE.fullmatch((text or "").strip())
    return f"`{found.group(0)}`" if found else _quote(text or "")


# What a reply means, in plain words. Public channels get this instead of the email's own text.
SUMMARIES = {
    "rejected": "They're not moving forward with this application.",
    "interview": "They'd like to set up an interview or a call.",
    "assessment": "They've sent a test or assessment to complete.",
    "offer": "They've made a job offer. 🎉",
    "bounce": "An email couldn't be delivered.",
    "reply": "They wrote back about the application.",
}


def clean_body(text: str) -> str:
    """The readable part of an email: no tracking placeholders (%%strtoreplaceopentracking%%), links or bracketed URLs."""
    text = re.sub(r"%%[^%\s]{1,80}%%", " ", text or "")
    text = re.sub(r"\[?\s*<?https?://\S+?>?\s*\]?(?=\s|$)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def summary_line(item: Dict[str, Any]) -> str:
    kind = item.get("kind") or "reply"
    if kind == "reply" and is_relayed(item.get("subject") or "", item.get("body") or ""):
        return "The employer sent you a message through the job board."
    return SUMMARIES.get(kind, SUMMARIES["reply"])


def render(item: Dict[str, Any], private: bool) -> str:
    """private: his own channel, with the email's text, the sender's address and a Gmail link.
    Otherwise (#vera-job-mail, which everyone sees): a plain summary and the job link only."""
    company = f" · {item['company']}" if item.get("company") else ""
    sender = f"{_quote(item['display'] or '')} {_address(item['from'])}" if private else _quote(item["display"] or "") or "the employer"
    lines = [f"{item['label']}{company}"]
    if item.get("for"):
        lines.append(f"**For:** {_quote(item['for'])}")           # whose application and mailbox: a name, never a ping
    lines += [f"**From:** {sender}".replace("  ", " ").rstrip(), f"**Subject:** {_quote(item['subject'])[:150]}"]
    if item.get("title"):
        lines.append(f"**Job:** {_quote(item['title'])}")
    lines.append(f"**In short:** {summary_line(item)}")
    # No quote of the email itself: The owner asked on 9 Nov 2026 ("I don't need the Gmail message, don't include it").
    # Where an answer goes, so he (or Vera, with vera_reply_email) can answer it.
    if private and item.get("reply_to"):
        lines.append(f"**Reply goes to:** {_address(item['reply_to'])}")
    links = [f"🔗 Job: <{item['url']}>" if item.get("url") else "", f"📧 Open in Gmail: <{item['gmail']}>" if private and item.get("gmail") else ""]
    if any(links):
        lines.append(" · ".join(x for x in links if x))
    if item.get("note"):
        lines.append(item["note"])
    return "\n".join(lines)


def digest(session, day: Optional[str] = None, gmail: bool = True) -> str:
    """One day's responses from employers, with job links, and Gmail links when gmail=True (his private channel only)."""
    day = day or datetime.now().date().isoformat()
    rows = [r for r in logged(session.uid) if (r.get("time") or "").startswith(day) and is_response(r)]
    when = datetime.fromisoformat(day).strftime("%a %d %b").replace(" 0", " ")
    head = f"📬 **Replies from employers for {_quote(whose(session))}, {when}:**"
    if not rows:
        return f"{head} none today."
    lines = [f"{head} {len(rows)}"]
    for r in rows:
        label = next((lab for k, lab, _ in KINDS if k == r.get("kind")), "💬 Reply")
        what = " · ".join(_quote(x) for x in (r.get("company"), r.get("title")) if x) or _quote(r.get("subject") or "")[:100]
        links = " · ".join(x for x in (f"<{r['url']}>" if r.get("url") else "", f"[Gmail](<{r['gmail']}>)" if gmail and r.get("gmail") else "") if x)
        lines.append(f"• {label}: {what}" + (f" · {links}" if links else ""))
    return "\n".join(lines)


def summary(session, days: int = 30) -> str:
    path = outreach.folder(session.uid) / "mail.csv"
    if not path.exists():
        return "No replies tracked yet."
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["time"] >= cutoff]
    if not rows:
        return f"No replies in the last {days} days."
    responses = [r for r in rows if is_response(r)]
    left_out = len(rows) - len(responses)
    note = f" ({left_out} automatic notice{'' if left_out == 1 else 's'} not shown.)" if left_out else ""
    if not responses:
        return f"No responses from employers in the last {days} days.{note}"
    counts: Dict[str, int] = {}
    for r in responses:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    return f"Last {days} days: " + ", ".join(f"{n} {k}" for k, n in counts.items()) + "." + note


def responses(session, days: int = 14) -> List[Dict[str, str]]:
    """Employers' responses of the last N days, newest first, for the AI to know about: what they are and who sent them,
    never the email's own text. One row per email (a recheck can log the same email twice)."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows, seen = [], set()
    for r in reversed(logged(session.uid)):
        if (r.get("time") or "") < cutoff or not is_response(r):
            continue
        key = r.get("message_id") or (r.get("from"), r.get("subject"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
    return rows


def brief(rows: List[Dict[str, str]], limit: int = 20) -> List[Dict[str, str]]:
    """What the AI sees: no message text (anyone can write anything in an email, and the owner doesn't want it repeated)."""
    out = []
    for i, r in enumerate(rows[:limit], 1):
        out.append({"number": i, "logged": (r.get("time") or "")[:16].replace("T", " "), "kind": r.get("kind") or "reply",
                    "company": _quote(r.get("company") or "")[:80], "job": _quote(r.get("title") or "")[:120],
                    "sender_name": _quote(r.get("display") or "")[:80], "subject": _quote(r.get("subject") or "")[:150],
                    "job_link": r.get("url") or "", "can_reply": bool(reply_address(r))})
    return out


def reply_address(row: Dict[str, str]) -> str:
    """Where an answer to this email goes: its Reply-To (a job board passing on an employer's message), or the sender
    if that's a person. Never a job board's no-reply address."""
    for address in (row.get("reply_to") or "", row.get("from") or ""):
        address = address.strip().lower()
        if outreach.EMAIL_RE.fullmatch(address) and not AUTOMATED_SENDER.search(address) and outreach.usable(address):
            return address
    return ""


def find_reply(session, which: str = "", days: int = 30) -> Optional[Dict[str, str]]:
    """The employer's email he wants to answer: a number from the list vera_job_mail gave, or words from the company,
    job, sender or subject. The newest match wins."""
    rows = responses(session, days)
    which = (which or "").strip().lstrip("#")
    if which.isdigit():
        n = int(which)
        return rows[n - 1] if 0 < n <= len(rows) else None
    words = [w for w in _words(which).split() if len(w) >= 3 and w not in ("the", "and", "inc", "email", "mail", "reply")]
    if not words:
        return rows[0] if rows else None
    scored = []
    for i, r in enumerate(rows):
        hay = _words(" ".join(r.get(k) or "" for k in ("company", "title", "display", "subject", "from", "reply_to")))
        hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}", hay))
        if hits:
            scored.append((hits, -i, r))
    return max(scored, key=lambda x: (x[0], x[1]))[2] if scored else None


__all__ = ["fetch_new", "track", "render", "summary", "digest", "is_response", "responses", "brief", "find_reply", "reply_address", "Path"]
