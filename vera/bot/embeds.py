"""Discord embeds (cards) for jobs, evaluations, applications and stats."""

from datetime import datetime, timezone
from typing import Any, Dict, List

import discord

from vera.agents.evaluator import salary_text, where
from vera.preferences import location_label
from vera.sources import LABELS

GREEN, AMBER, RED, BLUE, GREY = 0x2ECC71, 0xF1C40F, 0xE74C3C, 0x5865F2, 0x95A5A6
STATUS_ICON = {"preparing": "⏳", "ready": "📝", "submitted": "📨", "applied": "✅", "interview": "🗓️",
               "offer": "🎉", "rejected": "❌", "withdrawn": "↩️", "failed": "⚠️"}


def clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def bar(pct: int) -> str:
    full = round(pct / 10)
    return "█" * full + "░" * (10 - full) + f" {pct}%"


def color(score: int) -> int:
    return GREEN if score >= 75 else AMBER if score >= 55 else RED


def experience_text(ev: Dict[str, Any]) -> str:
    e = ev["components"]["experience"]
    parts = []
    if e.get("mandatory_years") is not None:
        parts.append(f"Required: {e['mandatory_years']:g}+ yrs")
    if e.get("preferred_years") is not None:
        parts.append(f"Preferred: {e['preferred_years']:g}+ yrs")
    return " · ".join(parts) or "Not stated"


def job_card(job: Dict[str, Any], ev: Dict[str, Any], summary: str = None) -> discord.Embed:
    """Recommendation card (spec section 6)."""
    e = discord.Embed(title=clip(job["title"], 240), url=job["url"], color=color(ev["score"]),
                      description=clip(summary or job.get("description", ""), 350))
    e.add_field(name="Company", value=clip(job.get("company") or "—", 100))
    e.add_field(name="Location", value=clip(where(job, ev), 100))
    e.add_field(name="Salary", value=clip(salary_text(job), 100))
    e.add_field(name="Employment type", value=job.get("employment_type") or "Not stated")
    e.add_field(name="Experience", value=experience_text(ev))
    level = ev["components"]["level"].get("detected")
    e.add_field(name="Level", value=level or "Not stated")
    sk = ev["components"]["skills"]
    quals = []
    if sk.get("matched"):
        quals.append("✅ " + ", ".join(sk["matched"][:8]))
    if sk.get("missing"):
        quals.append("➖ " + ", ".join(sk["missing"][:8]))
    quals.append("🎓 " + ev["components"]["education"]["note"])
    e.add_field(name="Qualifications", value=clip("\n".join(quals), 1000), inline=False)
    e.add_field(name="Match", value=f"`{bar(ev['score'])}`", inline=False)
    avail = {True: " · ✔ still open", False: " · closed", None: ""}.get(ev.get("available"))
    from vera.agents.scout import days_since_posted
    age = days_since_posted(job)
    posted = "" if age is None else " · posted today" if age < 1 else f" · posted {int(age)} day{'s' if int(age) != 1 else ''} ago"
    e.set_footer(text=f"Source: {LABELS.get(job['source'], job['source'])}{posted}{avail} · Apply link goes to the original listing")
    return e


def details_card(job: Dict[str, Any], ev: Dict[str, Any], summary: str) -> discord.Embed:
    """Full evaluation (spec section 5)."""
    e = discord.Embed(title=f"{clip(job['title'], 200)} — {ev['score']}% match", url=job["url"], color=color(ev["score"]),
                      description=clip(summary, 1500))
    names = {"role": "Position fit", "level": "Job level", "experience": "Experience", "skills": "Skills",
             "education": "Education", "location": "Location", "salary": "Salary", "employment": "Employment type"}
    lines = [f"`{c['score']:>3}%` **{names[k]}**: {clip(c['note'], 120)}" for k, c in ev["components"].items()]
    e.add_field(name="Breakdown", value=clip("\n".join(lines), 1024), inline=False)
    if ev["pros"]:
        e.add_field(name="Why it's recommended", value=clip("\n".join("• " + p for p in ev["pros"]), 1024), inline=False)
    if ev["cons"]:
        e.add_field(name="Concerns", value=clip("\n".join("• " + c for c in ev["cons"]), 1024), inline=False)
    e.add_field(name="Missing qualifications",
                value=clip("\n".join("• " + m for m in ev["missing"]) or "None found", 1024), inline=False)
    e.set_footer(text=f"Source: {LABELS.get(job['source'], job['source'])} · scores use only your /profile and /preferences")
    return e


