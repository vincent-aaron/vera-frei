"""Job-search preferences and candidate profile: defaults, choices and editing.

Everything here is data stored in the database, so preferences can change from
Discord at any time without touching code. Nothing assumes you only want
entry-level jobs: the defaults are a starting point you are expected to edit.
"""

import copy
import re
from typing import Any, Dict, List

JOB_LEVELS = ["Internship", "Fresh Graduate", "Entry Level", "Junior", "Associate", "Mid-Level",
              "Senior", "Lead", "Manager", "Any Level"]
EXPERIENCE = ["No Experience Required", "Fresh Graduates Welcome", "0-1 Years", "1-2 Years",
              "2-3 Years", "3-5 Years", "5+ Years", "Any Experience Level"]
EMPLOYMENT_TYPES = ["Full-time", "Part-time", "Contract", "Freelance", "Internship", "Temporary"]
WORK_SETUPS = ["Remote", "Hybrid", "On-site"]
SALARY_PERIODS = ["monthly", "yearly", "hourly"]

# Numeric experience ranges (years) each option accepts.
EXPERIENCE_RANGES = {
    "No Experience Required": (0, 0), "Fresh Graduates Welcome": (0, 0), "0-1 Years": (0, 1),
    "1-2 Years": (1, 2), "2-3 Years": (2, 3), "3-5 Years": (3, 5), "5+ Years": (5, 99),
    "Any Experience Level": (0, 99),
}

DEFAULT_PREFERENCES: Dict[str, Any] = {
    "job_levels": ["Fresh Graduate", "Entry Level", "Junior"],
    "custom_levels": [],
    "experience": ["No Experience Required", "Fresh Graduates Welcome", "0-1 Years"],
    "custom_experience_max_years": None,       # e.g. 1.5 -> accept jobs needing up to 1.5 yrs
    "reject_mandatory_experience_over": 2,      # reject if a job REQUIRES more than this (years)
    "positions": ["Software Developer", "Web Developer", "Software Engineer", "Full Stack Developer"],
    "only_listed_positions": False,   # True = never suggest a job whose title isn't one of `positions`
    "locations": [{"country": "Philippines", "region": "", "province": "", "city": ""}],
    "work_setup": ["Remote", "Hybrid", "On-site"],
    "willing_to_relocate": False,
    "salary": {"min": None, "max": None, "currency": "PHP", "period": "monthly",
               "negotiable": True, "accept_undisclosed": True},
    "employment_types": ["Full-time", "Contract"],
    "exclude": {"positions": [], "industries": [], "companies": [], "locations": [], "keywords": [], "onsite_locations": []},
    "requirements": {"degree": "", "languages": [], "skills": [], "schedule": "", "other": []},
    "sources": ["remotive", "arbeitnow", "jobicy", "himalayas", "themuse", "remoteok", "weworkremotely", "workingnomads", "companyforms", "hnhiring", "jobspresso", "realworkfromanywhere", "larajobs"],
    "min_match": 50,             # only recommend jobs scoring at least this %
    "results_per_search": 5,
    "language": "English",       # the language she chats in, everywhere
    "max_age_days": 7,           # never recommend jobs posted longer ago than this
    "daily_digest": {"enabled": False, "hour": 8},   # turn on per person
    "digest_count": 15,          # jobs posted each morning (max 20)
    "include_cover_letter": False,
    "followup_after_days": 7,
    # Application rounds: limits and options. A round only starts after the person approves its plan.
    "rounds": {"max_per_run": None, "max_per_day": None,    # None = no limit (other people always have one)
                  "cover_letters": True, "tabs": None, "tabs_per_site": None},
}

DEFAULT_PROFILE: Dict[str, Any] = {
    "name": "", "email": "", "phone": "", "location": "",
    "headline": "", "summary": "",
    "years_experience": 0,
    "education": [],             # [{"degree": "", "school": "", "year": "", "honors": ""}]
    "experience": [],            # [{"title": "", "company": "", "dates": "", "details": []}]
    "skills": [], "languages": [], "certifications": [], "achievements": [],
    "links": {"github": "", "linkedin": "", "portfolio": ""},
    "resumes": [],               # [{"name": "", "path": "", "tags": []}]
    "application": {},           # details forms ask for: expected pay, notice, work rights... set with /apply-details
}


def defaults() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_PREFERENCES)


def merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge patch into a copy of base (dicts merged, everything else replaced)."""
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def split_list(text: str) -> List[str]:
    """'a, b; c\\nd' -> ['a', 'b', 'c', 'd'] (empty input -> [])."""
    return [p.strip() for p in re.split(r"[,;\n]", text or "") if p.strip()]


def parse_locations(text: str) -> List[Dict[str, str]]:
    """One location per line: 'City, Province, Region, Country' (any part optional)."""
    out = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if not parts:
            continue
        if len(parts) == 1:
            out.append({"country": "", "region": "", "province": "", "city": parts[0]}
                       if parts[0].lower() not in COUNTRIES else
                       {"country": parts[0], "region": "", "province": "", "city": ""})
            continue
        city, *rest = parts
        country = rest[-1] if rest else ""
        province = rest[0] if len(rest) >= 2 else ""
        region = rest[1] if len(rest) >= 3 else ""
        out.append({"city": city, "province": province, "region": region, "country": country})
    return out


def location_label(loc: Dict[str, str]) -> str:
    return ", ".join(p for p in (loc.get("city"), loc.get("province"), loc.get("region"), loc.get("country")) if p) or "Anywhere"


def parse_money(text: str):
    if text is None or not str(text).strip():
        return None
    t = str(text).lower().replace(",", "").replace("₱", "").replace("php", "").strip()
    m = re.match(r"(\d+(?:\.\d+)?)\s*(k)?", t)
    if not m:
        raise ValueError(f"Not a number: {text}")
    v = float(m.group(1)) * (1000 if m.group(2) else 1)
    return int(v)


COUNTRIES = {"philippines", "usa", "united states", "singapore", "japan", "australia", "canada",
             "united kingdom", "uk", "germany", "worldwide", "anywhere", "remote"}


# Common Philippine places, so "Bulacan" or "Manila" get country Philippines.
PH_PLACES = {
    "metro manila": ("", "Metro Manila"), "ncr": ("", "Metro Manila"), "manila": ("Manila", "Metro Manila"),
    "quezon city": ("Quezon City", "Metro Manila"), "makati": ("Makati", "Metro Manila"), "taguig": ("Taguig", "Metro Manila"),
    "pasig": ("Pasig", "Metro Manila"), "mandaluyong": ("Mandaluyong", "Metro Manila"), "bgc": ("Taguig", "Metro Manila"),
    "pasay": ("Pasay", "Metro Manila"), "paranaque": ("Paranaque", "Metro Manila"), "caloocan": ("Caloocan", "Metro Manila"),
    "valenzuela": ("Valenzuela", "Metro Manila"), "muntinlupa": ("Muntinlupa", "Metro Manila"), "ortigas": ("Pasig", "Metro Manila"),
    "bulacan": ("", "Bulacan"), "malolos": ("Malolos", "Bulacan"), "hagonoy": ("Hagonoy", "Bulacan"), "meycauayan": ("Meycauayan", "Bulacan"),
    "pampanga": ("", "Pampanga"), "angeles": ("Angeles", "Pampanga"), "clark": ("Clark", "Pampanga"), "san fernando": ("San Fernando", "Pampanga"),
    "bataan": ("", "Bataan"), "balanga": ("Balanga", "Bataan"), "tarlac": ("", "Tarlac"), "nueva ecija": ("", "Nueva Ecija"),
    "zambales": ("", "Zambales"), "subic": ("Subic", "Zambales"), "cavite": ("", "Cavite"), "laguna": ("", "Laguna"),
    "rizal": ("", "Rizal"), "batangas": ("", "Batangas"), "cebu": ("", "Cebu"), "davao": ("", "Davao"), "iloilo": ("", "Iloilo"),
    "baguio": ("Baguio", "Benguet"),
}


def place(name: str, default_country: str = "Philippines") -> Dict[str, str]:
    """'Bulacan' -> {city:'', province:'Bulacan', region:'', country:'Philippines'}."""
    key = name.strip().lower()
    if key in PH_PLACES:
        city, province = PH_PLACES[key]
        return {"city": city, "province": province, "region": "", "country": "Philippines"}
    return {"city": name.strip(), "province": "", "region": "", "country": default_country}


def fill_country(locs: List[Dict[str, str]], default_country: str = "Philippines") -> List[Dict[str, str]]:
    """Locations without a country get one (needed to filter remote jobs open to you)."""
    out = []
    for l in locs:
        l = {"city": "", "province": "", "region": "", "country": "", **{k: (v or "") for k, v in l.items()}}
        if not l["country"]:
            key = (l["city"] or l["province"]).lower()
            l["country"] = "Philippines" if key in PH_PLACES or l["province"].lower() in PH_PLACES else default_country
        out.append(l)
    return out
