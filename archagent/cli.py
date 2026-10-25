"""Headless command line interface.

    ArchAgent.exe cli --doc Architecture_Documentation.md --views Architecture_View.md \
                      --out .\\space-fractions [--budget 1.0] [--no-thinking]
    ArchAgent.exe cli --preprocess-only --doc ... --views ... --out spec_dir

The API key is read from --api-key or the DEEPSEEK_API_KEY environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .agent.loop import Agent, AgentConfig
from .preprocess.spec_builder import build_spec


def _printer(event: dict) -> None:
    t = event["type"]
    if t == "tool_call":
        print(f"  -> {event['name']}({event['arguments']})")
    elif t == "tool_result":
        status = "ok" if event["ok"] else "ERROR"
        first = (event["output"].splitlines() or [""])[0][:150]
        print(f"     [{status}] {first}")
    elif t == "assistant":
        print(f"\n[assistant] {event['text'][:800]}")
    elif t == "turn":
        print(f"\n=== turn {event['n']}/{event['max']} ===")
    elif t == "cost":
        print(f"     cost ${event['cost_usd']:.4f} / ${event['budget_usd']:.2f}")
    elif t in ("phase", "status", "notice", "error"):
        print(f"[{t}] {event.get('text', '')}")
    elif t == "tests":
        print(f"[tests] {'PASSED' if event['ok'] else 'FAILED'}")
    elif t == "selftest":
        print(f"[selftest] {'OK' if event['ok'] else 'FAILED'}")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ArchAgent cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", required=True, help="architecture document (.md)")
    ap.add_argument("--views", help="architecture views with PlantUML (.md)")
    ap.add_argument("--out", required=True, help="output project directory")
    ap.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    ap.add_argument("--model", default="deepseek-v4-pro")
    ap.add_argument("--base-url", default="https://api.deepseek.com")
    ap.add_argument("--budget", type=float, default=1.0, help="hard USD cap (default 1.0)")
    ap.add_argument("--max-turns", type=int, default=60)
    ap.add_argument("--effort", default="high", choices=["low", "high", "max"])
    ap.add_argument("--no-thinking", action="store_true")
    ap.add_argument("--preprocess-only", action="store_true", help="only write spec.json")
    a = ap.parse_args(argv)

    if a.preprocess_only:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        spec = build_spec(a.doc, a.views)
        (out / "spec.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {out / 'spec.json'}")
        return 0

    if not a.api_key:
        print("error: provide --api-key or set DEEPSEEK_API_KEY", file=sys.stderr)
        return 2
    cfg = AgentConfig(api_key=a.api_key, doc_path=a.doc, views_path=a.views, output_dir=a.out,
                      model=a.model, base_url=a.base_url, thinking=not a.no_thinking,
                      reasoning_effort=a.effort, budget_usd=a.budget, max_turns=a.max_turns)
    t0 = time.time()
    result = Agent(cfg, on_event=_printer).run()
    print(f"\nStatus: {result.status} | cost ${result.cost['cost_usd']:.4f} | "
          f"tests: {result.tests_passed} | selftest: {result.selftest_ok} | {time.time() - t0:.0f}s")
    print(f"Report: {Path(result.output_dir) / '.agent' / 'run_report.md'}")
    return 0 if result.status == "completed" else 1