def application_card(prep: Dict[str, Any]) -> discord.Embed:
    """Prepared application shown for approval (spec 7.7)."""
    job, ev = prep["job"], prep["evaluation"]
    e = discord.Embed(title=f"📝 Ready for your approval: {clip(job['title'], 180)}", url=job["url"], color=BLUE,
                      description=f"**{job.get('company') or '—'}** · {where(job, ev)} · {ev['score']}% match\n"
                                  "Nothing is submitted until you press **Approve & submit**.")
    r = prep.get("resume")
    e.add_field(name="Résumé", value=(r["name"] if r else "⚠️ none uploaded (use /resume add)"), inline=True)
    e.add_field(name="Cover letter", value="Included" if prep.get("cover_letter") else "Not included", inline=True)
    how = "Submitted by the browser agent on this PC" if prep.get("auto_submit") else "You submit on the site (link above); Vera records it"
    e.add_field(name="How it's sent", value=how, inline=True)
    if ev["missing"]:
        e.add_field(name="Requirements you may not meet", value=clip("\n".join("• " + m for m in ev["missing"][:6]), 1024), inline=False)
    for q, a in list(prep["answers"].items())[:8]:
        e.add_field(name=clip(q, 250), value=clip(a, 1024), inline=False)
    if prep.get("cover_letter"):
        e.add_field(name="Cover letter", value=clip(prep["cover_letter"], 1024), inline=False)
    if prep.get("removed_claims"):
        e.add_field(name="🛡️ Fact-check", value=clip("Removed sentences claiming things not in your profile: "
                                                      + ", ".join(prep["removed_claims"]) + ". Add them to `/profile` if they're true.", 1024),
                    inline=False)
    note = "Answers written with AI from your profile." if prep.get("ai_used") else \
        f"AI unavailable ({prep.get('ai_error') or 'unknown'}), so these are template answers from your profile."
    e.set_footer(text=f"Application #{prep['application_id']} · {note}")
    return e


def application_row(a: Dict[str, Any]) -> str:
    date = (a.get("applied_at") or a.get("created_at") or "")[:10]
    extra = f" · interview {a['interview_at'][:16].replace('T', ' ')} UTC" if a.get("interview_at") else ""
    return f"{STATUS_ICON.get(a['status'], '•')} `#{a['id']}` **{clip(a['position'] or '', 60)}** — {clip(a['company'] or '', 40)} · {a['status']} · {date}{extra}"


def application_detail(a: Dict[str, Any]) -> discord.Embed:
    e = discord.Embed(title=f"{STATUS_ICON.get(a['status'], '')} #{a['id']} {clip(a['position'] or '', 200)}", url=a.get("url") or None,
                      color=BLUE)
    e.add_field(name="Company", value=a.get("company") or "—")
    e.add_field(name="Status", value=a["status"])
    e.add_field(name="Source", value=LABELS.get(a.get("source"), a.get("source") or "—"))
    e.add_field(name="Applied", value=(a.get("applied_at") or "not yet")[:10])
    e.add_field(name="Interview", value=(a.get("interview_at") or "—")[:16].replace("T", " ") + (" UTC" if a.get("interview_at") else ""))
    e.add_field(name="Résumé", value=a.get("resume") or "—")
    if a.get("notes"):
        e.add_field(name="Notes", value=clip(a["notes"], 1024), inline=False)
    return e


def prefs_card(p: Dict[str, Any]) -> discord.Embed:
    s = p["salary"]
    money = lambda v: f"{v:,}" if isinstance(v, (int, float)) else "—"
    ex = p["exclude"]
    rq = p["requirements"]
    e = discord.Embed(title="⚙️ Job-search preferences", color=BLUE,
                      description="Use the buttons to change anything. New settings apply to the next search.")
    e.add_field(name="Job levels", value=", ".join(p["job_levels"] + p["custom_levels"]) or "Any", inline=False)
    e.add_field(name="Experience", value=", ".join(p["experience"]) +
                (f"\nCustom max: {p['custom_experience_max_years']} yrs" if p.get("custom_experience_max_years") is not None else "") +
                (f"\nReject if a job REQUIRES more than {p['reject_mandatory_experience_over']} yrs" if p.get("reject_mandatory_experience_over") is not None else ""),
                inline=False)
    e.add_field(name="Positions" + (" (only these)" if p.get("only_listed_positions") else " (and similar)"),
                value=clip(", ".join(p["positions"]) or "Any", 1024), inline=False)
    e.add_field(name="Locations", value=clip("\n".join(location_label(l) for l in p["locations"]) or "Anywhere", 500))
    e.add_field(name="Work setup", value=", ".join(p["work_setup"]) + ("\nWilling to relocate" if p["willing_to_relocate"] else ""))
    e.add_field(name="Salary", value=f"{s['currency']} {money(s['min'])} – {money(s['max'])} / {s['period']}\n"
                                     f"Negotiable: {'yes' if s['negotiable'] else 'no'} · Undisclosed OK: {'yes' if s['accept_undisclosed'] else 'no'}")
    e.add_field(name="Employment types", value=", ".join(p["employment_types"]) or "Any")
    excl = [f"{k}: {', '.join(v)}" for k, v in ex.items() if v]
    e.add_field(name="Excluded", value=clip("\n".join(excl) or "Nothing", 1024), inline=False)
    reqs = [f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in rq.items() if v]
    e.add_field(name="Additional requirements", value=clip("\n".join(reqs) or "None", 1024), inline=False)
    digest = p["daily_digest"]
    e.add_field(name="Search", value=f"Min match {p['min_match']}% · {p['results_per_search']} per search · "
                                     f"Only jobs posted within {p['max_age_days']} days · "
                                     f"Daily digest: {('%d jobs at %02d:00' % (p['digest_count'], digest['hour'])) if digest['enabled'] else 'off'} · "
                                     f"Follow-up after {p['followup_after_days']} days · Cover letter: {'on' if p['include_cover_letter'] else 'off'}",
                inline=False)
    e.add_field(name="Sources", value=", ".join(LABELS.get(s_, s_) for s_ in p["sources"]) + " (+ jobs you add with /add-job)", inline=False)
    return e


