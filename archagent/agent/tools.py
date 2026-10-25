"""Tool system.

Mirrors the design of Claude Code's tool layer, simplified:

* one ``Tool`` interface (name, JSON schema, read-only flag, handler);
* every path is resolved inside the workspace root - nothing outside the
  chosen output directory can be read or written (sandbox);
* ``edit_file`` is search-and-replace with a *unique* match, and a file
  must have been read (or written) in this session before it can be
  edited - the same guard Claude Code uses against blind edits;
* large outputs are truncated with a note so the context stays small.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..runtime import runner

MAX_TOOL_OUTPUT = 12_000
MAX_FILE_BYTES = 400_000
IGNORED_DIRS = {".agent", "__pycache__", ".git", ".venv", "venv", "node_modules", ".pytest_cache"}


class ToolError(Exception):
    """An error reported back to the model as the tool result (not fatal)."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]
    read_only: bool = False

    def schema(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 2 // 3], text[-limit // 3:]
    return f"{head}\n\n[... {len(text) - len(head) - len(tail)} characters truncated ...]\n\n{tail}"


@dataclass
class Workspace:
    """State shared by all tools during one run."""

    root: Path
    todos: list[dict[str, str]] = field(default_factory=list)
    files_read: set[str] = field(default_factory=set)
    files_written: dict[str, int] = field(default_factory=dict)  # rel path -> write count
    last_test_ok: bool | None = None
    last_test_output: str = ""
    test_runs: int = 0
    finished: bool = False
    finish_summary: str = ""
    test_timeout: int = 180
    mutations: int = 0              # bumped by every write/edit
    tested_at_mutation: int = -1    # value of `mutations` when tests last ran

    @property
    def tests_stale(self) -> bool:
        return self.tested_at_mutation != self.mutations

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("path must be a non-empty string relative to the project root")
        rel = rel.strip().replace("\\", "/")
        if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
            # tolerate absolute paths that point inside the workspace
            p = Path(rel).resolve()
        else:
            p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"path '{rel}' escapes the project directory - access denied")
        if ".agent" in p.relative_to(self.root).parts:
            raise ToolError("'.agent/' is reserved for ArchAgent's own logs")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    def project_files(self) -> list[str]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not (set(p.relative_to(self.root).parts) & IGNORED_DIRS):
                out.append(self.rel(p))
        return out


# ---------------------------------------------------------------- handlers

def _todo_write(ws: Workspace, todos: list[dict[str, Any]]) -> str:
    valid = {"pending", "in_progress", "completed"}
    clean = []
    for i, t in enumerate(todos or []):
        status = str(t.get("status", "pending")).lower()
        if status not in valid:
            raise ToolError(f"todo #{i + 1}: status must be one of {sorted(valid)}")
        content = str(t.get("content", "")).strip()
        if not content:
            raise ToolError(f"todo #{i + 1}: content is empty")
        clean.append({"id": str(t.get("id", i + 1)), "content": content, "status": status})
    if sum(t["status"] == "in_progress" for t in clean) > 1:
        raise ToolError("only one todo may be in_progress at a time")
    ws.todos = clean
    done = sum(t["status"] == "completed" for t in clean)
    return f"Todo list updated ({done}/{len(clean)} completed)."


def _list_files(ws: Workspace, path: str = ".", max_depth: int = 4) -> str:
    base = ws.resolve(path)
    if not base.exists():
        return f"'{path}' does not exist (the project is empty so far)." if path in (".", "") else \
            f"'{path}' does not exist."
    if base.is_file():
        return f"{ws.rel(base)} ({base.stat().st_size} bytes)"
    lines = []
    for p in sorted(base.rglob("*")):
        rel_parts = p.relative_to(base).parts
        if set(p.relative_to(ws.root).parts) & IGNORED_DIRS or len(rel_parts) > max_depth:
            continue
        indent = "  " * (len(rel_parts) - 1)
        lines.append(f"{indent}{p.name}/" if p.is_dir() else f"{indent}{p.name}  ({p.stat().st_size} B)")
    return "\n".join(lines) if lines else "(empty directory)"


