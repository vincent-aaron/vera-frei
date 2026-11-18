"""9 Nov 2026: The owner gave Vera's role Administrator, but she must never kick, ban or change members' roles without him.
She has no tool for it: on Discord she only gets her own tools, and Hermes's Discord admin tools stay switched off."""

import re
from pathlib import Path

from vera.hermes import tools

REPO = Path(__file__).resolve().parents[1]
# Public repo layout: hermes-profile/config.example.yaml; live layout: profiles/vera/config.yaml.
CONFIG = next(p for p in (REPO / "hermes-profile" / "config.example.yaml", REPO.parent / "config.yaml") if p.exists())


def test_none_of_her_tools_can_moderate():
    names = [schema["name"] for _, schema in tools.TOOLS]
    assert not [n for n in names if re.search(r"kick|ban|timeout|mute|role|moderat|permission", n)]


def test_discord_gets_only_her_tools_and_hermes_admin_tools_stay_off():
    text = CONFIG.read_text(encoding="utf-8")
    toolsets = re.search(r"^platform_toolsets:\s*\n\s+discord:\s*\n((?:\s+- .+\n)+)", text, re.M)
    assert toolsets and re.findall(r"- (\S+)", toolsets.group(1)) == ["vera"]
    disabled = re.search(r"^\s+disabled_toolsets:\s*\n((?:\s+- .+\n)+)", text, re.M)
    assert disabled and {"discord", "discord_admin"} <= set(re.findall(r"- (\S+)", disabled.group(1)))


def test_her_code_never_kicks_or_bans():
    code = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "vera").rglob("*.py"))
    assert not re.search(r"\.(kick|ban|unban|timeout|add_roles|remove_roles)\(", code)


def test_her_discord_connection_refuses_kicks_bans_and_role_changes():
    import asyncio
    from types import SimpleNamespace
    import pytest
    from vera.hermes import discord_runtime as D
    calls = []

    class HTTP:
        def __getattr__(self, name):
            async def real(*a, **k):
                calls.append(name)
            return real
    http = HTTP()
    for name in D.MODERATION_CALLS + ("edit_member", "send_message"):
        setattr(http, name, getattr(http, name))
    bot = SimpleNamespace(http=http, user=SimpleNamespace(id=999))
    D.lock_moderation(bot)
    for name in D.MODERATION_CALLS:
        with pytest.raises(D.ModerationBlocked):
            asyncio.run(getattr(http, name)(1, 2))
    with pytest.raises(D.ModerationBlocked):
        asyncio.run(http.edit_member(1, 123, communication_disabled_until="x"))      # a timeout on a member
    asyncio.run(http.edit_member(1, 999, nick="Vera"))                             # her own nickname still works
    asyncio.run(http.send_message(1, params={}))                                    # and so does everything else
    assert calls == ["edit_member", "send_message"]
