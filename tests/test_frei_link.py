"""Tasks from Frei (kanban workers) only work when the owner approved that exact task in Frei; read and draft only."""
import json
import sqlite3

import pytest

from vera import config
from vera.hermes import frei_link, identity, tools


def _world(tmp_path, *, created_by="frei", assignee="vera", status="done", agent="vera"):
    k = sqlite3.connect(tmp_path / "kanban.db")
    k.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT, created_by TEXT)")
    k.execute("INSERT INTO tasks VALUES ('t1', 'x', 'do it frei-approval:abc123', ?, 'running', ?)", (assignee, created_by))
    k.commit()
    k.close()
    d = tmp_path / "profiles/frei/app/data"
    d.mkdir(parents=True)
    f = sqlite3.connect(d / "frei.db")
    f.execute("CREATE TABLE approvals (id TEXT PRIMARY KEY, kind TEXT, summary TEXT, payload TEXT, payload_hash TEXT, "
              "status TEXT, created_at REAL, decided_at REAL, result TEXT, kanban_task TEXT)")
    f.execute("CREATE TABLE agent_rules (id INTEGER PRIMARY KEY, agent TEXT, rule TEXT, approval TEXT, at REAL, active INTEGER)")
    f.execute("INSERT INTO approvals VALUES ('abc123', 'assign_task', 's', ?, 'h', ?, 0, 0, '{}', 't1')",
              (json.dumps({"agent": agent}), status))
    f.execute("INSERT INTO agent_rules VALUES (1, 'vera', 'Skip jobs needing 5+ years', 'abc123', 0, 1)")
    f.commit()
    f.close()


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("HQ_HERMES_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cli")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t1")
    monkeypatch.setattr(config, "OWNER_ID", 100000000000000014)


def call(name, args=None):
    fn = next(f for f, s in tools.TOOLS if s["name"] == name)
    return json.loads(tools.wrap(fn)(args or {}))


def test_approved_task_is_the_owner_with_kanban_limits(tmp_path, worker):
    _world(tmp_path)
    a = identity.current()
    assert a.platform == "kanban" and a.uid == config.OWNER_ID


@pytest.mark.parametrize("tool", ["vera_start_applications", "vera_send_emails", "vera_reply_email", "vera_settings",
                                  "vera_save_rule", "vera_learned", "vera_plan_applications", "vera_update_application"])
def test_approved_task_cannot_apply_email_or_change_settings(tmp_path, worker, tool):
    _world(tmp_path)
    out = call(tool, {})
    assert "error" in out and "Frei can only read and draft" in out["error"]


@pytest.mark.parametrize("change", [{"created_by": "vincent"}, {"assignee": "libero"}, {"status": "cancelled"},
                                    {"agent": "libero"}])
def test_unapproved_tasks_are_refused(tmp_path, worker, change):
    _world(tmp_path, **change)
    with pytest.raises(identity.NotAllowed):
        identity.current()


def test_rules_from_frei(tmp_path, worker):
    _world(tmp_path)
    assert frei_link.rules() == ["Skip jobs needing 5+ years"]


def test_every_kanban_tool_exists():
    names = {fn.__name__ for fn, _ in tools.TOOLS}
    assert tools.KANBAN_OK <= names
