"""Minimal DeepSeek chat-completions client (OpenAI-compatible), stdlib only.

Why stdlib: the .exe stays small and there is no dependency on the
OpenAI SDK. Handles thinking mode (``reasoning_content`` must be passed
back on every later request when tools are used), retries with
exponential backoff on 429/5xx/network errors, and clear errors for
401 (bad key) and 402 (insufficient balance).
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class ChatResponse:
    content: str
    reasoning_content: str | None
    tool_calls: list[dict[str, Any]]
    finish_reason: str
    usage: dict[str, Any]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def as_message(self) -> dict[str, Any]:
        """The assistant message to append to history (keeps reasoning_content)."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.reasoning_content:
            msg["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


_ERRORS = {
    400: "Bad request (invalid message format or parameters)",
    401: "Authentication failed - check your DeepSeek API key",
    402: "Insufficient balance on the DeepSeek account",
    422: "Invalid parameters",
    429: "Rate limit reached",
    500: "DeepSeek server error",
    503: "DeepSeek server overloaded",
}


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        thinking: bool = True,
        reasoning_effort: str = "high",
        timeout: float = 600.0,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise LLMError("No DeepSeek API key configured", status=401)
        self.api_key = api_key.strip()
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._ssl = ssl.create_default_context()  # uses the Windows cert store on Windows

    # ------------------------------------------------------------------ http
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ArchAgent/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl
                                        if self.base_url.startswith("https") else None) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except Exception:
                pass
            msg = _ERRORS.get(e.code, f"HTTP {e.code}")
            raise LLMError(f"{msg}{': ' + detail if detail else ''}", status=e.code,
                           retryable=e.code in (429, 500, 502, 503, 504)) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            reason = getattr(e, "reason", e)
            raise LLMError(f"Network error contacting DeepSeek: {reason}", retryable=True) from None
        # DeepSeek may send keep-alive blank lines before the JSON body under load.
        body = body.strip()
        if not body:
            raise LLMError("Empty response from DeepSeek", retryable=True)
        return json.loads(body)

    def _with_retries(self, fn: Callable[[], dict[str, Any]], on_retry=None) -> dict[str, Any]:
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                return fn()
            except LLMError as e:
                if not e.retryable or attempt == self.max_retries:
                    raise
                if on_retry:
                    on_retry(attempt + 1, str(e), delay)
                self._sleep(delay)
                delay = min(delay * 2, 30)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ api
    def build_payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                      max_tokens: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }
        if self.thinking:
            payload["reasoning_effort"] = self.reasoning_effort
        else:
            payload["temperature"] = 0.2
        if tools:
            payload["tools"] = tools  # tool_choice defaults to "auto"
        return payload

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             max_tokens: int = 16000, on_retry=None) -> ChatResponse:
        payload = self.build_payload(messages, tools, max_tokens)
        raw = self._with_retries(lambda: self._post("/chat/completions", payload), on_retry)
        try:
            choice = raw["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError):
            raise LLMError(f"Unexpected response shape: {str(raw)[:300]}") from None
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            tool_calls.append({
                "id": tc.get("id") or f"call_{len(tool_calls)}",
                "type": "function",
                "function": {"name": tc["function"]["name"],
                             "arguments": tc["function"].get("arguments") or "{}"},
            })
        return ChatResponse(
            content=msg.get("content") or "",
            reasoning_content=msg.get("reasoning_content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason") or "",
            usage=raw.get("usage") or {},
            raw=raw,
        )

    def ping(self) -> str:
        """Cheap connectivity/key check (a few tokens, thinking disabled)."""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
            "max_tokens": 5,
            "thinking": {"type": "disabled"},
        }
        raw = self._with_retries(lambda: self._post("/chat/completions", payload))
        return (raw.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
