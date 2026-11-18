"""A cover letter written for each job, using only facts from the profile.

Nobody reads these before they go out, so the AI's text goes through a fact
check: any sentence naming a technology the profile doesn't list, or claiming a
number of years, is removed. If too little survives, or the AI is down, an honest
template is used. Every letter is saved in agent_data/cover_letters.
"""

import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple
from xml.sax.saxutils import escape

from vera.apply import config, llm

TECH = [
    "python", "java", "javascript", "typescript", "php", "ruby", "golang", "rust", "kotlin", "swift", "scala",
    "c#", "c++", ".net", "asp.net", "node", "node.js", "express", "next.js", "nuxt", "react", "react.js", "angular",
    "vue", "svelte", "django", "flask", "fastapi", "spring", "laravel", "rails", "wordpress", "shopify", "tailwind",
    "bootstrap", "mysql", "postgres", "postgresql", "mongodb", "redis", "sql server", "oracle", "firebase", "graphql",
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform", "jenkins", "git", "github", "gitlab", "jira", "linux",
    "selenium", "cypress", "playwright", "jest", "pytest", "figma", "unity", "unreal", "flutter", "react native",
    "android", "ios", "machine learning", "pytorch", "tensorflow", "salesforce", "sap", "power bi", "tableau", "excel",
]
MIN_WORDS = 60
# In testing, NVIDIA's models added duties that weren't in the profile, which the fact check
# can't catch. If Gemini is unavailable, the honest template is sent instead.
LETTER_PROVIDERS = ["gemini"]


def _names_tech(sentence: str, tech: str) -> bool:
    return re.search(rf"(?<![\w+#.]){re.escape(tech)}(?![\w+#])", sentence, re.I) is not None


def _known_text(profile: Dict[str, Any]) -> str:
    parts = [profile.get("summary"), profile.get("education"), json.dumps(profile.get("skills") or []),
             json.dumps(profile.get("experience") or []), json.dumps(profile.get("certifications") or []),
             profile.get("github")]
    return " ".join(str(p) for p in parts if p).lower()


def scrub(text: str, profile: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Drop sentences with claims the profile can't back. Returns (clean text, removed items)."""
    known = _known_text(profile)
    kept, removed = [], []
    for line in text.splitlines():
        good = []
        for sentence in re.split(r"(?<=[.!?])\s+", line.strip()):
            bad = [t for t in TECH if _names_tech(sentence, t) and not _names_tech(known, t)]
            if re.search(r"\b\d+\+?\s*(years?|yrs?)\b", sentence, re.I) and "years" not in known:
                bad.append("a claim about years")
            if bad:
                removed += bad
            elif sentence:
                good.append(sentence)
        kept.append(" ".join(good))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip(), list(dict.fromkeys(removed))


def template(profile: Dict[str, Any], job: Dict[str, str]) -> str:
    """Used when the AI is down. Contains only profile facts."""
    title, company = job.get("title") or "this position", job.get("company") or "your company"
    posting = (job.get("description") or "").lower()
    skills = [s for s in profile.get("skills") or [] if re.split(r"[ (]", s)[0].lower() in posting]
    skills = skills or (profile.get("skills") or [])[:5]
    body = [f"I am applying for the {title} role at {company}."]
    if profile.get("education"):
        body.append(f"My education: {profile['education']}.")
    jobs = profile.get("experience") or []
    if jobs:
        body.append(f"Most recently I worked as {jobs[0].get('title')} at {jobs[0].get('company')}"
                    + (f" ({jobs[0]['dates']})" if jobs[0].get("dates") else "") + ".")
    if skills:
        body.append(f"Skills relevant to this role include {', '.join(skills[:6])}.")
    body.append("My résumé is attached, and I would welcome the chance to discuss how I can contribute to your team.")
    return " ".join(body)


def _finish(body: str, profile: Dict[str, Any], job: Dict[str, str]) -> str:
    company = job.get("company") or "the company"
    name = config.full_name(profile)
    contact = " | ".join(str(profile[k]) for k in ("email", "phone") if profile.get(k))
    return f"Dear Hiring Team at {company},\n\n{body}\n\nSincerely,\n{name}" + (f"\n{contact}" if contact else "")


async def build(profile: Dict[str, Any], job: Dict[str, str]) -> str:
    """Write, fact-check and save the letter for this job."""
    facts = {k: profile.get(k) for k in ("summary", "education", "skills", "experience", "certifications",
                                         "achievements", "years_experience", "github") if profile.get(k)}
    answer = await llm.complete(
        f"""Write the body of a cover letter: 110-170 words, first person, natural and specific, no greeting and no sign-off.
Connect the candidate's real background to this job. Mention only skills, tools and duties in the candidate facts.
Describe past work only in the words of its "details". A skill being listed does not mean it was used at a particular
job, so never say what was done with a skill at an employer unless "details" says so.
Lead with the candidate's most recent job if they have one. Name only the 3 to 5 skills most relevant to this job; never list them all.
If the job asks for something the candidate lacks, leave it out. Do not state a number of years of experience.

Candidate facts (the only facts you may use):
{json.dumps(facts, ensure_ascii=False)[:4000]}

Job: {job.get('title', '')} at {job.get('company', '')}
<job_posting>
{(job.get('description') or '')[:3500]}
</job_posting>""", temperature=0.4, max_tokens=1500, only=LETTER_PROVIDERS)
    body, _removed = scrub(answer or "", profile)
    # A letter must end on a finished sentence.
    if len(body.split()) < MIN_WORDS or not re.search(r"[.!?][\"')\]]?$", body):
        body = template(profile, job)
    letter = _finish(body, profile, job)
    save_text(letter, job)
    return letter


def _base(job: Dict[str, str]) -> Path:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{job.get('company', '')}_{job.get('title', '')}").strip("_")[:70] or "job"
    folder = config.DATA_DIR / "cover_letters"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{datetime.now():%Y%m%d}_{safe}"


def save_text(letter: str, job: Dict[str, str]) -> Path:
    path = _base(job).with_suffix(".txt")
    path.write_text(letter, encoding="utf-8")
    return path


def save_docx(letter: str, job: Dict[str, str]) -> Path:
    """Forms that only take a file get a Word document."""
    path = _base(job).with_suffix(".docx")
    paragraphs = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>' for line in letter.splitlines())
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    pkg = "http://schemas.openxmlformats.org/package/2006"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="{pkg}/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{pkg}/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml",
                   f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>{paragraphs}</w:body></w:document>')
    return path
