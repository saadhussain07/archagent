"""Test doubles: a scripted LLM and a fake DeepSeek HTTP server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from archagent.llm.client import ChatResponse

MINI_PROJECT = {
    "main.py": (
        "import sys\n"
        "from mini.core import add\n\n"
        "def main():\n"
        "    if '--selftest' in sys.argv:\n"
        "        assert add(2, 3) == 5\n"
        "        print('SELFTEST OK')\n"
        "        return 0\n"
        "    import tkinter as tk\n"
        "    tk.Tk().mainloop()\n\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    ),
    "mini/__init__.py": "",
    "mini/core.py": "def add(a, b):\n    return a - b  # bug on purpose\n",
    "tests/__init__.py": "",
    "tests/test_core.py": (
        "import unittest\nfrom mini.core import add\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    ),
    "README.md": "# Mini\n",
    "requirements.txt": "# stdlib only\n",
    "Dockerfile": "FROM python:3.12-slim\nCOPY . /app\nWORKDIR /app\nCMD python -m unittest\n",
}


def tool_call(i: int, name: str, args: dict) -> dict:
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def response(tool_calls=None, content="", reasoning="thinking...", finish="tool_calls", usage=None):
    return ChatResponse(
        content=content, reasoning_content=reasoning, tool_calls=tool_calls or [],
        finish_reason=finish if tool_calls else "stop",
        usage=usage or {"prompt_tokens": 9000, "prompt_cache_hit_tokens": 8000,
                        "prompt_cache_miss_tokens": 1000, "completion_tokens": 800},
    )


class ScriptedClient:
    """Plays back a list of responses; records every request it receives."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def chat(self, messages, tools=None, max_tokens=0, on_retry=None):
        self.requests.append([dict(m) for m in messages])
        if not self.script:
            return response(content="done")
        item = self.script.pop(0)
        return item(messages) if callable(item) else item


def happy_path_script():
    """Plan -> write (with a bug) -> test fails -> read -> fix -> test -> finish."""
    files = [{"path": p, "content": c} for p, c in MINI_PROJECT.items()]
    return [
        response([tool_call(1, "todo_write", {"todos": [
            {"id": "1", "content": "write code", "status": "in_progress"},
            {"id": "2", "content": "test", "status": "pending"}]})]),
        response([tool_call(2, "write_files", {"files": files})]),
        response([tool_call(3, "run_tests", {})]),
        response([tool_call(4, "read_file", {"path": "mini/core.py"})]),
        response([tool_call(5, "edit_file", {"path": "mini/core.py",
                                             "old_string": "return a - b  # bug on purpose",
                                             "new_string": "return a + b"})]),
        response([tool_call(6, "run_tests", {})]),
        response([tool_call(7, "finish", {"summary": "Mini project done."})]),
    ]


class FakeDeepSeek(BaseHTTPRequestHandler):
    """Minimal /chat/completions endpoint. Behaviour driven by class attrs."""

    responses: list = []
    received: list = []
    status_codes: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        FakeDeepSeek.received.append({"auth": self.headers.get("Authorization"), "body": body})
        code = FakeDeepSeek.status_codes.pop(0) if FakeDeepSeek.status_codes else 200
        if code != 200:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": f"fake {code}"}}).encode())
            return
        payload = FakeDeepSeek.responses.pop(0) if FakeDeepSeek.responses else {
            "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start_fake_server():
    FakeDeepSeek.responses, FakeDeepSeek.received, FakeDeepSeek.status_codes = [], [], []
    server = HTTPServer(("127.0.0.1", 0), FakeDeepSeek)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"
