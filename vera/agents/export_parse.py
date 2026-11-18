"""Understanding requests like "create me a table of my applications this whole week, include date and link".

Plain rules with no AI, so it works even when the free AI services are busy, and it can't invent dates.
"""

import difflib
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

COLUMN_ORDER = ["date", "title", "company", "setup", "location", "board", "status", "link"]
COLUMN_WORDS = {"date": r"\b(dates?|when)\b", "title": r"\b(job(?!\s+applications?)s?|titles?|positions?|roles?)\b", "company": r"\b(compan(y|ies)|employers?)\b",
                "setup": r"\b(setups?|set up|arrangements?)\b", "location": r"\b(locations?|places?|cit(y|ies))\b",
                "board": r"\b(boards?|sites?|sources?)\b", "status": r"\bstatus(es)?\b", "link": r"\b(links?|urls?)\b"}
FORMAT_WORDS = {"xlsx": r"\b(sheets?|spreadsheets?|excel|xlsx)\b", "docx": r"\b(word|docx?|docs|documents?)\b", "pdf": r"\bpdfs?\b"}
VERB = re.compile(r"\b(make|create|generate|build|give|send|export|download|prepare|get|want|need|turn|put|show|list|see|view|display|summari[sz]e|tabulate|gawa\w*|pagawa\w*|pakita\w*)\b", re.I)
SHOW_VERB = re.compile(r"\b(show|list|see|view|display|pakita\w*)\b", re.I)
SUBJECT = re.compile(r"\b(applications?|applied|job history)\b", re.I)
TABLE = re.compile(r"\b(tables?|tabulate)\b", re.I)
SUMMARY = re.compile(r"\b(summary|summaries|summari[sz]e|overview)\b", re.I)
LISTING = re.compile(r"\b(lists?|reports?|history)\b", re.I)
APPLYING = re.compile(r"\bapply\b|\bsend (my )?applications?\b", re.I)
NOT_THIS = re.compile(r"\b(resume|résumé|cv|cover letters?|recommend|suggest)\b|\b(job|work|role|position)s? (that|which|with|using)\b|\bjobs? .*\b(uses?|using|requires?)\b", re.I)
QUESTION = re.compile(r"^\W*(how|why|what|which|did|when|where|who|have|has|is|are|should|do i|does)\b", re.I)
ASKED_WHAT_I_APPLIED = re.compile(r"\b(what|which|how many)\b.*\b(apply|applied|applications?)\b", re.I)
ASKED_HOW_MANY = re.compile(r"\bhow many\b.*\b(apply|applied|applications?|sent|submitted)\b", re.I)
ALL_FORMATS = re.compile(r"\b(all|every|each) (\w+ )?(formats?|three|3|types?|of them)\b", re.I)

TYPOS = {"aspreadsheet": "spreadsheet", "tbale": "table", "tabel": "table", "tble": "table", "talbe": "table", "shet": "sheet", "sheeet": "sheet", "summry": "summary", "sumary": "summary",
         "sumarry": "summary", "summay": "summary", "wek": "week", "weeek": "week", "pfd": "pdf", "exel": "excel", "excell": "excel", "aplied": "applied", "appied": "applied"}
LONG_WORDS = ["application", "applications"]
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "few": 3, "couple": 2}
COUNT = r"(\d{1,3}|two|three|four|five|six|seven|eight|nine|ten|few|couple(?: of)?)"
BOARD_WORDS = {"Company form": r"company forms?|greenhouse|lever|ashby|workable", "Remote board": r"remote boards?"}
STATUS_WORDS = {"Interview": r"\binterviews?\b", "Offer": r"\boffers?\b", "Rejected": r"\b(rejected|rejections?)\b", "Withdrawn": r"\bwithdrawn\b"}
SETUP_WORDS = {"Remote": r"\b(remote|wfh|work from home)\b", "Hybrid": r"\bhybrid\b", "On-site": r"\bon[- ]?site\b"}
Range = Tuple[Optional[str], Optional[str], str]


def fix_typos(text: str) -> str:
    def fix(found) -> str:
        word = found.group(0).lower()
        if word in TYPOS:
            return TYPOS[word]
        if len(word) >= 7 and word.startswith("ap"):
            close = difflib.get_close_matches(word, LONG_WORDS, n=1, cutoff=0.8)
            if close:
                return close[0]
        return found.group(0)
    return re.sub(r"[A-Za-z]+", fix, text)


