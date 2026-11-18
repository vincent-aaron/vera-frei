"""Your job specification: decides whether a job is worth applying to.

Reads agent_profile.json -> "search" (Vera keeps it in sync with /preferences):
  keywords           job titles; the job's title must contain every word of at least one
  similar_titles     also accept any software developer/engineer title
  levels             e.g. ["Fresh Graduate", "Entry Level", "Junior"]; "Any Level" turns this off
  work_setup         any of ["Remote", "Hybrid", "On-site"]
  places             towns/provinces you can reach for on-site and hybrid jobs; remote jobs ignore this.
                     A job has to clearly be in one of them. Area names also cover the cities inside (see PLACE_AREAS)
  job_types          e.g. ["Full-time", "Contract"]
  max_years_required reject jobs demanding more than this many years
  min_salary_monthly reject jobs whose listed PHP pay tops out below this
  exclude_titles / exclude_companies / exclude_keywords / exclude_locations
  avoid_onsite       places too far for hybrid and on-site jobs. Remote jobs listing them are still fine
"""

import re
import unicodedata
from typing import Any, Dict, List, Optional

_LEVEL_WORDS = {"junior", "jr", "entry", "level", "fresh", "graduate", "associate", "trainee", "intern", "remote"}
TITLE_LEVELS = [
    (re.compile(r"\b(intern|internship|ojt|apprentice)\b", re.I), {"internship"}),
    (re.compile(r"\b(senior|sr|principal|staff|expert)\b", re.I), {"senior"}),
    (re.compile(r"\b(lead|head|architect)\b", re.I), {"lead"}),
    (re.compile(r"\b(manager|director|supervisor|vp)\b", re.I), {"manager"}),
    (re.compile(r"\b(mid[- ]?level|intermediate)\b", re.I), {"mid-level"}),
]
TYPES = {
    "Part-time": r"\bpart[- ]?time\b", "Contract": r"\b(contract|contractor)\b", "Freelance": r"\b(freelance|project[- ]based)\b",
    "Internship": r"\b(internship|ojt)\b", "Temporary": r"\btemporary\b", "Full-time": r"\bfull[- ]?time\b",
}
REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|home[- ]based|work anywhere)\b", re.I)
HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
YEARS_RE = re.compile(r"(?:at least |minimum (?:of )?|min\.? )?(\d{1,2})\s*(?:\+|-\s*\d{1,2}|to \d{1,2})?\s*(?:years?|yrs?)\b[^.\n]{0,40}\bexperience", re.I)
PHP_RE = re.compile(r"(?:₱|PHP|Php)\s*([\d,]{5,})(?:\s*(?:-|–|to)\s*(?:₱|PHP|Php)?\s*([\d,]{5,}))?", re.I)


OTHER_COUNTRY = re.compile(r"\b(united states|usa|u\.s\.|us|canada|canadian|united kingdom|uk|britain|ireland|germany|france|spain|portugal|italy|netherlands|"
                           r"poland|sweden|norway|denmark|finland|switzerland|austria|belgium|europe|eu|emea|australia|new zealand|india|brazil|mexico|latam|"
                           r"latin america|japan|korea|singapore|israel|north america|americas|nyc|new york|san francisco|california|london|berlin)\b", re.I)
OPEN_TO_ME = re.compile(r"\b(worldwide|anywhere|global|globally|apac|asia|southeast asia|sea)\b", re.I)

# In a job description, "remote" alone proves nothing ("work with our remote team"). It has to clearly describe this job.
CLEARLY_REMOTE = re.compile(r"\b(fully remote|100% remote|full[- ]?time remote|remote (work|position|role|job|setup|arrangement|opportunity|based)|"
                            r"(work|working) (remotely|from home)|work[- ]?from[- ]?home (setup|arrangement|position|role|job)|wfh (setup|arrangement|position|role|job)|"
                            r"permanent (wfh|work from home)|home[- ]based|(setup|arrangement|location)\W{0,3}(remote|wfh|work from home))\b", re.I)
NOT_REMOTE = re.compile(r"\b(not (a |an )?(fully )?(remote|wfh|work from home)|no (remote|wfh|work from home)|non[- ]remote|on[- ]?site|office[- ]based|"
                        r"report(ing)? to (the |our )?office|in[- ]office|must be (based|located) in|work from (the |our )?office)\b", re.I)

