"""End-to-end agent loop with a scripted LLM (no network, no tokens)."""

import json
import threading

import pytest

from archagent.agent import context
from archagent.agent.loop import Agent, AgentConfig
from conftest import DOC, VIEWS
from fakes import MINI_PROJECT, ScriptedClient, happy_path_script, response, tool_call


def make_agent(tmp_path, script, **cfg):
    events = []
    config = AgentConfig(api_key="k", doc_path=str(DOC), views_path=str(VIEWS),
                         output_dir=str(tmp_path / "out"), **cfg)
    client = ScriptedClient(script)
    agent = Agent(config, on_event=events.append, client=client)
    return agent, client, events


def test_happy_path_generates_tested_project(tmp_path):
    agent, client, events = make_agent(tmp_path, happy_path_script())
    r = agent.run()
    assert r.status == "completed"
    assert r.tests_passed is True and r.selftest_ok is True
    assert r.verification == []
    assert "mini/core.py" in r.files
    out = tmp_path / "out"
    assert (out / ".agent" / "spec.json").is_file()
    assert (out / ".agent" / "run_report.md").read_text().count("completed") >= 1
    lines = (out / ".agent" / "transcript.jsonl").read_text().splitlines()
    assert any(json.loads(l).get("kind") == "tool" for l in lines)
    kinds = {e["type"] for e in events}
    assert {"spec", "turn", "tool_call", "tool_result", "todos", "files", "tests", "cost", "done"} <= kinds


def test_prompt_prefix_is_stable_for_caching(tmp_path):
    agent, client, _ = make_agent(tmp_path, happy_path_script())
    agent.run()
    first = client.requests[0][:2]
    for req in client.requests[1:]:
        assert req[:2] == first  # system + task message byte-identical every turn
    assert "<spec_json>" in first[1]["content"] and "Space Fractions" in first[1]["content"]


def test_reasoning_content_is_passed_back(tmp_path):
    agent, client, _ = make_agent(tmp_path, happy_path_script())
    agent.run()
    later = client.requests[3]
    assistants = [m for m in later if m["role"] == "assistant"]
    assert assistants and all(m.get("reasoning_content") == "thinking..." for m in assistants)


def test_finish_rejected_until_deliverables_exist(tmp_path):
    files = [{"path": p, "content": c} for p, c in MINI_PROJECT.items() if p != "Dockerfile"]
    files = [f if f["path"] != "mini/core.py" else {**f, "content": "def add(a, b):\n    return a + b\n"}
             for f in files]
    script = [
        response([tool_call(1, "write_files", {"files": files})]),
        response([tool_call(2, "finish", {"summary": "done?"})]),
        lambda msgs: (_assert_rejection(msgs), response([tool_call(3, "write_file", {
            "path": "Dockerfile", "content": "FROM python:3.12-slim\n"})]))[1],
        response([tool_call(4, "finish", {"summary": "now done"})]),
    ]
    agent, _, events = make_agent(tmp_path, script)
    r = agent.run()
    assert r.status == "completed" and r.summary == "now done"
    assert any(e["type"] == "verification" for e in events)


def _assert_rejection(messages):
    notice = messages[-1]
    assert notice["role"] == "user" and "Dockerfile" in notice["content"]


def test_harness_runs_stale_tests_itself_on_finish(tmp_path):
    files = [{"path": p, "content": c} for p, c in MINI_PROJECT.items()]  # contains a bug
    script = [
        response([tool_call(1, "write_files", {"files": files})]),
        response([tool_call(2, "finish", {"summary": "untested"})]),  # never ran tests
        lambda msgs: (_assert_failing_tests_reported(msgs), response([tool_call(3, "read_file", {"path": "mini/core.py"})]))[1],
        response([tool_call(4, "edit_file", {"path": "mini/core.py", "old_string": "a - b", "new_string": "a + b"})]),
        response([tool_call(5, "finish", {"summary": "fixed"})]),
    ]
    agent, _, _ = make_agent(tmp_path, script)
    r = agent.run()
    assert r.status == "completed" and r.tests_passed is True and r.summary == "fixed"


def _assert_failing_tests_reported(messages):
    assert "test-suite is failing" in messages[-1]["content"]


