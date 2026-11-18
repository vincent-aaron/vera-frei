"""Every free AI provider Vera may use, in the order she tries them. One list for her own AI (vera/ai.py) and
the form engine (vera/apply/llm.py). Hermes's chat chain is in profiles\\vera\\config.yaml (fallback_providers).

FREE ONLY, and no credit card. Each provider here is a known company with a permanent free tier. Resellers
that pass prompts on to other services are left out on purpose, because people's résumés go through these
models. Sources: github.com/nejib1/Free-LLM (mirrors free-llm.com, refreshed daily) and github.com/cheahjs/
free-llm-api-resources. Checked 6 Nov 2026. A provider is skipped until its key is in .env
(profiles\\vera\\.env or app\\.env). OVH needs no key at all.

Models are a fixed list, never "newest": a provider's newest model isn't always free.
Check them all with:  vera.bat models
"""

import os
from typing import Callable, Dict, List, Optional

GEMINI_FREE = ["gemini-3.5-flash-lite", "gemini-3-flash-preview", "gemini-3.8-flash", "gemini-3.6-flash"]
# Groq: tested 6 Nov 2026 with the owner's key, all under 1.2 s with tool calls. (It no longer serves Llama.)
GROQ_FREE = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
# NVIDIA also hosts other labs' open models for free. Tested 6 Nov 2026 (English + tool calls); fastest first.
# DeepSeek works but takes 1-2 minutes on the free tier, so it is last. Kimi K2.6 was listed but not served (404).
NVIDIA_FREE = ["nvidia/nemotron-3.5-lightning-30b-a3b", "mistralai/mistral-large-4-1t-a52b", "nvidia/nemotron-3.5-super-vl-120b-a12b",
               "z-ai/glm-5.3-flash", "nvidia/nemotron-3-ultra-550b-a55b", "minimaxai/minimax-m3.1-flash", "deepseek-ai/deepseek-v4.1-flash"]
OVH_FREE = ["Meta-Llama-3_3-70B-Instruct", "gpt-oss-120b"]          # tested 6 Nov 2026: English, tool calls, ~2 s
# Z.AI: only the free Flash models. glm-5.3-flash asks for a balance there (it is free on NVIDIA instead).
ZAI_FREE = ["glm-4.7-flash", "glm-4.5-flash"]
MISTRAL_FREE = ["mistral-small-latest", "open-mistral-nemo"]
CLOUDFLARE_FREE = ["@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/openai/gpt-oss-120b"]
HUGGINGFACE_FREE = ["openai/gpt-oss-120b", "meta-llama/Llama-3.3-70B-Instruct"]
COHERE_FREE = ["command-a-03-2025"]

PROVIDERS: List[Dict] = [
    {"name": "gemini", "key": "GEMINI_API_KEY", "base": "https://generativelanguage.googleapis.com/v1beta/openai", "models": GEMINI_FREE,
     "get_key": "https://aistudio.google.com/apikey", "notes": "Google AI Studio free tier."},
    {"name": "groq", "key": "GROQ_API_KEY", "base": "https://api.groq.com/openai/v1", "models": GROQ_FREE,
     "get_key": "https://console.groq.com/keys", "notes": "Free tier, no card. About 30 requests a minute. Open-source models."},
    {"name": "nvidia", "key": "NVIDIA_API_KEY", "base": "https://integrate.api.nvidia.com/v1", "models": NVIDIA_FREE,
     "get_key": "https://build.nvidia.com", "notes": "NVIDIA NIM free tier (phone check)."},
    {"name": "ovh", "key": None, "base": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", "models": OVH_FREE,
     "get_key": None, "notes": "OVHcloud (EU). No key or account: 2 requests a minute per model."},
    {"name": "zai", "key": "GLM_API_KEY", "base": "https://api.z.ai/api/paas/v4", "models": ZAI_FREE,
     "get_key": "https://z.ai/manage-apikey/apikey-list", "notes": "Z.AI (Zhipu) free GLM Flash models, about 1 request a second."},
    {"name": "mistral", "key": "MISTRAL_API_KEY", "base": "https://api.mistral.ai/v1", "models": MISTRAL_FREE,
     "get_key": "https://console.mistral.ai/api-keys", "notes": "Free Experiment plan (phone check). Free-plan prompts may be used for training."},
    {"name": "cloudflare", "key": "CLOUDFLARE_API_TOKEN", "base": "https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1",
     "models": CLOUDFLARE_FREE, "get_key": "https://dash.cloudflare.com/profile/api-tokens",
     "notes": "Workers AI: 10,000 free units a day. Also needs CLOUDFLARE_ACCOUNT_ID."},
    {"name": "huggingface", "key": "HF_TOKEN", "base": "https://router.huggingface.co/v1", "models": HUGGINGFACE_FREE,
     "get_key": "https://huggingface.co/settings/tokens", "notes": "Small free monthly credit across Hugging Face's inference providers."},
    {"name": "cohere", "key": "COHERE_API_KEY", "base": "https://api.cohere.ai/compatibility/v1", "models": COHERE_FREE,
     "get_key": "https://dashboard.cohere.com/api-keys", "notes": "Trial key: 1,000 calls a month, personal use only."},
]


def keyless_allowed(get: Callable[[str], Optional[str]]) -> bool:
    """OVH works with no account. VERA_KEYLESS_AI=false turns that off (the tests do)."""
    return (get("VERA_KEYLESS_AI") or "true").strip().lower() not in ("0", "false", "no", "off")


def usable(get: Callable[[str], Optional[str]] = os.getenv, only: Optional[List[str]] = None) -> List[Dict]:
    """Providers ready to call, with their real base URL and headers. Skips any without a key."""
    out = []
    for p in PROVIDERS:
        if only and p["name"] not in only:
            continue
        key = (get(p["key"]) or "").strip() if p["key"] else None
        if p["key"] and not key:
            continue
        if not p["key"] and not keyless_allowed(get):
            continue
        base = p["base"]
        if "{CLOUDFLARE_ACCOUNT_ID}" in base:
            account = (get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
            if not account:
                continue
            base = base.replace("{CLOUDFLARE_ACCOUNT_ID}", account)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        out.append({"name": p["name"], "base": base, "models": p["models"], "headers": headers})
    return out


ENGLISH = "Write in English unless you are told to use another language."