def _count(found: str) -> int:
    return int(found) if found.isdigit() else NUMBERS[found.split()[0]]


def _label(since: date, until: date) -> str:
    one = lambda d, year=False: f"{d:%b} {d.day}" + (f", {d.year}" if year else "")
    return one(since, True) if since == until else f"{one(since, since.year != until.year)} to {one(until, True)}"


def _span(since: date, until: date, today: date) -> Optional[Range]:
    until = min(until, today)
    return (since.isoformat(), until.isoformat(), _label(since, until)) if since <= until else None


def _month_number(name: str) -> int:
    return MONTHS[name[:3].lower()]


def _safe_date(year: int, month: int, day: int, today: date, year_given: bool = False) -> Optional[date]:
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    return found.replace(year=year - 1) if found > today and not year_given else found


def _dates_in(text: str, today: date) -> List[date]:
    found: List[Tuple[int, date]] = []
    for m in re.finditer(rf"\b{MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", text, re.I):
        d = _safe_date(int(m.group(3) or today.year), _month_number(m.group(1)), int(m.group(2)), today, bool(m.group(3)))
        if d:
            found.append((m.start(), d))
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today, True)
        if d:
            found.append((m.start(), d))
    return [d for _pos, d in sorted(found, key=lambda x: x[0])]


def parse_range(text: str, today: Optional[date] = None) -> Optional[Range]:
    """The dates someone asked for, or None for all of them. "Last 3 days" includes today."""
    today = today or date.today()
    t = text.lower()
    short = re.search(rf"\b{MONTH}\.?\s+(\d{{1,2}})\s*(?:to|-|–|until|hanggang)\s*(\d{{1,2}})\b", t)
    if short:
        a = _safe_date(today.year, _month_number(short.group(1)), int(short.group(2)), today)
        b = _safe_date(today.year, _month_number(short.group(1)), int(short.group(3)), today)
        if a and b:
            return _span(a, b, today)
    dates = _dates_in(t, today)
    if len(dates) >= 2:
        return _span(min(dates[:2]), max(dates[:2]), today)
    if dates:
        if re.search(r"\b(since|from|after|starting)\b", t):
            return _span(dates[0], today, today)
        if re.search(r"\b(until|before|up to|through)\b", t):
            return (None, dates[0].isoformat(), f"up to {_label(dates[0], dates[0])}")
        return _span(dates[0], dates[0], today)
    ago = re.search(rf"\b{COUNT}\s+days?\s+ago\b", t)
    if ago:
        d = today - timedelta(days=_count(ago.group(1)))
        return _span(d, d, today)
    days = re.search(rf"\b(?:last|past|previous|nakaraang|for)\s+(?:the\s+)?{COUNT}\s*(?:days?|araw)\b", t)
    if days:
        return _span(today - timedelta(days=max(_count(days.group(1)), 1) - 1), today, today)
    weeks = re.search(rf"\b(?:last|past|previous)\s+{COUNT}\s*weeks?\b", t)
    if weeks:
        return _span(today - timedelta(weeks=_count(weeks.group(1))) + timedelta(days=1), today, today)
    monday = today - timedelta(days=today.weekday())
    if re.search(r"\b(this|current|ngayong|whole|entire|the)\s+(whole\s+|entire\s+|full\s+)?(week|linggo)\b", t) and not re.search(r"\b(last|previous)\s+week\b", t):
        return _span(monday, today, today)
    if re.search(r"\bpast week\b", t):
        return _span(today - timedelta(days=6), today, today)
    if re.search(r"\b(last|previous|nakaraang)\s+(week|linggo)\b", t):
        return _span(monday - timedelta(days=7), monday - timedelta(days=1), today)
    if re.search(r"\b(today|ngayong araw)\b", t):
        return _span(today, today, today)
    if re.search(r"\b(yesterday|kahapon)\b", t):
        d = today - timedelta(days=1)
        return _span(d, d, today)
    if re.search(r"\b(this|current|ngayong)\s+(whole\s+|entire\s+)?(month|buwan)\b", t):
        return _span(today.replace(day=1), today, today)
    if re.search(r"\b(last|previous|nakaraang)\s+(month|buwan)\b", t):
        end = today.replace(day=1) - timedelta(days=1)
        return _span(end.replace(day=1), end, today)
    named = re.search(rf"\b(?:in|for|of|during|sa)\s+{MONTH}\b", t) or re.search(r"\b(january|february|march|april|june|july|august|september|october|november|december)\b", t)
    if named:
        month = _month_number(named.group(1))
        year = today.year - 1 if month > today.month else today.year
        start = date(year, month, 1)
        end = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
        return _span(start, end, today)
    return None


