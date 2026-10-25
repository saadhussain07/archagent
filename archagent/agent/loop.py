"""The agent loop.

    preprocess -> [LLM call -> tool calls -> tool results]* -> verify -> report

Design points taken from Claude Code (simplified):
* a single while-loop: the model decides; the harness executes tools and
  feeds results back until the model stops calling tools / calls finish;
* recovery instead of crashing: bad tool arguments, unknown tools and
  failing tools become error *results* the model can react to; an output
  cut off by the token limit triggers a "continue / split it" message;
* a verification gate: ``finish`` is only accepted when the required
  deliverables exist and the test-suite passes (the harness re-runs the
  tests itself if anything changed since the last run - for free);
* a hard USD budget, with warnings to the model before it runs out;
* context compaction when the prompt grows large;
* everything is logged to ``<output>/.agent/`` for auditability.
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..llm.client import DeepSeekClient, LLMError
from ..llm.pricing import CostTracker
from ..llm.providers import get_provider
from ..preprocess.spec_builder import build_spec
from ..runtime import runner
from . import context, prompts
from .tools import Workspace, build_tools, execute, truncate

Event = dict[str, Any]
EventSink = Callable[[Event], None]

REQUIRED = {
    "main.py": "main.py (GUI entry point)",
    "README.md": "README.md",
    "requirements.txt": "requirements.txt",
    "Dockerfile": "Dockerfile",
}


@dataclass
class AgentConfig:
    api_key: str
    doc_path: str
    views_path: str | None
    output_dir: str
    model: str = "deepseek-v4-pro"
    base_url: str = "https://api.deepseek.com"
    provider: str = "deepseek"  # see llm/providers.py; non-DeepSeek = practice runs
    thinking: bool = True
    reasoning_effort: str = "high"
    budget_usd: float = 1.0
    max_turns: int = 60
    max_tokens_per_call: int = 32_000
    compact_threshold_tokens: int = 70_000
    extra_instructions: str = ""
    worst_case_pricing: bool = True


@dataclass
class RunResult:
    status: str  # "completed" | "stopped" | "budget" | "max_turns" | "error"
    summary: str
    turns: int
    cost: dict[str, Any]
    files: list[str]
    tests_passed: bool | None
    selftest_ok: bool | None
    output_dir: str
    elapsed_s: float
    error: str = ""
    verification: list[str] = field(default_factory=list)


class Agent:
    def __init__(self, config: AgentConfig, on_event: EventSink | None = None,
                 client: Any | None = None, stop_event: threading.Event | None = None):
        self.cfg = config
        self.emit: EventSink = on_event or (lambda e: None)
        self.stop_event = stop_event or threading.Event()
        self.provider = get_provider(config.provider)
        if config.max_tokens_per_call > self.provider.max_output_tokens:
            config.max_tokens_per_call = self.provider.max_output_tokens
        self.client = client or DeepSeekClient(
            config.api_key, model=config.model, base_url=config.base_url or self.provider.base_url,
            thinking=config.thinking, reasoning_effort=config.reasoning_effort,
            deepseek_api=self.provider.deepseek_api, extra_headers=self.provider.extra_headers,
            provider_name=self.provider.label.split(" - ")[0].split(" (")[0],
            max_retries=4 if self.provider.deepseek_api else 6)
        self.cost = CostTracker(model=config.model, budget_usd=config.budget_usd,
                                assume_peak=config.worst_case_pricing, free=self.provider.free)
        self.ws = Workspace(Path(config.output_dir))
        self.tools = build_tools(self.ws)
        self.messages: list[dict[str, Any]] = []
        self.agent_dir = self.ws.root / ".agent"
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self._transcript = (self.agent_dir / "transcript.jsonl").open("a", encoding="utf-8")
        self._warned = {"soft": False, "final": False}
        self._avg_output = 4000
        self._last_prompt_tokens = 0
        self._last_msg_count = 0
        self._last_miss_frac = 1.0

    # ------------------------------------------------------------- helpers
    def _event(self, kind: str, **data: Any) -> None:
        data["type"] = kind
        data["time"] = time.time()
        try:
            self.emit(data)
        except Exception:  # GUI problems must never kill the agent
            pass

    def _log(self, record: dict[str, Any]) -> None:
        record["ts"] = _dt.datetime.now().isoformat(timespec="seconds")
        self._transcript.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._transcript.flush()

    def _cost_event(self) -> None:
        self._event("cost", **self.cost.snapshot(), fraction=self.cost.fraction_used)

    def _missing_deliverables(self) -> list[str]:
        root = self.ws.root
        missing = [label for rel, label in REQUIRED.items() if not (root / rel).is_file()]
        tests_dir = root / "tests"
        if not (tests_dir.is_dir() and any(tests_dir.glob("test*.py"))):
            missing.append("tests/test_*.py (unittest test cases)")
        return missing

    def _verify(self) -> tuple[bool, list[str]]:
        """Deliverables + tests gate. Runs tests itself when stale (no tokens)."""
        missing = self._missing_deliverables()
        if self.ws.tests_stale and (self.ws.root / "tests").is_dir():
            self._event("status", text="Verifying: running the test-suite...")
            ok, out = runner.run_tests(self.ws.root, timeout=self.ws.test_timeout)
            self.ws.last_test_ok, self.ws.last_test_output = ok, out
            self.ws.tested_at_mutation = self.ws.mutations
            self._event("tests", ok=ok, output=truncate(out, 4000))
        problems = list(missing)
        if self.ws.last_test_ok is not True:
            problems.append("test-suite not passing")
        return not problems, problems

    # ----------------------------------------------------------------- run
    def run(self) -> RunResult:
        t0 = time.time()
        status, error, summary = "error", "", ""
        turn = 0
        verification_rejections = 0
        nudges = 0
        try:
            self._event("phase", name="preprocess", text="Preprocessing inputs into structured JSON")
            spec = build_spec(self.cfg.doc_path, self.cfg.views_path)
            (self.agent_dir / "spec.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False),
                                                      encoding="utf-8")
            d = spec["digest"]
            self._event("spec", system=d["system_name"], entities=len(d["entities"]),
                        use_cases=len(d["use_cases"]), diagrams=len(spec["architecture_views"]["index"]),
                        requirements=len(d["requirements"]),
                        path=str(self.agent_dir / "spec.json"))

            self.messages = [
                {"role": "system", "content": prompts.system_prompt(self.cfg.extra_instructions)},
                {"role": "user", "content": prompts.task_message(spec)},
            ]
            for m in self.messages:
                self._log({"kind": "message", **m})
            tool_schemas = [t.schema() for t in self.tools.values()]
            practice = "" if self.provider.deepseek_api else "  [PRACTICE RUN - not the required DeepSeek API]"
            self._event("phase", name="agent", text=f"Agent loop started ({self.cfg.model}){practice}")

            while True:
                if self.stop_event.is_set():
                    status, summary = "stopped", "Stopped by user."
                    break
                if turn >= self.cfg.max_turns:
                    status, summary = "max_turns", f"Reached the maximum of {self.cfg.max_turns} turns."
                    break

                # ---- budget guard (pessimistic estimate of the next call)
                prompt_est, miss_frac = self._estimate_next_prompt()
                out_est = min(self.cfg.max_tokens_per_call, self._avg_output * 2)
                est = self.cost.estimate_call_cost(int(prompt_est * miss_frac), out_est) + \
                    self.cost.estimate_cached_cost(int(prompt_est * (1 - miss_frac)))
                if self.cost.cost_usd + est > self.cost.budget_usd:
                    status = "budget"
                    summary = (f"Stopped before exceeding the ${self.cost.budget_usd:.2f} budget "
                               f"(spent ${self.cost.cost_usd:.3f}).")
                    break
                # cap output so even a maximal answer cannot cross the budget
                room = self.cost.budget_usd - self.cost.cost_usd - (est - self.cost.estimate_call_cost(0, out_est))
                max_tokens = max(1024, min(self.cfg.max_tokens_per_call, int(room / self.cost.output_rate())))
                # warn by remaining *calls*, so the model always gets a chance to wrap up
                frac = self.cost.fraction_used
                calls_left = self.cost.remaining_usd / est if est > 0 else 99
                if (frac >= 0.80 or calls_left < 3.5) and not self._warned["final"]:
                    self._warned["final"] = True
                    self._push_user(prompts.budget_notice(self.cost.cost_usd, self.cost.budget_usd, True))
                elif (frac >= 0.55 or calls_left < 7) and not self._warned["soft"]:
                    self._warned["soft"] = True
                    self._push_user(prompts.budget_notice(self.cost.cost_usd, self.cost.budget_usd, False))

                turn += 1
                self._event("turn", n=turn, max=self.cfg.max_turns)
                resp = self.client.chat(
                    self.messages, tools=tool_schemas, max_tokens=max_tokens,
                    on_retry=lambda a, e, d: self._event("status", text=f"Retry {a}: {e} (waiting {d:.0f}s)"))
                call_cost = self.cost.add(resp.usage)
                self._last_prompt_tokens = int(resp.usage.get("prompt_tokens", 0) or 0)
                self._last_msg_count = len(self.messages)
                hit = int(resp.usage.get("prompt_cache_hit_tokens", 0) or 0)
                self._last_miss_frac = 1 - hit / self._last_prompt_tokens if self._last_prompt_tokens else 1.0
                out_tokens = int(resp.usage.get("completion_tokens", 0) or 0)
                self._avg_output = int(0.6 * self._avg_output + 0.4 * max(out_tokens, 500))
                self._cost_event()
                msg = resp.as_message()
                self.messages.append(msg)
                self._log({"kind": "message", **msg, "usage": resp.usage, "cost_usd": call_cost})
                if resp.reasoning_content:
                    self._event("reasoning", text=resp.reasoning_content)
                if resp.content.strip():
                    self._event("assistant", text=resp.content)

                if resp.tool_calls:
                    nudges = 0
                    finish_requested = self._run_tool_calls(resp.tool_calls)
                    if finish_requested:
                        ok, problems = self._verify()
                        if ok or verification_rejections >= 3:
                            status = "completed"
                            summary = self.ws.finish_summary or "Project generated."
                            if not ok:
                                summary += f" (finished with open issues: {', '.join(problems)})"
                            break
                        verification_rejections += 1
                        self.ws.finished = False
                        self._event("verification", ok=False, problems=problems)
                        self._push_user(prompts.verification_notice(
                            [p for p in problems if p != "test-suite not passing"],
                            self.ws.last_test_ok, self.ws.last_test_output))
                    if resp.finish_reason == "length":
                        self._push_user("[ArchAgent] Your last output hit the token limit. Write large "
                                        "files one at a time or split them into smaller modules.")
                    continue

                # ---- no tool calls
                if resp.finish_reason == "length":
                    self._push_user("[ArchAgent] Output was cut off by the token limit. Continue, "
                                    "writing large files in smaller pieces via tools.")
                    continue
                ok, problems = self._verify()
                if ok:
                    status, summary = "completed", resp.content.strip()[:2000] or "Project generated."
                    break
                nudges += 1
                if nudges > 2:
                    status, summary = "completed", "Model stopped calling tools; see open issues."
                    break
                self._push_user(prompts.verification_notice(
                    [p for p in problems if p != "test-suite not passing"],
                    self.ws.last_test_ok, self.ws.last_test_output)
                    + "\nKeep working using the tools; do not reply with plain text only.")

        except LLMError as e:
            status, error = "error", str(e)
            summary = f"LLM error: {e}"
            self._event("error", text=summary)
        except Exception as e:  # noqa: BLE001
            import traceback
            status, error = "error", f"{type(e).__name__}: {e}"
            summary = error
            self._event("error", text=traceback.format_exc())

        return self._finalise(status, summary, error, turn, time.time() - t0)

    def _estimate_next_prompt(self) -> tuple[int, float]:
        """(prompt tokens, expected uncached fraction) for the next request.

        Uses the real token count of the previous request plus an estimate
        for the messages appended since; new messages are always uncached.
        """
        if not self._last_prompt_tokens:
            return context.estimate_tokens(self.messages), 1.0
        new = context.estimate_tokens(self.messages[self._last_msg_count:])
        total = max(self._last_prompt_tokens + new, context.estimate_tokens(self.messages))
        miss_tokens = self._last_prompt_tokens * min(1.0, self._last_miss_frac + 0.1) + new
        return total, min(1.0, miss_tokens / total)

    def _push_user(self, text: str) -> None:
        msg = {"role": "user", "content": text}
        self.messages.append(msg)
        self._log({"kind": "message", **msg})
        self._event("notice", text=text)

    def _run_tool_calls(self, tool_calls: list[dict[str, Any]]) -> bool:
        finish = False
        for tc in tool_calls:
            name = tc["function"]["name"]
            args = tc["function"].get("arguments") or "{}"
            self._event("tool_call", id=tc["id"], name=name, arguments=_preview_args(name, args))
            started = time.time()
            if self.stop_event.is_set():
                ok, output = False, "Cancelled: the user stopped the agent."
            else:
                ok, output = execute(self.tools, name, args)
            self.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": output})
            self._log({"kind": "tool", "name": name, "ok": ok, "output": output,
                       "seconds": round(time.time() - started, 2)})
            self._event("tool_result", id=tc["id"], name=name, ok=ok, output=truncate(output, 3000),
                        seconds=round(time.time() - started, 2))
            if name == "todo_write" and ok:
                self._event("todos", todos=list(self.ws.todos))
            if name in ("write_file", "write_files", "edit_file"):
                self._event("files", files=self.ws.project_files())
            if name == "run_tests":
                self._event("tests", ok=self.ws.last_test_ok, output=truncate(self.ws.last_test_output, 4000))
            if name == "finish" and ok:
                finish = True
        self._maybe_compact()
        return finish

    def _maybe_compact(self) -> None:
        est = context.estimate_tokens(self.messages)
        if est > self.cfg.compact_threshold_tokens:
            saved = context.compact(self.messages)
            if saved:
                self._event("status", text=f"Context compacted: ~{saved // 4} tokens elided "
                                           f"(was ~{est} tokens)")

    # ------------------------------------------------------------- finalise
    def _finalise(self, status: str, summary: str, error: str, turns: int, elapsed: float) -> RunResult:
        files = self.ws.project_files()
        tests_ok = self.ws.last_test_ok
        selftest_ok = None
        if files:  # final, token-free checks of whatever was generated
            if self.ws.tests_stale and (self.ws.root / "tests").is_dir():
                tests_ok, out = runner.run_tests(self.ws.root, timeout=self.ws.test_timeout)
                self.ws.last_test_ok, self.ws.last_test_output = tests_ok, out
                self.ws.tested_at_mutation = self.ws.mutations
                self._event("tests", ok=tests_ok, output=truncate(out, 4000))
            if (self.ws.root / "main.py").is_file():
                code, out = runner.run_script(self.ws.root, "main.py", ["--selftest"], timeout=60)
                selftest_ok = code == 0 and "SELFTEST OK" in out
                self._event("selftest", ok=selftest_ok, output=truncate(out, 2000))
        _, problems = self._verify() if files else (False, ["no files generated"])
        result = RunResult(status=status, summary=summary, turns=turns, cost=self.cost.snapshot(),
                           files=files, tests_passed=tests_ok, selftest_ok=selftest_ok,
                           output_dir=str(self.ws.root), elapsed_s=round(elapsed, 1), error=error,
                           verification=problems)
        self._write_report(result)
        self._transcript.close()
        self._event("done", result=result.__dict__)
        return result

    def _write_report(self, r: RunResult) -> None:
        (self.agent_dir / "run_report.json").write_text(json.dumps(r.__dict__, indent=2), encoding="utf-8")
        c = r.cost
        test_line = {True: "PASSED", False: "FAILED", None: "not run"}[r.tests_passed]
        lines = [
            "# ArchAgent run report", "",
            f"- Status: **{r.status}**",
            f"- Provider: {self.provider.label} (`{self.cfg.base_url or self.provider.base_url}`)",
            f"- Model: `{c['model']}` (thinking: {self.cfg.thinking and self.provider.deepseek_api}, "
            f"effort: {self.cfg.reasoning_effort})",
            f"- Turns: {r.turns} | LLM calls: {c['calls']} | Time: {r.elapsed_s:.0f}s",
            f"- Tokens: {c['prompt_cache_hit_tokens']:,} cached input, {c['prompt_cache_miss_tokens']:,} "
            f"uncached input, {c['completion_tokens']:,} output ({c['reasoning_tokens']:,} reasoning)",
            f"- Cost: **${c['cost_usd']:.4f}** of ${c['budget_usd']:.2f} budget ({c['pricing_mode']} pricing)",
            f"- Tests: {test_line} | Self-test: {r.selftest_ok}",
            f"- Open issues: {', '.join(r.verification) or 'none'}", "",
            "## Summary", "", r.summary or "-", "",
            "## Generated files", "", *[f"- `{f}`" for f in r.files],
        ]
        if r.error:
            lines += ["", "## Error", "", "```", r.error, "```"]
        (self.agent_dir / "run_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _preview_args(name: str, arguments: str) -> str:
    """Human-readable one-liner of a tool call for the GUI log."""
    try:
        a = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return arguments[:200]
    if name == "write_files":
        return ", ".join(f"{f.get('path')} ({len(f.get('content', ''))} ch)" for f in a.get("files", []))
    if name == "write_file":
        return f"{a.get('path')} ({len(a.get('content', ''))} ch)"
    if name == "edit_file":
        return f"{a.get('path')}"
    if name == "todo_write":
        return f"{len(a.get('todos', []))} items"
    return json.dumps(a, ensure_ascii=False)[:200]
