# PyInstaller spec - builds two single-file Windows executables:
#   dist/ArchAgent.exe      double-click desktop GUI (no console window)
#   dist/ArchAgent-cli.exe  same program with a console, for `ArchAgent-cli.exe cli ...`
#
# Build:  pyinstaller packaging/ArchAgent.spec --noconfirm --clean
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from archagent.agent.prompts import BUNDLED_STDLIB  # noqa: E402

# The generated project runs on the interpreter embedded in the .exe, so every
# stdlib module the agent is allowed to use must be bundled explicitly.
hidden = sorted(set(BUNDLED_STDLIB) | {
    "tkinter.colorchooser", "sqlite3.dbapi2", "unittest.case", "unittest.loader",
    "unittest.runner", "unittest.suite", "json.decoder", "logging.handlers", "xml.etree.ElementTree",
    "zipfile", "pprint", "heapq", "bisect", "operator", "numbers", "weakref", "struct", "platform",
    "locale", "calendar", "html", "difflib", "traceback", "inspect", "types", "warnings", "codecs",
})

datas = [
    (str(ROOT / "examples" / "Architecture_Documentation.md"), "examples"),
    (str(ROOT / "examples" / "Architecture_View.md"), "examples"),
    (str(ROOT / "packaging" / "icon.png"), "packaging"),
]

a = Analysis(
    [str(ROOT / "archagent" / "__main__.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["numpy", "pandas", "matplotlib", "PIL", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    icon=str(ROOT / "packaging" / "icon.ico"),
)

gui = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="ArchAgent", console=False, **common)
cli = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="ArchAgent-cli", console=True, **common)