def parse_columns(text: str) -> Optional[List[str]]:
    """Columns to keep, or None for all of them. "Include date and link" needs nothing, since those are shown anyway. The job is always kept."""
    t = text.lower()
    stop = r"(?=\b(?:for|from|in|of|this|last|past|on|but|include|including|with)\b|[.;]|$)"
    only = re.search(rf"\b(?:only|just)\b(.*?){stop}", t)
    if only:
        wanted = [c for c in COLUMN_ORDER if re.search(COLUMN_WORDS[c], only.group(1))]
        if wanted:
            return [c for c in COLUMN_ORDER if c in wanted or c == "title"]
    dropped: List[str] = []
    for found in re.finditer(rf"\b(?:without|exclude|excluding|remove|hide|no|don'?t include|do not include)\b(.*?){stop}", t):
        dropped += [c for c in COLUMN_ORDER if re.search(COLUMN_WORDS[c], found.group(1)) and c != "title"]
    return [c for c in COLUMN_ORDER if c not in dropped] if dropped else None


def parse_filters(text: str) -> Dict[str, Any]:
    t = text.lower()
    section = None
    if re.search(r"\b(failed|failures?|not finished|unfinished|couldn'?t finish)\b", t):
        section = "unfinished"
    elif re.search(r"\b(only (the )?(sent|submitted)|(sent|submitted) only|i sent)\b", t):
        section = "sent"
    setups = [s for s, rx in SETUP_WORDS.items() if re.search(rx, t)]
    return {"statuses": [s for s, rx in STATUS_WORDS.items() if re.search(rx, t)], "boards": [b for b, rx in BOARD_WORDS.items() if re.search(rx, t)], "section": section,
            "setups": [] if len(setups) == len(SETUP_WORDS) else setups,      # naming all three means sorting by them, not picking one
            "group": bool(re.search(r"\b(categori[sz]\w*|group\w*|by setup|per setup|sorted by setup)\b", t))}


def plan(text: str, today: Optional[date] = None) -> Union[None, str, Dict[str, Any]]:
    """What someone wants made from their applications: a plan, "ask" if it's unclear, or None if this isn't about that."""
    t = fix_typos(text)
    when = parse_range(t, today)
    chat, formats = None, []
    how_many = bool(ASKED_HOW_MANY.search(t)) and not NOT_THIS.search(t)
    if how_many:
        chat = "summary"
    elif ASKED_WHAT_I_APPLIED.search(t) and QUESTION.match(t):
        if not when or NOT_THIS.search(t):
            return None                  # "what document did you attach to my applications?" isn't asking for a list
        chat = "table"
    else:
        if NOT_THIS.search(t) or QUESTION.match(t):
            return None
        subject = bool(SUBJECT.search(t))
        names_a_file = TABLE.search(t) or SUMMARY.search(t) or any(re.search(rx, t, re.I) for rx in FORMAT_WORDS.values())
        if not VERB.search(t) and not (subject and names_a_file):      # "table of my applications" is clear enough
            return None
        if ALL_FORMATS.search(t) and subject:
            formats = ["xlsx", "docx", "pdf"]
        else:
            formats = [kind for kind, rx in FORMAT_WORDS.items() if re.search(rx, t, re.I)]
        if APPLYING.search(t) and not formats and not TABLE.search(t) and not SUMMARY.search(t):
            return None
        if re.search(r"\bapply\b", t, re.I):
            return None
        table, summary = bool(TABLE.search(t)), bool(SUMMARY.search(t))
        if formats:
            if not subject and not when and formats != ["xlsx"]:
                return "ask"
        elif table:
            chat = "table"
        elif summary:
            if not subject and not when:
                return "ask"
            chat = "summary"
        elif subject and (LISTING.search(t) or SHOW_VERB.search(t) or when):
            chat = "table"
        else:
            return None
    return {"formats": formats, "chat": chat, "count": how_many, "since": when[0] if when else None, "until": when[1] if when else None, "label": when[2] if when else "",
            "columns": parse_columns(t), **parse_filters(t)}
