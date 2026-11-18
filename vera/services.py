"""Service layer ("backend API"): every feature the Discord bot, the CLI or a
future web/Hermes front-end can call. Keeps Discord code free of business logic.

Vera is multi-user: `Vera(db).user(discord_user_id)` returns a session whose
methods read and change only that person's profile, preferences and applications.
"""

import io
import re
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from vera import ai, config, preferences
from vera.agents import applier, evaluator, scout, writer
from vera.db import DB, now
from vera.sources import LABELS
from vera.sources.base import Source

APP_STATUSES = ["preparing", "ready", "submitted", "applied", "interview", "offer", "rejected", "withdrawn", "failed"]


class Vera:
    def __init__(self, db: Optional[DB] = None):
        self.db = db or DB()

    def user(self, uid: int) -> "UserSession":
        return UserSession(self.db, int(uid))

    # ------------------------------------------------------------ shared
    def job_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        """Jobs are referenced in Discord by their short numeric key (SQLite rowid) or URL."""
        r = self.db.row("SELECT id FROM jobs WHERE rowid=?", (int(key),)) if str(key).isdigit() else \
            self.db.row("SELECT id FROM jobs WHERE id=? OR url=?", (key, key))
        return self.db.job(r["id"]) if r else None

    def job_key(self, job_id: str) -> int:
        return self.db.row("SELECT rowid FROM jobs WHERE id=?", (job_id,))["rowid"]

    def add_manual_job(self, url: str, title: str, company: str = "", location: str = "",
                       description: str = "", salary: str = "", employment_type: str = "", added_by: Optional[int] = None) -> Dict[str, Any]:
        """For LinkedIn / JobStreet / Indeed / Kalibrr / Bossjob links (no free API).
        A hand-added job belongs to whoever added it (added_by); someone else adding the same link never rewrites it."""
        site = re.search(r"https?://(?:www\.|ph\.)?([^/]+)", url or "")
        desc = (f"Salary: {salary}\n\n" if salary else "") + (description or "")
        job = Source.job("manual", url, title=title, company=company, location=location,
                         remote=int("remote" in f"{location} {title}".lower()), employment_type=employment_type,
                         level="", description=desc, url=url, tags=[site.group(1)] if site else [], posted_at=now())
        existing = self.db.job(job["id"])
        owners = self.manual_owners(job["id"])
        if existing is not None and not owners:      # added before owners were kept: whoever has it on their list
            owners = [int(r["user_id"]) for r in self.db.rows("SELECT user_id FROM user_job_state WHERE job_id=?", (job["id"],))]
            if owners:
                self.db.set_json(f"manual_by:{job['id']}", owners)
        if existing is None or (added_by is not None and owners in ([], [int(added_by)])):
            self.db.upsert_job(job)                  # only its own owner may rewrite it
        if added_by is not None and int(added_by) not in owners:
            self.db.set_json(f"manual_by:{job['id']}", owners + [int(added_by)])
        return self.db.job(job["id"])

    def manual_owners(self, job_id: str) -> List[int]:
        return [int(x) for x in self.db.get_json(f"manual_by:{job_id}") or []]

    def may_use_job(self, job: Optional[Dict[str, Any]], uid: int) -> bool:
        """Anyone may use a job from the feeds. A hand-added one only by the people who added it
        (jobs added before 7 Nov 2026 have no record: then whoever has it on their list)."""
        if not job or job.get("source") != "manual":
            return bool(job)
        owners = self.manual_owners(job["id"])
        return int(uid) in owners if owners else self.db.state(job["id"], uid) is not None

    def due_reminders(self) -> List[Dict[str, Any]]:
        """Interviews within 24h (not yet reminded) and follow-ups due, for every user."""
        out = []
        nowu = datetime.now(timezone.utc)
        for a in self.db.rows("SELECT * FROM applications WHERE status='interview' AND interview_at IS NOT NULL AND reminded=0"):
            when = datetime.fromisoformat(a["interview_at"])
            if nowu <= when <= nowu + timedelta(hours=24):
                out.append({"kind": "interview", "app": a, "when": when})
        for a in self.db.rows("SELECT * FROM applications WHERE status IN ('submitted','applied') AND reminded=0 AND applied_at IS NOT NULL"):
            days = int(self.db.get_prefs(a["user_id"]).get("followup_after_days") or 0)
            if days and datetime.fromisoformat(a["applied_at"]) <= nowu - timedelta(days=days):
                out.append({"kind": "followup", "app": a})
        return out

    def mark_reminded(self, app_id: int) -> None:
        self.db.execute("UPDATE applications SET reminded=1 WHERE id=?", (app_id,))


