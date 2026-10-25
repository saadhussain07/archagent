"""LLM provider presets (all OpenAI-compatible /chat/completions endpoints).

DeepSeek official is the default and the one the evaluation task requires.
The free presets exist so the agent can be tried out at zero cost; their
model lists change often, so the model field in the GUI stays editable.
Free-tier limits checked 2026-10-25: OpenRouter ~20 req/min and ~50 req/day
without purchased credits; Gemini free tier is Flash / Flash-Lite only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str
    models: tuple[str, ...]
    deepseek_api: bool = False       # supports `thinking` + needs reasoning_content passed back
    free: bool = False               # cost is reported as $0
    max_output_tokens: int = 32_000
    key_url: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        key="deepseek", label="DeepSeek (official) - required for submission",
        base_url="https://api.deepseek.com", models=("deepseek-v4-pro", "deepseek-flash"),
        deepseek_api=True, max_output_tokens=32_000, key_url="https://platform.deepseek.com/api_keys"),
    "openrouter": Provider(
        key="openrouter", label="OpenRouter - free models (practice)",
        base_url="https://openrouter.ai/api/v1",
        models=("nvidia/nemotron-3-super-120b-a12b:free", "minimax/minimax-m3.1-flash:free",
                "nvidia/nemotron-3-ultra-550b-a55b:free", "google/gemma-4-31b-it:free",
                "cohere/north-mini-code:free"),
        free=True, max_output_tokens=16_000, key_url="https://openrouter.ai/keys",
        extra_headers={"HTTP-Referer": "https://github.com/saadhussain07/archagent", "X-Title": "ArchAgent"},
        note="Free: ~20 requests/min, ~50/day. Pick a model that supports tool calling."),
    "gemini": Provider(
        key="gemini", label="Google Gemini - free tier (practice)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        models=("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.6-flash"),
        free=True, max_output_tokens=16_000, key_url="https://aistudio.google.com/apikey",
        note="Free tier: Flash-Lite has the highest daily quota."),
    "custom": Provider(
        key="custom", label="Custom OpenAI-compatible endpoint",
        base_url="http://localhost:11434/v1", models=("qwen3-coder",),
        free=True, max_output_tokens=16_000, note="e.g. Ollama or LM Studio running locally."),
}

DEFAULT_PROVIDER = "deepseek"


def get_provider(key: str | None) -> Provider:
    return PROVIDERS.get(key or DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER])


def provider_for_label(label: str) -> Provider:
    return next((p for p in PROVIDERS.values() if p.label == label), PROVIDERS[DEFAULT_PROVIDER])
