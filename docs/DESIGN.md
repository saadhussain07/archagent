# ArchAgent: design notes

This document explains how ArchAgent works and which parts of Claude Code's design it adopts, simplifies or leaves out.

## 1. Goals and constraints (from the task)

| Requirement | How it is met |
|---|---|
| Simplified code agent based on open-source Claude Code research | Agent loop, tool system, edit rules, todo planning, compaction, verification (sections 3–7) |
| LLM = `deepseek-v4-pro`, official API | `llm/client.py`, OpenAI-compatible `POST https://api.deepseek.com/chat/completions` |
| Windows desktop `.exe`, double-click, no Python on target | PyInstaller one-file, windowed; generated code runs on the **embedded** interpreter (section 8) |
| Minimal GUI: API key, input files, trigger, output dir | `gui/app.py` |
| Input: architecture doc (preprocess to JSON) + UML views | `preprocess/` → `spec.json` (section 2) |
| Output: layout + source, requirements.txt, Dockerfile, README, tests | System prompt deliverables + verification gate |
| Generated game is a desktop app with UI | Target platform pinned to Python/Tkinter in the system prompt |
| Token cost under $1 | Cost tracker + budget guard + cache-friendly prompts (section 6) |

## 2. Preprocessing: Markdown and PlantUML to JSON

This step is deterministic and uses no tokens.

* `markdown_parser.py` builds a heading tree with paragraphs, bullets, tables (as row dicts) and code blocks. It then extracts:
  * `requirements`: FR, NFR and ASR IDs, merged from every table they appear in and from prose such as "meets ASR-2 (security)";
  * `components`, `tech_stack` (options with justification, plus the recommended stack) and `deliverables`.
* `plantuml_parser.py` detects the diagram kind from the `@startuml` name, falling back to content heuristics, and parses all 11 kinds:
  * classes, with members, visibility, parameters and return types;
  * relations, with kind and diamond position;
  * the state machine: initial, terminal and triggered transitions;
  * activity flow, with nested decisions;
  * sequence and collaboration messages, with call/return classification and `Scenario:` notes;
  * packages and node graphs.
* `spec_builder.py` adds a cross-view **digest**: the facts the generated code must honour. It also adds source hashes for traceability.

The model gets a grounded contract and does not spend reasoning tokens re-parsing UML. The JSON is about 29k characters and is cached after the first turn.

## 3. Agent loop (`agent/loop.py`)

```
messages = [system, task(spec)]
loop:
    stop? / max turns? / budget guard (estimate next call)  → exit
    resp = deepseek.chat(messages, tools)                    # thinking mode, reasoning passed back
    append assistant message (content + reasoning_content + tool_calls)
    if tool calls:   run each → append tool results → compact if large
                     if finish: verification gate → done | push "rejected because ..."
    else:            verify → done | nudge (max 2)
finalise: harness runs tests + main.py --selftest, writes run report
```

Like Claude Code's main loop, errors are recovered rather than fatal:

* Invalid JSON arguments, unknown tools, a bad argument signature or a handler exception all come back to the model as an `Error: ...` tool result.
* `finish_reason == "length"` (output cut off) leads to a message asking the model to write large files in smaller pieces.
* LLM HTTP errors are retried with exponential backoff (429, 5xx, network). 401 and 402 fail fast with a readable message.

**DeepSeek specifics.** With thinking mode and tools, `reasoning_content` must be passed back on every later request, or the API returns 400. `ChatResponse.as_message()` keeps it, and compaction never touches it.

## 4. Tool system (`agent/tools.py`)

Claude Code runs every tool through one interface and one permission pipeline. ArchAgent's equivalent:

* a `Tool(name, description, JSON schema, handler, read_only)` dataclass;
* `Workspace.resolve()` acts as the permission layer: absolute paths, `..` and drive letters are resolved and must stay inside the output folder, and `.agent/` is reserved;
* `edit_file` follows Claude Code's editing strategy: search-and-replace, the file must have been read first, `old_string` must match exactly once unless `replace_all` is set, and a no-op edit is rejected;
* `write_files` is a batching tool added for cost: one round trip can create a dozen files;
* `run_tests` syntax-checks every `.py` file in memory, then runs `unittest` discovery in a child process with a timeout;
* `run_python` runs a headless script (for example `--selftest`) with a timeout, and refuses paths outside the project;
* outputs are truncated (head and tail are kept) to protect the context window.

There is no general shell tool. That reduces the blast radius, and the target machine has no shell tooling anyway. The two runner tools cover what the agent needs: compile, test and smoke-run.

## 5. Planning and verification

* `todo_write` corresponds to Claude Code's TodoWrite: a list of items, each `pending`, `in_progress` or `completed`, with exactly one in progress at a time. It is rendered live in the GUI's *Plan* tab.
* **Verification gate** (Claude Code uses "verification reminders"). `finish` is rejected until the required deliverables exist and the tests pass. The harness tracks a mutation counter. If anything changed after the last test run, it re-runs the tests itself, so the model cannot finish on stale results and no tokens are spent. After 3 rejections it accepts and records the open issues in the report.

## 6. Context, caching and cost

* **Prefix caching.** The system prompt and the task message (with the spec) never change during a run, and the tests assert this. DeepSeek bills cached input at $0.044/M against $1.32/M uncached (peak), so most input costs about 1/30 of the full price.
* **Compaction** (`agent/context.py`) mirrors the first stages of Claude Code's pipeline: trim large old tool outputs, then fold old file bodies in write and edit arguments. The last 8 messages, the cached prefix and reasoning are kept. It is deterministic and needs no summarisation call.
* **Budget guard** (`llm/pricing.py`, loop):
  * cost is computed from real `usage` at peak rates by default (a worst-case figure);
  * the next request is estimated as the previous real prompt plus the new messages, with the new messages treated as uncached;
  * the run stops if the next call could cross the budget, and `max_tokens` is capped to the money left;
  * warnings go to the model at roughly 7 and 3 calls remaining.
* **Estimate for this task:** about 28 turns, a prefix of about 10k tokens, about 95k output tokens (code plus reasoning). That gives **≈ $0.30 off-peak, ≈ $0.61 peak**.

## 7. Prompt design (`agent/prompts.py`)

The system prompt is layered: identity → environment (fixed target platform and the stdlib whitelist) → workflow (plan, design, implement, verify, finish, plus efficiency rules) → quality bar (layering, explicit state machine, SQLite durability, GUI polish, tests that never open a window, `--selftest`) → deliverables.

It also tells the model how to *map* the microservice spec onto a desktop architecture, so the architecture is honoured rather than ignored. The generated README must document that mapping and a traceability matrix.

## 8. Packaging: why the target needs no Python

PyInstaller bundles the CPython runtime, Tk/Tcl and every stdlib module on the whitelist (`BUNDLED_STDLIB`, imported by the `.spec` file so the two lists cannot drift apart). When frozen, `sys.executable` is `ArchAgent.exe`, so the runner starts **itself** in a hidden child mode:

```
ArchAgent.exe --child run-tests  <project> <out.txt>
ArchAgent.exe --child run-script <project> <out.txt> main.py --selftest
ArchAgent.exe --child launch     <project> main.py        # "Launch app" button
```

Output goes to a file, not a pipe, because a windowed PyInstaller app has `sys.stdout = None`. CI runs these child modes against the frozen exe on `windows-latest` before uploading it.

## 9. Not included (deliberately)

* Sub-agents, MCP, hooks, plan mode and a memory system. They are out of scope for a single-task generator.
* Streaming. The GUI shows per-turn progress, which keeps the client simple and the retry logic robust.
* An LLM-based summary compaction stage. It was not needed at this project size, and it would break the cache prefix.
