"""Settings from .env (never hard-coded) and file locations."""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]          # hermes/profiles/vera/app
load_dotenv(ROOT / ".env")
# Her Hermes profile's .env too (never overriding the lines above), so a new AI key only has to go in one place.
load_dotenv(ROOT.parent / ".env")
# Settings saved before the rename to Vera still work: FREI_X counts as VERA_X.
for _key in [k for k in os.environ if k.startswith("FREI_")]:
    os.environ.setdefault("VERA_" + _key[len("FREI_"):], os.environ[_key])

DATA_DIR = Path(os.getenv("VERA_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "vera.db"
RESUME_DIR = DATA_DIR / "resumes"


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
# Bot owner: gets email, job mail and the private channels. Everyone can use Vera.
OWNER_ID = _int("DISCORD_OWNER_ID")
# Optional: register slash commands to one server instantly (otherwise global, can take up to 1h).
GUILD_ID = _int("DISCORD_GUILD_ID")
# The only servers Vera stays in (9 Nov 2026: her Discord app was a Public Bot, so anyone could add her to theirs).
# She leaves any other server as soon as she's added. More servers: VERA_ALLOWED_GUILDS=id1,id2. Empty means none set,
# and then she leaves nothing.
ALLOWED_GUILDS = {g for g in [GUILD_ID] + [int(x) for x in re.findall(r"\d{5,}", os.getenv("VERA_ALLOWED_GUILDS", ""))] if g}
MAIN_CHANNEL = os.getenv("VERA_MAIN_CHANNEL", "application")
GUIDE_CHANNEL = os.getenv("VERA_GUIDE_CHANNEL", "vera-instructions")
OUTREACH_CHANNEL = os.getenv("VERA_OUTREACH_CHANNEL", "vera-social-outreach")
# Employers' replies are posted here as they arrive, plus a summary of the day's replies every evening.
JOB_MAIL_CHANNEL = os.getenv("VERA_JOB_MAIL_CHANNEL", "vera-job-mail")
MAIL_DIGEST_HOUR = int(os.getenv("VERA_MAIL_DIGEST_HOUR", "20") or 20)     # 8 PM, local time
# The owner's list of future improvements that need new code. Posted by vera_note.
NOTES_CHANNEL = os.getenv("VERA_NOTES_CHANNEL", "vera-notes")
# The owner's private category. Other members get these by DM.
OWNER_CATEGORY = os.getenv("VERA_OWNER_CATEGORY", "vera-owner")
OWNER_CHANNELS = {"chat": OWNER_CATEGORY, "email": f"{OWNER_CATEGORY}-email", "resume": f"{OWNER_CATEGORY}-resume",
                  "reports": f"{OWNER_CATEGORY}-reports", "files": f"{OWNER_CATEGORY}-files", "updates": f"{OWNER_CATEGORY}-updates"}
# Where each source's applications get posted. Sources without a channel use the main one.
# Asking for applications in these channels means that source, unless you name another.
CHANNEL_SITES = {"vera-remote": ["remote_boards"], "vera-company-site-application": ["company_forms"]}

# Cold email: your own Gmail and an app password. Sending is off until both are set.
OUTREACH_EMAIL = os.getenv("OUTREACH_EMAIL", "").strip()
OUTREACH_APP_PASSWORD = os.getenv("OUTREACH_APP_PASSWORD", "").strip().replace(" ", "")
OUTREACH_MAX_PER_DAY = _int("OUTREACH_MAX_PER_DAY", 20)
# Replies visible to everyone (false) or only to the person who asked (true).
PRIVATE_REPLIES = os.getenv("VERA_PRIVATE_REPLIES", "false").strip().lower() in ("1", "true", "yes")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()           # free, no card: open-source models (Llama, GPT-OSS, Qwen)
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()     # free "Experiment" plan (phone check)
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")
OPENROUTER_FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "OPENROUTER_FALLBACK_MODELS", "google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3.5-super-vl:free,dots-studio/dots-3-note-preview:free,nvidia/nemotron-3-ultra-550b-a55b:free,openrouter/free").split(",") if m.strip()]

# Limits for other people's application rounds, which share the owner's PC and connection.
SHARED_MAX_PER_RUN = _int("VERA_SHARED_MAX_PER_RUN", 5)
SHARED_MAX_PER_DAY = _int("VERA_SHARED_MAX_PER_DAY", 10)

USER_AGENT = "Vera-JobHunter/1.0 (personal job search assistant)"
TIMEZONE = os.getenv("VERA_TIMEZONE", "Asia/Manila")


# The form engine and its browser handle untrusted web pages, and run for other members too. They never need these.
ENGINE_HIDDEN = ("DISCORD_TOKEN", "OUTREACH_EMAIL", "OUTREACH_APP_PASSWORD")
# Inside Hermes the process also holds Hermes's secrets (its Discord token, API keys). The engine only keeps the
# free AI keys it uses for form answers.
ENGINE_AI_KEYS = ("GEMINI_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY")
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|_KEY$|_KEYS$|CREDENTIAL|COOKIE|AUTH", re.I)


def engine_env(folder, browser=None, profile=None) -> dict:
    """Settings for one person's form engine: their folder, and none of the owner's login secrets."""
    env = {k: v for k, v in os.environ.items() if k not in ENGINE_HIDDEN and k not in ("VERA_BROWSER_DIR", "VERA_PROFILE_PATH")
           and (k in ENGINE_AI_KEYS or not SECRET_NAME.search(k))}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", VERA_APPLY_DATA=str(folder))
    if browser:
        env["VERA_BROWSER_DIR"] = str(browser)
    if profile:
        env["VERA_PROFILE_PATH"] = str(profile)
    return env


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
