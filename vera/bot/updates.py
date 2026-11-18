"""Telling the owner what changed: new versions of Vera, and rules she picks up in chat.

Vera doesn't rewrite her own code. "Learning" here means saved answers and preferences. Everything else
comes from a person updating her, and each update adds a note below. Notes post once, the next time she starts.
"""

import logging
from typing import List, Tuple

log = logging.getLogger("vera.updates")

KEY_CHANNEL, KEY_LAST = "updates_channel", "updates_last_note"

# Add new notes at the end, with the next number.
NOTES: List[Tuple[int, str]] = [
    (1, "**Hello, I'm Vera.** I find jobs that fit you from free job APIs, score them against your real profile, and prepare "
        "applications. Nothing is sent until you say **proceed**."),
    (2, "**Application rounds:** ask me to apply and I show the plan first: where, how many, which titles and which résumé. "
        "Say **proceed** to send, or **test** to fill forms without sending. Nothing runs on a schedule."),
    (3, "**Company forms only:** I fill application forms on Greenhouse, Lever, Ashby and Workable. I never sign in to job "
        "boards, and I never bypass bot checks or CAPTCHAs: a site that shows one is left alone for a while."),
    (4, "**Job mail:** say *any replies?* and I post employers' real replies (interviews, assessments, rejections, offers) in "
        "your private email channel. I only read your mail, and never answer or delete it on my own."),
    (5, "**Email with approval:** cold emails and replies to employers are shown in full first and sent only after your **proceed**."),
    (6, "**I learn from you, privately:** when you correct me or tell me how you like things, I save it and show it in your "
        "private updates channel. Other members never see it."),
    (7, "**Only your server:** I leave any server that isn't yours, and I never kick, ban or change anyone's roles."),
    (8, "**Voice:** get in a voice channel, then tag me with *join vc*. *Leave vc* or *hang up* ends it."),
]


def unposted(last: int) -> List[str]:
    return [text for number, text in NOTES if number > last]


async def post_new(bot) -> int:
    """Post notes she hasn't posted yet. Safe to run at every start and reconnect."""
    db = bot.vera.db
    last = int(db.get_json(KEY_LAST) or 0)
    notes = unposted(last)
    if not notes:
        return 0
    target = await bot.updates_target()
    if target is None:
        return 0
    from vera.bot.core import split_message
    header = "🆕 **What's new in Vera**\n"
    for part in split_message(header + "\n".join(f"• {n}" for n in notes)):
        await target.send(part)
    db.set_json(KEY_LAST, NOTES[-1][0])
    return len(notes)