def _read_file(ws: Workspace, path: str, offset: int = 1, limit: int = 600) -> str:
    p = ws.resolve(path)
    if not p.is_file():
        raise ToolError(f"file '{path}' not found")
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = max(int(offset or 1), 1)
    limit = max(int(limit or 600), 1)
    chunk = lines[offset - 1: offset - 1 + limit]
    ws.files_read.add(ws.rel(p))
    numbered = "\n".join(f"{i:5d}\t{ln}" for i, ln in enumerate(chunk, start=offset))
    more = len(lines) - (offset - 1 + len(chunk))
    if more > 0:
        numbered += f"\n[... {more} more lines; call read_file with offset={offset + len(chunk)} ...]"
    return numbered or "(empty file)"


def _write_file(ws: Workspace, path: str, content: str) -> str:
    p = ws.resolve(path)
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ToolError(f"file too large (> {MAX_FILE_BYTES} bytes); split it into modules")
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")
    rel = ws.rel(p)
    ws.files_written[rel] = ws.files_written.get(rel, 0) + 1
    ws.files_read.add(rel)
    ws.mutations += 1
    n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    return f"{'Overwrote' if existed else 'Created'} {rel} ({n} lines)."


def _write_files(ws: Workspace, files: list[dict[str, str]]) -> str:
    if not files:
        raise ToolError("files is empty")
    results, errors = [], []
    for f in files:
        try:
            results.append(_write_file(ws, f.get("path", ""), f.get("content", "")))
        except ToolError as e:
            errors.append(f"{f.get('path')}: {e}")
    out = "\n".join(results)
    if errors:
        out += "\nERRORS:\n" + "\n".join(errors)
    return out


def _edit_file(ws: Workspace, path: str, old_string: str, new_string: str,
               replace_all: bool = False) -> str:
    p = ws.resolve(path)
    rel = ws.rel(p)
    if not p.is_file():
        raise ToolError(f"file '{path}' not found - use write_file to create it")
    if rel not in ws.files_read:
        raise ToolError(f"read '{rel}' with read_file before editing it")
    if old_string == new_string:
        raise ToolError("old_string and new_string are identical")
    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_string) if old_string else 0
    if count == 0:
        raise ToolError("old_string not found in file (it must match exactly, including indentation)")
    if count > 1 and not replace_all:
        raise ToolError(f"old_string occurs {count} times; add surrounding context to make it unique "
                        "or pass replace_all=true")
    text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    p.write_text(text, encoding="utf-8", newline="\n")
    ws.files_written[rel] = ws.files_written.get(rel, 0) + 1
    ws.mutations += 1
    return f"Edited {rel} ({count if replace_all else 1} replacement{'s' if replace_all and count > 1 else ''})."


def _search_files(ws: Workspace, pattern: str, glob: str = "*") -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"invalid regex: {e}")
    hits = []
    for rel in ws.project_files():
        if not fnmatch.fnmatch(Path(rel).name, glob) and not fnmatch.fnmatch(rel, glob):
            continue
        p = ws.root / rel
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 200:
                        return "\n".join(hits) + "\n[... more matches omitted ...]"
        except OSError:
            continue
    return "\n".join(hits) if hits else "No matches."


def _run_tests(ws: Workspace) -> str:
    ok, out = runner.run_tests(ws.root, timeout=ws.test_timeout)
    ws.test_runs += 1
    ws.tested_at_mutation = ws.mutations
    ws.last_test_ok = ok
    ws.last_test_output = out
    status = "ALL TESTS PASSED" if ok else "TESTS FAILED"
    return f"{status}\n{truncate(out, 8000)}"


def _run_python(ws: Workspace, script: str, args: list[str] | None = None, timeout: int = 60) -> str:
    p = ws.resolve(script)
    if not p.is_file():
        raise ToolError(f"script '{script}' not found")
    code, out = runner.run_script(ws.root, ws.rel(p), [str(a) for a in (args or [])],
                                  timeout=min(int(timeout or 60), 120))
    return f"exit code {code}\n{truncate(out, 8000) or '(no output)'}"


def _finish(ws: Workspace, summary: str) -> str:
    ws.finished = True
    ws.finish_summary = str(summary or "").strip()
    return "Finish requested; running deliverable verification."


