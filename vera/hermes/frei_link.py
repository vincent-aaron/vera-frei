"""Link to Frei, the owner's manager agent. READ-ONLY: this file never writes to Frei's or the kanban database.

1. approved_task(): a kanban worker (Hermes source "kanban") may use Vera's tools only when the task was created by Frei
   (created_by "frei", assigned to this profile) AND carries an approval id that Frei's approvals table shows the owner
   approved ("proceed <id>") for exactly this task. Anything else: refused.
2. rules(): the lasting rules the owner approved for Vera through Frei (frei_agent_rule), read at every turn.
Added 13 Nov 2026 with Frei (profiles\frei).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

PROFILE = "vera"
MARK = re.compile(r"frei-approval:([0-9a-f]{6})\b")
TASK_KINDS = {"assign_task", "request_changes", "request_video"}


def root() -> Path:
    # hermes/profiles/vera/app/vera/hermes/frei_link.py -> hermes
    return Path(os.getenv("HQ_HERMES_ROOT") or Path(__file__).resolve().parents[5])


@contextmanager
def _ro(db: Path) -> Iterator[Optional[sqlite3.Connection]]:
    if not db.is_file():
        yield None
        return
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def approved_task(task_id: str) -> Optional[str]:
    """The approval id if this kanban task is a Frei task the owner approved for this agent, else None."""
    if not task_id:
        return None
    try:
        with _ro(root() / "kanban.db") as k:
            t = k.execute("SELECT assignee, created_by, body FROM tasks WHERE id = ?", (task_id,)).fetchone() if k else None
        if not t or t["assignee"] != PROFILE or t["created_by"] != "frei":
            return None
        m = MARK.search(t["body"] or "")
        if not m:
            return None
        with _ro(root() / "profiles" / "frei" / "app" / "data" / "frei.db") as f:
            a = f.execute("SELECT kind, status, payload, kanban_task FROM approvals WHERE id = ?",
                          (m.group(1),)).fetchone() if f else None
        if not a or a["kind"] not in TASK_KINDS or a["status"] != "done" or a["kanban_task"] != task_id:
            return None
        if json.loads(a["payload"]).get("agent") != PROFILE:
            return None
        return m.group(1)
    except (sqlite3.Error, ValueError):
        return None


def rules() -> List[str]:
    try:
        with _ro(root() / "profiles" / "frei" / "app" / "data" / "frei.db") as f:
            if f is None:
                return []
            return [r["rule"] for r in f.execute("SELECT rule FROM agent_rules WHERE agent = ? AND active = 1 ORDER BY id",
                                                 (PROFILE,))]
    except sqlite3.Error:
        return []
