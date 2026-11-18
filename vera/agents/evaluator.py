"""Evaluator agent: scores a job against the CURRENT preferences and profile.

Deterministic and explainable (no AI needed), so every percentage comes with
reasons. It never assumes qualifications you didn't list in /profile.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from vera.preferences import EXPERIENCE_RANGES, location_label

WEIGHTS = {"role": 15, "level": 10, "experience": 20, "skills": 25, "education": 5,
           "location": 15, "salary": 5, "employment": 5}

LEVEL_PATTERNS = [
    ("Internship", r"\bintern(ship)?\b|\bojt\b|\bpracticum\b"),
    ("Fresh Graduate", r"\bfresh ?grad(uate)?s?\b|\bnew ?grad(uate)?\b|\bgraduate (program|trainee)\b"),
    ("Manager", r"\bmanager\b|\bhead of\b|\bdirector\b|\bvp\b"),
    ("Lead", r"\blead\b|\bprincipal\b|\bstaff (engineer|developer)\b|\barchitect\b"),
    ("Senior", r"\bsenior\b|\bsr\.?\b|\bsnr\b"),
    ("Mid-Level", r"\bmid[- ]?(level|senior)?\b|\bintermediate\b|\bexperienced\b"),
    ("Associate", r"\bassociate\b"),
    ("Junior", r"\bjunior\b|\bjr\.?\b"),
    ("Entry Level", r"\bentry[- ]?level\b|\btrainee\b|\bno experience\b"),
]

SKILLS = [
    "python", "java", "javascript", "typescript", "php", "c#", "c++", "go", "golang", "ruby", "rust", "kotlin",
    "swift", "dart", "scala", "sql", "html", "css", "sass", "react", "react.js", "next.js", "vue", "angular",
    "svelte", "node.js", "node", "express", "laravel", "django", "flask", "fastapi", "spring", "spring boot",
    ".net", "asp.net", "rails", "flutter", "react native", "android", "ios", "unity", "tailwind", "bootstrap",
    "jquery", "mysql", "postgresql", "postgres", "mongodb", "redis", "sqlite", "firebase", "supabase",
    "graphql", "rest", "api", "docker", "kubernetes", "aws", "azure", "gcp", "linux", "git", "ci/cd",
    "terraform", "figma", "wordpress", "shopify", "selenium", "playwright", "pytest", "jest", "testing",
    "machine learning", "pandas", "excel", "power bi", "tableau", "networking", "agile", "scrum",
]
# Knowing the left skill implies the right one (only safe, obvious implications).
IMPLIES = {"mysql": ["sql"], "postgresql": ["sql"], "sqlite": ["sql"], "laravel": ["php"], "django": ["python"],
           "flask": ["python"], "fastapi": ["python"], "react": ["javascript"], "vue": ["javascript"],
           "angular": ["javascript", "typescript"], "next.js": ["react", "javascript"], "tailwind": ["css"],
           "bootstrap": ["css"], "sass": ["css"], "spring boot": ["spring", "java"], "asp.net": [".net", "c#"],
           "flutter": ["dart"], "react native": ["react", "javascript"]}
ALIASES = {"react.js": "react", "reactjs": "react", "nodejs": "node.js", "node": "node.js", "postgres": "postgresql",
           "golang": "go", "js": "javascript", "ts": "typescript", "vue.js": "vue", "nextjs": "next.js"}


def _canon(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s*\(.*?\)", "", s)        # "Laravel (CRUD operations)" -> "laravel"
    return ALIASES.get(s, s)


def _has(text: str, skill: str) -> bool:
    return re.search(r"(?<![a-z0-9+#.])" + re.escape(skill) + r"(?![a-z0-9+#])", text) is not None


def skills_in_text(text: str) -> List[str]:
    """Known skills mentioned anywhere in a text (e.g. an imported résumé)."""
    text = (text or "").lower()
    found = []
    for s in SKILLS:
        if _has(text, s):
            c = _canon(s)
            if c not in found:
                found.append(c)
    return found


def candidate_skills(profile: Dict[str, Any], prefs: Dict[str, Any] = None) -> set:
    """Skills the candidate has: profile list + résumé text + safe implications."""
    prefs = prefs or {}
    mine = {_canon(s) for s in (profile.get("skills") or []) + (prefs.get("requirements", {}).get("skills") or [])}
    mine |= {_canon(s) for s in (prefs.get("requirements", {}).get("languages") or [])}
    mine |= set(skills_in_text(profile.get("resume_text", "")))
    mine |= {implied for s in list(mine) for implied in IMPLIES.get(s, [])}
    return mine


def job_skills(job: Dict[str, Any]) -> List[str]:
    text = f"{job.get('title', '')} {' '.join(job.get('tags') or [])} {job.get('description', '')}".lower()
    found = []
    for s in SKILLS:
        if _has(text, s):
            c = _canon(s)
            if c not in found:
                found.append(c)
    return found


def detect_level(job: Dict[str, Any]) -> Optional[str]:
    return detect_level_source(job)[0]


def detect_level_source(job: Dict[str, Any]):
    """(level, where): 'title' = stated in the job title (strong), 'label' = the job board's own
    seniority tag (weak: e.g. Himalayas tags most jobs 'Mid-level'), 'description' or None."""
    title = (job.get("title") or "").lower()
    for level, pat in LEVEL_PATTERNS:
        if re.search(pat, title):
            return level, "title"
    desc = (job.get("description") or "")[:1500].lower()
    if re.search(LEVEL_PATTERNS[1][1], desc) or re.search(r"no (prior )?experience (required|needed)|entry[- ]level", desc):
        return ("Fresh Graduate" if re.search(LEVEL_PATTERNS[1][1], desc) else "Entry Level"), "description"
    label = f"{job.get('level', '')} {job.get('employment_type', '')}".lower()
    if re.search(r"any", label):
        return None, None
    for level, pat in LEVEL_PATTERNS:
        if re.search(pat, label):
            return level, "label"
    return None, None


def experience_required(text: str) -> Tuple[Optional[float], Optional[float]]:
    """(mandatory_years, preferred_years) parsed from the description."""
    mandatory = preferred = None
    for sent in re.split(r"(?<=[.!?\n•])", (text or "").lower()):
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+|plus)?\s*(?:-|to)?\s*(\d+)?\s*\+?\s*(?:years?|yrs?)\b[^.]{0,60}?(experience|exp\b|working|in |with |of )", sent)
        if not m:
            continue
        years = float(m.group(1))
        if years > 20:
            continue
        if re.search(r"prefer|nice to have|a plus|is a plus|bonus|ideally|advantage|desirable", sent):
            preferred = years if preferred is None else min(preferred, years)
        else:
            mandatory = years if mandatory is None else min(mandatory, years)
    return mandatory, preferred


ROLE_FILLER = {"junior", "jr", "senior", "sr", "entry", "level", "lead", "principal", "staff", "associate", "mid",
               "i", "ii", "iii", "iv", "the", "a", "an", "of", "and", "or", "for", "in", "remote", "trainee", "intern"}
ROLE_SYNONYMS = {"dev": "develo", "devs": "develo", "programmer": "progra", "fullstack": "full", "frontend": "front",
                 "backend": "back", "front-end": "front", "back-end": "back", "full-stack": "full", "qa": "qa",
                 "tester": "test", "testing": "test", "test": "test", "engineer": "engine", "engineering": "engine"}


def _role_words(text: str) -> List[str]:
    t = re.sub(r"full[\s-]?stack", "fullstack", (text or "").lower())
    t = re.sub(r"front[\s-]?end", "frontend", t)
    t = re.sub(r"back[\s-]?end", "backend", t)
    out = []
    for w in re.findall(r"[a-z0-9+#.]+", t):
        if w in ROLE_FILLER or len(w) < 2:
            continue
        out.append(ROLE_SYNONYMS.get(w, w[:6]))      # crude stem: developer/development -> "develo"
    return out


def position_match(title: str, position: str) -> float:
    """Share of a position's key words found in the job title (1.0 = full match).
    'Junior Web Developer' and 'Web Development Specialist' match 'Web Developer'; 'Web Designer' doesn't."""
    pw = _role_words(position)
    if not pw:
        return 0.0
    tw = set(_role_words(title))
    return sum(1 for w in pw if w in tw) / len(pw)