class UserSession:
    def __init__(self, db: DB, uid: int):
        self.db, self.uid = db, uid

    # ---------------------------------------------------------------- settings
    @property
    def prefs(self) -> Dict[str, Any]:
        return self.db.get_prefs(self.uid)

    @property
    def profile(self) -> Dict[str, Any]:
        return self.db.get_profile(self.uid)

    def update_prefs(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        prefs = preferences.merge(self.prefs, patch)
        self.db.set_prefs(self.uid, prefs)
        return prefs

    def reset_prefs(self) -> Dict[str, Any]:
        self.db.set_prefs(self.uid, preferences.defaults())
        return self.prefs

    def update_profile(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        profile = preferences.merge(self.profile, patch)
        self.db.set_profile(self.uid, profile)
        return profile

    # ---------------------------------------------------------------- jobs
    async def find_jobs(self, query: str = None, limit: int = None, force: bool = False) -> Dict[str, Any]:
        return await scout.find_jobs(self.db, self.uid, query, limit, force)

    def evaluate(self, job: Dict[str, Any]) -> Dict[str, Any]:
        ev = evaluator.evaluate(job, self.prefs, self.profile)
        self.db.save_evaluation(job["id"], self.uid, ev["score"], ev)
        return ev

    def set_job_state(self, job_id: str, state: str, message_id: int = None) -> None:
        self.db.set_state(job_id, self.uid, state, message_id)

    # ---------------------------------------------------------------- applications
    async def start_application(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Spec 7 steps 1-6: retrieve, verify, analyse, pick résumé, prepare answers."""
        existing = self.db.application_for_job(job["id"], self.uid)
        if existing and existing["status"] in ("submitted", "applied", "interview", "offer"):
            return {"error": f"You already applied to this job (status: {existing['status']})."}
        available = await scout.check_available(self.db, job)
        if available is False:
            return {"error": "This job looks closed (the page says it's no longer available)."}
        prefs, profile = self.prefs, self.profile
        ev = self.evaluate(job)
        resume = writer.choose_resume(profile, job)
        prepared = await writer.prepare_answers(job, profile, prefs, ev)
        app_id = existing["id"] if existing and existing["status"] in ("preparing", "ready", "failed", "withdrawn") else \
            self.db.create_application(job, self.uid)
        self.db.update_application(app_id, status="ready", resume=(resume or {}).get("name"),
                                   answers={"answers": prepared["answers"], "cover_letter": prepared["cover_letter"]})
        self.set_job_state(job["id"], "applying")
        return {"application_id": app_id, "job": job, "evaluation": ev, "available": available, "resume": resume,
                "user_id": self.uid, **prepared, "auto_submit": applier.can_auto_submit(job["url"], self.uid)}

    def owns(self, app: Optional[Dict[str, Any]]) -> bool:
        return bool(app) and int(app.get("user_id") or 0) == self.uid

    def update_answer(self, app_id: int, question: str, answer: str) -> None:
        app = self.db.application(app_id)
        data = app.get("answers") or {"answers": {}}
        data["answers"][question] = answer
        self.db.update_application(app_id, answers=data)

    async def approve(self, app_id: int) -> Dict[str, Any]:
        """Spec 7 step 8 happened (you approved): submit if possible, then record the status."""
        app = self.db.application(app_id)
        if not self.owns(app):
            return {"status": "failed", "detail": "not your application"}
        answers = (app.get("answers") or {}).get("answers", {})
        if applier.can_auto_submit(app["url"], self.uid):
            self.db.update_application(app_id, status="preparing", notes="submitting via browser agent")
            status, detail = await applier.submit_via_browser(self, app["url"], answers)
            if status == "submitted":
                self.db.update_application(app_id, status="submitted", notes=detail)
                self.set_job_state(app["job_id"], "applied")
            else:
                self.db.update_application(app_id, status="ready", notes=f"auto-submit {status}: {detail}")
            return {"status": status, "detail": detail, "manual_url": None if status == "submitted" else app["url"]}
        self.db.update_application(app_id, status="ready", notes="approved; apply on the site, then mark as applied")
        return {"status": "manual", "detail": "This site can't be submitted automatically.", "manual_url": app["url"]}

    def mark(self, app_id: int, status: str, note: str = None) -> None:
        assert status in APP_STATUSES, status
        fields = {"status": status}
        if note:
            app = self.db.application(app_id)
            fields["notes"] = ((app.get("notes") or "") + f"\n[{datetime.now():%Y-%m-%d}] {note}").strip()
        self.db.update_application(app_id, **fields)
        app = self.db.application(app_id)
        if app and app.get("job_id") and status in ("submitted", "applied", "interview", "offer"):
            self.set_job_state(app["job_id"], "applied")

    def set_interview(self, app_id: int, when: datetime, note: str = "") -> None:
        self.db.update_application(app_id, status="interview", interview_at=when.astimezone(timezone.utc).isoformat(timespec="minutes"),
                                   reminded=0)
        if note:
            self.mark(app_id, "interview", note)

    def add_note(self, app_id: int, note: str) -> None:
        self.mark(app_id, self.db.application(app_id)["status"], note)

    # ---------------------------------------------------------------- résumés & profile import
    def add_resume(self, src_path: Path, name: str, tags: List[str]) -> Dict[str, Any]:
        folder = config.RESUME_DIR / str(self.uid)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / src_path.name
        if src_path.resolve() != dest.resolve():
            shutil.copy(src_path, dest)
        profile = self.profile
        profile["resumes"] = [r for r in profile["resumes"] if r.get("name") != name] + \
            [{"name": name, "path": str(dest), "tags": tags}]
        self.db.set_profile(self.uid, profile)
        return profile["resumes"][-1]

    async def import_resume(self, path: Path) -> Dict[str, Any]:
        """Read a PDF/DOCX résumé and fill the profile with ONLY what it says."""
        import asyncio
        try:
            text = await asyncio.to_thread(extract_text, path)       # off the main loop, so a slow file can't freeze chat
        except (ValueError, OSError, zipfile.BadZipFile, KeyError) as e:
            return {"ok": False, "reason": f"couldn't read that file ({str(e)[:80] or type(e).__name__})"}
        except Exception as e:               # a broken PDF can raise anything; never let it take Vera down
            return {"ok": False, "reason": f"couldn't read that file ({type(e).__name__})"}
        if not text.strip():
            return {"ok": False, "reason": "couldn't read any text from that file (scanned image PDFs aren't supported)"}
        patch: Dict[str, Any] = {"resume_text": text[:20000]}
        data = ai.extract_json(await ai.complete(
            "Extract the candidate's details from this résumé. Use ONLY what is written; leave fields empty if missing.\n"
            'Return JSON: {"name":"","email":"","phone":"","location":"","headline":"","summary":"",'
            '"years_experience":0,"education":[{"degree":"","school":"","year":"","honors":""}],'
            '"experience":[{"title":"","company":"","dates":"","details":[]}],"skills":[],"languages":[],'
            '"certifications":[],"achievements":[],"links":{"github":"","linkedin":"","portfolio":""}}\n\n'
            f"RÉSUMÉ:\n{text[:9000]}", temperature=0, max_tokens=1800))
        allowed = set(preferences.DEFAULT_PROFILE) - {"resumes"}
        for k, v in data.items():
            if k in allowed and v not in (None, "", [], {}):
                patch[k] = v
        if "skills" not in patch:   # no AI: still pick up known skills from the text
            patch["skills"] = [s for s in evaluator.skills_in_text(text)]
        self.update_profile(patch)
        return {"ok": True, "ai_used": len(data) > 0, "fields": sorted(k for k in patch if k != "resume_text")}

    # ---------------------------------------------------------------- statistics
    def statistics(self) -> Dict[str, Any]:
        uid = self.uid
        apps = self.db.rows("SELECT status, source, applied_at FROM applications WHERE user_id=?", (uid,))
        by_status: Dict[str, int] = {}
        by_source: Dict[str, int] = {}
        for a in apps:
            by_status[a["status"]] = by_status.get(a["status"], 0) + 1
            src = LABELS.get(a["source"], a["source"] or "?")
            by_source[src] = by_source.get(src, 0) + 1
        sent = [a for a in apps if a["applied_at"]]
        nowu = datetime.now(timezone.utc)
        recent = lambda days: sum(1 for a in sent if datetime.fromisoformat(a["applied_at"]) >= nowu - timedelta(days=days))
        responded = sum(by_status.get(s, 0) for s in ("interview", "offer", "rejected"))
        avg = self.db.row("SELECT AVG(e.score) s FROM applications a JOIN user_evaluations e ON e.job_id=a.job_id AND e.user_id=a.user_id "
                          "WHERE a.applied_at IS NOT NULL AND a.user_id=?", (uid,))
        count = lambda states: self.db.row(f"SELECT COUNT(*) c FROM user_job_state WHERE user_id=? AND state IN ({states})", (uid,))["c"]
        return {
            "jobs_tracked": self.db.row("SELECT COUNT(*) c FROM jobs")["c"],
            "recommended": count("'posted','saved','skipped','applying','applied'"),
            "saved": count("'saved'"), "skipped": count("'skipped'"),
            "applications": len(apps), "sent": len(sent), "this_week": recent(7), "this_month": recent(30),
            "by_status": by_status, "by_source": by_source,
            "response_rate": round(100 * responded / len(sent)) if sent else 0,
            "interview_rate": round(100 * (by_status.get("interview", 0) + by_status.get("offer", 0)) / len(sent)) if sent else 0,
            "avg_match_applied": round(avg["s"]) if avg and avg["s"] else None,
        }


MAX_RESUME_PAGES, MAX_RESUME_TEXT, MAX_DOCX_XML = 30, 200_000, 20 * 1024 * 1024


def extract_text(path: Path) -> str:
    """Plain text from a PDF (pypdf) or DOCX (built-in zip/XML), free and offline."""
    # Limits, so a small upload built to unpack into gigabytes can't freeze the laptop. A real résumé is far smaller.
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        import itertools
        pages = PdfReader(str(path)).pages
        text = ""
        for page in itertools.islice(pages, MAX_RESUME_PAGES):        # never builds the whole page list
            text += (page.extract_text() or "") + "\n"
            if len(text) > MAX_RESUME_TEXT:
                break
        return text[:MAX_RESUME_TEXT]
    if suffix == ".docx":
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML:
                raise ValueError("this Word file is too large to read as a résumé")
            xml = z.read(info).decode("utf-8", errors="ignore")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    return ""


__all__ = ["Vera", "UserSession", "APP_STATUSES", "extract_text", "io"]