def test_budget_guard_stops_before_overspend(tmp_path):
    # ~$0.10 per call, all uncached: warnings at 55% / 80%, then a hard stop
    usage = {"prompt_tokens": 60_000, "prompt_cache_hit_tokens": 0,
             "prompt_cache_miss_tokens": 60_000, "completion_tokens": 5_000}
    script = [response([tool_call(i, "list_files", {})], usage=usage) for i in range(30)]
    agent, client, events = make_agent(tmp_path, script, budget_usd=1.0)
    r = agent.run()
    assert r.status == "budget"
    assert 0.7 < r.cost["cost_usd"] <= 1.0
    notices = [e["text"] for e in events if e["type"] == "notice"]
    assert any("Budget check" in n for n in notices)
    assert any("almost exhausted" in n for n in notices)


def test_budget_guard_single_huge_call(tmp_path):
    heavy = {"prompt_tokens": 400_000, "prompt_cache_hit_tokens": 0,
             "prompt_cache_miss_tokens": 400_000, "completion_tokens": 10_000}
    script = [response([tool_call(i, "list_files", {})], usage=heavy) for i in range(10)]
    agent, _, _ = make_agent(tmp_path, script, budget_usd=1.0)
    r = agent.run()
    assert r.status == "budget" and r.cost["cost_usd"] <= 1.0


def test_max_turns(tmp_path):
    script = [response([tool_call(i, "list_files", {})]) for i in range(10)]
    agent, _, _ = make_agent(tmp_path, script, max_turns=3)
    r = agent.run()
    assert r.status == "max_turns" and r.turns == 3


def test_stop_event(tmp_path):
    stop = threading.Event()
    config = AgentConfig(api_key="k", doc_path=str(DOC), views_path=str(VIEWS),
                         output_dir=str(tmp_path / "out"))

    def stopper(msgs):
        stop.set()
        return response([tool_call(1, "list_files", {})])
    agent = Agent(config, client=ScriptedClient([stopper] * 5), stop_event=stop)
    r = agent.run()
    assert r.status == "stopped" and r.turns == 1


def test_llm_error_is_reported_not_raised(tmp_path):
    from archagent.llm.client import LLMError

    def boom(msgs):
        raise LLMError("Authentication failed", status=401)
    agent, _, events = make_agent(tmp_path, [boom])
    r = agent.run()
    assert r.status == "error" and "Authentication" in r.error
    assert (tmp_path / "out" / ".agent" / "run_report.md").is_file()


def test_length_cutoff_triggers_split_hint(tmp_path):
    script = [response(content="partial...", finish="length")]
    script[0].finish_reason = "length"
    script += [response([tool_call(1, "list_files", {})])]
    agent, client, _ = make_agent(tmp_path, script, max_turns=2)
    agent.run()
    assert "token limit" in client.requests[1][-1]["content"]


def test_compaction_stubs_old_bodies_but_keeps_prefix():
    big = "x" * 5000
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "T"}]
    msgs.append({"role": "assistant", "content": "", "reasoning_content": "r",
                 "tool_calls": [tool_call(1, "write_files", {"files": [{"path": "a.py", "content": big}]})]})
    msgs.append({"role": "tool", "tool_call_id": "call_1", "content": big})
    msgs += [{"role": "user", "content": "recent"}] * 8
    saved = context.compact(msgs)
    assert saved > 9000
    assert msgs[0]["content"] == "S" and msgs[1]["content"] == "T"
    args = json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"])
    assert args["files"][0]["path"] == "a.py" and "written to disk" in args["files"][0]["content"]
    assert msgs[2]["reasoning_content"] == "r"
    assert msgs[3]["content"].startswith("[compacted]")
    assert context.compact(msgs) == 0  # idempotent


@pytest.mark.parametrize("name", ["write_file", "edit_file"])
def test_preview_args_never_dumps_file_bodies(name):
    from archagent.agent.loop import _preview_args
    out = _preview_args(name, json.dumps({"path": "p.py", "content": "y" * 9999,
                                          "old_string": "a", "new_string": "b"}))
    assert len(out) < 100 and "p.py" in out