# Places near you, by the area name in your settings. Only names that can't be confused with somewhere else:
# San Fernando is also in La Union, Morong is also in Rizal, and Los Angeles contains "Angeles".
PLACE_AREAS = {
    "metro manila": r"metro manila|ncr|national capital region|manila|makati|mandaluyong|taguig|bgc|bonifacio global city|fort bonifacio|mckinley|pasig|ortigas|"
                    r"quezon city|qc|san juan city|pasay|paranaque|las pinas|muntinlupa|alabang|marikina|caloocan|malabon|navotas|valenzuela|pateros|"
                    r"binondo|ermita|malate|intramuros|cubao|eastwood|rockwell|greenhills",
    "manila": r"manila|binondo|ermita|malate|intramuros|sampaloc|tondo|quiapo",
    "bulacan": r"bulacan|malolos|meycauayan|hagonoy|calumpit|marilao|bocaue|balagtas|guiguinto|plaridel|pulilan|baliuag|baliwag|bustos|obando|paombong|san jose del monte|norzagaray|pandi|san ildefonso",
    "pampanga": r"pampanga|angeles city|clark freeport|clark field|clarkfield|mabalacat|apalit|guagua|lubao|porac|arayat|city of san fernando",
    "bataan": r"bataan|balanga|mariveles|dinalupihan|orani|limay|bagac|abucay",
}
# Somewhere else in the Philippines. "Rizal Drive, BGC" is a street in Taguig, not the province.
FAR_PLACES = re.compile(r"\b(cavite|dasmarinas|bacoor|imus|laguna|santa rosa|sta\.? rosa|calamba|binan|rizal|antipolo|cainta|taytay|batangas|lipa|cebu|mandaue|davao|iloilo|bacolod|negros|"
                        r"baguio|benguet|pangasinan|dagupan|la union|ilocos|tarlac|nueva ecija|cabanatuan|zambales|subic|olongapo|quezon province|lucena|albay|legazpi|naga city|camarines|"
                        r"palawan|mindoro|leyte|tacloban|samar|bohol|tagbilaran|cagayan|isabela|zamboanga|misamis|general santos|bukidnon|mindanao|visayas)\b"
                        r"(?!\s+(?:ave|avenue|st|street|drive|dr|road|rd|blvd|boulevard|highway|hwy|ext|extension)\b)", re.I)


