"""'Join voice' without the slash command: a plain request becomes Hermes's /voice command."""

from types import SimpleNamespace

import pytest

from vera.hermes import hooks


@pytest.mark.parametrize("text", [
    "join voice", "<@123> join vc", "Vera, can you join the voice channel?", "join me in vc please",
    "hop on vc", "come to voice", "get in the call", "pls join my voice chat po", "talk to me in voice",
    "connect to voice", "Join VC!",
])
def test_plain_join_requests_become_voice_join(text):
    assert hooks.voice_command(text) == "/voice join"


@pytest.mark.parametrize("text", ["leave voice", "<@123> leave the vc", "disconnect from voice", "hang up",
                                  "can you leave the call now", "get out of vc"])
def test_plain_leave_requests_become_voice_leave(text):
    assert hooks.voice_command(text) == "/voice leave"


@pytest.mark.parametrize("text", [
    "how do I join voice?", "join voice and check my mail", "how to use vera voice channel",
    "/voice join", "join", "voice", "can you join the interview call with Acme on Monday",
    "what's the voice you use", "leave", "join 5 jobs", "",
])
def test_anything_else_is_left_alone(text):
    assert hooks.voice_command(text) is None


def _event(text, raw):
    source = SimpleNamespace(platform=SimpleNamespace(value="discord"), user_id="100000000000000014",
                             chat_id="100000000000000010", is_bot=False)
    return SimpleNamespace(text=text, raw_message=raw, source=source)


def test_only_messages_to_vera_are_rewritten(monkeypatch, tmp_path):
    me, other = object(), object()
    rt = SimpleNamespace(ready=True, user=me, spawn=lambda make: None)
    monkeypatch.setattr(hooks.runtime, "live", lambda: rt)
    monkeypatch.setattr(hooks.identity, "record", lambda *a: None)
    guild = SimpleNamespace(self_role=None)
    to_her = SimpleNamespace(guild=guild, mentions=[me], role_mentions=[], reference=None, attachments=[])
    to_someone_else = SimpleNamespace(guild=guild, mentions=[other], role_mentions=[], reference=None, attachments=[])

    assert hooks.on_inbound(event=_event("join vc", to_her)) == {"action": "rewrite", "text": "/voice join"}
    assert hooks.on_inbound(event=_event("join vc", to_someone_else)) is None
    assert hooks.on_inbound(event=_event("how do I join vc?", to_her)) is None
