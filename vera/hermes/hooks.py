"""Hermes hooks: what came in, and what the AI should know before it answers.

on_inbound    every Discord message, before Hermes's AI: remember what the person typed (for the go-ahead rule),
              and save a PDF or Word résumé they sent to Vera, under their own account only.
before_turn   once per turn: the live facts (who's asking, where, what's running, plans waiting), so the AI
              never guesses numbers or claims a run started.
"""

import json
import logging
import re
from typing import Any, Dict, Optional

from vera import config
from vera.agents import assistant as A
from vera.agents import rounds
from vera.hermes import identity, runtime

log = logging.getLogger("vera.hermes.hooks")
RESUME_TYPES = (".pdf", ".docx")


def _platform(source) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").lower()


def on_inbound(event=None, **kwargs) -> Optional[Dict[str, Any]]:
    """pre_gateway_dispatch. Never blocks the message: it records, saves résumés on the side, and turns a plain
    "join voice" / "leave voice" into Hermes's /voice command."""
    try:
        source = getattr(event, "source", None)
        if event is None or source is None or _platform(source) != "discord" or getattr(source, "is_bot", False):
            return None
        uid = getattr(source, "user_id", None) or getattr(event, "user_id", None)
        chat = getattr(source, "chat_id", None)
        if not (str(uid or "").isdigit() and str(chat or "").isdigit()):
            return None
        text = getattr(event, "text", "") or ""
        identity.record(int(chat), int(uid), text)
        raw = getattr(event, "raw_message", None)
        rt = runtime.live()
        if raw is not None and rt is not None and rt.ready and _to_vera(raw, rt):
            files = [f for f in getattr(raw, "attachments", []) or [] if f.filename.lower().endswith(RESUME_TYPES)]
            if files:
                rt.spawn(lambda: _save_resume(rt, raw, int(uid), files[0]))
            command = voice_command(text)
            if command:
                log.info(f"'{text[:60]}' -> {command}")
                return {"action": "rewrite", "text": command}
    except Exception:
        log.exception("couldn't look at an incoming message")
    return None


# "Join voice" without the slash command (The owner, 9 Nov 2026). Hermes's own /voice join does the joining: it needs the
# gateway's voice wiring, which a tool can't reach. Only a short, plain request counts, so "how do I join voice?" or
# "join voice and check my mail" still go to the AI as normal messages.
_MENTION = re.compile(r"<@[!&]?\d+>")
_FILLER = re.compile(r"\b(?:hey|hi|ok(?:ay)?|vera|please|pls|plz|now|po|can you|could you|would you|will you|"
                     r"you can|go ahead and|just|quickly|real quick|for me|with me|with us|thanks|thank you|ty)\b")
_VOICE = r"(?:the |my |our |this |a )?(?:voice(?: channel| chat| call| room)?|vc|call)"
_JOIN = re.compile(rf"(?:join(?: me| us)?(?: in| on)?|come(?: in| into| to| on| join)?|hop(?: on| in| into)|"
                   rf"get(?: on| in| into)|jump(?: on| in| into)|connect(?: to)?|enter) {_VOICE}(?: here)?")
_JOIN_ME = re.compile(rf"(?:talk to me|let'?s talk) (?:in|on) {_VOICE}")
_LEAVE = re.compile(rf"(?:leave|exit|quit|disconnect(?: from)?|get out of|drop(?: out of| off)?|hang up(?: the)?) {_VOICE}|hang up")


def voice_command(text: str) -> Optional[str]:
    """'/voice join' or '/voice leave' when the whole message just asks for that, else None."""
    said = _MENTION.sub(" ", text or "").lower().replace("’", "'")
    if said.lstrip().startswith("/") or len(said) > 80:
        return None
    said = re.sub(r"[^a-z' ]+", " ", said)
    said = " ".join(_FILLER.sub(" ", said).split())
    if _JOIN.fullmatch(said) or _JOIN_ME.fullmatch(said):
        return "/voice join"
    if _LEAVE.fullmatch(said):
        return "/voice leave"
    return None


def _to_vera(message, rt) -> bool:
    """A DM, a mention of her or her role, or a reply to her."""
    if getattr(message, "guild", None) is None:
        return True
    me = rt.user
    role = getattr(message.guild, "self_role", None)
    replied = getattr(getattr(message, "reference", None), "resolved", None)
    return (me in (message.mentions or []) or (role is not None and role in (message.role_mentions or []))
            or getattr(replied, "author", None) == me)


async def _save_resume(rt, message, uid: int, file) -> None:
    try:
        note = await rt.save_sent_resume(uid, file, in_private=message.guild is None)
        await message.reply(note, mention_author=False)
    except Exception:
        log.exception("couldn't save a résumé")


