"""Free / OpenAI-compatible providers (practice mode)."""

import pytest

from archagent.agent.loop import Agent, AgentConfig
from archagent.llm.client import DeepSeekClient, LLMError
from archagent.llm.pricing import CostTracker
from archagent.llm.providers import PROVIDERS, get_provider, provider_for_label
from conftest import DOC, VIEWS
from fakes import FakeDeepSeek, ScriptedClient, happy_path_script, start_fake_server


@pytest.fixture
def server():
    srv, url = start_fake_server()
    yield url
    srv.shutdown()


def other(url, **kw):
    return DeepSeekClient("k", base_url=url, deepseek_api=False, provider_name="OpenRouter",
                          extra_headers={"X-Title": "ArchAgent"}, sleep=lambda s: None, **kw)


def test_non_deepseek_payload_drops_deepseek_only_fields(server):
    msgs = [{"role": "user", "content": "x"},
            {"role": "assistant", "content": "", "reasoning_content": "secret thoughts"}]
    other(server).chat(msgs, tools=[{"type": "function", "function": {"name": "t"}}])
    body = FakeDeepSeek.received[0]["body"]
    assert "thinking" not in body and "reasoning_effort" not in body
    assert all("reasoning_content" not in m for m in body["messages"])
    assert body["tools"] and body["temperature"] == 0.2
    assert msgs[1]["reasoning_content"] == "secret thoughts"  # caller's history untouched


def test_error_object_with_http_200_is_raised(server):
    FakeDeepSeek.responses.append({"error": {"code": 404, "message": "No endpoints support tool use"}})
    with pytest.raises(LLMError, match="tool use"):
        other(server, max_retries=0).chat([{"role": "user", "content": "x"}])


def test_dict_arguments_and_missing_ids_are_normalised(server):
    FakeDeepSeek.responses.append({"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"function": {"name": "list_files", "arguments": {"path": "."}}}]}}]})
    r = other(server).chat([{"role": "user", "content": "x"}])
    assert r.tool_calls[0]["function"]["arguments"] == '{"path": "."}'
    assert r.tool_calls[0]["id"]


def test_rate_limit_message_names_provider(server):
    FakeDeepSeek.status_codes.append(429)
    with pytest.raises(LLMError, match="free quota reached at OpenRouter"):
        other(server, max_retries=0).chat([{"role": "user", "content": "x"}])


def test_local_endpoint_needs_no_key():
    DeepSeekClient("", base_url="http://localhost:11434/v1", deepseek_api=False)
    with pytest.raises(LLMError):
        DeepSeekClient("", base_url="https://openrouter.ai/api/v1", deepseek_api=False)


def test_free_provider_costs_zero_but_counts_tokens():
    t = CostTracker(model="nvidia/nemotron:free", free=True)
    assert t.add({"prompt_tokens": 50_000, "completion_tokens": 9_000}) == 0.0
    assert t.total.output == 9_000 and t.snapshot()["pricing_mode"] == "free tier ($0)"
    assert t.estimate_call_cost(10**6, 10**6) == 0.0


def test_presets():
    assert get_provider(None).key == "deepseek" and get_provider("nope").key == "deepseek"
    assert PROVIDERS["deepseek"].models[0] == "deepseek-v4-pro" and not PROVIDERS["deepseek"].free
    for p in PROVIDERS.values():
        assert provider_for_label(p.label) is p
        assert p.base_url.startswith("http") and p.models


def test_practice_run_is_labelled_in_report(tmp_path):
    cfg = AgentConfig(api_key="k", doc_path=str(DOC), views_path=str(VIEWS),
                      output_dir=str(tmp_path / "out"), provider="openrouter",
                      model="minimax/minimax-m3.1-flash:free", base_url=PROVIDERS["openrouter"].base_url)
    events = []
    r = Agent(cfg, on_event=events.append, client=ScriptedClient(happy_path_script())).run()
    assert r.status == "completed" and r.cost["cost_usd"] == 0.0
    assert cfg.max_tokens_per_call == PROVIDERS["openrouter"].max_output_tokens
    assert any("PRACTICE RUN" in e.get("text", "") for e in events if e["type"] == "phase")
    assert "OpenRouter" in (tmp_path / "out" / ".agent" / "run_report.md").read_text()