def _mask(value: str, kind: str) -> str:
    if not value:
        return ""
    if kind == "email" and "@" in value:
        name, _, dom = value.partition("@")
        return f"{name[:2]}•••@{dom}"
    return value[:4] + "•••" + value[-2:] if len(value) > 6 else "•••"


def profile_card(pr: Dict[str, Any], private: bool = True) -> discord.Embed:
    """private=False (public channel): email and phone are partly hidden."""
    e = discord.Embed(title=f"👤 {pr.get('name') or 'Your profile'}", color=BLUE,
                      description=clip(pr.get("headline") or pr.get("summary") or "Add a summary with the buttons below.", 500))
    email = pr.get("email") if private else _mask(pr.get("email"), "email")
    phone = pr.get("phone") if private else _mask(pr.get("phone"), "phone")
    e.add_field(name="Contact", value=clip("\n".join(x for x in (email, phone, pr.get("location")) if x) or "—", 300))
    e.add_field(name="Experience", value=f"{pr.get('years_experience') or 0} yrs\n" + clip(
        "\n".join(f"{x.get('title')} @ {x.get('company')}" for x in pr.get("experience") or []) or "—", 400))
    e.add_field(name="Education", value=clip("\n".join(" · ".join(v for v in (x.get("degree"), x.get("school"), x.get("year"), x.get("honors")) if v)
                                                        for x in pr.get("education") or []) or "—", 500), inline=False)
    e.add_field(name="Skills", value=clip(", ".join(pr.get("skills") or []) or "—", 1024), inline=False)
    if pr.get("certifications") or pr.get("achievements"):
        e.add_field(name="Certifications & achievements", value=clip(", ".join((pr.get("certifications") or []) + (pr.get("achievements") or [])), 1024), inline=False)
    e.add_field(name="Résumés", value=clip("\n".join(f"📄 {r['name']} ({', '.join(r.get('tags', [])) or 'no tags'})" for r in pr.get("resumes") or []) or "None. Add one with /resume add", 1024), inline=False)
    e.set_footer(text="Vera only uses these facts. It never invents qualifications.")
    return e


def stats_card(s: Dict[str, Any]) -> discord.Embed:
    e = discord.Embed(title="📊 Job-search statistics", color=BLUE)
    e.add_field(name="Applications sent", value=f"{s['sent']} total\n{s['this_week']} this week · {s['this_month']} this month")
    e.add_field(name="Response rate", value=f"{s['response_rate']}%\nInterview rate {s['interview_rate']}%")
    e.add_field(name="Avg match (applied)", value=f"{s['avg_match_applied']}%" if s["avg_match_applied"] else "—")
    e.add_field(name="By status", value="\n".join(f"{STATUS_ICON.get(k, '•')} {k}: {v}" for k, v in sorted(s["by_status"].items())) or "—")
    e.add_field(name="By source", value="\n".join(f"{k}: {v}" for k, v in s["by_source"].items()) or "—")
    e.add_field(name="Listings", value=f"{s['jobs_tracked']} tracked · {s['recommended']} recommended\n{s['saved']} saved · {s['skipped']} skipped")
    e.timestamp = datetime.now(timezone.utc)
    return e


def lines_embed(title: str, lines: List[str], empty: str) -> discord.Embed:
    return discord.Embed(title=title, color=BLUE, description=clip("\n".join(lines) or empty, 4000))
