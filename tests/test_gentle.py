"""Bot-check rests, limits, and the coach."""

import asyncio
from datetime import datetime, timedelta

import pytest

from vera import config
from vera.agents import assistant, rounds
from vera.apply import __main__ as engine
from vera.db import DB
from vera.services import Vera


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(engine.config, "STATE_PATH", tmp_path / "state.json")
    return tmp_path


def test_a_second_bot_check_in_a_day_means_a_full_day(data, monkeypatch, capsys):
    monkeypatch.setattr(engine, "save_state", lambda _s: None)
    monkeypatch.setattr(engine, "load_state", lambda: {})
    state = {}
    engine.rest(state, "company_form")
    first = datetime.fromisoformat(state["company_form"])
    assert timedelta(hours=5) < first - datetime.now() < timedelta(hours=7)
    engine.rest(state, "company_form")
    assert datetime.fromisoformat(state["company_form"]) - datetime.now() > timedelta(hours=23)


def test_your_no_limit_rule_still_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OWNER_ID", 111)
    limits = rounds.build_profile(Vera(DB(tmp_path / "t.db")).user(111))["limits"]
    assert limits["max_per_run"] >= rounds.NO_LIMIT and limits["max_per_day"] >= rounds.NO_LIMIT


def test_she_coaches_from_your_real_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OWNER_ID", 111)
    for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.setattr(config, key, "")
    seen = []

    async def fake_ai(prompt, **_k):
        seen.append(prompt)
        return "ok"
    monkeypatch.setattr(assistant.ai, "complete", fake_ai)
    asyncio.run(assistant.chat(Vera(DB(tmp_path / "t.db")).user(111), "prep me for an interview"))
    for rule in ("How you coach", "red flags", "Situation, Task, Action, Result", "never invent numbers", "Never add skills"):
        assert rule in seen[0], rule
