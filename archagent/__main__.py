"""Entry point.

    python -m archagent                  -> desktop GUI
    python -m archagent cli ...          -> headless CLI (see archagent/cli.py)
    ArchAgent.exe --child <mode> ...     -> internal: run generated code
"""

import os
import sys

if __package__ in (None, ""):
    # Executed as a script (PyInstaller entry or `python archagent/__main__.py`).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--child":
        from archagent.runtime.runner import child_main
        return child_main(argv[1:])
    if argv and argv[0] == "cli":
        from archagent.cli import main as cli_main
        return cli_main(argv[1:])
    from archagent.gui.app import run_gui
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
