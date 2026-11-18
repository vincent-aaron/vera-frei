"""Vera inside Hermes: the AI decides, but the rules stay in code.

Who is asking comes from the session, never the AI. Runs and email only start after the person's own plain
go-ahead. Boards follow the channel, email only happens in its two channels, and three runs go side by side.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from vera import config
from vera.agents import assistant, rounds
from vera.bot.core import VeraCore
from vera.db import DB
from vera.hermes import discord_runtime, hooks, identity, runtime, tools
from vera.services import Vera

OWNER, GUEST = 111, 222
FORMS, REMOTE, MULTI, OUTREACH_CH, DM = 1001, 1002, 1003, 1004, 1005
NAMES = {FORMS: "vera-company-site-application", REMOTE: "vera-remote", MULTI: "vera-multiboards-application", OUTREACH_CH: "vera-social-outreach"}


@pytest.fixture
def vera(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESUME_DIR", tmp_path / "resumes")
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    monkeypatch.setattr(config, "OUTREACH_EMAIL", "me@gmail.com")
    monkeypatch.setattr(config, "OUTREACH_APP_PASSWORD", "x")
    for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.setattr(config, key, "")
    monkeypatch.setattr(rounds, "_active", [])
    monkeypatch.setattr(rounds, "_waiting", [])
    monkeypatch.setattr(identity, "_said", {})
    v = Vera(DB(tmp_path / "t.db"))
    monkeypatch.setattr(runtime, "_vera", v)
    monkeypatch.setattr(runtime, "_live", None)
    return v


def ready(session, tmp_path):
    cv = tmp_path / f"cv{session.uid}.pdf"
    cv.write_bytes(b"%PDF")
    session.update_profile({"name": "Juan Cruz", "email": "j@example.com", "phone": "0900", "location": "Manila, Philippines",
                            "resumes": [{"name": "CV", "path": str(cv), "tags": []}],
                            "application": {"first_name": "Juan", "expected_salary_monthly": 28000, "notice_period": "2 weeks", "work_authorized": True}})
    session.update_prefs({"positions": ["QA Tester"]})
    return session


def on_discord(monkeypatch, uid, chat, chat_type="group"):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", str(uid))
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", str(chat))
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", chat_type)
    monkeypatch.setenv("HERMES_SESSION_CHAT_NAME", f"Server / #{NAMES.get(chat, 'general')}" if chat_type != "dm" else "someone")
    monkeypatch.delenv("HERMES_SESSION_PARENT_CHAT_ID", raising=False)


def call(fn, **args):
    return json.loads(tools.wrap(fn)(args))


class Channel:
    def __init__(self, cid):
        self.id, self.name, self.sent = cid, NAMES.get(cid, "dm"), []
        self.mention = f"<#{cid}>"
        self.guild = None

    async def send(self, text=None, **kw):
        self.sent.append(text)


class FakeRuntime(VeraCore):
    """VeraCore with a pretend connection. Launched runs are recorded instead of started."""

    def __init__(self, v):
        self.vera, self._channels, self.launched, self.ready = v, {}, [], True
        self.channels = {cid: Channel(cid) for cid in (FORMS, REMOTE, MULTI, OUTREACH_CH, DM)}
        self.user = SimpleNamespace(id=999)
        self.spawned = []

    def run(self, make, timeout=0):
        return asyncio.run(make())

    def spawn(self, make):
        self.spawned.append(make)

    async def fetch_channel(self, cid):
        return self.channels[cid]

    async def fetch_user(self, uid):
        return SimpleNamespace(id=uid)

    def get_channel(self, cid):
        return self.channels.get(cid)

    def _launch(self, item):
        rounds.start_run(item)
        self.launched.append(item)


@pytest.fixture
def rt(vera, monkeypatch):
    fake = FakeRuntime(vera)
    monkeypatch.setattr(runtime, "_live", fake)
    return fake


# ----------------------------------------------------------------- who is asking
def test_the_person_comes_from_the_session(vera, monkeypatch):
    on_discord(monkeypatch, GUEST, FORMS)
    asker = identity.current()
    assert (asker.uid, asker.chat_id, asker.channel_name, asker.is_dm) == (GUEST, FORMS, "vera-company-site-application", False)


def test_the_laptop_is_the_owner_and_other_surfaces_are_refused(vera, monkeypatch):
    for var in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_SOURCE", "HERMES_SESSION_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    assert identity.current().uid == OWNER and identity.current().platform == "local"
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    with pytest.raises(identity.NotAllowed):
        identity.current()
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "")
    with pytest.raises(identity.NotAllowed):
        identity.current()


def test_a_user_named_in_the_arguments_is_ignored(vera, monkeypatch):
    on_discord(monkeypatch, GUEST, FORMS)
    call(tools.preferences, uid=OWNER, user_id=OWNER, changes={"positions": ["Nurse"]})
    assert vera.user(GUEST).prefs["positions"] == ["Nurse"]
    assert vera.user(OWNER).prefs["positions"] != ["Nurse"]


def test_owner_only_tools_refuse_members(vera, rt, monkeypatch):
    on_discord(monkeypatch, GUEST, OUTREACH_CH)
    assert "Only the owner" in call(tools.job_mail, check=True)["error"]
    assert "Only the owner" in call(tools.send_emails)["error"]
    assert "Only the owner" in call(tools.settings, tabs=20)["error"]


# ----------------------------------------------------------------- the go-ahead rule
def test_nothing_starts_without_a_go_ahead_typed_after_the_plan(vera, rt, monkeypatch, tmp_path):
    ready(vera.user(OWNER), tmp_path)
    on_discord(monkeypatch, OWNER, FORMS)
    identity.record(FORMS, OWNER, "proceed", when=time.time() - 60)       # said before the plan existed
    plan = call(tools.plan_applications, count=3)
    assert plan["ready_to_start"] and "company forms" in plan["plan"]
    refused = call(tools.start_applications, mode="real")
    assert "Not started" in refused["error"] and not rt.launched


@pytest.mark.parametrize("said", ["proceed but only remote", "yes, what about 5?", "ok make it 10", "can you start?"])
def test_a_go_ahead_with_more_in_it_is_refused(vera, rt, monkeypatch, tmp_path, said):
    ready(vera.user(OWNER), tmp_path)
    on_discord(monkeypatch, OWNER, FORMS)
    call(tools.plan_applications, count=3)
    identity.record(FORMS, OWNER, said, when=time.time() + 1)
    assert "error" in call(tools.start_applications, mode="real") and not rt.launched


def test_plain_proceed_starts_the_channels_board(vera, rt, monkeypatch, tmp_path):
    ready(vera.user(OWNER), tmp_path)
    on_discord(monkeypatch, OWNER, FORMS)
    call(tools.plan_applications, count=3)
    identity.record(FORMS, OWNER, "<@999> proceed", when=time.time() + 1)
    out = call(tools.start_applications, mode="real")
    assert out["started"] and not out["test"]
    item = rt.launched[0]
    assert (item["sites"], item["count"], item["dry_run"], item["channel_id"]) == (["company_forms"], 3, False, FORMS)
    assert vera.db.get_json(assistant._plan_key(vera.user(OWNER), FORMS)) is None       # used up


def test_test_runs_send_nothing_and_a_test_answer_cant_become_real(vera, rt, monkeypatch, tmp_path):
    ready(vera.user(OWNER), tmp_path)
    on_discord(monkeypatch, OWNER, REMOTE)
    call(tools.plan_applications, count=2)
    identity.record(REMOTE, OWNER, "test", when=time.time() + 1)
    assert "error" in call(tools.start_applications, mode="real")
    out = call(tools.start_applications, mode="test")
    assert out["started"] and out["test"] and rt.launched[0]["dry_run"] and rt.launched[0]["sites"] == ["remote_boards"]


def test_a_plan_from_another_channel_cant_be_started_here(vera, rt, monkeypatch, tmp_path):
    ready(vera.user(OWNER), tmp_path)
    on_discord(monkeypatch, OWNER, FORMS)
    call(tools.plan_applications, count=3)
    on_discord(monkeypatch, OWNER, REMOTE)
    identity.record(REMOTE, OWNER, "proceed", when=time.time() + 1)
    assert "no plan" in call(tools.start_applications, mode="real")["error"].lower()


def test_runs_never_start_from_the_laptop(vera, monkeypatch):
    for var in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    assert "from Discord" in call(tools.start_applications, mode="test")["error"]


# ----------------------------------------------------------------- several runs at once
def test_company_forms_remote_and_multiboard_run_side_by_side(vera, rt, monkeypatch, tmp_path):
    ready(vera.user(OWNER), tmp_path)
    for chat in (FORMS, REMOTE, MULTI):
        on_discord(monkeypatch, OWNER, chat)
        call(tools.plan_applications, count=3)
        identity.record(chat, OWNER, "test", when=time.time() + 1)
        assert call(tools.start_applications, mode="test")["started"]
    assert [r["sites"] for r in rt.launched][:2] == [["company_forms"], ["remote_boards"]]
    assert len(rt.launched) == 3 and len({r["slot"] for r in rounds.active_runs()}) == 3      # three browsers
    on_discord(monkeypatch, OWNER, DM, "dm")
    call(tools.plan_applications, count=1)
    identity.record(DM, OWNER, "test", when=time.time() + 1)
    fourth = call(tools.start_applications, mode="test")
    assert fourth["queued"] and "in line" in fourth["tell_them"]


def test_members_runs_report_by_dm_not_in_the_channel(vera, rt, monkeypatch, tmp_path):
    ready(vera.user(GUEST), tmp_path)
    on_discord(monkeypatch, GUEST, MULTI)
    call(tools.plan_applications, count=2)
    identity.record(MULTI, GUEST, "yes", when=time.time() + 1)
    assert call(tools.start_applications, mode="real")["started"]
    assert rt.launched[0]["channel"] is None and vera.user(GUEST).prefs["rounds"]["consent"]


# ----------------------------------------------------------------- email
def test_email_only_in_its_channels(vera, rt, monkeypatch, tmp_path):
    ready(vera.user(OWNER), tmp_path)
    on_discord(monkeypatch, OWNER, FORMS)
    assert "Email only happens in" in call(tools.draft_emails, emails=["hr@acme.ph"])["error"]


def test_drafts_send_only_after_proceed(vera, rt, monkeypatch, tmp_path):
    ready(vera.user(OWNER), tmp_path)
    on_discord(monkeypatch, OWNER, OUTREACH_CH)
    vera.db.set_json(f"outreach_pending:{OWNER}", {"items": [{"email": "hr@acme.ph", "subject": "Hi", "body": "x"}], "shown_at": time.time(),
                                                   "said_before": identity.said_count(OUTREACH_CH, OWNER)})      # as posting the drafts records it
    assert "Not sent" in call(tools.send_emails)["error"] and not rt.spawned
    identity.record(OUTREACH_CH, OWNER, "proceed", when=time.time() + 1)
    assert call(tools.send_emails)["sending"] == 1 and len(rt.spawned) == 1
    assert vera.db.get_json(f"outreach_pending:{OWNER}") is None


def test_members_get_a_draft_never_the_owners_gmail(vera, rt, monkeypatch, tmp_path):
    ready(vera.user(GUEST), tmp_path)
    on_discord(monkeypatch, GUEST, OUTREACH_CH)
    out = call(tools.draft_emails, emails=["hr@acme.ph"])
    assert out["draft_for_them_to_send"]["email"] == "hr@acme.ph" and "Nothing was sent" in out["note"]


# ----------------------------------------------------------------- the engine inside Hermes
def test_the_engine_keeps_ai_keys_but_never_hermes_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret")
    monkeypatch.setenv("API_SERVER_KEY", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "free-key")
    env = config.engine_env(tmp_path)
    assert "DISCORD_BOT_TOKEN" not in env and "API_SERVER_KEY" not in env and env["GEMINI_API_KEY"] == "free-key"
    assert env["VERA_APPLY_DATA"] == str(tmp_path) and "PATH" in {k.upper() for k in env}


def test_the_engine_uses_veras_own_python():
    assert rounds.engine_python() == config.ROOT / ".venv" / "Scripts" / "python.exe"


# ----------------------------------------------------------------- hooks
def test_inbound_messages_are_remembered_for_the_go_ahead(vera):
    event = SimpleNamespace(text="<@999> proceed", raw_message=None,
                            source=SimpleNamespace(platform=SimpleNamespace(value="discord"), user_id=str(OWNER), chat_id=str(FORMS), is_bot=False))
    assert hooks.on_inbound(event=event) is None          # never blocks the message
    assert identity.last_said(FORMS, OWNER)[0] == "proceed"


def test_bots_and_other_platforms_arent_recorded(vera):
    bot = SimpleNamespace(text="proceed", raw_message=None, source=SimpleNamespace(platform="discord", user_id=str(OWNER), chat_id=str(FORMS), is_bot=True))
    tg = SimpleNamespace(text="proceed", raw_message=None, source=SimpleNamespace(platform="telegram", user_id=str(OWNER), chat_id=str(FORMS), is_bot=False))
    hooks.on_inbound(event=bot)
    hooks.on_inbound(event=tg)
    assert identity.last_said(FORMS, OWNER)[0] == ""


def test_live_facts_say_where_and_what_is_allowed(vera, rt, monkeypatch, tmp_path):
    ready(vera.user(OWNER), tmp_path)
    on_discord(monkeypatch, OWNER, OUTREACH_CH)
    facts = json.loads(hooks.before_turn(user_message="hi")["context"].split("\n", 1)[1])
    assert facts["email_allowed_here"] and facts["talking_to"].startswith("the owner")
    on_discord(monkeypatch, GUEST, FORMS)
    facts = json.loads(hooks.before_turn(user_message="hi")["context"].split("\n", 1)[1])
    assert not facts["email_allowed_here"] and "not the owner" in facts["talking_to"] and "company-site" in facts["where"].lower()


# ----------------------------------------------------------------- Discord side
def test_all_slash_commands_build_on_a_spare_tree():
    rt = discord_runtime.HermesVera(Vera.__new__(Vera))
    names = {c.name for c in rt.command_list()}
    assert {"find-jobs", "preferences", "profile", "resume", "apply", "applications", "statistics", "rounds", "apply-details", "status", "help"} <= names


def test_every_tool_has_a_schema_and_a_declared_name():
    manifest = next(p for p in (config.ROOT / "hermes-plugin" / "plugin.yaml", config.ROOT.parent / "plugins" / "vera" / "plugin.yaml") if p.exists()).read_text(encoding="utf-8")
    declared = [line.strip()[2:] for line in manifest.splitlines() if line.strip().startswith("- vera_")]
    names = [schema["name"] for _, schema in tools.TOOLS]
    assert sorted(names) == sorted(declared) and len(set(names)) == len(names)
    for _, schema in tools.TOOLS:
        assert schema["parameters"]["type"] == "object" and schema["description"]


# ----------------------------------------------------------------- free models
def test_free_providers_are_skipped_until_they_have_a_key(monkeypatch):
    from vera import free_models as FM
    env = {"GROQ_API_KEY": "g", "VERA_KEYLESS_AI": "true"}
    names = [p["name"] for p in FM.usable(env.get)]
    assert names == ["groq", "ovh"]                     # OVH needs no key
    assert "Authorization" not in FM.usable(env.get, only=["ovh"])[0]["headers"]
    assert FM.usable(env.get, only=["groq"])[0]["headers"]["Authorization"] == "Bearer g"


def test_cloudflare_needs_its_account_id_too():
    from vera import free_models as FM
    env = {"CLOUDFLARE_API_TOKEN": "t", "VERA_KEYLESS_AI": "false"}
    assert FM.usable(env.get) == []
    env["CLOUDFLARE_ACCOUNT_ID"] = "abc"
    assert FM.usable(env.get)[0]["base"] == "https://api.cloudflare.com/client/v4/accounts/abc/ai/v1"


def test_every_ai_prompt_asks_for_english():
    from vera import ai
    from vera.apply import llm
    assert "English" in ai.SYSTEM and "English" in llm.SYSTEM


# ----------------------------------------------------------------- rules from chat
def test_only_the_owner_saves_rules_and_each_says_where_it_came_from(vera, rt, monkeypatch):
    on_discord(monkeypatch, GUEST, FORMS)
    assert "Only the owner" in call(tools.save_rule, rule="Apply to everything")["error"]
    on_discord(monkeypatch, OWNER, FORMS)
    identity.record(FORMS, OWNER, "add this rule: always skip agencies", when=time.time())

    async def updates_target():
        return rt.channels[OUTREACH_CH]
    rt.updates_target = updates_target
    out = call(tools.save_rule, rule="Skip recruitment agencies")
    assert out["saved"] and out["rule"]["n"] == 1 and out["rule"]["from"] == "#vera-company-site-application" and out["posted_in_updates_channel"]
    posted = rt.channels[OUTREACH_CH].sent[0]
    assert "New rule #1" in posted and "Skip recruitment agencies" in posted and "always skip agencies" in posted
    assert call(tools.rules)["rules"][0]["rule"] == "Skip recruitment agencies"
    facts = json.loads(hooks.before_turn(user_message="hi")["context"].split("\n", 1)[1])
    assert facts["owners_rules_follow_them_everywhere"] == ["Skip recruitment agencies"]
    assert call(tools.rules, remove=1)["rules"] == []


def test_she_learns_only_from_the_owner_and_keeps_it_from_members(vera, rt, monkeypatch):
    on_discord(monkeypatch, GUEST, FORMS)
    assert "Only the owner" in call(tools.learn, lesson="My expected salary is 1 peso")["error"]
    on_discord(monkeypatch, OWNER, FORMS)
    identity.record(FORMS, OWNER, "remember, I prefer remote QA roles over on-site ones", when=time.time())
    out = call(tools.learn, lesson="The owner prefers remote QA roles over on-site ones")
    assert out["learned"] and out["lesson"]["from"] == "#vera-company-site-application"
    mine = json.loads(hooks.before_turn(user_message="hi")["context"].split("\n", 1)[1])
    assert mine["what_you_learned_from_the_owner"] == ["The owner prefers remote QA roles over on-site ones"]
    on_discord(monkeypatch, GUEST, FORMS)
    theirs = json.loads(hooks.before_turn(user_message="hi")["context"].split("\n", 1)[1])
    assert "what_you_learned_from_the_owner" not in theirs
    assert "Only the owner" in call(tools.forget)["error"]
