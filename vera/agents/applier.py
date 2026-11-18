"""Applier agent: submits an application ONLY after you approve it in Discord.

- Company-form links (Greenhouse, Lever, Ashby, Workable), owner only: Vera's form engine (vera/apply)
  fills and submits using your approved answers. These need no login. If a site shows a bot check,
  nothing is submitted and Vera tells you to apply yourself.
- Every other link: Vera gives you the link + your prepared answers and records the application
  when you press "Mark as applied". She never signs in to job boards.
"""

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple

from vera import config

BROWSER_SITES = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com")


def _folder(uid: int) -> Path:
    from vera.agents import rounds
    return rounds.user_dir(uid)


def can_auto_submit(url: str, uid: int = None) -> bool:
    """Only for the bot owner, and only company forms, which need no login."""
    if not config.OWNER_ID or uid != config.OWNER_ID:
        return False
    from urllib.parse import urlparse
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    if urlparse(url or "").scheme not in ("http", "https"):
        return False
    # The real site (or a subdomain), judged by the host: "https://evil.example/lever.co/" isn't Lever.
    return any(host == s or host.endswith("." + s) for s in BROWSER_SITES)


def _norm(text: str) -> str:   # same normalisation as vera/apply/answers.py
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+ ]", " ", (text or "").lower())).strip()


# A drive letter, a network or home path, a parent-folder hop, or a secrets file: an "answer" like this is an attempt
# to make the form engine upload a file from the laptop. Answers are words, never paths.
PATH_LIKE = re.compile(r"(^|\s)[a-z]:[\\/]|\\\\|(^|\s)~[\\/]|\.\.[\\/]|(^|[\\/\s])\.env\b|%[a-z_]+%|[\\/](users|windows|appdata|etc|home)[\\/]|file:", re.I)
MAX_ANSWER = 1500


def safe_answer(answer) -> bool:
    return isinstance(answer, (str, int, float, bool)) and not PATH_LIKE.search(str(answer)) and len(str(answer)) <= MAX_ANSWER


def share_answers(uid: int, answers: Dict[str, str]) -> None:
    """Add answers to this person's answer memory, used by the form engine. Anything that looks like a file path is dropped."""
    path = _folder(uid) / "answers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    for q, a in answers.items():
        if not safe_answer(a):
            continue
        data[_norm(q)] = a
        data.setdefault("_sources", {})[_norm(q)] = "user"      # their own answer, not an AI guess
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


async def submit_via_browser(session, url: str, answers: Dict[str, str], timeout_min: int = 15) -> Tuple[str, str]:
    """Run the form engine for one URL. Returns (status, detail)."""
    from vera.agents import rounds
    rounds.write_profile(session)
    share_answers(session.uid, answers)
    proc = await asyncio.create_subprocess_exec(
        str(rounds.engine_python()), "-m", "vera.apply", "apply", f"--url={url}", "--max", "1", "--background",
        cwd=str(config.ROOT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=config.engine_env(_folder(session.uid)),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_min * 60)
    except asyncio.TimeoutError:
        rounds.kill_tree(proc.pid)
        return "failed", "the form engine timed out"
    text = out.decode(errors="replace")
    m = re.search(r"->\s*(SUBMITTED|SKIPPED|FAILED|DRY-RUN|BLOCKED|NEEDS_ANSWER|NEEDS_LOGIN|FILTERED):?\s*(.*)", text)
    if not m:
        return "failed", (text.strip().splitlines() or ["no output"])[-1][:300]
    status = m.group(1).lower()
    detail = m.group(2).strip()
    un = re.search(r"unanswered:\s*(.*)", text)
    if un:
        detail += f" | unanswered: {un.group(1).strip()}"
    return ("submitted" if status == "submitted" else status), detail[:900]


__all__ = ["can_auto_submit", "submit_via_browser", "share_answers", "sys"]
