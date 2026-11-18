"""Free AI with failover across every provider in vera/free_models.py, then OpenRouter (free models only).

Free tiers are often rate-limited (HTTP 429) or return empty answers, so each call
tries every provider you have a key for, resting one for a few minutes if it says
slow down. Everything that uses AI has a non-AI fallback, so Vera works without a key.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from vera import config
from vera import free_models as FM

log = logging.getLogger("vera.ai")

SYSTEM = ("You are Vera, a careful job-hunting assistant. Never invent experience, skills, degrees, "
          "certifications, employers, salaries or achievements. Only use facts you are given. "
          "When asked for JSON, return only valid JSON with no markdown fences. " + FM.ENGLISH)

last_error: Optional[str] = None


COOLDOWN_SECONDS = 300
_cooldown: Dict[str, float] = {}

# FREE MODELS ONLY. The providers and their fixed model lists are in vera/free_models.py.
GEMINI_FREE, NVIDIA_FREE, GROQ_FREE, MISTRAL_FREE = FM.GEMINI_FREE, FM.NVIDIA_FREE, FM.GROQ_FREE, FM.MISTRAL_FREE


def _get(name: str) -> Optional[str]:
    """Keys config already knows come from config (so they can be switched off), the rest from the environment."""
    return getattr(config, name) if hasattr(config, name) else os.getenv(name)


def _direct_providers() -> List[Dict[str, Any]]:
    return FM.usable(_get)


def free_openrouter_models() -> List[str]:
    """Only ":free" models, whatever .env says."""
    listed = [config.OPENROUTER_MODEL] + config.OPENROUTER_FALLBACK_MODELS
    free = [m for m in dict.fromkeys(listed) if m.endswith(":free") or m == "openrouter/free"]
    return free or ["openrouter/free"]


def providers() -> List[str]:
    """Names of the providers you have keys for, in the order they're tried."""
    names = [p["name"] for p in _direct_providers()]
    return names + (["openrouter"] if config.OPENROUTER_API_KEY else [])


def configured() -> bool:
    return bool(providers())


async def _direct(client: httpx.AsyncClient, p: Dict[str, Any], prompt: str, temperature: float, max_tokens: int) -> Optional[str]:
    """Ask one provider. Returns None so the next one can try."""
    global last_error
    if _cooldown.get(p["name"], 0) > time.monotonic():
        return None
    headers = p["headers"]
    for model in p["models"]:
        try:
            r = await client.post(f"{p['base']}/chat/completions", headers=headers, json={
                "model": model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]})
        except httpx.HTTPError as e:
            last_error = f"{p['name']}: network error ({type(e).__name__})"
            continue
        if r.status_code == 429:
            _cooldown[p["name"]] = time.monotonic() + COOLDOWN_SECONDS
            last_error = f"{p['name']}: rate-limited"
            return None
        if r.status_code in (401, 403):
            last_error = f"{p['name']}: key rejected (HTTP {r.status_code})"
            return None
        if r.status_code != 200:
            last_error = f"{p['name']}/{model}: HTTP {r.status_code}"
            continue
        choice = (r.json().get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content")
        if choice.get("finish_reason") == "length":     # cut off mid-sentence: not usable
            last_error = f"{p['name']}/{model}: answer was cut off"
            continue
        if text and text.strip():
            last_error = None
            return text.strip()
        last_error = f"{p['name']}/{model}: empty answer"
    return None


async def complete(prompt: str, temperature: float = 0.3, max_tokens: int = 1500) -> Optional[str]:
    """Return the model's text, or None if every provider failed."""
    global last_error
    if not configured():
        last_error = "no AI provider available (add a key from vera/free_models.py to .env)"
        return None
    async with httpx.AsyncClient(timeout=90) as client:
        for p in _direct_providers():
            text = await _direct(client, p, prompt, temperature, max_tokens)
            if text:
                return text
        if not config.OPENROUTER_API_KEY:
            log.warning(f"AI unavailable: {last_error}")
            return None
    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "Content-Type": "application/json",
               "HTTP-Referer": "http://localhost/vera", "X-OpenRouter-Title": "Vera Job Hunter"}
    models = free_openrouter_models()
    async with httpx.AsyncClient(timeout=90) as client:
        for model in models:
            for attempt in range(2):
                if attempt:
                    await asyncio.sleep(3)
                try:
                    r = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
                        "model": model, "temperature": temperature, "max_tokens": max_tokens,
                        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]})
                except httpx.HTTPError as e:
                    last_error = f"network error ({type(e).__name__})"
                    continue
                if r.status_code in (401, 402, 403):
                    last_error = f"OpenRouter rejected the key (HTTP {r.status_code})"
                    return None
                if r.status_code == 429 or r.status_code >= 500:
                    last_error = "free models are rate-limited right now"
                    continue
                if r.status_code != 200:
                    last_error = f"HTTP {r.status_code} from {model}"
                    break
                choice = (r.json().get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content")
                if text and text.strip():
                    last_error = None
                    return text.strip()
                last_error = f"{model} returned an empty answer"
                break
    log.warning(f"AI unavailable: {last_error}")
    return None


def extract_json(text: Optional[str]) -> Dict[str, Any]:
    if not text:
        return {}
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {}
    except ValueError:
        pass
    start, depth = t.find("{"), 0
    if start < 0:
        return {}
    for i in range(start, len(t)):
        depth += {"{": 1, "}": -1}.get(t[i], 0)
        if depth == 0:
            try:
                v = json.loads(t[start:i + 1])
                return v if isinstance(v, dict) else {}
            except ValueError:
                return {}
    return {}
