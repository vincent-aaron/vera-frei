"""What's new posts, new rule posts, and what she says about herself."""

import asyncio
from types import SimpleNamespace

import pytest

from vera import config
from vera.agents import assistant
from vera.bot import guide, updates
from vera.bot.main import VeraBot
from vera.db import DB
from vera.services import Vera

OWNER, GUEST, BOT = 111, 222, 999


@pytest.fixture
def vera(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESUME_DIR", tmp_path / "resumes")
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.setattr(config, key, "")
    return Vera(DB(tmp_path / "t.db"))


class Sink:
    def __init__(self):
        self.sent = []
        self.guild = None

    async def send(self, text, **_k):
        self.sent.append(text)


def bot(vera, target):
    async def updates_target():
        return target
    return SimpleNamespace(vera=vera, updates_target=updates_target, user=SimpleNamespace(id=BOT))


def say(session, text):
    return asyncio.run(assistant.handle(session, text))


def test_notes_post_once(vera):
    target = Sink()
    b = bot(vera, target)
    assert asyncio.run(updates.post_new(b)) == len(updates.NOTES)
    assert "What's new" in target.sent[0]
    sent = len(target.sent)
    assert asyncio.run(updates.post_new(b)) == 0 and len(target.sent) == sent


def test_only_newer_notes_post_after_an_update(vera, monkeypatch):
    target = Sink()
    b = bot(vera, target)
    asyncio.run(updates.post_new(b))
    monkeypatch.setattr(updates, "NOTES", updates.NOTES + [(999, "Brand new thing")])
    asyncio.run(updates.post_new(b))
    assert "Brand new thing" in target.sent[-1] and "No repeats" not in target.sent[-1]


def test_nothing_is_lost_if_there_is_nowhere_to_post(vera):
    asyncio.run(updates.post_new(bot(vera, None)))
    assert vera.db.get_json(updates.KEY_LAST) is None


def test_notes_are_numbered_in_order():
    numbers = [n for n, _t in updates.NOTES]
    assert numbers == sorted(set(numbers))


def test_your_exact_message_sets_this_channel(vera):
    r = say(vera.user(OWNER), "eveytime you self improve or a new rule is added post it here")
    assert r["action"] == "set_updates_channel" and "post updates here" in r["reply"] and "don't improve myself" in r["reply"]


def test_a_member_cant_move_your_updates(vera):
    assert say(vera.user(GUEST), "post your updates here")["action"] == "none"


@pytest.mark.parametrize("text", ["post the job here", "send my resume here", "apply to 5 jobs", "what's new?", "send my applications"])
def test_other_messages_arent_mistaken_for_it(vera, text):
    assert say(vera.user(OWNER), text)["action"] != "set_updates_channel"


def private_target():
    target = Sink()
    return target


def public_target():
    target = Sink()
    target.guild = SimpleNamespace(default_role=object())
    target.category, target.overwrites = SimpleNamespace(name="vera"), {}
    target.permissions_for = lambda _r: SimpleNamespace(view_channel=True)
    return target


def test_new_rules_show_details_privately_but_not_in_public(vera):
    patch = {"work_setup": ["Remote"]}
    private, public = private_target(), public_target()
    asyncio.run(VeraBot.note_new_rules(bot(vera, private), {"patch": patch}))
    asyncio.run(VeraBot.note_new_rules(bot(vera, public), {"patch": {"salary": {"min": 28000}}}))
    assert "Remote" in private.sent[0] and "New rule" in private.sent[0]
    assert "salary" in public.sent[0] and "28000" not in public.sent[0]


def test_nothing_new_posts_nothing(vera):
    target = Sink()
    asyncio.run(VeraBot.note_new_rules(bot(vera, target), {"patch": {}}))
    assert not target.sent


def test_a_saved_answer_is_noted_without_the_answer(vera):
    me = vera.user(OWNER)
    me.db.set_json(f"open_questions:{OWNER}", ["Expected salary?"])
    r = say(me, "1: 28000")
    assert "Expected salary" in r["learned"] and "28000" not in r["learned"]


def test_she_is_told_the_truth_about_herself_and_to_match_your_language(vera, monkeypatch):
    seen = []

    async def fake_ai(prompt, **_k):
        seen.append(prompt)
        return "ok"
    monkeypatch.setattr(assistant.ai, "complete", fake_ai)
    asyncio.run(assistant.chat(vera.user(OWNER), "does hermes self improvement work?"))
    assert "NOT built on Hermes" in seen[0] and "Reply in English" in seen[0] and "every channel and DM" in seen[0] and "can't rewrite her own code" in seen[0]


def test_the_guide_says_what_to_do_when_she_is_off():
    assert "No green dot" in guide.MESSAGES[0] and "contact the owner" in guide.MESSAGES[0]
    assert len(guide.MESSAGES[0]) <= 2000


def test_your_exact_message_sets_english_everywhere(vera):
    me = vera.user(OWNER)
    r = say(me, "only response in english as default would this rule be added to other channels as well")
    assert me.prefs["language"] == "English" and "every channel" in r["reply"] and r["learned"]


@pytest.mark.parametrize("text,language", [("reply in tagalog", "Tagalog"), ("speak taglish please", "Taglish"), ("answer in filipino", "Tagalog"), ("english only", "English"), ("respond in english from now on", "English")])
def test_changing_the_language(vera, text, language):
    me = vera.user(OWNER)
    say(me, text)
    assert me.prefs["language"] == language


@pytest.mark.parametrize("text", ["I want jobs that use english", "write my cover letter in tagalog", "is your english okay", "hello"])
def test_other_messages_dont_change_the_language(vera, text):
    assert assistant.language_request(text) is None


def test_the_language_is_used_in_chat_and_is_per_person(vera, monkeypatch):
    seen = []

    async def fake_ai(prompt, **_k):
        seen.append(prompt)
        return "ok"
    monkeypatch.setattr(assistant.ai, "complete", fake_ai)
    guest = vera.user(GUEST)
    say(guest, "reply in tagalog")
    asyncio.run(assistant.chat(guest, "hello"))
    asyncio.run(assistant.chat(vera.user(OWNER), "kumusta"))
    assert "Reply in Tagalog" in seen[0] and "Reply in English" in seen[1]
