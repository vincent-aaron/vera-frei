"""SQLite storage (free, a single local file at data/vera.db).

Multi-user: every Discord user has their own preferences, profile, job states,
scores and applications. Job listings themselves are shared.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from vera import config, preferences

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,                 -- source:external_id
  source TEXT NOT NULL, title TEXT NOT NULL, company TEXT, location TEXT,
  remote INTEGER, employment_type TEXT, level TEXT,
  salary_min REAL, salary_max REAL, salary_currency TEXT, salary_period TEXT,
  description TEXT, url TEXT NOT NULL, tags TEXT, posted_at TEXT,
  fingerprint TEXT,                    -- company+title, for cross-source de-duplication
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, available INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_jobs_fp ON jobs(fingerprint);

CREATE TABLE IF NOT EXISTS user_evaluations (
  job_id TEXT, user_id INTEGER, score INTEGER, details TEXT, evaluated_at TEXT,
  PRIMARY KEY (job_id, user_id)
);

-- What a user did with a job: posted / saved / skipped / applying / applied
CREATE TABLE IF NOT EXISTS user_job_state (
  job_id TEXT, user_id INTEGER, state TEXT NOT NULL, updated_at TEXT NOT NULL, message_id INTEGER,
  PRIMARY KEY (job_id, user_id)
);

CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT, company TEXT, position TEXT, source TEXT, url TEXT,
  status TEXT NOT NULL,          -- preparing, ready, submitted, applied, interview, offer, rejected, withdrawn, failed
  resume TEXT, answers TEXT, notes TEXT,
  applied_at TEXT, interview_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  reminded INTEGER DEFAULT 0, user_id INTEGER
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, application_id INTEGER, kind TEXT, detail TEXT, at TEXT
);

CREATE TABLE IF NOT EXISTS source_cache (source TEXT, query TEXT, fetched_at TEXT, PRIMARY KEY (source, query));
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path=None):
        config.ensure_dirs()
        self.path = str(path or config.DB_PATH)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Single-user data from Vera v1 -> the owner's account."""
        owner = config.OWNER_ID or 0
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(applications)")]
        if "user_id" not in cols:
            self.conn.execute("ALTER TABLE applications ADD COLUMN user_id INTEGER")
        self.conn.execute("UPDATE applications SET user_id=? WHERE user_id IS NULL", (owner,))
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "job_state" in tables:
            self.conn.execute("INSERT OR IGNORE INTO user_job_state SELECT job_id, ?, state, updated_at, message_id FROM job_state", (owner,))
            self.conn.execute("DROP TABLE job_state")
        if "evaluations" in tables:
            self.conn.execute("INSERT OR IGNORE INTO user_evaluations SELECT job_id, ?, score, details, evaluated_at FROM evaluations", (owner,))
            self.conn.execute("DROP TABLE evaluations")
        for key in ("preferences", "profile"):
            r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if r:
                self.conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES(?,?)", (f"{key}:{owner}", r[0]))
                self.conn.execute("DELETE FROM kv WHERE key=?", (key,))
        r = self.conn.execute("SELECT value FROM kv WHERE key='last_digest'").fetchone()
        if r:
            self.conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES(?,?)", (f"last_digest:{owner}", r[0]))
            self.conn.execute("DELETE FROM kv WHERE key='last_digest'")

    # ------------------------------------------------------------ helpers
    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def rows(self, sql: str, params: Iterable = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def row(self, sql: str, params: Iterable = ()) -> Optional[Dict[str, Any]]:
        r = self.rows(sql, params)
        return r[0] if r else None

    # ------------------------------------------------------------ key/value
    def get_json(self, key: str, default=None):
        r = self.row("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_json(self, key: str, value) -> None:
        self.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))

    # ------------------------------------------------------------ per-user settings
    def get_prefs(self, uid: int) -> Dict[str, Any]:
        # Merged over defaults, so new settings appear automatically after updates.
        return preferences.merge(preferences.defaults(), self.get_json(f"preferences:{uid}", {}))

    def set_prefs(self, uid: int, prefs: Dict[str, Any]) -> None:
        self.set_json(f"preferences:{uid}", prefs)
        self._register(uid)

    def get_profile(self, uid: int) -> Dict[str, Any]:
        return preferences.merge(preferences.DEFAULT_PROFILE, self.get_json(f"profile:{uid}", {}))

    def set_profile(self, uid: int, profile: Dict[str, Any]) -> None:
        self.set_json(f"profile:{uid}", profile)
        self._register(uid)

    def _register(self, uid: int) -> None:
        users = set(self.get_json("users", []))
        if uid not in users:
            self.set_json("users", sorted(users | {uid}))

    def users(self) -> List[int]:
        """Everyone who has saved preferences or a profile (derived, so it can't drift)."""
        ids = {int(r["key"].split(":", 1)[1]) for r in self.rows("SELECT key FROM kv WHERE key LIKE 'preferences:%' OR key LIKE 'profile:%'")
               if r["key"].split(":", 1)[1].isdigit()}
        return sorted(ids | set(self.get_json("users", [])))

    # ------------------------------------------------------------ jobs (shared)
    def upsert_job(self, job: Dict[str, Any]) -> bool:
        """Insert or refresh a job. Returns True if it is new."""
        exists = self.row("SELECT id FROM jobs WHERE id=?", (job["id"],))
        ts = now()
        if exists:
            self.execute("UPDATE jobs SET last_seen=?, available=1 WHERE id=?", (ts, job["id"]))
            return False
        cols = ["id", "source", "title", "company", "location", "remote", "employment_type", "level",
                "salary_min", "salary_max", "salary_currency", "salary_period", "description", "url",
                "tags", "posted_at", "fingerprint"]
        vals = [json.dumps(job.get(c)) if c == "tags" else job.get(c) for c in cols]
        self.execute(f"INSERT INTO jobs({','.join(cols)},first_seen,last_seen) VALUES({','.join('?' * len(cols))},?,?)",
                     vals + [ts, ts])
        return True

    def job(self, job_id: str) -> Optional[Dict[str, Any]]:
        j = self.row("SELECT * FROM jobs WHERE id=?", (job_id,))
        if j:
            j["tags"] = json.loads(j["tags"] or "[]")
        return j

    def find_by_fingerprint(self, fp: str) -> Optional[Dict[str, Any]]:
        return self.row("SELECT * FROM jobs WHERE fingerprint=? LIMIT 1", (fp,))

    def jobs_for_sources(self, sources: List[str], days: int = 30) -> List[Dict[str, Any]]:
        q = ",".join("?" * len(sources)) or "''"
        rows = self.rows(f"SELECT * FROM jobs WHERE source IN ({q}) AND available=1 "
                         f"AND last_seen >= datetime('now', ?) ORDER BY last_seen DESC", list(sources) + [f"-{days} days"])
        for r in rows:
            r["tags"] = json.loads(r["tags"] or "[]")
        return rows

    # ------------------------------------------------------------ per-user job data
    def save_evaluation(self, job_id: str, uid: int, score: int, details: Dict[str, Any]) -> None:
        self.execute("INSERT INTO user_evaluations VALUES(?,?,?,?,?) ON CONFLICT(job_id,user_id) DO UPDATE SET "
                     "score=excluded.score, details=excluded.details, evaluated_at=excluded.evaluated_at",
                     (job_id, uid, score, json.dumps(details, ensure_ascii=False), now()))

    def set_state(self, job_id: str, uid: int, state: str, message_id: int = None) -> None:
        self.execute("INSERT INTO user_job_state(job_id,user_id,state,updated_at,message_id) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(job_id,user_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at, "
                     "message_id=COALESCE(excluded.message_id, user_job_state.message_id)",
                     (job_id, uid, state, now(), message_id))

    def state(self, job_id: str, uid: int) -> Optional[str]:
        r = self.row("SELECT state FROM user_job_state WHERE job_id=? AND user_id=?", (job_id, uid))
        return r["state"] if r else None

    def saved_jobs(self, uid: int) -> List[Dict[str, Any]]:
        return self.rows("SELECT j.*, e.score FROM user_job_state s JOIN jobs j ON j.id=s.job_id "
                         "LEFT JOIN user_evaluations e ON e.job_id=j.id AND e.user_id=s.user_id "
                         "WHERE s.state='saved' AND s.user_id=? ORDER BY s.updated_at DESC", (uid,))

    def recent_jobs(self, uid: int, limit: int = 8) -> List[Dict[str, Any]]:
        return self.rows("SELECT j.rowid AS key, j.title, j.company, j.location, s.state, e.score FROM user_job_state s "
                         "JOIN jobs j ON j.id=s.job_id LEFT JOIN user_evaluations e ON e.job_id=j.id AND e.user_id=s.user_id "
                         "WHERE s.user_id=? ORDER BY s.updated_at DESC LIMIT ?", (uid, limit))

    # ------------------------------------------------------------ applications
    def create_application(self, job: Dict[str, Any], uid: int, status: str = "preparing") -> int:
        ts = now()
        cur = self.execute("INSERT INTO applications(job_id,company,position,source,url,status,created_at,updated_at,user_id) "
                           "VALUES(?,?,?,?,?,?,?,?,?)",
                           (job.get("id"), job.get("company"), job.get("title"), job.get("source"), job.get("url"),
                            status, ts, ts, uid))
        self.log_event(cur.lastrowid, "created", status)
        return cur.lastrowid

    def application(self, app_id: int) -> Optional[Dict[str, Any]]:
        a = self.row("SELECT * FROM applications WHERE id=?", (app_id,))
        if a and a.get("answers"):
            a["answers"] = json.loads(a["answers"])
        return a

    def application_for_job(self, job_id: str, uid: int) -> Optional[Dict[str, Any]]:
        a = self.row("SELECT id FROM applications WHERE job_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (job_id, uid))
        return self.application(a["id"]) if a else None

    def update_application(self, app_id: int, **fields) -> None:
        if "answers" in fields and not isinstance(fields["answers"], str):
            fields["answers"] = json.dumps(fields["answers"], ensure_ascii=False)
        fields["updated_at"] = now()
        if fields.get("status") in ("submitted", "applied") and not fields.get("applied_at"):
            cur = self.application(app_id)
            if cur and not cur.get("applied_at"):
                fields["applied_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE applications SET {sets} WHERE id=?", list(fields.values()) + [app_id])
        if "status" in fields:
            self.log_event(app_id, "status", fields["status"])

    def applications(self, uid: int, status: str = None, limit: int = 25) -> List[Dict[str, Any]]:
        if status:
            return self.rows("SELECT * FROM applications WHERE user_id=? AND status=? ORDER BY updated_at DESC LIMIT ?",
                             (uid, status, limit))
        return self.rows("SELECT * FROM applications WHERE user_id=? ORDER BY updated_at DESC LIMIT ?", (uid, limit))

    def log_event(self, app_id: int, kind: str, detail: str = "") -> None:
        self.execute("INSERT INTO events(application_id,kind,detail,at) VALUES(?,?,?,?)", (app_id, kind, detail, now()))

    # ------------------------------------------------------------ source cache (shared)
    def cache_age_hours(self, source: str, query: str) -> Optional[float]:
        r = self.row("SELECT fetched_at FROM source_cache WHERE source=? AND query=?", (source, query))
        if not r:
            return None
        then = datetime.fromisoformat(r["fetched_at"])
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600

    def mark_fetched(self, source: str, query: str) -> None:
        self.execute("INSERT INTO source_cache VALUES(?,?,?) ON CONFLICT(source,query) DO UPDATE SET fetched_at=excluded.fetched_at",
                     (source, query, now()))
