"""Shared test setup."""

import pytest

from vera import config
from vera import free_models as FM

# Tests set the Gemini, NVIDIA and OpenRouter keys themselves. The newer providers' keys are hidden here, so the
# owner's real keys (in .env) never make a test call the internet.
NEWER_KEYS = [p["key"] for p in FM.PROVIDERS if p["key"] and p["key"] not in ("GEMINI_API_KEY", "NVIDIA_API_KEY")] + ["CLOUDFLARE_ACCOUNT_ID"]


@pytest.fixture(autouse=True)
def pretend_sites_count_as_websites(monkeypatch):
    """Engine tests use made-up sites (x.test, a.test, example.com) that don't exist. The browser guard would rightly block
    them, so in tests those reserved names count as public. Real private addresses, localhost and file:// are still blocked."""
    from vera.apply import netguard
    real = netguard.host_is_public
    reserved = (".test", ".example", ".invalid", "example.com", "example.org", "example.net")
    monkeypatch.setattr(netguard, "host_is_public",
                        lambda host: True if (host or "").lower().endswith(reserved) or (host or "").lower() in ("x", "a", "b") else real(host))


@pytest.fixture(autouse=True)
def no_real_ai_keys(monkeypatch):
    monkeypatch.setenv("VERA_KEYLESS_AI", "false")        # OVH needs no key at all
    for key in NEWER_KEYS:
        monkeypatch.delenv(key, raising=False)
        if hasattr(config, key):
            monkeypatch.setattr(config, key, "")