def _plain_place(text: str) -> str:
    """Las Piñas and Las Pinas are the same place."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def place_fits(where: str, places: List[str]) -> bool:
    """Is this clearly one of your places, and nowhere far as well? A listing that also names somewhere far could send you there."""
    text = _plain_place(where)
    if not text or OTHER_COUNTRY.search(text):
        return False
    # "Far" is relative: for someone who lives in Cebu, Cebu is home.
    if FAR_PLACES.search(text) and not any(FAR_PLACES.search(_plain_place(p)) for p in places):
        return False
    return any(re.search(rf"\b(?:{PLACE_AREAS.get(_plain_place(p).strip(), re.escape(_plain_place(p).strip()))})\b", text) for p in places if p.strip())


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9+#]+", text.lower().replace("full stack", "fullstack").replace("full-stack", "fullstack"))


# "Similar jobs" mode: any developer/engineer/programmer title, except engineering outside software.
ROLE_RE = re.compile(r"\b(developers?|programmers?|software engineers?|engineers?|coders?|devops|sde|sre)\b", re.I)
NOT_SOFTWARE_RE = re.compile(r"\b(civil|mechanical|electrical|electronics?|chemical|structural|industrial|mining|process|plant|field|"
                             r"facilities|maintenance|hvac|geotechnical|environmental|safety|sales|pre-?sales|marketing|business|"
                             r"real estate|property|land|audio|sound|biomedical|aerospace|automotive|manufacturing|piping|site)\b", re.I)


def title_matches(title: str, keywords: List[str], similar: bool = False) -> bool:
    if not keywords:
        return True
    if similar and ROLE_RE.search(title) and not NOT_SOFTWARE_RE.search(title):
        return True
    have = set(_words(title))
    for kw in keywords:
        need = [w for w in _words(kw) if w not in _LEVEL_WORDS]
        if need and all(w in have for w in need):
            return True
    return False


def detect_setup(job: Dict[str, str]) -> Optional[str]:
    """Remote / Hybrid from the title and location, then the start of the posting, where it has to be clear."""
    heading = f"{job.get('title', '')} {job.get('location', '')}"
    if HYBRID_RE.search(heading):
        return "Hybrid"
    if REMOTE_RE.search(heading):
        return "Remote"
    start = (job.get("description") or "")[:800]
    if HYBRID_RE.search(start):
        return "Hybrid"
    if CLEARLY_REMOTE.search(start) and not NOT_REMOTE.search(start):
        return "Remote"
    return None


def rejects(job: Dict[str, str], spec: Dict[str, Any]) -> Optional[str]:
    """Why this job doesn't fit, or None if it does."""
    title, desc = job.get("title") or "", job.get("description") or ""
    if not title_matches(title, spec.get("keywords") or [], bool(spec.get("similar_titles"))):
        return f"title '{title[:60]}' is not one of your job titles"

    for word in spec.get("exclude_titles") or []:
        if word.lower() in title.lower():
            return f"excluded title '{word}'"
    for word in spec.get("exclude_companies") or []:
        if word.lower() in (job.get("company") or "").lower():
            return f"excluded company '{word}'"
    for word in spec.get("exclude_locations") or []:
        if word.lower() in (job.get("location") or "").lower():
            return f"excluded location '{word}'"
    for word in spec.get("exclude_keywords") or []:
        if word.lower() in f"{title} {desc}".lower():
            return f"excluded keyword '{word}'"

    allowed = {str(l).lower() for l in spec.get("levels") or []}
    if allowed and "any level" not in allowed:
        for pattern, levels in TITLE_LEVELS:
            if pattern.search(title) and not (levels & allowed):
                return f"level: '{title[:60]}' is above or outside your levels"

    setups = set(spec.get("work_setup") or [])
    if setups and setups != {"Remote", "Hybrid", "On-site"}:
        found = detect_setup(job) or "On-site"
        if found not in setups:
            return f"work setup looks {found}; you want {', '.join(sorted(setups))}"

    # "Remote, United States" usually means you must live there, so it would only waste an application.
    if detect_setup(job) == "Remote":
        home = (spec.get("home_country") or "Philippines").lower()
        place = (job.get("location") or "").lower()
        if OTHER_COUNTRY.search(place) and not OPEN_TO_ME.search(place) and home not in place:
            return f"remote, but only for {job.get('location', '')[:50]}"
        # On overseas sites, "Remote Toronto" also means that area. Philippine sites just list a local city.
        host = re.sub(r"^https?://", "", job.get("url") or "").split("/")[0].lower()
        overseas_site = bool(host) and not (host.startswith("ph.") or host.endswith(".ph"))
        leftover = re.sub(r"\b(remote|home[- ]based|work from home|wfh|fully|100%|only|based)\b|[^a-z ]", " ", place).strip()
        if overseas_site and leftover and home not in place and not OPEN_TO_ME.search(place):
            return f"remote, but only for {job.get('location', '')[:50]}"

    # On-site and hybrid jobs have to be somewhere you can actually go.
    places = spec.get("places") or []
    where = job.get("location") or ""
    if detect_setup(job) != "Remote" and any(re.search(rf"\b{re.escape(_plain_place(a).strip())}\b", _plain_place(where)) for a in spec.get("avoid_onsite") or [] if a.strip()):
        return f"on-site/hybrid in {where[:50]}, which is too far"
    if places and detect_setup(job) != "Remote" and not place_fits(where, places):
        # No clear location is a no too: a job that only says "Philippines" could turn out to be anywhere.
        return f"on-site/hybrid in {where[:50]}, outside your locations" if where.strip() else "on-site/hybrid with no location listed, so it could be too far"

    types = spec.get("job_types") or []
    if types:
        meta = f"{title} {job.get('location', '')} {desc[:800]}"
        found = [t for t, rx in TYPES.items() if re.search(rx, meta, re.I)]
        if found and not set(found) & set(types):
            return f"job type {found[0]}; you want {', '.join(types)}"

    limit = spec.get("max_years_required")
    if limit is not None:
        asked = [int(y) for y in YEARS_RE.findall(desc)]
        if asked and min(asked) > float(limit):
            return f"requires {min(asked)}+ years (your limit is {limit:g})"

    floor = spec.get("min_salary_monthly")
    if floor:
        m = PHP_RE.search(f"{job.get('salary', '')} {desc[:1500]}")
        if m:
            top = int((m.group(2) or m.group(1)).replace(",", ""))
            if top < float(floor):
                return f"pay tops out at PHP {top:,}; your minimum is {int(floor):,}"
    return None
