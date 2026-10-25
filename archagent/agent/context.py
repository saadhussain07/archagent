"""Context management (a simplified version of Claude Code's compaction).

Claude Code compacts in stages: trim large old tool outputs first, then
fold old segments, and only summarise as a last resort. We do the first
two stages, deterministically and without extra LLM calls:

1. old tool results longer than ``keep_chars`` are replaced by a stub;
2. old ``write_file(s)`` / ``edit_file`` call arguments have their file
   bodies replaced by a stub (the content is on disk; the model can
   ``read_file`` it again if it really needs to).

The most recent ``keep_recent`` messages are never touched, and the
system prompt + task message (the cached prefix) are never touched.
``reasoning_content`` is left intact because DeepSeek requires it to be
passed back in tool-calling conversations.
"""

from __future__ import annotations

import json
from typing import Any

PREFIX_MESSAGES = 2  # system + task message


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough estimate (~3.5 chars/token for code-heavy JSON)."""
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "") + len(m.get("reasoning_content") or "")
        for tc in m.get("tool_calls") or []:
            chars += len(tc["function"].get("arguments") or "")
    return int(chars / 3.5)


def _stub_arguments(name: str, arguments: str) -> str | None:
    try:
        args = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return None
    changed = False
    if name == "write_file" and len(args.get("content", "")) > 200:
        args["content"] = f"<{len(args['content'])} chars written to disk; read_file to view>"
        changed = True
    elif name == "write_files":
        for f in args.get("files", []):
            if len(f.get("content", "")) > 200:
                f["content"] = f"<{len(f['content'])} chars written to disk>"
                changed = True
    elif name == "edit_file":
        for key in ("old_string", "new_string"):
            if len(args.get(key, "")) > 300:
                args[key] = args[key][:120] + f"...<{len(args[key])} chars>"
                changed = True
    return json.dumps(args, ensure_ascii=False) if changed else None


def compact(messages: list[dict[str, Any]], keep_recent: int = 8, keep_chars: int = 600) -> int:
    """Compact ``messages`` in place. Returns the number of characters saved."""
    saved = 0
    end = max(PREFIX_MESSAGES, len(messages) - keep_recent)
    for m in messages[PREFIX_MESSAGES:end]:
        if m.get("role") == "tool":
            content = m.get("content") or ""
            if len(content) > keep_chars and not content.startswith("[compacted]"):
                first = content.splitlines()[0][:200] if content else ""
                m["content"] = f"[compacted] {first} ... ({len(content)} chars elided)"
                saved += len(content) - len(m["content"])
        elif m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                new = _stub_arguments(fn["name"], fn.get("arguments") or "")
                if new is not None:
                    saved += len(fn["arguments"]) - len(new)
                    fn["arguments"] = new
    return saved
