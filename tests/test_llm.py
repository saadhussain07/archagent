"""DeepSeek client (against a local fake server) and cost accounting."""

import datetime as dt

import pytest

from archagent.llm.client import DeepSeekClient, LLMError
from archagent.llm.pricing import CostTracker, is_peak, rates_for
from fakes import FakeDeepSeek, start_fake_server


@pytest.fixture
def server():
    srv, url = start_fake_server()
    yield url
    srv.shutdown()


def client(url, **kw):
    return DeepSeekClient("sk-test", base_url=url, sleep=lambda s: None, **kw)


def test_payload_has_thinking_and_tools(server):
    FakeDeepSeek.responses.append({
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "reasoning_content": "let me think",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "list_files", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 60,
                  "prompt_cache_miss_tokens": 40, "completion_tokens": 20}})
    r = client(server).chat([{"role": "user", "content": "hi"}],
                            tools=[{"type": "function", "function": {"name": "list_files"}}])
    sent = FakeDeepSeek.received[0]
    assert sent["auth"] == "Bearer sk-test"
    assert sent["body"]["model"] == "deepseek-v4-pro"
    assert sent["body"]["thinking"] == {"type": "enabled"}
    assert sent["body"]["reasoning_effort"] == "high"
    assert sent["body"]["tools"][0]["function"]["name"] == "list_files"
    assert r.tool_calls[0]["function"]["name"] == "list_files"
    # reasoning_content must survive into history (required by DeepSeek with tools)
    assert r.as_message()["reasoning_content"] == "let me think"


def test_non_thinking_payload(server):
    client(server, thinking=False).chat([{"role": "user", "content": "x"}])
    body = FakeDeepSeek.received[0]["body"]
    assert body["thinking"] == {"type": "disabled"} and "reasoning_effort" not in body


def test_retries_on_503_then_succeeds(server):
    FakeDeepSeek.status_codes.extend([503, 429])
    retries = []
    r = client(server).chat([{"role": "user", "content": "x"}], on_retry=lambda *a: retries.append(a))
    assert r.content == "OK" and len(retries) == 2


def test_401_is_not_retried(server):
    FakeDeepSeek.status_codes.extend([401, 200])
    with pytest.raises(LLMError) as e:
        client(server).chat([{"role": "user", "content": "x"}])
    assert e.value.status == 401 and "API key" in str(e.value)
    assert len(FakeDeepSeek.received) == 1


def test_402_insufficient_balance_message(server):
    FakeDeepSeek.status_codes.append(402)
    with pytest.raises(LLMError, match="Insufficient balance"):
        client(server).chat([{"role": "user", "content": "x"}])


def test_ping(server):
    assert client(server).ping() == "OK"
    assert FakeDeepSeek.received[0]["body"]["max_tokens"] == 5


def test_missing_key_rejected():
    with pytest.raises(LLMError):
        DeepSeekClient("")


def test_network_error_is_retryable():
    c = DeepSeekClient("k", base_url="http://127.0.0.1:9", max_retries=0, timeout=2)
    with pytest.raises(LLMError) as e:
        c.chat([{"role": "user", "content": "x"}])
    assert e.value.retryable


# ------------------------------------------------------------------- pricing

def test_cost_math_peak():
    t = CostTracker(budget_usd=1.0, assume_peak=True)
    cost = t.add({"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 500_000,
                  "prompt_cache_miss_tokens": 500_000, "completion_tokens": 100_000})
    assert cost == pytest.approx(0.5 * 0.044 + 0.5 * 1.32 + 0.1 * 3.96)
    assert t.fraction_used == pytest.approx(cost)


def test_cost_infers_miss_when_absent():
    t = CostTracker(assume_peak=True)
    t.add({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 400, "completion_tokens": 0})
    assert t.total.cache_miss == 600


def test_peak_windows():
    monday = dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc)
    assert is_peak(monday.replace(hour=7)) and is_peak(monday.replace(hour=2))
    assert not is_peak(monday.replace(hour=5)) and not is_peak(monday.replace(hour=12))
    assert not is_peak(dt.datetime(2026, 10, 25, 7, tzinfo=dt.timezone.utc))  # Sunday
    assert rates_for("deepseek-v4-pro", monday.replace(hour=12))["output"] == 1.98


def test_estimate_is_worst_case():
    t = CostTracker()
    assert t.estimate_call_cost(10_000, 5_000) == pytest.approx((10_000 * 1.32 + 5_000 * 3.96) / 1e6)
