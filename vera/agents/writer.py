"""Writer agent: summarises jobs and prepares application answers.

Answers use ONLY facts from your profile. If the AI is unavailable, honest
template answers are built from the same facts.
"""

import json
import re
from typing import Any, Dict, List, Optional

from vera import ai
from vera.agents.evaluator import SKILLS, _canon, _has, candidate_skills, job_skills

NEGATION = re.compile(r"\b(not|no|never|haven'?t|hasn'?t|without|lack|yet to|eager to learn|willing to learn|learning)\b", re.I)


def scrub_claims(answer: str, profile: Dict[str, Any]) -> tuple:
    """Remove sentences that claim skills the profile doesn't support.
    Returns (clean_answer, [removed skill names])."""
    mine = candidate_skills(profile)
    kept, removed = [], []
    for sent in re.split(r"(?<=[.!?])\s+", answer.strip()):
        low = sent.lower()
        claims = [_canon(s) for s in SKILLS if _has(low, s)]
        bad = [c for c in claims if c not in mine]
        if bad and not NEGATION.search(low):
            removed += bad
            continue
        if NEGATION.search(low) and re.search(r"\b(years?|experience|worked extensively|professional)\b", low) \
                and re.search(r"\b(not yet|haven'?t|have not|lack)\b", low):
            removed.append("unprompted shortcoming")
            continue
        kept.append(sent)
    return " ".join(kept).strip(), list(dict.fromkeys(removed))

BASE_QUESTIONS = [
    "Tell us about yourself.",
    "Why are you interested in this position?",
    "What relevant skills do you bring to this role?",
    "What is your expected salary?",
    "When are you available to start?",
]


def facts(profile: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("name", "location", "headline", "summary", "years_experience", "education", "experience", "skills",
            "languages", "certifications", "achievements", "links")
    return {k: profile.get(k) for k in keep if profile.get(k)}


def job_questions(job: Dict[str, Any]) -> List[str]:
    """Questions the posting explicitly asks applicants to answer."""
    qs = []
    for line in (job.get("description") or "").splitlines():
        line = line.strip(" •-*\t")
        if 15 < len(line) < 200 and line.endswith("?") and re.search(r"\b(you|your)\b", line, re.I):
            qs.append(line)
    return qs[:3]


def choose_resume(profile: Dict[str, Any], job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pick the résumé whose tags best match the job (first one if none match)."""
    resumes = profile.get("resumes") or []
    if not resumes:
        return None
    text = f"{job.get('title', '')} {' '.join(job_skills(job))}".lower()
    scored = sorted(resumes, key=lambda r: -sum(1 for t in r.get("tags", []) if t.lower() in text))
    return scored[0]


async def summarize(job: Dict[str, Any]) -> str:
    desc = job.get("description") or ""
    if len(desc) < 500:
        return desc or "No description provided."
    text = await ai.complete(
        "Summarise this job posting in 3-4 short bullet points (what you'd do, must-have requirements, "
        "notable perks). Use only the text given.\n\n" + desc[:7000], temperature=0.2, max_tokens=400)
    return text or (desc[:600].rsplit(" ", 1)[0] + "…")


def _salary_answer(prefs: Dict[str, Any]) -> str:
    s = prefs.get("salary", {})
    if s.get("min") and s.get("max"):
        a = f"{s.get('currency', '')} {s['min']:,}–{s['max']:,} per {s.get('period', 'month').rstrip('ly') or 'month'}"
    elif s.get("min"):
        a = f"{s.get('currency', '')} {s['min']:,} per {s.get('period', 'month').rstrip('ly') or 'month'}"
    else:
        return "I'm open to discussing a fair salary for this role."
    return a.strip() + (" (negotiable)" if s.get("negotiable") else "")


def _fallback(q: str, job: Dict[str, Any], profile: Dict[str, Any], prefs: Dict[str, Any], matched: List[str]) -> str:
    name = profile.get("name") or "I"
    edu = (profile.get("education") or [{}])[0]
    exp = (profile.get("experience") or [{}])[0]
    skills = ", ".join(matched or (profile.get("skills") or [])[:6])
    ql = q.lower()
    if "about yourself" in ql:
        parts = [f"I'm {name}" if profile.get("name") else "I'm a job seeker"]
        if edu.get("degree"):
            parts.append(f"with a {edu['degree']}" + (f" from {edu['school']}" if edu.get("school") else ""))
        s = " ".join(parts) + "."
        if exp.get("title"):
            s += f" I worked as {exp['title']}" + (f" at {exp['company']}" if exp.get("company") else "") + "."
        if skills:
            s += f" My skills include {skills}."
        return s
    if "interested" in ql:
        return (f"The {job.get('title')} role at {job.get('company') or 'your company'} matches what I want to do"
                + (f", and it uses skills I have, such as {skills}" if skills else "") + ".")
    if "skills" in ql:
        return f"Relevant skills: {skills}." if skills else "Please see my résumé for my skills."
    if "salary" in ql:
        return _salary_answer(prefs)
    if "start" in ql or "available" in ql:
        return profile.get("availability") or "I can discuss my start date."
    return ""


async def prepare_answers(job: Dict[str, Any], profile: Dict[str, Any], prefs: Dict[str, Any],
                          evaluation: Dict[str, Any]) -> Dict[str, Any]:
    questions = BASE_QUESTIONS + [q for q in job_questions(job) if q not in BASE_QUESTIONS]
    matched = evaluation.get("components", {}).get("skills", {}).get("matched", [])
    prompt = f"""Prepare honest answers for a job application.

CANDIDATE FACTS (the ONLY facts you may use):
{json.dumps(facts(profile), ensure_ascii=False)[:5000]}

Salary answer to use: {_salary_answer(prefs)}
Availability: {profile.get('availability') or 'not specified - say it can be discussed'}

JOB: {job.get('title')} at {job.get('company')}
{(job.get('description') or '')[:3500]}

Answer each question in first person, 2-4 sentences, professional, specific to this job.
STRICT: mention only skills, tools, duties and experience that appear in the facts above.
Do not add tools (e.g. Git, Docker, documentation) or responsibilities that are not listed.
Do not list what the candidate lacks; focus on what they genuinely have and their eagerness to learn.
Questions:
{json.dumps(questions, ensure_ascii=False)}

Return JSON: {{"answers": {{"<question>": "<answer>", ...}}}}"""
    data = ai.extract_json(await ai.complete(prompt, temperature=0.4))
    got = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    answers, used_ai, removed = {}, False, []
    for q in questions:
        a = got.get(q)
        if isinstance(a, str) and a.strip():
            clean, gone = scrub_claims(a.strip(), profile)
            removed += gone
            if clean:
                answers[q], used_ai = clean, True
                continue
            a = None
        if isinstance(a, str) and a.strip():
            answers[q], used_ai = a.strip(), True
        else:
            fb = _fallback(q, job, profile, prefs, matched)
            if fb:
                answers[q] = fb
    cover = None
    if prefs.get("include_cover_letter"):
        cover = ai.extract_json(await ai.complete(
            f"Write a short, honest cover letter (max 180 words) for {job.get('title')} at {job.get('company')} "
            f"using ONLY these facts: {json.dumps(facts(profile), ensure_ascii=False)[:4000]}. "
            'Return JSON {"cover_letter": "..."}', temperature=0.5)).get("cover_letter")
    return {"answers": answers, "cover_letter": cover, "ai_used": used_ai, "ai_error": None if used_ai else ai.last_error,
            "removed_claims": list(dict.fromkeys(removed))}
