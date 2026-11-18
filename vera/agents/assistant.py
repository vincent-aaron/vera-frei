"""Assistant agent: understands plain-language messages to Vera, e.g.

    "@Vera suggest only jobs that require no experience and are either remote
     or in Bulacan, Pampanga, Bataan, Manila"

It turns them into preference changes (validated, never free-form code), and
an action such as running a search. Uses free AI when available, with a
rule-based parser as fallback so common requests always work.
"""

import json
import re
from typing import Any, Dict, List, Optional

from contextvars import ContextVar

from vera import ai, preferences as P
from vera.agents import export_parse

ACTIONS = ("search", "chat", "none", "show_preferences", "statistics", "help", "send_resume")

ROLE_NOUN = re.compile(r"\b(developer|dev|engineer|programmer|tester|qa|analyst|designer|specialist|administrator|admin|"
                       r"support|technician|assistant|intern|scientist|architect|consultant|officer|devops|coordinator|"
                       r"manager|writer|editor|accountant|agent|representative|teacher|tutor|clerk)s?\b", re.I)
# "send me my resume", "can I see my CV", "show me a copy of my résumé", "my resume please"
RESUME_ASK = re.compile(r"\b(see|send|show|give|get|view|download|share|attach)\s+(me\s+)?(a\s+copy\s+of\s+)?(my|the)\s+"
                        r"(resume|résumé|cv)\b|\bmy\s+(resume|résumé|cv)\s+(please|pls|file)\b", re.I)


# A preference may only change if the message mentions it (stops the AI from
# silently "tidying up" settings you didn't ask about, e.g. removing Remote).
MENTIONS = {
    "work_setup": r"remote|hybrid|on-?site|onsite|office|wfh|work from home|home based",
    "willing_to_relocate": r"relocat|move to",
    "job_levels": r"intern|fresh|entry|junior|\bjr\b|senior|\bsr\b|mid|lead|manager|associate|level|graduate",
    "custom_levels": r"level",
    "experience": r"experience|\byears?\b|\byrs?\b|fresh|graduate",
    "reject_mandatory_experience_over": r"experience|\byears?\b|\byrs?\b|fresh|graduate",
    "custom_experience_max_years": r"experience|\byears?\b|\byrs?\b",
    "locations": r"location|place|city|province|region|country|near|around|\bin\b|from|" + "|".join(P.PH_PLACES),
    "salary": r"salary|pay|php|₱|peso|\d+\s*k\b|\d{4,}|compensation|rate",
    "employment_types": r"full|part|contract|freelance|intern|temporary|permanent|project",
    "positions": r"developer|engineer|programmer|tester|\bqa\b|analyst|designer|support|position|role|job title|jobs? (like|i want)|specialist|admin",
    "only_listed_positions": r"only|just|exclusively|similar|related",
    "exclude": r"exclude|except|avoid|not |no |without|don'?t|skip",
    "requirements": r"degree|language|skill|schedule|shift|require",
    "sources": r"source|remotive|arbeitnow|jobicy|himalayas|muse|remote ?ok|we work remotely|nomads",
    "min_match": r"match|percent|%",
    "results_per_search": r"\d+\s*(jobs|results)|results|how many",
    "digest_count": r"\d+\s*(jobs|results)|how many|digest|morning|daily",
    "daily_digest": r"morning|daily|every day|digest|\bam\b|\bpm\b|o'?clock",
    "max_age_days": r"\bdays?\b|\bweeks?\b|old|recent|new|latest|posted",
    "followup_after_days": r"follow",
    "include_cover_letter": r"cover",
}


def guard(patch: Dict[str, Any], text: str) -> Dict[str, Any]:
    t = text.lower()
    return {k: v for k, v in patch.items() if re.search(MENTIONS.get(k, r"$^"), t)}


def extract_positions(text: str) -> List[str]:
    """'only web developer, QA tester and IT support jobs' -> ['Web Developer', 'QA Tester', 'IT Support']."""
    t = re.sub(r"\s+", " ", text)
    chunks = []
    for pat in (r"\b(?:jobs?|positions?|roles?)\s+(?:that\s+)?i\s+(?:want|like|prefer)\s*(?:are|is)?\s*:?\s*(.+?)(?:[.;!?]|$)",
                r"\bi\s+(?:want|like|prefer)\s+(?:jobs?|positions?|roles?)\s+(?:like|as|such as)?\s*:?\s*(.+?)(?:[.;!?]|$)",
                r"\bonly\s+(.+?)\s+(?:jobs?|positions?|roles?|work)\b",
                r"\b(?:jobs?|positions?|roles?)\s+(?:like|such as|as)\s*:?\s*(.+?)(?:[.;!?]|\bthat\b|\bwhich\b|$)",
                r"\b(?:i want|i'm looking for|im looking for|looking for|suggest)\s+(?:only\s+)?(.+?)\s+(?:jobs?|positions?|roles?)\b",
                r"\bpositions?\s*:\s*(.+?)(?:[.;!?]|$)"):
        chunks += re.findall(pat, t, re.I)
    out = []
    for chunk in chunks:
        for part in re.split(r",|/|\band\b|\bor\b|&", chunk, flags=re.I):
            part = re.sub(r"\b(only|remote|hybrid|on-?site|no experience|entry[- ]level|fresh grad\w*|full[- ]?time|part[- ]?time|"
                          r"the|some|any|these|those|this|jobs?|positions?|roles?)\b", " ", part, flags=re.I).strip(" -:")
            part = re.sub(r"\s+", " ", part)
            if part and ROLE_NOUN.search(part) and len(part) <= 40:
                acronyms = {"qa", "it", "ui", "ux", "ui/ux", "hr", "seo", "ai", "ml", "erp", "sap", "crm", "php", "net", ".net"}
                name = " ".join(w.upper() if w.lower() in acronyms else w if w.isupper() and len(w) <= 4 else w.capitalize()
                                for w in part.split())
                if name.lower() not in [o.lower() for o in out]:
                    out.append(name)
    return out
ENUMS = {"job_levels": P.JOB_LEVELS, "experience": P.EXPERIENCE, "work_setup": P.WORK_SETUPS,
         "employment_types": P.EMPLOYMENT_TYPES}


# --------------------------------------------------------------------------- validation
def _enum_list(values, allowed: List[str]) -> List[str]:
    out = []
    for v in values or []:
        hit = next((a for a in allowed if a.lower() == str(v).strip().lower()), None)
        if hit and hit not in out:
            out.append(hit)
    return out


def _num(v, lo, hi, cast=int):
    try:
        return max(lo, min(hi, cast(v)))
    except (TypeError, ValueError):
        return None