def before_turn(session_id: str = "", user_message: str = "", platform: str = "", **kwargs) -> Optional[Dict[str, str]]:
    """pre_llm_call: live facts for this person and place."""
    try:
        asker = identity.current()
    except identity.NotAllowed:
        return None
    except Exception:
        log.exception("couldn't work out who's asking")
        return None
    if asker.platform == "local":
        identity.record(None, asker.uid, user_message or "")      # at the laptop, the go-ahead is what he typed here
    try:
        return {"context": facts(asker)}
    except Exception:
        log.exception("couldn't gather the live facts")
        return None


def _frei_rules() -> list:
    """Rules the owner approved for Vera through Frei, his manager agent (read-only from Frei's database)."""
    try:
        from vera.hermes import frei_link
        return frei_link.rules()[-20:]
    except Exception:
        log.exception("couldn't read Frei's rules")
        return []


def facts(asker: identity.Asker) -> str:
    from vera.hermes.tools import vera
    session = vera().user(asker.uid)
    pr, pf = session.profile, session.prefs
    owner = rounds.is_owner(asker.uid)
    board = config.CHANNEL_SITES.get(asker.channel_name)
    place = ("The owner's laptop (Hermes terminal)" if asker.platform == "local" else
             "a task from Frei (The owner's manager agent) that the owner approved: read and draft only, then end with a "
             "short plain summary of the result" if asker.platform == "kanban" else "a DM" if asker.is_dm
             else f"#{asker.channel_name}" + (f", the {', '.join(board)} channel" if board else ""))
    rt = runtime.live()
    private_here = asker.is_dm or asker.platform == "local"
    if not private_here and owner and rt is not None and rt.ready and asker.chat_id:
        from vera.bot import private      # needs discord.py, which is loaded once connected
        ch = rt.get_channel(asker.chat_id)
        private_here = bool(ch) and private.is_owner_place(ch, config.OWNER_ID, rt.user.id)
    plan = session.db.get_json(A._plan_key(session, asker.scope))
    drafts = session.db.get_json(f"outreach_pending:{asker.uid}")
    drafts = drafts.get("items") if isinstance(drafts, dict) else drafts
    said, _ = identity.last_said(asker.scope, asker.uid)
    data = {
        "talking_to": "the owner" if owner else f"server member <@{asker.uid}> (not the owner: their own résumé and details only, company forms only, never the owner's accounts)",
        "where": place, "private_place": private_here,
        "email_allowed_here": owner and asker.channel_name in A.email_channels(),
        "reply_language": pf.get("language") or "English",
        "right_now": A._live_facts(session),
        "plan_waiting_here": bool(plan), "email_drafts_waiting": len(drafts or []),
        "their_last_message_is_a_go_ahead": bool(said) and A.plain_yes(said),
        "still_needed_to_apply": rounds.blockers(rounds.build_profile(session)),
        "owners_rules_follow_them_everywhere": [r["rule"] for r in session.db.get_json("owner_rules") or []],
        **({"rules_owner_approved_through_frei": _frei_rules()} if owner else {}),
        **({"what_you_learned_from_the_owner": [x["lesson"] for x in session.db.get_json("owner_notes") or []]} if owner else {}),
        "candidate": {"name": pr.get("name"), "headline": pr.get("headline"), "years_experience": pr.get("years_experience"),
                      "skills": (pr.get("skills") or [])[:20], "experience": [f"{x.get('title')} at {x.get('company')}" for x in (pr.get("experience") or [])[:4]]},
        "wants": {"positions": pf.get("positions"), "levels": pf.get("job_levels"), "setup": pf.get("work_setup"),
                  "max_experience_years": pf.get("reject_mandatory_experience_over")},
    }
    from vera.hermes import tools
    if owner:
        # 9 Nov 2026: he moved a conversation from #vera-job-mail to #vera-owner-email, and it arrived with nothing.
        carried = tools.handoff_here(asker)
        if carried:
            data["carried_over_here"] = carried
        # In the email channels she knows the replies that came in (who and what, never the email's text), so she never
        # says "no replies" while employers have written. The posts in the channel are Vera's own, so the AI can't see them.
        if data["email_allowed_here"]:
            try:
                from vera.agents import jobmail
                data["employer_responses_last_14_days"] = jobmail.brief(jobmail.responses(session, 14), limit=12)
            except Exception:
                log.exception("couldn't read the job mail log")
    return "VERA LIVE FACTS (from her own records; trust these over memory):\n" + json.dumps(data, ensure_ascii=False, default=str)[:7000]