# ---------------------------------------------------------------- registry

def build_tools(ws: Workspace) -> dict[str, Tool]:
    S = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
    tools = [
        Tool("todo_write",
             "Create or replace the task plan. Use it first to plan, then keep it updated: exactly one "
             "item in_progress while working, mark items completed as soon as they are done.",
             S({"todos": {"type": "array", "items": {"type": "object", "properties": {
                 "id": {"type": "string"}, "content": {"type": "string"},
                 "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                 "required": ["content", "status"]}}}, ["todos"]),
             lambda todos: _todo_write(ws, todos), read_only=True),
        Tool("list_files", "List files and folders in the project (recursive).",
             S({"path": {"type": "string", "description": "relative folder, default '.'"},
                "max_depth": {"type": "integer"}}, []),
             lambda path=".", max_depth=4: _list_files(ws, path, max_depth), read_only=True),
        Tool("read_file", "Read a text file with line numbers. Required before edit_file.",
             S({"path": {"type": "string"}, "offset": {"type": "integer", "description": "1-based start line"},
                "limit": {"type": "integer"}}, ["path"]),
             lambda path, offset=1, limit=600: _read_file(ws, path, offset, limit), read_only=True),
        Tool("search_files", "Regex search across project files. Returns path:line: text.",
             S({"pattern": {"type": "string"}, "glob": {"type": "string", "description": "e.g. '*.py'"}},
               ["pattern"]),
             lambda pattern, glob="*": _search_files(ws, pattern, glob), read_only=True),
        Tool("write_file", "Create or overwrite ONE file with the full content.",
             S({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
             lambda path, content: _write_file(ws, path, content)),
        Tool("write_files",
             "Create or overwrite SEVERAL files in one call (preferred: fewer round-trips, lower cost).",
             S({"files": {"type": "array", "items": {"type": "object", "properties": {
                 "path": {"type": "string"}, "content": {"type": "string"}},
                 "required": ["path", "content"]}}}, ["files"]),
             lambda files: _write_files(ws, files)),
        Tool("edit_file",
             "Replace an exact, unique snippet in an existing file. Prefer this over rewriting a whole "
             "file for small fixes. old_string must match exactly once unless replace_all=true.",
             S({"path": {"type": "string"}, "old_string": {"type": "string"},
                "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
               ["path", "old_string", "new_string"]),
             lambda path, old_string, new_string, replace_all=False:
                 _edit_file(ws, path, old_string, new_string, replace_all)),
        Tool("run_tests",
             "Syntax-check all .py files, then run `unittest` discovery on tests/ with the bundled "
             "Python. Returns the full test report.",
             S({}, []), lambda: _run_tests(ws), read_only=True),
        Tool("run_python",
             "Run a non-interactive Python script from the project (headless; no GUI windows) and "
             "return its output. Use for smoke checks, e.g. a --selftest flag.",
             S({"script": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"}}, ["script"]),
             lambda script, args=None, timeout=60: _run_python(ws, script, args, timeout), read_only=True),
        Tool("finish",
             "Call when every deliverable exists and run_tests passes. Give a short summary.",
             S({"summary": {"type": "string"}}, ["summary"]),
             lambda summary="": _finish(ws, summary), read_only=True),
    ]
    return {t.name: t for t in tools}


def execute(tools: dict[str, Tool], name: str, arguments: str | dict[str, Any]) -> tuple[bool, str]:
    """Run a tool call. Returns (ok, output). Never raises."""
    tool = tools.get(name)
    if tool is None:
        return False, f"Error: unknown tool '{name}'. Available: {', '.join(tools)}"
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        return False, (f"Error: could not parse arguments as JSON ({e}). If the content is very long, "
                       "split it across several write_file calls.")
    try:
        return True, truncate(tool.handler(**args))
    except ToolError as e:
        return False, f"Error: {e}"
    except TypeError as e:
        return False, f"Error: bad arguments for {name}: {e}"
    except Exception as e:  # noqa: BLE001 - surface to the model, keep the loop alive
        return False, f"Error: {type(e).__name__}: {e}"