def _accepted_max_years(prefs: Dict[str, Any]) -> float:
    if prefs.get("custom_experience_max_years") is not None:
        return float(prefs["custom_experience_max_years"])
    ranges = [EXPERIENCE_RANGES[e] for e in prefs.get("experience", []) if e in EXPERIENCE_RANGES]
    return max((r[1] for r in ranges), default=99)


def _contains_any(text: str, words: List[str]) -> Optional[str]:
    t = text.lower()
    for w in words:
        if w and re.search(r"\b" + re.escape(w.lower()) + r"\b", t):
            return w
    return None


def evaluate(job: Dict[str, Any], prefs: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    title = job.get("title") or ""
    desc = job.get("description") or ""
    text_all = f"{title}\n{desc}"
    comps: Dict[str, Dict[str, Any]] = {}
    pros: List[str] = []
    cons: List[str] = []
    missing: List[str] = []
    hard_reject: List[str] = []

    # ---- exclusions (hard rejects) -------------------------------------------------
    ex = prefs.get("exclude", {})
    if (w := _contains_any(title, ex.get("positions", []))):
        hard_reject.append(f"excluded position '{w}'")
    if (w := _contains_any(job.get("company") or "", ex.get("companies", []))):
        hard_reject.append(f"excluded company '{w}'")
    if (w := _contains_any(job.get("location") or "", ex.get("locations", []))):
        hard_reject.append(f"excluded location '{w}'")
    if (w := _contains_any(f"{' '.join(job.get('tags') or [])} {desc[:2000]}", ex.get("industries", []))):
        hard_reject.append(f"excluded industry '{w}'")
    if (w := _contains_any(text_all, ex.get("keywords", []))):
        hard_reject.append(f"excluded keyword '{w}'")

    # ---- role / position fit ------------------------------------------------------
    positions = prefs.get("positions") or []
    tl = title.lower()
    best, best_pos = 0.0, None
    for p in positions:
        hit = position_match(title, p)
        if hit > best:
            best, best_pos = hit, p
    role = 100 if best >= 0.99 else int(40 + best * 60) if best > 0 else (50 if not positions else 15)
    comps["role"] = {"score": role, "note": f"matches “{best_pos}”" if role >= 80 else
                     "partly matches your positions" if role >= 50 else "not one of your positions"}
    if prefs.get("only_listed_positions") and positions and best < 0.99:
        hard_reject.append("not one of the positions you listed")

    # ---- job level ----------------------------------------------------------------
    allowed = set(prefs.get("job_levels", [])) | set(prefs.get("custom_levels", []))
    level, where_from = detect_level_source(job)
    entryish = {"Internship", "Fresh Graduate", "Entry Level", "Junior"}
    if "Any Level" in allowed or not allowed:
        lv = 100
    elif level is None:
        lv = 70
    elif level in allowed:
        lv = 100
    elif level in entryish and allowed & entryish:
        lv = 85
    elif where_from == "label":
        lv = 45          # only the job board's tag says so; the title/description don't
    else:
        lv = 10
    note = ("level not stated" if not level else
            f"tagged {level} by the job board" if where_from == "label" else f"looks {level}")
    comps["level"] = {"score": lv, "note": note, "detected": level}
    if lv <= 10:
        cons.append(f"{level} level is outside your preferred levels")

    # ---- experience ---------------------------------------------------------------
    mandatory, preferred = experience_required(desc)
    have = float(profile.get("years_experience") or 0)
    ok_max = _accepted_max_years(prefs)
    reject_over = prefs.get("reject_mandatory_experience_over")
    if mandatory is None:
        es = 100 if level in ("Internship", "Fresh Graduate", "Entry Level") else 75
        enote = "no minimum experience stated"
    elif reject_over is not None and mandatory > float(reject_over):
        es = 0
        enote = f"requires {mandatory:g}+ yrs (your limit is {reject_over})"
        hard_reject.append(enote)
    elif mandatory <= have:
        es, enote = 100, f"requires {mandatory:g} yrs, you have {have:g}"
    elif mandatory <= ok_max:
        es = max(30, int(100 - (mandatory - have) * 25))
        enote = f"requires {mandatory:g} yrs, you have {have:g}"
        missing.append(f"{mandatory - have:g} more year(s) of experience")
    else:
        es, enote = 15, f"requires {mandatory:g} yrs (above your experience settings)"
        cons.append(enote)
    if preferred is not None and preferred > have:
        enote += f"; {preferred:g} yrs preferred (not required)"
    comps["experience"] = {"score": es, "note": enote, "mandatory_years": mandatory, "preferred_years": preferred}

    # ---- skills -------------------------------------------------------------------
    needed = job_skills(job)
    mine = candidate_skills(profile, prefs)
    matched = [s for s in needed if s in mine]
    lacking = [s for s in needed if s not in mine]
    if not needed:
        ss, snote = 60, "no specific technologies listed"
    else:
        ss = int(100 * len(matched) / len(needed))
        snote = f"{len(matched)}/{len(needed)} listed skills match"
    if lacking:
        missing += [f"skill: {s}" for s in lacking[:8]]
    must = [_canon(s) for s in prefs.get("requirements", {}).get("languages") or []]
    if must and needed and not any(m in needed for m in must):
        ss = min(ss, 30)
        cons.append("doesn't use your required languages (" + ", ".join(must) + ")")
    comps["skills"] = {"score": ss, "note": snote, "matched": matched, "missing": lacking}

    # ---- education ----------------------------------------------------------------
    edu_text = desc.lower()
    has_degree = bool(profile.get("education")) or bool(re.search(
        r"\b(bachelor|b\.?s\.?|bsit|bscs|degree|diploma)\b", (profile.get("resume_text") or "").lower()))
    if re.search(r"\b(master'?s|msc|m\.s\.|phd|doctorate)\b", edu_text) and not re.search(r"bachelor'?s? or (higher|master)", edu_text):
        degree_note = "asks for a master's/PhD"
        edu = 40 if has_degree else 10
        missing.append("postgraduate degree")
    elif re.search(r"\b(bachelor'?s?|b\.?s\.?c?|degree|college graduate|undergraduate)\b", edu_text):
        degree_note = "asks for a bachelor's degree"
        edu = 100 if has_degree else 20
        if not has_degree:
            missing.append("bachelor's degree")
    else:
        degree_note, edu = "no degree requirement stated", 90
    req_degree = prefs.get("requirements", {}).get("degree")
    if req_degree and req_degree.lower() not in edu_text:
        degree_note += f" (your preference '{req_degree}' not mentioned)"
    comps["education"] = {"score": edu, "note": degree_note}

    # ---- location / work setup ----------------------------------------------------
    setups = set(prefs.get("work_setup") or [])
    loc_text = (job.get("location") or "").lower()
    remote = bool(job.get("remote")) or "remote" in loc_text or "remote" in tl
    my_places = [p for loc in prefs.get("locations", []) for p in (loc.get("city"), loc.get("province"), loc.get("region"), loc.get("country")) if p]
    my_countries = {(loc.get("country") or "").lower() for loc in prefs.get("locations", []) if loc.get("country")}
    if remote:
        if "Remote" not in setups:
            ls, lnote = 20, "remote job, but you didn't select Remote"
        else:
            restricted = loc_text and not re.search(r"worldwide|anywhere|global|remote$|^remote", loc_text)
            if restricted and my_countries and not any(c in loc_text for c in my_countries) and "asia" not in loc_text and "apac" not in loc_text:
                ls, lnote = 35, f"remote, but limited to {job.get('location')}"
                cons.append(lnote)
            else:
                ls, lnote = 100, "remote" + (f" ({job.get('location')})" if job.get("location") else "")
    else:
        hit = next((p for p in my_places if p.lower() in loc_text), None)
        if setups and not (setups & {"On-site", "Hybrid"}):
            ls, lnote = 15, "on-site/hybrid, but you only want remote"
        elif hit:
            ls, lnote = 100, f"in {hit}"
        elif not loc_text:
            ls, lnote = 60, "location not stated"
        elif prefs.get("willing_to_relocate"):
            ls, lnote = 55, f"{job.get('location')} (would need to relocate)"
        else:
            ls, lnote = 15, f"{job.get('location')} is outside your locations"
            cons.append(lnote)
    comps["location"] = {"score": ls, "note": lnote, "remote": remote}

    # ---- salary -------------------------------------------------------------------
    sal = prefs.get("salary", {})
    jmin, jmax = job.get("salary_min"), job.get("salary_max")
    same_unit = (not job.get("salary_currency") or (job.get("salary_currency") or "").upper() == (sal.get("currency") or "").upper()) \
        and (not job.get("salary_period") or job.get("salary_period") == sal.get("period"))
    if not (jmin or jmax):
        ss2 = 70 if sal.get("accept_undisclosed", True) else 20
        snote2 = "salary not disclosed"
    elif not same_unit:
        ss2 = 60
        snote2 = f"{_fmt_salary(job)} (different currency/period, compare manually)"
    elif sal.get("min") and (jmax or jmin) < sal["min"]:
        ss2 = 40 if sal.get("negotiable") else 10
        snote2 = f"{_fmt_salary(job)} is below your minimum"
        cons.append(snote2)
    else:
        ss2, snote2 = 100, _fmt_salary(job)
    comps["salary"] = {"score": ss2, "note": snote2}

    # ---- employment type ----------------------------------------------------------
    wanted = {t.lower() for t in prefs.get("employment_types") or []}
    jt = (job.get("employment_type") or "").lower()
    if not jt or not wanted:
        es2, enote2 = 80, "employment type not stated" if not jt else "any type"
    elif any(w in jt for w in wanted):
        es2, enote2 = 100, job.get("employment_type")
    else:
        es2, enote2 = 20, f"{job.get('employment_type')} (not one of your types)"
        cons.append(enote2)
    comps["employment"] = {"score": es2, "note": enote2}

    # ---- overall ------------------------------------------------------------------
    overall = round(sum(comps[k]["score"] * w for k, w in WEIGHTS.items()) / sum(WEIGHTS.values()))
    if comps["level"]["score"] <= 10:
        overall = min(overall, 45)   # clearly the wrong seniority for your current settings
    if comps["location"]["score"] <= 35:
        overall = min(overall, 45)   # you can't realistically take it from where you are
    if comps["role"]["score"] <= 15:
        overall = min(overall, 40)   # not one of the positions you asked for
        cons.append("not one of your preferred positions")
    if hard_reject:
        overall = min(overall, 10)
        cons = [f"Rejected: {r}" for r in hard_reject] + cons
    if comps["role"]["score"] >= 80:
        pros.append("matches a position you want")
    if comps["skills"]["matched"]:
        pros.append("uses your skills: " + ", ".join(comps["skills"]["matched"][:6]))
    if comps["experience"]["score"] >= 90:
        pros.append(comps["experience"]["note"])
    if comps["location"]["score"] >= 90:
        pros.append(comps["location"]["note"])
    if comps["salary"]["score"] >= 90 and (jmin or jmax):
        pros.append("salary meets your minimum")

    return {
        "score": int(overall), "recommended": not hard_reject and overall >= int(prefs.get("min_match", 50)),
        "components": comps, "pros": pros, "cons": cons, "missing": list(dict.fromkeys(missing)),
        "hard_reject": hard_reject, "job_skills": needed,
    }


def _fmt_salary(job: Dict[str, Any]) -> str:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    cur = job.get("salary_currency") or ""
    per = job.get("salary_period") or ""
    fmt = lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else str(v)
    rng = f"{fmt(lo)}–{fmt(hi)}" if lo and hi and lo != hi else fmt(lo or hi)
    return f"{cur} {rng}{' / ' + per if per else ''}".strip()


def salary_text(job: Dict[str, Any]) -> str:
    if job.get("salary_min") or job.get("salary_max"):
        return _fmt_salary(job)
    m = re.search(r"^Salary: (.+)$", job.get("description") or "", re.M)
    return m.group(1) if m else "Not disclosed"


def where(job: Dict[str, Any], ev: Dict[str, Any]) -> str:
    loc = job.get("location") or "Not stated"
    return f"{loc} · Remote" if ev["components"]["location"].get("remote") and "remote" not in loc.lower() else loc


__all__ = ["evaluate", "job_skills", "detect_level", "experience_required", "salary_text", "where", "location_label"]