def sanitize(patch: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only valid preference keys and values."""
    out: Dict[str, Any] = {}
    for k, v in (patch or {}).items():
        if k in ENUMS:
            vals = _enum_list(v if isinstance(v, list) else [v], ENUMS[k])
            if vals:
                out[k] = vals
        elif k in ("positions", "custom_levels"):
            out[k] = [str(x).strip() for x in (v if isinstance(v, list) else P.split_list(str(v))) if str(x).strip()][:20]
        elif k == "locations":
            locs = []
            for l in v if isinstance(v, list) else []:
                if isinstance(l, str):
                    locs.append(P.place(l))
                elif isinstance(l, dict):
                    locs.append({kk: str(l.get(kk) or "") for kk in ("city", "province", "region", "country")})
            default = next((l.get("country") for l in current.get("locations", []) if l.get("country")), "Philippines")
            if locs:
                out[k] = P.fill_country(locs, default)
        elif k in ("min_match",):
            n = _num(v, 0, 100)
            if n is not None:
                out[k] = n
        elif k in ("results_per_search", "digest_count"):
            n = _num(v, 1, 20)
            if n is not None:
                out[k] = n
        elif k in ("max_age_days", "followup_after_days"):
            n = _num(v, 0, 365)
            if n is not None:
                out[k] = n
        elif k in ("reject_mandatory_experience_over", "custom_experience_max_years"):
            out[k] = None if v is None else _num(v, 0, 50, float)
        elif k in ("willing_to_relocate", "include_cover_letter", "only_listed_positions"):
            out[k] = bool(v)
        elif k == "daily_digest" and isinstance(v, dict):
            d = dict(current["daily_digest"])
            if "enabled" in v:
                d["enabled"] = bool(v["enabled"])
            if v.get("hour") is not None and _num(v["hour"], 0, 23) is not None:
                d["hour"] = _num(v["hour"], 0, 23)
            out[k] = d
        elif k == "salary" and isinstance(v, dict):
            s = dict(current["salary"])
            for kk in ("min", "max"):
                if kk in v:
                    s[kk] = None if v[kk] in (None, "") else _num(v[kk], 0, 10**8)
            if v.get("currency"):
                s["currency"] = str(v["currency"]).upper()[:5]
            if v.get("period") in P.SALARY_PERIODS:
                s["period"] = v["period"]
            for kk in ("negotiable", "accept_undisclosed"):
                if kk in v:
                    s[kk] = bool(v[kk])
            out[k] = s
        elif k in ("exclude", "requirements") and isinstance(v, dict):
            base = dict(current[k])
            for kk, vv in v.items():
                if kk in base:
                    base[kk] = vv if isinstance(vv, str) and kk in ("degree", "schedule") else \
                        [str(x).strip() for x in (vv if isinstance(vv, list) else P.split_list(str(vv))) if str(x).strip()]
            out[k] = base
        elif k == "sources":
            from vera.sources import SOURCES
            vals = [x for x in (v if isinstance(v, list) else [v]) if x in SOURCES]
            if vals:
                out[k] = vals
    return out


# --------------------------------------------------------------------------- rule-based fallback
def rule_parse(text: str, current: Dict[str, Any]) -> Dict[str, Any]:
    t = text.lower()
    patch: Dict[str, Any] = {}
    action = "search" if re.search(r"\b(suggest|find|search|look for|show me|recommend|jobs?)\b", t) else "none"
    if RESUME_ASK.search(text):
        return {"patch": {}, "action": "send_resume", "query": None, "reply": None}

    positions = extract_positions(text)
    if positions:
        patch["positions"] = positions
        # Listing the jobs you want = suggest only those, unless you ask for similar ones too.
        patch["only_listed_positions"] = not re.search(r"\b(similar|related|and the like|like these|etc)\b", t)

    if re.search(r"no (work )?experience|without experience|zero experience|0 (years|yrs)|fresh ?grad", t):
        patch["experience"] = ["No Experience Required", "Fresh Graduates Welcome"]
        patch["reject_mandatory_experience_over"] = 0
    m = re.search(r"(?:up to|max(?:imum)?|at most|less than)\s*(\d+)\s*(?:years?|yrs?)", t)
    if m:
        patch["reject_mandatory_experience_over"] = int(m.group(1))

    setups = [s for s, pat in (("Remote", r"\bremote|wfh|work from home"), ("Hybrid", r"\bhybrid"),
                               ("On-site", r"on-?site|onsite|in office|office based")) if re.search(pat, t)]
    places = [name for name in P.PH_PLACES if re.search(r"\b" + re.escape(name) + r"\b", t)]
    # "San Fernando" also contains "fernando"; keep longest matches only
    places = [p for p in places if not any(p != q and p in q for q in places)]
    if places:
        patch["locations"] = [P.place(p) for p in places]
        if "Remote" in setups or not setups:
            setups = sorted(set(setups) | {"On-site", "Hybrid"}, key=P.WORK_SETUPS.index)
    if setups:
        patch["work_setup"] = setups

    m = re.search(r"(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*jobs", t) or re.search(r"(\d{1,2})\s*jobs", t)
    if m:
        patch["digest_count"] = int(m.group(m.lastindex))
    if re.search(r"every (morning|day)|daily", t):
        hour = re.search(r"(\d{1,2})\s*(am|pm)", t)
        h = (int(hour.group(1)) % 12 + (12 if hour.group(2) == "pm" else 0)) if hour else current["daily_digest"]["hour"]
        patch["daily_digest"] = {"enabled": True, "hour": h}
    m = re.search(r"(?:within|last|past|not older than)\s*(\d+)\s*(day|week)", t)
    if m:
        patch["max_age_days"] = int(m.group(1)) * (7 if m.group(2) == "week" else 1)
    m = re.search(r"salary[^0-9]*(\d[\d,]*\s*k?)", t)
    if m:
        try:
            patch["salary"] = {"min": P.parse_money(m.group(1))}
        except ValueError:
            pass
    if not patch and action == "none":
        action = "chat"
    if re.search(r"\b(stats|statistics|progress)\b", t):
        action = "statistics"
    elif re.search(r"\b(preferences|settings|filters)\b", t) and not patch:
        action = "show_preferences"
    return {"patch": patch, "action": action, "query": None, "reply": None}


# --------------------------------------------------------------------------- AI interpretation
SCHEMA_HELP = f"""Preference keys you may change (send the full new value):
- job_levels: list from {P.JOB_LEVELS}
- experience: list from {P.EXPERIENCE}
- reject_mandatory_experience_over: number of years (0 = only jobs that require no experience) or null
- positions: list of job titles (e.g. ["Web Developer", "QA Tester"])
- only_listed_positions: true = suggest ONLY jobs whose title matches one of `positions`
- locations: list of {{"city","province","region","country"}} (Philippine places -> country "Philippines")
- work_setup: list from {P.WORK_SETUPS}
- willing_to_relocate: true/false
- salary: {{"min","max","currency","period"(monthly/yearly/hourly),"negotiable","accept_undisclosed"}}
- employment_types: list from {P.EMPLOYMENT_TYPES}
- exclude: {{"positions","industries","companies","locations","keywords"}} (lists)
- requirements: {{"degree","languages","skills","schedule","other"}}
- min_match (0-100), results_per_search (1-20), digest_count (1-20), max_age_days, followup_after_days
- daily_digest: {{"enabled": true/false, "hour": 0-23}}
- include_cover_letter: true/false"""


async def interpret(text: str, current: Dict[str, Any]) -> Dict[str, Any]:
    fallback = rule_parse(text, current)
    if fallback["action"] == "send_resume":           # no AI needed, always works
        return {**fallback, "source": "rules"}
    prompt = f"""You are Vera, a job-hunting assistant on Discord. Turn the user's message into
preference changes and an action. Change ONLY what the user asked for.

{SCHEMA_HELP}

Rules:
- "no experience" -> experience ["No Experience Required","Fresh Graduates Welcome"] and reject_mandatory_experience_over 0.
- "remote OR in <places>" -> work_setup ["Remote","Hybrid","On-site"] and locations = those places.
- If they ask to find/suggest/show jobs, action = "search" (query = extra keywords only, else null).
- "only <titles> jobs" / "these are the jobs I want: ..." -> positions = those titles and only_listed_positions true.
- If they ask to see/send/get their résumé or CV, action = "send_resume" and patch = {{}}.
- If they ask a question, want advice, or are just talking (and change nothing), action = "chat" and patch = {{}}.
- action is one of {list(ACTIONS)}.

Current preferences:
{json.dumps(current, ensure_ascii=False)[:3500]}

Recent conversation, for understanding short follow-ups. If this message continues that topic and isn't asking to find jobs, action = "chat":
{_history_text.get() or "(nothing yet)"}

User message: {text!r}

Return ONLY JSON: {{"patch": {{...}}, "action": "...", "query": null, "reply": "one or two friendly sentences describing what you changed/will do"}}"""
    data = ai.extract_json(await ai.complete(prompt, temperature=0, max_tokens=900))
    if not data or not isinstance(data.get("patch"), dict):
        return {**fallback, "source": "rules"}
    action = data.get("action") if data.get("action") in ACTIONS else fallback["action"]
    # Merge: rules catch things the model sometimes misses (e.g. digest count);
    # the guard drops AI changes to settings the message never mentioned.
    patch = {**fallback["patch"], **guard(data["patch"], text)}
    return {"patch": patch, "action": action, "query": data.get("query") or None,
            "reply": data.get("reply"), "source": "ai"}


def describe(patch: Dict[str, Any]) -> List[str]:
    lines = []
    for k, v in patch.items():
        if k == "locations":
            v = "; ".join(P.location_label(l) for l in v)
        elif k == "daily_digest":
            v = f"every day at {v['hour']:02d}:00" if v.get("enabled") else "off"
        elif k == "only_listed_positions":
            k, v = "suggest only your listed positions", "yes" if v else "no (similar jobs too)"
        elif k == "reject_mandatory_experience_over":
            k, v = "reject jobs requiring more than", f"{v:g} years" if v is not None else "no limit"
        elif isinstance(v, list):
            v = ", ".join(map(str, v))
        elif isinstance(v, dict):
            v = ", ".join(f"{a}={b}" for a, b in v.items() if b not in (None, "", []))
        lines.append(f"• **{k.replace('_', ' ')}**: {v}")
    return lines


def _context(session) -> str:
    pr, pf = session.profile, session.prefs
    edu = "; ".join(" ".join(v for v in (e.get("degree"), e.get("school"), e.get("honors")) if v) for e in pr.get("education") or [])
    exp = "; ".join(f"{x.get('title')} at {x.get('company')} ({x.get('dates')})" for x in pr.get("experience") or [])
    recent = session.db.recent_jobs(session.uid, 8)
    apps = session.db.applications(session.uid, limit=8)
    from vera.sources import LABELS
    how_vera_works = {
        "about_vera": "Vera is a custom Discord bot written in Python. She is NOT built on Hermes or any other agent framework, and has no Hermes self-improvement. "
                      "She can't rewrite her own code or improve herself. She only learns saved answers to form questions and preferences people give her. "
                      "New abilities come from the owner updating her, and those updates are posted in the owner's updates channel. "
                      "She runs on the owner's laptop, so she's offline when it's off or she isn't running. "
                      "Settings and rules belong to the person, not the channel: they apply in every channel and DM. "
                      "She only remembers the last few messages in this channel. For numbers and run status, use right_now and never guess.",
        "job_sources": "Vera searches free public job APIs: " + ", ".join(LABELS[s] for s in pf["sources"] if s in LABELS)
                       + ". Results are cached for a few hours to respect each provider, and only jobs posted within "
                       f"{pf['max_age_days']} days are recommended.",
        "applying": "Vera can apply for people. They just say so in chat, e.g. 'apply to 5 jobs'; she shows the plan and "
                    "starts only after a plain proceed (or 'test', which sends nothing). Nothing runs on a schedule. She applies "
                    "through company forms (Greenhouse, Lever, Ashby, Workable) found on the free job APIs, never signs in to job "
                    "boards, and never bypasses bot checks. They can also ask her to recommend jobs without applying. Saying 'stop' ends a run.",
        "not_possible": "Job boards that need an account (LinkedIn, JobStreet, Indeed and the like) aren't searched or applied to. "
                        "Users can paste jobs from those sites with /add-job and Vera will score and track them.",
        "user_data": "The user's profile comes from their résumé: filled from /resume add with import_profile, and "
                     "editable with /profile and /apply-details. Preferences come from "
                     "/preferences or from what the user tells Vera in chat. All data stays on the owner's PC in Vera's "
                     "local database; each Discord user has their own separate data.",
        "email": "Vera can send cold email from the owner's Gmail, only when he asks in the outreach channel or job mail channel. She shows drafts first and sends after he says proceed. "
                 "She can't open Facebook links, but a pasted post with an email address works. Other members get a draft to send themselves.",
        "commands": "/find-jobs, /preferences, /profile, /resume add|get|list|remove, /saved, /apply, /applications, "
                    "/statistics, /add-job, /ask, /status, /help",
    }
    return json.dumps({
        "right_now": _live_facts(session),
        "how_vera_works": how_vera_works,
        "candidate": {"name": pr.get("name"), "headline": pr.get("headline"), "years_experience": pr.get("years_experience"),
                      "education": edu, "experience": exp, "skills": pr.get("skills", [])[:30],
                      "certifications": pr.get("certifications"), "achievements": pr.get("achievements")},
        "preferences": {"positions": pf["positions"], "levels": pf["job_levels"], "experience": pf["experience"],
                        "locations": [P.location_label(l) for l in pf["locations"]], "work_setup": pf["work_setup"],
                        "salary": pf["salary"], "max_age_days": pf["max_age_days"],
                        "daily_digest": pf["daily_digest"], "digest_count": pf["digest_count"]},
        "recent_jobs": [{"number": r["key"], "title": r["title"], "company": r["company"], "match": r["score"],
                         "status": r["state"]} for r in recent],
        "applications": [{"id": a["id"], "position": a["position"], "company": a["company"], "status": a["status"]} for a in apps],
    }, ensure_ascii=False)[:6000]


def _live_facts(session) -> Dict[str, Any]:
    """Real numbers, so she never guesses how many applications there are or whether a run is going."""
    from datetime import date, timedelta

    from vera.agents import rounds, exports
    try:
        sent = exports.collect(session)["sent"]
    except Exception:
        sent = []
    today = date.today()
    monday = (today - timedelta(days=today.weekday())).isoformat()
    return {"applications_sent_total": len(sent), "sent_today": sum(r["date"] == today.isoformat() for r in sent), "sent_this_week": sum(r["date"] >= monday for r in sent),
            "run_status": rounds.status_now(session)}


_chat_scope: ContextVar[str] = ContextVar("vera_chat_scope", default="")
_history_text: ContextVar[str] = ContextVar("vera_history_text", default="")


def _history_key(session) -> str:
    return f"chat_history:{session.uid}:{_chat_scope.get()}"


def _recent_chat(session) -> str:
    turns = session.db.get_json(_history_key(session)) or []
    return "\n".join(f"{who}: {said}" for who, said in turns)


def _remember_chat(session, text: str, reply: Optional[str]) -> None:
    turns = (session.db.get_json(_history_key(session)) or []) + [["User", text[:300]]] + ([["You", reply[:300]]] if reply else [])
    session.db.set_json(_history_key(session), turns[-8:])


def _job_from_text(session, text: str):
    """'job 123' / '#123' / a pasted link -> that job, so Vera can talk about it."""
    from vera.services import Vera
    m = re.search(r"(?:job|#)\s*(\d+)\b", text, re.I) or re.search(r"(https?://\S+)", text)
    return Vera(session.db).job_by_key(m.group(1)) if m else None


async def chat(session, text: str, situation: str = "", busy: str = "") -> str:
    job = _job_from_text(session, text)
    job_part = ""
    if job:
        job_part = (f"\nThe user is asking about this job: {job.get('title')} at {job.get('company')} "
                    f"({job.get('location')}).\n{(job.get('description') or '')[:3500]}\n")
    prompt = f"""You are Vera, an AI job-hunting assistant on Discord, for job seekers in the Philippines.
You can talk about anything, but you're best at: finding and explaining jobs, career advice, résumé and interview tips,
salary expectations, and planning a job search.

How you talk: like an easygoing friend who knows job hunting. Casual, warm, plain words. Answer first, then add anything useful.
Reply in {session.prefs.get("language") or "English"}, even if they write in another language. Only they can change this, by asking you to.
Keep it short: a sentence or two for a simple question.
Use bullets only when a list really helps. Don't repeat what they said, restate lists they've already seen, lecture, or sound corporate.

How you coach (this is what you're for):
- Base everything on the candidate's real profile in the context: their level, projects, current job, skills and what they want. Mention their real projects by name when it helps.
- A job they show you: say how well it fits (strong, okay or weak) with two or three reasons from the posting against their profile. Flag red flags: asking for money or fees, pay far above normal, chat-only "recruiters", a personal Gmail with no company, or vague duties.
- Interview prep: the five most likely questions for that role, and how to answer each with their real experience, using Situation, Task, Action, Result.
- Follow-ups: short and polite, about a week after applying. Write it for them if they ask.
- Salary: never invent numbers. Use their saved expected salary, and suggest checking real salary data on the job boards.
- Cover letters and résumés: suggest specific changes using their real details. Never add skills or experience they don't have.
- Never say you applied, emailed, or started anything unless right_now says so.

Use Discord markdown, max ~1500 characters.
Never invent facts about the user; use only the context below. If you're unsure, say so.
When asked how you work, where jobs or the user's data come from, or about job boards like LinkedIn, answer from "how_vera_works".
If they want to change job filters, say what you'd change and remind them they can say it directly (e.g. "@Vera only remote jobs").

CONTEXT (this user's own data):
{_context(session)}
{job_part}{situation}
RECENT CONVERSATION, so you can follow short replies like "no, just the amount". You can't see anything older:
{_recent_chat(session) or "(nothing yet)"}

User: {text}"""
    reply = await ai.complete(prompt, temperature=0.6, max_tokens=900)
    if reply:
        return reply
    if busy:
        return busy
    return ("My AI brain is busy right now (the free models are rate-limited), so I can't chat properly this minute. "
            "Please try again shortly. Meanwhile I can still change your filters (e.g. *@Vera only remote jobs that need no experience*), "
            "search (`/find-jobs`), and show `/applications` or `/statistics`.")


# --------------------------------------------------------------------------- "apply for me" conversation
# Vera gathers what she needs, says exactly what she'll do, and only starts after a clear yes.
STOP_RE = re.compile(r"^\W*(stop|cancel|abort|halt|tigil|itigil)\b|\b(stop|cancel)\s+(it|now|applying|the run|that|her)\b|\bstop applying\b", re.I)
PROGRESS_RE = re.compile(r"\b(how('?s| is)\b.{0,40}\b(going|run|running|doing)|progress|how many (have you|did you|so far)|are you (done|finished)|is (it|she|the run) (running|done)|tapos na)\b", re.I)
RESUME_USED_RE = re.compile(r"\b(what|which|anong|ano(ng)?)\b.*\b(resume|résumé|cv)\b", re.I)
QUESTION_START = re.compile(r"^\W*(how|why|what|which|did|when|where|who|have|has|is|are|should|do i|does)\b", re.I)
APPLY_RE = re.compile(r"\b(apply|applying|mag-?apply|send (my )?applications?)\b", re.I)
YES_RE = re.compile(r"^\W*(yes|yep|yeah|yup|sure|ok|okay|go|go ahead|start|proceed|continue|tuloy|ituloy|push through|let'?s go|apply now|send now|do it|sige|oo|opo|game|confirm)\b", re.I)
TEST_RE = re.compile(r"\b(test|try it|dry run|without sending|don'?t send|huwag i?send)\b", re.I)
NO_RE = re.compile(r"^\W*(no|nope|never ?mind|hindi|wag|huwag|not now)\b", re.I)
SITE_WORDS = {"company_forms": r"company forms?|greenhouse|lever|ashby|workable",
              "remote_boards": r"remote (job )?(boards?|sites?)|remotive|jobicy|remote ?ok|we ?work ?remotely|working ?nomads|himalayas|hacker ?news"}
RECOMMEND_RE = re.compile(r"\b(recommend|suggest|what jobs? (fit|suit|match)|may (ma)?recommend)\b", re.I)
NO_LIMIT_RE = re.compile(r"\b(no limit|until i (say|tell)|till i (say|tell)|keep applying|as many as)\b", re.I)
OUTREACH_RE = re.compile(r"\b(cold[- ]?emails?|outreach|reach out|write an? e-?mail|(?:search|find|look|check)\b[^.\n]{0,40}\b(?:facebook|fb|company sites?)|email (the |this |that |these |some |\d+ )?(compan\w+|recruiters?|employers?|hr|hiring|post|site|job)|send (cold |\d+ )?emails?)\b", re.I)
FACEBOOK_RE = re.compile(r"\b(facebook|fb)\b", re.I)
SITES_ONLY_RE = re.compile(r"\b(company|companies'?) sites?\b|\bwebsites?\b", re.I)
MAIL_RE = re.compile(r"\b(check (my )?(e-?mail|mail|inbox)|any (replies|reply|responses?|e-?mails?|mail)|did (anyone|they|someone|any\w*) (reply|respond|answer)|replies so far)\b", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
DO_NOT_EMAIL_RE = re.compile(r"\b(?:don'?t|do not|never|stop)\s+(?:e-?mail(?:ing)?|contact(?:ing)?)\s+(\S+)", re.I)
ANSWER_RE = re.compile(r"^\s*(\d{1,2})\s*[:.)=-]\s*(.+)$", re.S)
SKIP_ONE_RE = re.compile(r"^\W*(?:skip|drop|remove)\s+(\d{1,2})\b", re.I)
CHAT_ONLY_KEYS = ("digest_count", "results_per_search", "daily_digest")   # "5 jobs" here means how many to apply to


YES_WORDS = set("yes yep yeah yup ya sure ok okay okey k go ahead start proceed continue tuloy ituloy push through do it sige oo opo game confirm confirmed please now lets let's let us na po "
                "thanks thank you send them apply to those that run all right alright good fine looks sounds great perfect for me the applications emails real "
                # Filler around a go-ahead ("got it, proceed", "okay then, go ahead with the plan"). Any other word means a change, so it waits.
                "got noted understood cool nice awesome then just with this plan you can i said already kick off on ahead".split())
# A go-ahead word anywhere in the message. plain_yes still needs every word to be a yes word or filler.
GO_WORD_RE = re.compile(r"\b(yes|yep|yeah|yup|sure|ok|okay|go|go ahead|start|proceed|continue|tuloy|ituloy|push through|let'?s go|apply now|send now|do it|"
                        r"sige|oo|opo|game|confirm|all ?right|alright|looks good|sounds good)\b", re.I)
NO_WORDS = set("no nope never mind nevermind hindi wag huwag not now cancel thanks thank you po it na lang dont don't apply send them stop".split())
ASKING_TAG = re.compile(r"\b(right|correct|tama|diba|di ba|ba|noh)\W*$", re.I)
ASKING_START = re.compile(r"^\W*(ano|bakit|paano|saan|ilan|kailan|what do you mean|explain|ibig sabihin|meaning)\b", re.I)
ASKING_WORDS = re.compile(r"\b(explain|mean|tell me|what|why|how|which|ano|bakit|paano)\b", re.I)
POLITE_REQUEST = re.compile(r"^\W*(can|could|would|will|please|pwede|paki)\b", re.I)


UPDATES_WHERE = re.compile(r"\b(post|send|put|share|announce|tell)\b.*\b(here|this channel|dito)\b", re.I)
UPDATES_WHAT = re.compile(r"\b(improve\w*|updates?|new rules?|changes?|changelog|what'?s new|learn\w*)\b", re.I)


LANGUAGE_NAMES = {"english": "English", "tagalog": "Tagalog", "filipino": "Tagalog", "taglish": "Taglish"}
LANGUAGE_RULE = re.compile(r"\b(respon\w*|repl(y|ies)|talk|speak|answer\w*|chat|usap|use)\b.*\b(english|tagalog|filipino|taglish)\b|"
                           r"\b(english|tagalog|filipino|taglish)\b.*\b(only|default|lang|from now on|please)\b", re.I)
NOT_LANGUAGE_RULE = re.compile(r"\b(jobs?|cover letters?|letters?|e-?mails?|resume|résumé|cv|forms?|interview)\b", re.I)


def language_request(text: str) -> Optional[str]:
    """A language they want her to chat in. "Jobs that use English" and "write my letter in Tagalog" are about something else."""
    if NOT_LANGUAGE_RULE.search(text) or not LANGUAGE_RULE.search(text):
        return None
    return LANGUAGE_NAMES[re.findall(r"english|tagalog|filipino|taglish", text.lower())[0]]


EMAIL_WORD = re.compile(r"\b(e-?mails?|outreach)\b", re.I)
JUST_ASKING_EMAIL = re.compile(r"\b(just (want(ed)? to )?ask|only asking|not actually send|don'?t (actually )?send|not make you|if you (can|could)|asking if)\b", re.I)
CAN_YOU = re.compile(r"^\W*(?:\w+\W+)?(can|could|are you able to|do you|will you|would you|pwede|kaya mo)\b", re.I)
TABS_RULE = re.compile(r"\b(\d{1,2})\s+tabs?\b(\s+per\s+(site|board))?", re.I)
EMAIL_ANSWER = ("Yes, I can send email from your Gmail, but only when you ask in {where}. I show you the drafts first, and nothing goes out until you say **proceed**.\n"
                "Send me a job post's text with an email address, or say *cold email 3 companies*. I can't open Facebook links, so paste the post itself.\n"
                "If a job post asks you to email them, I'll just tell you in the run report.")


def email_channels() -> set:
    from vera import config
    # His private email channel too (9 Nov 2026): he moved a reply to an employer there, and she couldn't draft it.
    return {config.OUTREACH_CHANNEL, config.JOB_MAIL_CHANNEL, config.OWNER_CHANNELS["email"]}


def where_email_works() -> str:
    from vera import config
    return f"#{config.OUTREACH_CHANNEL}, #{config.JOB_MAIL_CHANNEL} or your private #{config.OWNER_CHANNELS['email']}"


def asking_about_email(text: str, named: list) -> bool:
    """"Can you send emails?" is a question. "Can you email hr@acme.ph?" or "can you cold email 3 companies?" is a request."""
    if not EMAIL_WORD.search(text):
        return False
    if JUST_ASKING_EMAIL.search(text):
        return True
    return bool(CAN_YOU.search(text)) and not named and parse_count(text) is None


SHOW_LETTER = re.compile(r"\b(show|see|send|give|view|read|sample|example|what)\b.*\bcover letters?\b|\bcover letters?\b.*\b(you (use|used|sent|wrote)|used|sent|sample|example)\b", re.I)


def latest_cover_letter(session, text: str) -> Optional[str]:
    """A real letter she wrote, not an invented template. The closest match if they named a job."""
    from vera.agents import rounds
    folder = rounds.user_dir(session.uid) / "cover_letters"
    letters = sorted(folder.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True) if folder.exists() else []
    if not letters:
        return None
    words = [w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in ("show", "cover", "letter", "letters", "give", "send", "that", "used", "sent", "with", "sample", "example", "what", "your", "this", "like", "want", "from")]
    best = max(letters, key=lambda f: sum(w in f.name.lower() for w in words))
    return best


def plain_yes(text: str) -> bool:
    """A clear yes and nothing else. "Ok, but only remote" and "ok, what about 5?" are not a go-ahead to send anything in someone's name."""
    if "?" in text or not GO_WORD_RE.search(text):
        return False
    return all(word in YES_WORDS for word in re.findall(r"[\w']+", text.lower()))


def plain_no(text: str) -> bool:
    """A clear no. "No, make it 5 jobs" is a change, not a cancel."""
    return bool(NO_RE.search(text)) and "?" not in text and all(word in NO_WORDS for word in re.findall(r"[\w']+", text.lower()))


def just_asking(text: str) -> bool:
    """A question or a check, with no change to make. "Can you make it remote?" is a change; "can you explain?" isn't."""
    asking = text.rstrip().endswith("?") or QUESTION_START.search(text) or ASKING_TAG.search(text) or ASKING_START.search(text)
    if not asking:
        return False
    return not POLITE_REQUEST.search(text) or bool(ASKING_WORDS.search(text))


def parse_count(text: str) -> Optional[int]:
    if NO_LIMIT_RE.search(text):
        from vera.agents.rounds import NO_LIMIT
        return NO_LIMIT
    m = re.fullmatch(r"\W*(\d{1,3})\W*", text) or re.search(r"\b(\d{1,3})\s*(?:\w+\s+){0,4}?(?:jobs?|applications?|positions?|roles?|companies)\b", text, re.I) \
        or re.search(r"\b(\d{1,3})\s+different\b", text, re.I) \
        or re.search(r"\b(?:at ?least|up to|around|about|mga|atleast)\s*(\d{1,3})\b", text, re.I)
    return max(1, int(m.group(1))) if m else None


def export_request(text: str):
    """Which files someone wants of their applications, "ask" if it's unclear, or None."""
    found = export_parse.plan(text)
    return found["formats"] if isinstance(found, dict) else found


NOT_WORD = r"\b(?:don'?t|dont|do not|not|no|without|exclude|excluding|except|skip|avoid|remove|never|hindi|wag|huwag|besides)\b"
USE_AGAIN = re.compile(r"\b(again|back|re-?enable|allow)\b", re.I)


def excluded_sites(text: str) -> List[str]:
    """Sources someone said not to use: "don't use remote boards", "not workable", "exclude company forms"."""
    if USE_AGAIN.search(text):
        return []
    return [s for s, pat in SITE_WORDS.items()
            if re.search(rf"{NOT_WORD}(?:\W+(?:use|using|apply|applying|go|try|search|on|to|with|the|in|from|any|include))*\W+(?:{pat})", text, re.I)]


def parse_sites(text: str) -> Optional[List[str]]:
    """Sources someone asked to use. Naming one to rule it out doesn't count."""
    out = excluded_sites(text)
    found = [s for s, pat in SITE_WORDS.items() if re.search(pat, text, re.I) and s not in out]
    return found or None


def _skipped_here(session, channel_id: Optional[int]) -> List[str]:
    return (session.db.get_json(f"channel_skip:{channel_id}") or []) if channel_id else []


def _usable_sites(session, sites: Optional[List[str]], skip: List[str]) -> Optional[List[str]]:
    """Sources for this plan (company forms, remote boards), without skipped ones. None means all of them."""
    if sites is None:
        return [s for s in ("remote_boards", "company_forms") if s not in skip] if skip else None
    return [s for s in sites if s not in skip] or None


def _no_board_rules(patch: Dict[str, Any]) -> Dict[str, Any]:
    """A job source isn't a keyword or a company to block. Blocking "workable" as a keyword would reject any job that merely mentions it."""
    exclude = patch.get("exclude")
    if not isinstance(exclude, dict):
        return patch
    is_board = lambda word: any(re.search(pat, str(word), re.I) for pat in SITE_WORDS.values())
    cleaned = {k: [w for w in v if not is_board(w)] if isinstance(v, list) else v for k, v in exclude.items()}
    return {**patch, "exclude": cleaned}


def wants_to_apply(text: str) -> bool:
    return bool(APPLY_RE.search(text)) and not QUESTION_START.search(text)


def _plan_where(session, req: Dict[str, Any]) -> List[str]:
    labels = {"company_forms": "company forms (Greenhouse, Lever, Ashby, Workable)",
              "remote_boards": "remote boards (Real Work From Anywhere, Jobspresso, Remotive, Jobicy, RemoteOK, We Work Remotely, Working Nomads, Himalayas, LaraJobs, Hacker News, company job lists)"}
    if req.get("sites"):
        return [labels.get(s, s) for s in req["sites"]]
    return [labels["remote_boards"], labels["company_forms"]]


def _experience_text(years) -> str:
    if years is None:
        return "no limit"
    return "only jobs that need no experience" if years == 0 else f"0 to {years:g} year{'' if years == 1 else 's'}, including no-experience jobs"


def plan_answer(session, req: Dict[str, Any], text: str) -> Optional[str]:
    """Questions about the plan, answered from the real settings, so she can't get a number wrong. Anything else goes to the AI."""
    from vera.agents import rounds
    p, t = session.prefs, text.lower()
    if re.search(r"\b(experience|exp|years?|yrs?|taon)\b", t):
        years = p.get("reject_mandatory_experience_over")
        if years is None:
            return "I'm not limiting by experience right now, so I'll look at any job that fits otherwise. Want a limit? Say *up to 1 year*."
        said = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", t)]
        what = ("I'll only go for jobs that need no experience." if years == 0 else
                f"I'll go for jobs asking anywhere from 0 to {years:g} year{'' if years == 1 else 's'}, including ones that need no experience. If a job wants more than that, I skip it.")
        if said and max(said) == years:
            return f"Yep! {what}"
        return what + (f" Want it higher? Say *up to {max(said):g} years*." if said and max(said) > years else "")
    if re.search(r"\b(test|difference|real)\b", t):
        return "**Test** fills in the forms but stops before sending, so nothing reaches employers. **Yes** sends real applications in your name."
    if re.search(r"\b(resume|résumé|cv)\b", t):
        return f"I'll use **{rounds.Path(rounds.build_profile(session).get('resume_path') or 'none yet').name}**."
    if re.search(r"\b(level|junior|senior|entry|fresh)\b", t):
        return f"I'm going for {', '.join(p.get('job_levels') or []) or 'any level'}. Tell me if you want that changed."
    if re.search(r"\b(how many|ilan|limit)\b", t):
        count = req.get("count")
        if not count:
            return "You haven't told me how many yet. Just give me a number."
        return "As many as I can, until you say **stop**." if count >= rounds.NO_LIMIT else f"Up to {count}. I might send fewer if not enough jobs fit."
    if re.search(r"\b(where|boards?|sites?|saan)\b", t):
        return f"I'll look on {', '.join(_plan_where(session, req))}."
    if re.search(r"\b(titles?|positions?|roles?)\b", t):
        return f"I'm looking for {', '.join(p.get('positions') or []) or 'nothing yet, so tell me which jobs you want'}, plus similar titles."
    if re.search(r"\b(remote|hybrid|on-?site|setup)\b", t):
        return f"Right now that's {', '.join(p.get('work_setup') or []) or 'any setup'}. Say something like *only remote jobs* to change it."
    if re.search(r"\b(full-?time|part-?time|contract|job type)\b", t):
        return f"Right now that's {', '.join(p.get('employment_types') or []) or 'any type'}. Tell me if you want that changed."
    return None


def _plan_nudge(session, req: Dict[str, Any]) -> str:
    """One line on what she's waiting for, instead of the whole plan again."""
    from vera.agents import rounds
    missing = rounds.blockers(rounds.build_profile(session))
    if missing:
        return f"I still need your own: {', '.join(missing)}."
    if not req.get("count"):
        return "Just tell me how many jobs."
    return "Say **proceed** when you're ready, **test** to try it without sending, or tell me what to change."


def apply_summary(session, req: Dict[str, Any]) -> str:
    from vera.agents import rounds
    p, is_owner = session.prefs, rounds.is_owner(session.uid)
    profile = rounds.build_profile(session)
    limit = rounds.limits_for(session.uid, p["rounds"])["max_per_day"]
    where = _plan_where(session, req)
    years = p.get("reject_mandatory_experience_over")
    lines = ["Here's the plan. Tell me if anything looks off, or ask me about it:",
              f"• **Where:** {', '.join(where)}",
              ("• **How many:** as many as I can, until you say **stop**" if req.get("count") and min(req["count"], limit) >= rounds.NO_LIMIT
               else f"• **How many:** up to {min(req['count'], limit)} in total" + (", spread across these" if len(where) > 1 else "")) if req.get("count") else "• **How many:** you didn't say, so tell me a number",
              f"• **Job titles:** {', '.join(p.get('positions') or []) or 'none yet, so tell me which jobs you want'}",
              f"• **Level:** {', '.join(p.get('job_levels') or []) or 'any'}",
              f"• **Experience:** {_experience_text(years)}",
              f"• **Work setup:** {', '.join(p.get('work_setup') or [])}",
              f"• **Job type:** {', '.join(p.get('employment_types') or [])}",
              f"• **Résumé:** {rounds.Path(profile.get('resume_path') or 'none yet').name}"]
    resting = rounds.resting_text(session.uid)
    if resting:
        lines.append(f"\n⏸ **Resting after a bot check:** {resting}. I'll skip those, so this run may send fewer, or wait until then.")
    missing = rounds.blockers(profile)
    if missing:
        lines.append("\n**I still need your own:** " + ", ".join(missing) + ". Just tell me here, like *my email is you@example.com*. "
                     "Send your résumé as a PDF or Word file. DM me for this, so others can't see it.")
    elif not req.get("count") or not p.get("positions"):
        lines.append("\nWhat should I add or change?")
    else:
        lines.append("\nI might find fewer if not enough jobs fit. Say **proceed** (or **yes**) and I'll send real applications in your name, "
                     "**test** to try it without sending, or just tell me what to change.")
        if not is_owner and not p["rounds"].get("consent"):
            lines.append(rounds.CONSENT_TEXT)
    return "\n".join(lines)


MY_DETAILS_RE = re.compile(r"\bmy (e-?mail|phone|mobile|number|name|linkedin|location)\b|\bi (live|am based|'m based) in\b", re.I)
SAFE_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def _contact_from_chat(session, text: str) -> List[str]:
    """Their own contact details, given in conversation."""
    top: Dict[str, Any] = {}
    saved = []
    if re.search(r"\bmy e-?mail\b|\be-?mail(?: address)?(?: is|:)", text, re.I) and SAFE_ADDRESS.search(text):
        top["email"] = SAFE_ADDRESS.search(text).group(0)
        saved.append("email")
    m = re.search(r"(?i:\b(?:phone|mobile|number|contact)(?: number)?(?: is|:)?)\s*(\+?\d[\d \-]{8,}\d)", text)
    if m:
        top["phone"] = m.group(1)
        saved.append("phone")
    m = re.search(r"(?i:\bmy name is)\s+([A-Z][\w.'-]+(?: [A-Z][\w.'-]+){0,4})", text)
    if m:
        top["name"] = m.group(1)
        saved.append("name")
    m = re.search(r"(?i:\b(?:i live in|i am based in|i'm based in|my location is))\s+([A-Z][^.\n]{2,70})", text)
    if m:
        top["location"] = m.group(1).strip()
        saved.append("location")
    m = re.search(r"(linkedin\.com/in/[\w-]+)", text, re.I)
    if m:
        top["links"] = {"linkedin": "https://" + m.group(1)}
        saved.append("LinkedIn")
    if top:
        session.update_profile(top)
    return saved


def _details_from_chat(session, text: str) -> None:
    """Pick up details people give in conversation: 'expected salary 30k', 'I can start in 2 weeks'."""
    _contact_from_chat(session, text)
    patch: Dict[str, Any] = {}
    m = re.search(r"(?:salary|pay|expect\w*)[^0-9$₱]*(?:₱|php\s*)?(\d[\d,]*\s*k?)\b", text, re.I)
    if m and not re.search(r"usd|\$|dollar", text, re.I):
        try:
            patch["expected_salary_monthly"] = P.parse_money(m.group(1))
        except ValueError:
            pass
    m = re.search(r"\b(?:start|notice)\b.*?\b(immediately|asap|\d+\s*(?:days?|weeks?|months?)(?:'? notice)?)", text, re.I)
    if m:
        patch["notice_period"] = m.group(1)
    if patch:
        session.update_profile({"application": patch})


async def _outreach_conversation(session, text: str, channel_name: Optional[str] = None, plan_key: str = "") -> Optional[Dict[str, Any]]:
    """Cold email through chat: she drafts and shows them, and only sends after a yes."""
    from vera import config
    from vera.agents import rounds, outreach
    key = f"outreach_pending:{session.uid}"
    pending = session.db.get_json(key)
    done = lambda action, reply, **kw: {"action": action, "reply": reply, "patch": {}, "changes": [], **kw}
    wanted = OUTREACH_RE.search(text)
    named = outreach.EMAIL_RE.findall(text)
    stop = DO_NOT_EMAIL_RE.search(text)
    if MAIL_RE.search(text) and not wanted:
        return None
    if asking_about_email(text, named):
        return done("none", EMAIL_ANSWER.format(where=where_email_works()))
    asked_to_email = wanted or stop or (named and re.search(r"\b(e-?mail|send|message|contact|resume|cv|hiring|apply)\b", text, re.I))
    if not (pending or asked_to_email):
        return None
    # Email only happens where the owner said it may. Everywhere else she points the way, and the drafts wait.
    if rounds.is_owner(session.uid) and channel_name is not None and channel_name not in email_channels():
        return done("none", f"I only send email from {where_email_works()}. Ask me there and I'll show you the drafts first.") if asked_to_email else None
    if not rounds.is_owner(session.uid):
        # Never the owner's Gmail or accounts. Other people get a draft to send from their own email.
        if session.db.get_json(plan_key or f"apply_request:{session.uid}") and not wanted:
            return None
        if not named:
            return done("none", "I can't send email for you, because I only have the owner's account and won't use it for anyone else. "
                                "Paste a post with an email address, or say *write an email to hr@company.com*, and I'll write a draft for you to send from your own email.")
        context = text[:2500] if len(text) > 120 else ""
        lead = {"email": named[0].lower(), "company": named[0].rsplit("@", 1)[-1].split(".")[0].title(), "title": outreach.title_from(text) if context else "", "url": "", "context": context}
        item = await outreach.draft(session, lead)
        return done("none", f"📝 **Here's a draft to send from your own email, with your résumé attached. I haven't sent anything.**\n**To:** {item['email']}\n**Subject:** {item['subject']}\n\n{item['body']}")
    if stop:
        who = stop.group(1).strip(".,").lower()
        outreach.block(session.uid, who)
        session.db.set_json(key, None)
        return done("none", f"🚫 Okay. I won't email **{who}** again.", learned="Added an address to your do-not-contact list.")
    if pending and not wanted and not named:
        skip = SKIP_ONE_RE.match(text)
        if skip:
            items = [x for i, x in enumerate(pending, 1) if i != int(skip.group(1))]
            session.db.set_json(key, items or None)
            return done("none", outreach.preview(items) if items else "Okay, none left. Nothing was sent.")
        if plain_no(text) or STOP_RE.search(text):
            session.db.set_json(key, None)
            return done("none", "Okay, cancelled. Nothing was sent.")
        if plain_yes(text):
            if not outreach.can_send():
                return done("none", outreach.setup_help())
            session.db.set_json(key, None)
            return done("send_outreach", f"📨 Sending {len(pending)} email(s), about 90 seconds apart. I'll report in #{config.OUTREACH_CHANNEL}.", request=pending)
        waiting = f"Your {len(pending)} draft(s) are still waiting. Say **proceed** to send them, *skip 2* to drop one, or **no** to cancel."
        if just_asking(text):
            situation = f"\nThe user has {len(pending)} cold email draft(s) waiting to be sent, to: {', '.join(x.get('email', '') for x in pending)[:300]}. Nothing has been sent yet. Don't say you sent or will send anything.\n"
            answer = await chat(session, text, situation, busy="My AI is busy this minute, so I can't answer that properly. Try again in a bit.")
            return done("none", f"{answer}\n\n{waiting}")
        return None      # something else: answer normally, and keep the drafts waiting
    # A new batch
    if not rounds.resume_path(session.profile):
        return done("none", "I need your résumé first. Send it with `/resume add`.")
    count = min(parse_count(text) or 5, config.OUTREACH_MAX_PER_DAY)
    from vera.agents import web
    links = URL_RE.findall(text)
    if named:
        leads, refused = [], []
        # A long message is a pasted post, so its text gives the email something real to refer to.
        context = text[:2500] if len(text) > 120 else ""
        title = outreach.title_from(text) if context else ""
        for e in dict.fromkeys(x.lower() for x in named):
            why = outreach.contactable(session.uid, e)
            (refused.append(f"{e}: {why}") if why else leads.append({"email": e, "company": e.rsplit("@", 1)[-1].split(".")[0].title(), "title": title, "url": "", "context": context}))
        if refused and not leads:
            return done("none", "I can't email that address: " + "; ".join(refused))
    elif links:
        if web.needs_login(links[0]):
            return done("none", "I can't read Facebook, LinkedIn or Instagram posts, because they need a login. Paste the post's text here, including the email address, and I'll write from that.")
        leads = [x for u in links[:count] for x in await outreach.from_site(u) if not outreach.contactable(session.uid, x["email"])]
        if not leads:
            return done("none", "I couldn't find a contact email on that site. If you have one, say *email hr@company.com*.")
    else:
        source = "facebook" if FACEBOOK_RE.search(text) else "sites" if SITES_ONLY_RE.search(text) else None
        leads = outreach.find_leads(session, limit=count) if source is None else []
        if len(leads) < count:
            have = {outreach.who(x["email"]) for x in leads}
            leads += [x for x in await outreach.web_leads(session, count - len(leads), source) if outreach.who(x["email"]) not in have]
    room = outreach.room_today(session.uid)
    if room <= 0:
        return done("none", f"You've reached today's limit of {config.OUTREACH_MAX_PER_DAY} emails. Going past it risks your Gmail being suspended. Try again tomorrow.")
    if not leads:
        return done("none", "I searched and couldn't find any hiring emails right now. Many posts don't show an email in search results, so try again later. *recommend 20 jobs for me* can also turn some up from job posts. "
                            "You can also paste a post, link a company site, or name one: *email hr@company.com*.")
    items = [await outreach.draft(session, lead) for lead in leads[:min(count, room)]]
    session.db.set_json(key, items)
    return done("none", outreach.preview(items) + ("" if outreach.can_send() else "\n\n" + outreach.setup_help()))


def _plan_key(session, channel_id: Optional[int]) -> str:
    """A plan belongs to the channel it was made in, so "proceed" here can't start a plan from another channel."""
    return f"apply_request:{session.uid}" + (f":{channel_id}" if channel_id else "")


async def _apply_conversation(session, text: str, channel_sites: Optional[List[str]] = None, channel_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Returns a result if this message belongs to the apply conversation, else None."""
    from vera.agents import rounds
    key = _plan_key(session, channel_id)
    pending = session.db.get_json(key)
    done = lambda action, reply, **kw: {"action": action, "reply": reply, "patch": {}, "changes": [], **kw}
    excluded = excluded_sites(text)
    if excluded and channel_id:     # a rule for this channel, until they say otherwise
        session.db.set_json(f"channel_skip:{channel_id}", sorted(set(_skipped_here(session, channel_id)) | set(excluded)))
    elif channel_id and USE_AGAIN.search(text) and not APPLY_RE.search(text):
        named = [s for s, pat in SITE_WORDS.items() if re.search(pat, text, re.I)]
        if named:
            session.db.set_json(f"channel_skip:{channel_id}", [s for s in _skipped_here(session, channel_id) if s not in named])
            return done("none", f"Okay, {', '.join(n.replace('_', ' ') for n in named)} can be used in this channel again.", learned=f"Using {', '.join(n.replace('_', ' ') for n in named)} in this channel again.")
    skip = _skipped_here(session, channel_id) or excluded
    if excluded and not pending and not wants_to_apply(text):
        names = ", ".join(n.replace("_", " ") for n in excluded)
        return done("none", f"Got it, I won't use {names} in this channel. Tell me if you want it back.", learned=f"Not using {names} in this channel.")
    numbered = ANSWER_RE.match(text)
    questions = session.db.get_json(f"open_questions:{session.uid}") or []
    if numbered and 1 <= int(numbered.group(1)) <= len(questions):
        question = questions[int(numbered.group(1)) - 1]
        rounds.save_answer(session.uid, question, numbered.group(2).strip())
        return done("none", f"✅ Saved. For **{question[:100]}** I'll answer: {numbered.group(2).strip()[:150]}", learned=f"Saved your answer to a form question: {question[:100]}")

    if STOP_RE.search(text):
        running_here = any(channel_id is None or r.get("channel_id") in (None, channel_id) for r in rounds.runs_for(session.uid))
        if running_here or rounds.waiting_for(session.uid, channel_id):
            return done("stop_apply", "🛑 Stopping now. Anything already sent has gone out, and I'll send you the report.")
        if pending:
            session.db.set_json(key, None)
            return done("none", "Okay, cancelled. Nothing was sent.")
        if rounds.running_for(session.uid) or rounds.waiting_for(session.uid):
            return done("stop_apply", "🛑 Stopping now.")      # the bot explains which run, and where
        return done("none", "There's no run of yours to stop right now.")
    if PROGRESS_RE.search(text):
        return done("none", rounds.status_now(session))
    if RESUME_USED_RE.search(text):
        name = rounds.Path(rounds.build_profile(session).get("resume_path") or "").name
        # (an actual résumé question, answered directly)
        return done("none", f"I use **{name}**." if name
                    else "You haven't given me a résumé yet. Send one with `/resume add`.")

    following_up, before = False, {}
    if RECOMMEND_RE.search(text) and not APPLY_RE.search(text):
        if not rounds.has_room(session.uid, "recommend"):
            return done("none", "I'm busy with other runs right now. Say **stop** to end one, or ask again in a while.")
        return done("recommend", "🔎 Looking now. I'll list what fits, and I won't apply to anything.",
                    request={"count": min(parse_count(text) or 10, 50), "sites": parse_sites(text) or channel_sites})

    if pending:
        if plain_no(text):
            session.db.set_json(key, None)
            return done("none", "Okay, I won't apply. Nothing was sent.")
        is_yes = plain_yes(text)
        if not is_yes and just_asking(text):     # a question gets an answer: nothing changes, and the plan keeps waiting
            answer = plan_answer(session, pending, text)
            if answer is None:
                situation = (f"\nYou've just shown them this plan and are waiting for their go-ahead. Answer their question about it from these details, "
                             f"without repeating the whole plan. You have NOT started, and nothing has been sent.\n{apply_summary(session, pending)}\n")
                answer = await chat(session, text, situation, busy="My AI is busy this minute, so I can't answer that one properly. Try again in a bit, or check the plan above.")
            return done("none", f"{answer}\n\n{_plan_nudge(session, pending)}")
        is_test = not is_yes and bool(TEST_RE.search(text))
        if is_test or is_yes:
            profile = rounds.build_profile(session)
            if rounds.blockers(profile) or not pending.get("count") or not session.prefs.get("positions"):
                return done("none", apply_summary(session, pending))
            session.db.set_json(key, None)
            if not is_test and not rounds.is_owner(session.uid):
                session.update_prefs({"rounds": {"consent": True}})
            what = "🧪 Starting the test. I'll fill forms but send nothing. I'll post each one as it goes, then a full report at the end." if is_test else "🚀 Starting now. I'll post each application as it goes, then a full report at the end. Say **stop** to end it, or ask *how's it going?*"
            return done("start_apply", what, request={**pending, "dry_run": is_test})
        # Anything else is an answer or a change to what she said.
        before = {**pending}
        pending = {**pending, "count": parse_count(text) or pending.get("count"), "sites": _usable_sites(session, parse_sites(text) or pending.get("sites"), skip)}
        following_up = True
    elif wants_to_apply(text):
        pending = {"count": parse_count(text), "sites": _usable_sites(session, parse_sites(text) or channel_sites, skip)}
    elif re.match(r"^\W*(proceed|continue|go ahead|tuloy|ituloy|push through)\W*$", text, re.I):
        return done("none", "There's no plan waiting in this channel. Tell me what you want, like *apply to 5 jobs*.")
    else:
        return None

    profile_before = json.dumps(session.profile, sort_keys=True, default=str)
    _details_from_chat(session, text)
    current = session.prefs
    bare_number = re.fullmatch(r"\W*\d{1,3}\W*", text) is not None
    found = {} if bare_number else await interpret(text, current)
    patch = sanitize(_no_board_rules({k: v for k, v in (found.get("patch") or {}).items() if k not in CHAT_ONLY_KEYS}), current)
    patch = {k: v for k, v in patch.items() if v != current.get(k)}
    if patch:
        session.update_prefs(patch)
    session.db.set_json(key, pending)
    if not following_up:
        return done("none", apply_summary(session, pending), patch=patch, changes=[])
    changed = patch or excluded or pending != before or json.dumps(session.profile, sort_keys=True, default=str) != profile_before
    if changed:
        return done("none", "Got it, updated.\n\n" + apply_summary(session, pending), patch=patch, changes=[])
    if YES_RE.search(text):    # sounds like a go-ahead, but with more in it: be clear, and never let the AI claim it started
        return done("none", "I haven't started yet. To start, send just **proceed**. If something gets blocked, I'll tell you here and in the report.", patch=patch, changes=[])
    # Just talking: reply like a person, not with the same list again.
    situation = f"\nYou're waiting for their go-ahead on a job application plan. You have NOT started, and nothing has been sent. Reply briefly and naturally.\n{apply_summary(session, pending)}\n"
    answer = await chat(session, text, situation, busy="I'm not sure what you'd like me to change.")
    return done("none", f"{answer}\n\n{_plan_nudge(session, pending)}", patch=patch, changes=[])


async def handle(session, text: str, channel_sites: Optional[List[str]] = None, channel_id: Optional[int] = None, channel_name: Optional[str] = None) -> Dict[str, Any]:
    """channel_id and channel_name come from Discord. Left out, as in the command line, no channel rules apply."""
    scope = _chat_scope.set(str(channel_id or ""))
    history = _history_text.set(_recent_chat(session))
    try:
        result = await _handle(session, text, channel_sites, channel_id, channel_name)
        _remember_chat(session, text, result.get("reply"))
        return result
    finally:
        _chat_scope.reset(scope)
        _history_text.reset(history)


async def _handle(session, text: str, channel_sites: Optional[List[str]], channel_id: Optional[int], channel_name: Optional[str]) -> Dict[str, Any]:
    from vera.agents import rounds as _ap, outreach as _out
    tabs = TABS_RULE.search(text)
    if tabs and _ap.is_owner(session.uid) and re.search(r"\b(use|open|set|make|increase|change|run|want)\b", text[:tabs.start()], re.I):      # "you left 3 tabs open" isn't a request
        number = max(1, min(int(tabs.group(1)), 30))
        field, what = ("tabs_per_site", "per board") if tabs.group(2) else ("tabs", "at once")
        session.update_prefs({"rounds": {field: number}})
        return {"action": "none", "patch": {}, "changes": [], "learned": f"I'll use up to {number} tabs {what}.",
                "reply": f"Got it, up to **{number}** tabs {what} from your next run. More tabs on one board makes bot checks more likely."}
    if SHOW_LETTER.search(text):
        letter = latest_cover_letter(session, text)
        if not letter:
            return {"action": "none", "patch": {}, "changes": [], "reply": "I haven't written any cover letters for you yet. I write one for each job when I apply."}
        job = letter.stem.split("_", 1)[-1].replace("_", " ")
        return {"action": "none", "patch": {}, "changes": [], "reply": f"Here's one I really used, for **{job}**. I write a new one for each job.\n\n{letter.read_text(encoding='utf-8')[:1500]}"}
    language = language_request(text)
    if language:
        session.update_prefs({"language": language})
        return {"action": "none", "patch": {}, "changes": [], "learned": f"I'll chat in {language} by default.",
                "reply": f"Got it, I'll reply in {language} from now on. That works everywhere: every channel and our DMs. Tell me if you want to switch."}
    if UPDATES_WHERE.search(text) and UPDATES_WHAT.search(text):
        if not _ap.is_owner(session.uid):
            return {"action": "none", "patch": {}, "changes": [], "reply": "Only the owner can choose where my updates go."}
        return {"action": "set_updates_channel", "patch": {}, "changes": [],
                "reply": "Got it, I'll post updates here: new versions of me, and new rules you give me. I don't improve myself, though. New abilities come from updates to my code."}
    wanted = export_parse.plan(text)
    if wanted == "ask":
        return {"action": "none", "patch": {}, "changes": [],
                "reply": "I can make a table, summary, Excel sheet, Word document or PDF of your job applications. Try *make a table of my applications this week*. "
                         "Your résumé is separate: say *send me my resume*."}
    if wanted:
        when = f" for {wanted['label']}" if wanted["label"] else ""
        return {"action": "send_export", "patch": {}, "changes": [], "request": {"formats": wanted["formats"], "options": wanted},
                "reply": f"📊 Putting your applications together{when}."}
    if MY_DETAILS_RE.search(text) and not session.db.get_json(_plan_key(session, channel_id)):
        saved = _contact_from_chat(session, text)
        if saved:
            return {"action": "none", "patch": {}, "changes": [], "reply": f"✅ Saved your {', '.join(saved)}. I use this only for your own applications."}
    if MAIL_RE.search(text) and not OUTREACH_RE.search(text):
        base = {"patch": {}, "changes": []}
        if not _ap.is_owner(session.uid):
            return {**base, "action": "none", "reply": "Mail tracking uses the owner's Gmail, so it's owner-only for now."}
        if not _out.can_send():
            return {**base, "action": "none", "reply": _out.setup_help()}
        return {**base, "action": "check_mail", "reply": "📬 Checking your Gmail now."}
    emailing = await _outreach_conversation(session, text, channel_name, _plan_key(session, channel_id))
    if emailing is not None:
        return emailing
    applying = await _apply_conversation(session, text, channel_sites, channel_id)
    if applying is not None:
        return applying
    current = session.prefs
    result = await interpret(text, current)
    patch = sanitize(_no_board_rules(result["patch"]), current)
    patch = {k: v for k, v in patch.items() if v != current.get(k)}
    if patch:
        session.update_prefs(patch)
    result["patch"] = patch
    result["changes"] = describe(patch)
    if result["action"] == "chat" or (not patch and result["action"] == "none"):
        result["action"] = "chat"
        result["reply"] = await chat(session, text)
    return result


__all__ = ["handle", "interpret", "rule_parse", "sanitize", "describe", "Optional"]
