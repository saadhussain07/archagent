"""Tkinter desktop GUI for ArchAgent.

Left: configuration (API key, model, inputs, output folder, budget).
Right: live view of the agent - activity log, plan (todos), generated
files with preview, test output and the preprocessed JSON spec.
Bottom: turn counter, token usage and a cost bar against the budget.

The agent runs in a worker thread; it talks to the UI only through a
queue that the Tk main loop polls, so the window never freezes.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import APP_NAME, __version__
from ..agent.loop import Agent, AgentConfig
from ..config import Settings
from ..llm.client import DeepSeekClient, LLMError
from ..preprocess.spec_builder import build_spec
from ..runtime import runner

# palette
INK = "#1d2433"
MUTED = "#5b6475"
BG = "#f4f6fa"
CARD = "#ffffff"
ACCENT = "#3b5bdb"
ACCENT_DARK = "#2f49b0"
OK = "#2b8a3e"
ERR = "#c92a2a"
WARN = "#e67700"
HEADER = "#151b2b"
LOG_BG = "#0f1420"
LOG_FG = "#d7dce6"


def _font_family() -> str:
    return "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"


def _mono_family() -> str:
    return "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings.load()
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.started_at = 0.0
        self.last_output_dir: Path | None = None

        root.title(f"{APP_NAME} {__version__} - DeepSeek code agent")
        root.geometry("1280x800")
        root.minsize(1080, 680)
        root.configure(bg=BG)
        self._style()
        self._build()
        self._load_settings_into_form()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)

    # ------------------------------------------------------------------ style
    def _style(self) -> None:
        f, m = _font_family(), _mono_family()
        self.f_base = (f, 10)
        self.f_bold = (f, 10, "bold")
        self.f_small = (f, 9)
        self.f_title = (f, 16, "bold")
        self.f_mono = (m, 10)
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", font=self.f_base, background=BG, foreground=INK)
        st.configure("Card.TFrame", background=CARD)
        st.configure("Card.TLabel", background=CARD, foreground=INK)
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=self.f_small)
        st.configure("Section.TLabel", background=CARD, foreground=ACCENT, font=self.f_bold)
        st.configure("Card.TCheckbutton", background=CARD)
        st.map("Card.TCheckbutton", background=[("active", CARD)])
        st.configure("TEntry", fieldbackground="#fbfcfe", padding=4)
        st.configure("TButton", padding=(10, 5))
        st.configure("Accent.TButton", background=ACCENT, foreground="white", font=self.f_bold,
                     padding=(14, 8), borderwidth=0)
        st.map("Accent.TButton", background=[("disabled", "#9aa7d6"), ("active", ACCENT_DARK)])
        st.configure("Stop.TButton", foreground=ERR, padding=(14, 8))
        st.configure("Status.TLabel", background=BG, foreground=MUTED, font=self.f_small)
        st.configure("Cost.Horizontal.TProgressbar", troughcolor="#e3e7ef", background=OK,
                     bordercolor="#e3e7ef", lightcolor=OK, darkcolor=OK)
        st.configure("Treeview", rowheight=24, fieldbackground=CARD, background=CARD)
        st.configure("Treeview.Heading", font=self.f_bold)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", padding=(14, 6))

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        header = tk.Frame(self.root, bg=HEADER, height=58)
        header.pack(fill="x")
        tk.Label(header, text="◆ ArchAgent", bg=HEADER, fg="white", font=self.f_title).pack(
            side="left", padx=(18, 10), pady=12)
        tk.Label(header, text="Architecture docs + UML  →  a working, tested desktop app  ·  "
                              "powered by DeepSeek", bg=HEADER, fg="#9aa4bd", font=self.f_base).pack(
            side="left", pady=12)

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body, style="Card.TFrame", padding=16)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        self._build_settings(left)

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self._build_tabs(right)

        self._build_statusbar()

    def _section(self, parent, text: str, row: int) -> int:
        ttk.Label(parent, text=text.upper(), style="Section.TLabel").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(12 if row else 0, 4))
        return row + 1

    def _build_settings(self, p: ttk.Frame) -> None:
        self.v_key = tk.StringVar()
        self.v_show = tk.BooleanVar(value=False)
        self.v_remember = tk.BooleanVar()
        self.v_model = tk.StringVar()
        self.v_thinking = tk.BooleanVar()
        self.v_effort = tk.StringVar()
        self.v_doc = tk.StringVar()
        self.v_views = tk.StringVar()
        self.v_out = tk.StringVar()
        self.v_budget = tk.StringVar()
        self.v_turns = tk.StringVar()
        p.columnconfigure(1, weight=1)

        r = self._section(p, "DeepSeek API", 0)
        ttk.Label(p, text="API key", style="Card.TLabel").grid(row=r, column=0, sticky="w")
        self.e_key = ttk.Entry(p, textvariable=self.v_key, show="•", width=34)
        self.e_key.grid(row=r, column=1, sticky="ew", padx=6)
        ttk.Button(p, text="Test", command=self._test_key, width=6).grid(row=r, column=2)
        r += 1
        flags = ttk.Frame(p, style="Card.TFrame")
        flags.grid(row=r, column=1, columnspan=2, sticky="w", padx=6)
        ttk.Checkbutton(flags, text="Show", variable=self.v_show, style="Card.TCheckbutton",
                        command=lambda: self.e_key.configure(show="" if self.v_show.get() else "•")
                        ).pack(side="left")
        ttk.Checkbutton(flags, text="Remember key", variable=self.v_remember,
                        style="Card.TCheckbutton").pack(side="left", padx=10)
        r += 1
        ttk.Label(p, text="Model", style="Card.TLabel").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Combobox(p, textvariable=self.v_model, values=["deepseek-v4-pro", "deepseek-flash"],
                     width=20).grid(row=r, column=1, sticky="w", padx=6)
        r += 1
        ttk.Label(p, text="Reasoning", style="Card.TLabel").grid(row=r, column=0, sticky="w")
        think = ttk.Frame(p, style="Card.TFrame")
        think.grid(row=r, column=1, columnspan=2, sticky="w", padx=6)
        ttk.Checkbutton(think, text="Thinking mode", variable=self.v_thinking,
                        style="Card.TCheckbutton").pack(side="left")
        ttk.Combobox(think, textvariable=self.v_effort, values=["low", "high", "max"], width=6,
                     state="readonly").pack(side="left", padx=8)

        r = self._section(p, "Inputs", r + 1)
        self._file_row(p, r, "Architecture doc", self.v_doc, self._pick_doc)
        r += 1
        self._file_row(p, r, "UML views", self.v_views, self._pick_views)
        r += 1
        ttk.Button(p, text="Preview structured JSON", command=self._preview_spec).grid(
            row=r, column=1, sticky="w", padx=6, pady=(4, 0))

        r = self._section(p, "Output", r + 1)
        self._file_row(p, r, "Project folder", self.v_out, self._pick_out)

        r = self._section(p, "Limits", r + 1)
        ttk.Label(p, text="Budget (USD)", style="Card.TLabel").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(p, from_=0.05, to=5.0, increment=0.05, textvariable=self.v_budget, width=8).grid(
            row=r, column=1, sticky="w", padx=6)
        r += 1
        ttk.Label(p, text="Max turns", style="Card.TLabel").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Spinbox(p, from_=5, to=200, increment=5, textvariable=self.v_turns, width=8).grid(
            row=r, column=1, sticky="w", padx=6)
        r += 1
        ttk.Label(p, text="Cost is priced at DeepSeek peak rates (worst case);\nthe agent stops "
                          "before the budget is exceeded.", style="Muted.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))

        r += 1
        actions = ttk.Frame(p, style="Card.TFrame")
        actions.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(18, 6))
        self.b_start = ttk.Button(actions, text="▶  Generate project", style="Accent.TButton",
                                  command=self._start)
        self.b_start.pack(side="left", fill="x", expand=True)
        self.b_stop = ttk.Button(actions, text="■ Stop", style="Stop.TButton", command=self._stop,
                                 state="disabled")
        self.b_stop.pack(side="left", padx=(8, 0))

        r += 1
        after = ttk.Frame(p, style="Card.TFrame")
        after.grid(row=r, column=0, columnspan=3, sticky="ew")
        self.b_open = ttk.Button(after, text="Open folder", command=self._open_folder)
        self.b_tests = ttk.Button(after, text="Run tests", command=self._rerun_tests)
        self.b_launch = ttk.Button(after, text="Launch app", command=self._launch_app)
        for b in (self.b_open, self.b_tests, self.b_launch):
            b.pack(side="left", expand=True, fill="x", padx=(0, 4))

    def _file_row(self, p, r, label, var, cmd) -> None:
        ttk.Label(p, text=label, style="Card.TLabel").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(p, textvariable=var, width=34).grid(row=r, column=1, sticky="ew", padx=6)
        ttk.Button(p, text="…", width=3, command=cmd).grid(row=r, column=2)

    def _build_tabs(self, parent) -> None:
        nb = ttk.Notebook(parent)
        nb.grid(row=0, column=0, sticky="nsew")
        self.nb = nb

        # Activity log
        f_log = ttk.Frame(nb)
        self.t_log = self._text(f_log, dark=True)
        for tag, color in (("tool", "#8fb4ff"), ("ok", "#69db7c"), ("err", "#ff8787"),
                           ("assistant", "#ffffff"), ("think", "#7c8599"), ("phase", "#ffd43b"),
                           ("notice", "#ffa94d"), ("dim", "#7c8599")):
            self.t_log.tag_configure(tag, foreground=color)
        self.t_log.tag_configure("assistant", font=(_font_family(), 10))
        nb.add(f_log, text="Activity")

        # Plan
        f_plan = ttk.Frame(nb, padding=8)
        self.tv_plan = ttk.Treeview(f_plan, columns=("status",), show="tree headings", selectmode="none")
        self.tv_plan.heading("#0", text="Task")
        self.tv_plan.heading("status", text="Status")
        self.tv_plan.column("status", width=120, anchor="center", stretch=False)
        self.tv_plan.tag_configure("completed", foreground=OK)
        self.tv_plan.tag_configure("in_progress", foreground=ACCENT, font=self.f_bold)
        self.tv_plan.tag_configure("pending", foreground=MUTED)
        self.tv_plan.pack(fill="both", expand=True)
        nb.add(f_plan, text="Plan")

        # Files
        f_files = ttk.Frame(nb, padding=8)
        pw = ttk.PanedWindow(f_files, orient="horizontal")
        pw.pack(fill="both", expand=True)
        lf = ttk.Frame(pw)
        self.tv_files = ttk.Treeview(lf, show="tree", selectmode="browse")
        self.tv_files.pack(fill="both", expand=True)
        self.tv_files.bind("<<TreeviewSelect>>", self._on_file_select)
        pw.add(lf, weight=1)
        rf = ttk.Frame(pw)
        self.t_preview = self._text(rf)
        pw.add(rf, weight=3)
        nb.add(f_files, text="Files")

        # Tests
        f_tests = ttk.Frame(nb)
        self.t_tests = self._text(f_tests)
        nb.add(f_tests, text="Tests")

        # Spec
        f_spec = ttk.Frame(nb)
        self.t_spec = self._text(f_spec)
        nb.add(f_spec, text="Spec JSON")

    def _text(self, parent, dark: bool = False) -> tk.Text:
        frame = tk.Frame(parent, bg=LOG_BG if dark else CARD)
        frame.pack(fill="both", expand=True)
        t = tk.Text(frame, wrap="word", font=self.f_mono, relief="flat", borderwidth=0,
                    padx=12, pady=10, bg=LOG_BG if dark else CARD, fg=LOG_FG if dark else INK,
                    insertbackground=LOG_FG, state="disabled")
        sb = ttk.Scrollbar(frame, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        return t

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(14, 0, 14, 10))
        bar.pack(fill="x")
        self.l_status = ttk.Label(bar, text="Ready.", style="Status.TLabel")
        self.l_status.pack(side="left")
        self.l_cost = ttk.Label(bar, text="$0.0000 / $1.00", style="Status.TLabel")
        self.l_cost.pack(side="right")
        self.pb_cost = ttk.Progressbar(bar, length=180, maximum=1.0, style="Cost.Horizontal.TProgressbar")
        self.pb_cost.pack(side="right", padx=8)
        self.l_tokens = ttk.Label(bar, text="", style="Status.TLabel")
        self.l_tokens.pack(side="right", padx=12)
        self.l_turn = ttk.Label(bar, text="", style="Status.TLabel")
        self.l_turn.pack(side="right", padx=12)

    # ----------------------------------------------------------------- form
    def _load_settings_into_form(self) -> None:
        s = self.settings
        self.v_key.set(s.api_key)
        self.v_remember.set(s.remember_key)
        self.v_model.set(s.model)
        self.v_thinking.set(s.thinking)
        self.v_effort.set(s.reasoning_effort)
        self.v_doc.set(s.doc_path)
        self.v_views.set(s.views_path)
        self.v_out.set(s.output_dir)
        self.v_budget.set(f"{s.budget_usd:.2f}")
        self.v_turns.set(str(s.max_turns))
        self._refresh_after_buttons()

    def _save_form(self) -> Settings:
        s = self.settings
        s.api_key = self.v_key.get().strip()
        s.remember_key = bool(self.v_remember.get())
        s.model = self.v_model.get().strip() or "deepseek-v4-pro"
        s.thinking = bool(self.v_thinking.get())
        s.reasoning_effort = self.v_effort.get() or "high"
        s.doc_path, s.views_path = self.v_doc.get().strip(), self.v_views.get().strip()
        s.output_dir = self.v_out.get().strip()
        try:
            s.budget_usd = max(0.01, float(self.v_budget.get()))
        except ValueError:
            s.budget_usd = 1.0
        try:
            s.max_turns = max(1, int(float(self.v_turns.get())))
        except ValueError:
            s.max_turns = 60
        try:
            s.save()
        except OSError:
            pass
        return s

    def _pick_doc(self) -> None:
        f = filedialog.askopenfilename(title="Architecture document",
                                       filetypes=[("Markdown", "*.md"), ("All files", "*.*")])
        if f:
            self.v_doc.set(f)

    def _pick_views(self) -> None:
        f = filedialog.askopenfilename(title="Architecture views (PlantUML)",
                                       filetypes=[("Markdown / PlantUML", "*.md *.puml *.txt"),
                                                  ("All files", "*.*")])
        if f:
            self.v_views.set(f)

    def _pick_out(self) -> None:
        d = filedialog.askdirectory(title="Output project folder", mustexist=False)
        if d:
            self.v_out.set(d)

    # --------------------------------------------------------------- actions
    def _test_key(self) -> None:
        s = self._save_form()
        self._set_status("Testing API key…")

        def work():
            try:
                reply = DeepSeekClient(s.api_key, model=s.model, base_url=s.base_url,
                                       max_retries=1, timeout=30).ping()
                self.events.put({"type": "_keytest", "ok": True, "text": f"Key works. Model replied: {reply!r}"})
            except LLMError as e:
                self.events.put({"type": "_keytest", "ok": False, "text": str(e)})
        threading.Thread(target=work, daemon=True).start()

    def _preview_spec(self) -> None:
        s = self._save_form()
        try:
            spec = build_spec(s.doc_path, s.views_path or None)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"Could not parse inputs:\n{e}")
            return
        self._set_text(self.t_spec, json.dumps(spec, indent=2, ensure_ascii=False))
        self.nb.select(4)
        d = spec["digest"]
        self._set_status(f"Parsed '{d['system_name']}': {len(d['entities'])} entities, "
                         f"{len(spec['architecture_views']['index'])} diagrams, "
                         f"{len(d['requirements'])} requirements.")

    def _validate(self, s: Settings) -> str | None:
        if not s.api_key:
            return "Enter your DeepSeek API key."
        if not Path(s.doc_path).is_file():
            return "Select the architecture document (.md)."
        if s.views_path and not Path(s.views_path).is_file():
            return "The UML views file does not exist."
        if not s.output_dir:
            return "Choose an output folder."
        out = Path(s.output_dir)
        if out.exists() and any(p for p in out.iterdir() if p.name != ".agent"):
            if not messagebox.askyesno(APP_NAME, f"'{out}' is not empty.\n\nFiles there may be "
                                                 "overwritten. Continue?"):
                return ""
        return None

    def _start(self) -> None:
        s = self._save_form()
        problem = self._validate(s)
        if problem is not None:
            if problem:
                messagebox.showwarning(APP_NAME, problem)
            return
        cfg = AgentConfig(api_key=s.api_key, doc_path=s.doc_path, views_path=s.views_path or None,
                          output_dir=s.output_dir, model=s.model, base_url=s.base_url,
                          thinking=s.thinking, reasoning_effort=s.reasoning_effort,
                          budget_usd=s.budget_usd, max_turns=s.max_turns,
                          extra_instructions=s.extra_instructions)
        for t in (self.t_log, self.t_tests, self.t_preview):
            self._set_text(t, "")
        self.tv_plan.delete(*self.tv_plan.get_children())
        self.tv_files.delete(*self.tv_files.get_children())
        self.pb_cost["value"] = 0
        self.l_cost.configure(text=f"$0.0000 / ${s.budget_usd:.2f}")
        self.stop_event = threading.Event()
        self.last_output_dir = Path(s.output_dir)
        self.started_at = time.time()
        self.b_start.configure(state="disabled")
        self.b_stop.configure(state="normal")
        self._refresh_after_buttons(running=True)
        self.nb.select(0)
        self._log(f"Output folder: {s.output_dir}\n", "dim")

        def work():
            try:
                Agent(cfg, on_event=self.events.put, stop_event=self.stop_event).run()
            except Exception as e:  # noqa: BLE001
                self.events.put({"type": "error", "text": f"{type(e).__name__}: {e}"})
                self.events.put({"type": "done", "result": None})
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._tick()

    def _stop(self) -> None:
        self.stop_event.set()
        self.b_stop.configure(state="disabled")
        self._set_status("Stopping after the current step…")

    def _open_folder(self) -> None:
        d = self.last_output_dir or Path(self.v_out.get())
        if not d.exists():
            return
        if sys.platform == "win32":
            os.startfile(str(d))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(d)])

    def _rerun_tests(self) -> None:
        d = self.last_output_dir or Path(self.v_out.get())
        self._set_status("Running the generated test-suite…")

        def work():
            ok, out = runner.run_tests(d)
            self.events.put({"type": "tests", "ok": ok, "output": out})
            self.events.put({"type": "status", "text": "Tests passed." if ok else "Tests failed."})
        threading.Thread(target=work, daemon=True).start()

    def _launch_app(self) -> None:
        d = self.last_output_dir or Path(self.v_out.get())
        if not (d / "main.py").is_file():
            messagebox.showinfo(APP_NAME, "No main.py in the output folder yet.")
            return
        runner.launch_script(d, "main.py")
        self._set_status("Launched the generated application.")

    def _refresh_after_buttons(self, running: bool = False) -> None:
        d = Path(self.v_out.get() or ".")
        has_main = (d / "main.py").is_file()
        self.b_open.configure(state="normal" if d.exists() else "disabled")
        self.b_tests.configure(state="disabled" if running or not (d / "tests").is_dir() else "normal")
        self.b_launch.configure(state="disabled" if running or not has_main else "normal")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "The agent is still running. Quit anyway?"):
                return
            self.stop_event.set()
        self._save_form()
        self.root.destroy()

    # ---------------------------------------------------------------- events
    def _tick(self) -> None:
        if self.worker and self.worker.is_alive():
            el = int(time.time() - self.started_at)
            self.l_status.configure(text=f"Running… {el // 60:02d}:{el % 60:02d}  ·  {self._phase}")
            self.root.after(1000, self._tick)

    _phase = "starting"

    def _poll(self) -> None:
        try:
            for _ in range(200):
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, e: dict) -> None:
        t = e["type"]
        if t == "phase":
            self._phase = e["text"]
            self._log(f"\n● {e['text']}\n", "phase")
        elif t == "spec":
            self._log(f"  Parsed '{e['system']}': {e['entities']} entities, {e['use_cases']} use cases, "
                      f"{e['diagrams']} diagrams, {e['requirements']} requirements\n", "dim")
            try:
                self._set_text(self.t_spec, Path(e["path"]).read_text(encoding="utf-8"))
            except OSError:
                pass
        elif t == "turn":
            self._phase = f"turn {e['n']}"
            self.l_turn.configure(text=f"Turn {e['n']}/{e['max']}")
            self._log(f"\n── turn {e['n']} ──\n", "dim")
        elif t == "reasoning":
            txt = e["text"].strip().replace("\n", " ")
            self._log(f"  💭 {txt[:400]}{'…' if len(txt) > 400 else ''}\n", "think")
        elif t == "assistant":
            self._log(f"{e['text'].strip()}\n", "assistant")
        elif t == "tool_call":
            self._phase = f"running {e['name']}"
            self._log(f"  ⚙ {e['name']}  {e['arguments']}\n", "tool")
        elif t == "tool_result":
            first = (e["output"].splitlines() or [""])[0][:220]
            good = e["ok"] and not first.startswith(("TESTS FAILED", "SYNTAX ERRORS"))
            self._log(f"    {'✓' if good else '✗'} {first}  ({e['seconds']}s)\n", "ok" if good else "err")
        elif t == "todos":
            self._render_todos(e["todos"])
        elif t == "files":
            self._render_files(e["files"])
        elif t == "tests":
            self._set_text(self.t_tests, ("PASSED\n\n" if e["ok"] else "FAILED\n\n") + (e.get("output") or ""))
            self._log(f"  Tests: {'PASSED' if e['ok'] else 'FAILED'}\n", "ok" if e["ok"] else "err")
        elif t == "selftest":
            self._log(f"  Self-test (main.py --selftest): {'OK' if e['ok'] else 'FAILED'}\n",
                      "ok" if e["ok"] else "err")
        elif t == "cost":
            frac = min(e["fraction"], 1.0)
            self.pb_cost["value"] = frac
            color = OK if frac < 0.55 else WARN if frac < 0.8 else ERR
            ttk.Style(self.root).configure("Cost.Horizontal.TProgressbar", background=color,
                                           lightcolor=color, darkcolor=color)
            self.l_cost.configure(text=f"${e['cost_usd']:.4f} / ${e['budget_usd']:.2f}")
            self.l_tokens.configure(text=f"in {e['prompt_cache_hit_tokens'] + e['prompt_cache_miss_tokens']:,} "
                                         f"({e['prompt_cache_hit_tokens']:,} cached) · out {e['completion_tokens']:,}")
        elif t in ("notice", "verification"):
            text = e.get("text") or f"Verification rejected finish: {', '.join(e.get('problems', []))}"
            self._log(f"  ! {text.splitlines()[0]}\n", "notice")
        elif t == "status":
            self._log(f"  {e['text']}\n", "dim")
            if not (self.worker and self.worker.is_alive()):
                self._set_status(e["text"])
        elif t == "error":
            self._log(f"\nERROR: {e['text']}\n", "err")
        elif t == "_keytest":
            (messagebox.showinfo if e["ok"] else messagebox.showerror)(APP_NAME, e["text"])
            self._set_status(e["text"])
        elif t == "done":
            self._on_done(e.get("result"))

    def _on_done(self, r: dict | None) -> None:
        self.b_start.configure(state="normal")
        self.b_stop.configure(state="disabled")
        self._refresh_after_buttons()
        if self.last_output_dir:
            self._render_files([p.relative_to(self.last_output_dir).as_posix()
                                for p in sorted(self.last_output_dir.rglob("*"))
                                if p.is_file() and ".agent" not in p.parts and "__pycache__" not in p.parts])
        if not r:
            self._set_status("Agent crashed - see the Activity log.")
            return
        c = r["cost"]
        msg = (f"{r['status'].upper()} in {r['elapsed_s']:.0f}s · {r['turns']} turns · "
               f"${c['cost_usd']:.4f} · tests {'passed' if r['tests_passed'] else 'FAILED' if r['tests_passed'] is False else 'n/a'}")
        self._set_status(msg)
        self._log(f"\n● {msg}\n", "phase")
        if r.get("summary"):
            self._log(f"{r['summary']}\n", "assistant")
        self._log(f"Report: {Path(r['output_dir']) / '.agent' / 'run_report.md'}\n", "dim")

    # --------------------------------------------------------------- helpers
    def _render_todos(self, todos: list[dict]) -> None:
        self.tv_plan.delete(*self.tv_plan.get_children())
        icon = {"completed": "✔", "in_progress": "▶", "pending": "○"}
        for t in todos:
            self.tv_plan.insert("", "end", text=f" {icon.get(t['status'], '○')}  {t['content']}",
                                values=(t["status"].replace("_", " "),), tags=(t["status"],))

    def _render_files(self, files: list[str]) -> None:
        tv = self.tv_files
        tv.delete(*tv.get_children())
        nodes: dict[str, str] = {"": ""}
        for f in files:
            parts = f.split("/")
            for i in range(1, len(parts)):
                key = "/".join(parts[:i])
                if key not in nodes:
                    nodes[key] = tv.insert(nodes["/".join(parts[:i - 1])], "end", text=f"📁 {parts[i - 1]}",
                                           open=True)
            tv.insert(nodes["/".join(parts[:-1])], "end", iid=f"file::{f}", text=f"📄 {parts[-1]}")

    def _on_file_select(self, _evt=None) -> None:
        sel = self.tv_files.selection()
        if not sel or not sel[0].startswith("file::") or not self.last_output_dir:
            return
        p = self.last_output_dir / sel[0][6:]
        try:
            self._set_text(self.t_preview, p.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            self._set_text(self.t_preview, str(e))

    def _log(self, text: str, tag: str | None = None) -> None:
        t = self.t_log
        t.configure(state="normal")
        t.insert("end", text, (tag,) if tag else ())
        t.see("end")
        t.configure(state="disabled")

    def _set_text(self, t: tk.Text, text: str) -> None:
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", text)
        t.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.l_status.configure(text=text)


def run_gui() -> int:
    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    try:
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "packaging" / "icon.png"
        if icon.is_file():
            root.iconphoto(True, tk.PhotoImage(file=str(icon)))
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0
