"""Paths for one person's run, and the candidate profile used to answer application questions.

Vera points VERA_APPLY_DATA at that person's folder, so profiles, answers, logs,
letters and browser sign-ins are never shared between people.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]          # hermes/profiles/vera/app
DATA_DIR = Path(os.getenv("VERA_APPLY_DATA") or ROOT / "data" / "apply" / "owner")
# Runs side by side each get their own copy of these two.
PROFILE_PATH = Path(os.getenv("VERA_PROFILE_PATH") or DATA_DIR / "profile.json")
BROWSER_PROFILE_DIR = Path(os.getenv("VERA_BROWSER_DIR") or DATA_DIR / "browser")          # keeps the sign-ins
ANSWERS_PATH = DATA_DIR / "answers.json"            # remembered answers
LOG_PATH = DATA_DIR / "applications.csv"            # every attempt
SCREENSHOT_DIR = DATA_DIR / "screenshots"
STATE_PATH = DATA_DIR / "state.json"                # per-site cooldowns after bot checks

DEFAULT_LIMITS = {"max_per_run": 10, "max_per_day": 25, "delay_seconds": [25, 60]}

# Fields that most application forms ask for; `check` warns when they're empty.
IMPORTANT_FIELDS = [
    "first_name", "last_name", "email", "phone", "city", "resume_path",
    "expected_salary_monthly", "notice_period",
]


class ProfileError(RuntimeError):
    pass


def load_profile() -> Dict[str, Any]:
    if not PROFILE_PATH.exists():
        raise ProfileError(f"No profile found at {PROFILE_PATH}. Vera writes it before each run.")
    with open(PROFILE_PATH, encoding="utf-8") as f:
        profile = json.load(f)
    profile.setdefault("search", {})
    profile["limits"] = {**DEFAULT_LIMITS, **profile.get("limits", {})}
    return profile


def missing_fields(profile: Dict[str, Any]) -> List[str]:
    missing = [k for k in IMPORTANT_FIELDS if profile.get(k) in (None, "", [])]
    resume = profile.get("resume_path")
    if resume and not Path(resume).is_file():
        missing.append(f"resume_path (file not found: {resume})")
    return missing


UPLOAD_TYPES = (".pdf", ".docx", ".doc", ".txt", ".rtf", ".odt")


def allowed_upload(path: Any, profile: Dict[str, Any]) -> bool:
    """The only files a site may ever receive: the person's own résumé (or its PDF copy), or a cover letter
    written into their folder. Anything else on this laptop (.env files, browser data, other people's files) is refused."""
    try:
        p = Path(str(path)).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False
    if not p.is_file():
        return False
    resume = profile.get("resume_path")
    if resume:
        try:
            mine = Path(resume).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            mine = None
        if mine is not None and (p == mine or p == mine.with_suffix(".pdf")):
            return True                  # their own résumé, kept by Vera
    return (DATA_DIR / "cover_letters").resolve() in p.parents and p.suffix.lower() in UPLOAD_TYPES


def full_name(profile: Dict[str, Any]) -> str:
    return " ".join(p for p in (profile.get("first_name"), profile.get("last_name")) if p)


def ensure_dirs() -> None:
    for d in (DATA_DIR, BROWSER_PROFILE_DIR, SCREENSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
