"""Run generated code with the interpreter bundled inside the .exe.

The target machine has no Python. When frozen by PyInstaller,
``sys.executable`` *is* ArchAgent.exe, so we re-launch ourselves with a
hidden sub-command:

    ArchAgent.exe --child run-tests  <project_dir> <output_file>
    ArchAgent.exe --child run-script <project_dir> <output_file> <script> [args...]
    ArchAgent.exe --child launch     <project_dir> <script>

Output goes to a file rather than a pipe because a windowed (no console)
PyInstaller app has ``sys.stdout = None``.
"""

from __future__ import annotations

import io
import os
import runpy
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

CHILD_FLAG = "--child"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def python_command() -> list[str]:
    """Command prefix that starts *this* application in child mode."""
    if getattr(sys, "frozen", False):
        return [sys.executable, CHILD_FLAG]
    main = Path(__file__).resolve().parents[1] / "__main__.py"
    return [sys.executable, str(main), CHILD_FLAG]


def _spawn(args: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    fd, out_path = tempfile.mkstemp(prefix="archagent_", suffix=".log")
    os.close(fd)
    cmd = python_command() + [args[0], str(cwd), out_path] + args[1:]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
               ARCHAGENT_HEADLESS="1")
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        code = -9
    try:
        text = Path(out_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    if code == -9:
        text += f"\n[TIMEOUT] process killed after {timeout}s (does a test open a GUI window or wait for input?)"
    return code, text


def compile_check(project: Path) -> list[str]:
    """Syntax-check every .py file; returns a list of error strings."""
    errors = []
    for f in sorted(project.rglob("*.py")):
        if any(part.startswith(".") or part in ("__pycache__", "venv", ".venv") for part in f.parts):
            continue
        try:  # compile in memory: no .pyc files are written into the project
            compile(f.read_text(encoding="utf-8", errors="replace"), str(f), "exec")
        except SyntaxError as e:
            errors.append(f"{f.relative_to(project)}:{e.lineno}: {e.msg}\n    {(e.text or '').rstrip()}")
        except Exception as e:  # pragma: no cover
            errors.append(f"{f.relative_to(project)}: {e}")
    return errors


def run_tests(project: str | Path, timeout: int = 180) -> tuple[bool, str]:
    project = Path(project)
    errors = compile_check(project)
    if errors:
        return False, "SYNTAX ERRORS:\n" + "\n".join(errors)
    code, out = _spawn(["run-tests"], project, timeout)
    return code == 0, out


def run_script(project: str | Path, script: str, args: list[str] | None = None,
               timeout: int = 60) -> tuple[int, str]:
    return _spawn(["run-script", script, *(args or [])], Path(project), timeout)


def launch_script(project: str | Path, script: str) -> subprocess.Popen:
    """Start the generated application (GUI) detached from the agent."""
    project = Path(project)
    cmd = python_command() + ["launch", str(project), script]
    return subprocess.Popen(cmd, cwd=str(project), stdin=subprocess.DEVNULL)


# ---------------------------------------------------------------- child side

def _prepare(project: Path) -> None:
    os.chdir(project)
    for p in (project / "src", project):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _child_run_tests(project: Path, out: io.TextIOBase) -> int:
    import unittest
    _prepare(project)
    start = project / "tests" if (project / "tests").is_dir() else project
    # tests/ as a package -> import as tests.test_x; plain folder -> import test_x directly
    top = project if (start == project or (start / "__init__.py").is_file()) else start
    suite = unittest.defaultTestLoader.discover(str(start), pattern="test*.py", top_level_dir=str(top))
    count = suite.countTestCases()
    out.write(f"Discovered {count} test(s) in {start.relative_to(project) if start != project else '.'}\n")
    if count == 0:
        out.write("NO TESTS FOUND - tests must live in tests/test_*.py and subclass unittest.TestCase\n")
        return 5
    result = unittest.TextTestRunner(stream=out, verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def _child_run_script(project: Path, out: io.TextIOBase, script: str, args: list[str]) -> int:
    _prepare(project)
    target = (project / script).resolve()
    if project.resolve() not in target.parents and target != project.resolve():
        out.write("refusing to run a script outside the project\n")
        return 2
    sys.argv = [str(target), *args]
    sys.stdout = sys.stderr = out
    try:
        runpy.run_path(str(target), run_name="__main__")
        return 0
    except SystemExit as e:
        return int(e.code or 0) if isinstance(e.code, int) else 1


def child_main(argv: list[str]) -> int:
    """Entry for ``--child <mode> ...`` invocations."""
    mode, project = argv[0], Path(argv[1]).resolve()
    if mode == "launch":
        _prepare(project)
        target = project / argv[2]
        sys.argv = [str(target), *argv[3:]]
        runpy.run_path(str(target), run_name="__main__")
        return 0

    out_file = argv[2]
    with open(out_file, "w", encoding="utf-8", buffering=1) as out:
        sys.stdout = sys.stderr = out
        try:
            if mode == "run-tests":
                return _child_run_tests(project, out)
            if mode == "run-script":
                return _child_run_script(project, out, argv[3], argv[4:])
            out.write(f"unknown child mode {mode}\n")
            return 2
        except BaseException:  # report everything, including import errors
            traceback.print_exc(file=out)
            return 1
