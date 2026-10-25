"""Tool system: sandboxing, Claude-Code-style edit rules, batching."""

import json

import pytest

from archagent.agent.tools import Workspace, build_tools, execute, truncate


@pytest.fixture
def env(tmp_path):
    ws = Workspace(tmp_path / "proj")
    return ws, build_tools(ws)


def call(tools, name, **args):
    return execute(tools, name, json.dumps(args))


def test_write_read_roundtrip(env):
    ws, tools = env
    ok, out = call(tools, "write_file", path="pkg/a.py", content="x = 1\ny = 2\n")
    assert ok and "Created pkg/a.py (2 lines)" in out
    ok, out = call(tools, "read_file", path="pkg/a.py")
    assert ok and "1\tx = 1" in out and "2\ty = 2" in out
    assert ws.mutations == 1


@pytest.mark.parametrize("bad", ["../evil.py", "/etc/passwd", "a/../../x", "C:/Windows/x.txt"])
def test_sandbox_blocks_escape(env, bad):
    _, tools = env
    ok, out = call(tools, "write_file", path=bad, content="x")
    assert not ok and "denied" in out or "escapes" in out


def test_agent_dir_reserved(env):
    _, tools = env
    ok, out = call(tools, "write_file", path=".agent/x", content="x")
    assert not ok and "reserved" in out


def test_edit_requires_prior_read(env, tmp_path):
    ws, tools = env
    (ws.root / "b.py").write_text("a = 1\n")
    ok, out = call(tools, "edit_file", path="b.py", old_string="a = 1", new_string="a = 2")
    assert not ok and "read 'b.py'" in out
    call(tools, "read_file", path="b.py")
    ok, _ = call(tools, "edit_file", path="b.py", old_string="a = 1", new_string="a = 2")
    assert ok and (ws.root / "b.py").read_text() == "a = 2\n"


def test_edit_requires_unique_match(env):
    ws, tools = env
    call(tools, "write_file", path="c.py", content="x = 0\nx = 0\n")
    ok, out = call(tools, "edit_file", path="c.py", old_string="x = 0", new_string="x = 1")
    assert not ok and "2 times" in out
    ok, out = call(tools, "edit_file", path="c.py", old_string="x = 0", new_string="x = 1", replace_all=True)
    assert ok and (ws.root / "c.py").read_text() == "x = 1\nx = 1\n"


def test_edit_not_found(env):
    _, tools = env
    call(tools, "write_file", path="d.py", content="a\n")
    ok, out = call(tools, "edit_file", path="d.py", old_string="zzz", new_string="y")
    assert not ok and "not found" in out


def test_write_files_batch_reports_partial_errors(env):
    ws, tools = env
    ok, out = call(tools, "write_files", files=[
        {"path": "one.py", "content": "1"}, {"path": "../two.py", "content": "2"}])
    assert ok and "Created one.py" in out and "ERRORS" in out
    assert (ws.root / "one.py").exists()


def test_todo_write_validation(env):
    ws, tools = env
    ok, _ = call(tools, "todo_write", todos=[{"content": "a", "status": "in_progress"},
                                             {"content": "b", "status": "pending"}])
    assert ok and ws.todos[0]["status"] == "in_progress"
    ok, out = call(tools, "todo_write", todos=[{"content": "a", "status": "in_progress"},
                                               {"content": "b", "status": "in_progress"}])
    assert not ok and "only one" in out
    ok, out = call(tools, "todo_write", todos=[{"content": "a", "status": "done"}])
    assert not ok


def test_list_and_search(env):
    ws, tools = env
    call(tools, "write_files", files=[{"path": "src/m.py", "content": "def hello():\n    pass\n"},
                                      {"path": "README.md", "content": "hello world"}])
    ok, out = call(tools, "list_files")
    assert "src/" in out and "m.py" in out
    ok, out = call(tools, "search_files", pattern=r"def \w+", glob="*.py")
    assert out.strip() == "src/m.py:1: def hello():"


def test_bad_json_and_unknown_tool_are_recoverable(env):
    _, tools = env
    ok, out = execute(tools, "write_file", '{"path": "x.py", "content": "unterminated')
    assert not ok and "could not parse" in out
    ok, out = execute(tools, "rm_rf", "{}")
    assert not ok and "unknown tool" in out
    ok, out = execute(tools, "read_file", json.dumps({"nope": 1}))
    assert not ok and "bad arguments" in out


def test_run_tests_tool_reports_failures_and_staleness(env):
    ws, tools = env
    call(tools, "write_files", files=[
        {"path": "tests/__init__.py", "content": ""},
        {"path": "tests/test_a.py", "content": "import unittest\nclass T(unittest.TestCase):\n"
                                               "    def test_x(self):\n        self.assertEqual(1, 2)\n"}])
    assert ws.tests_stale
    ok, out = call(tools, "run_tests")
    assert ok and out.startswith("TESTS FAILED") and ws.last_test_ok is False
    assert not ws.tests_stale


def test_finish_sets_flag(env):
    ws, tools = env
    ok, _ = call(tools, "finish", summary="done")
    assert ok and ws.finished and ws.finish_summary == "done"


def test_truncate_keeps_head_and_tail():
    text = "A" * 10_000 + "B" * 10_000
    out = truncate(text, 3000)
    assert out.startswith("A") and out.endswith("B") and "truncated" in out and len(out) < 3200


def test_schemas_are_valid_openai_tools(env):
    _, tools = env
    for t in tools.values():
        s = t.schema()
        assert s["type"] == "function" and s["function"]["parameters"]["type"] == "object"
