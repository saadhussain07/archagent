"""User settings, stored in %APPDATA%\\ArchAgent\\settings.json (or ~/.config/archagent).

The API key is only written to disk when the user ticks "Remember key".
It can also come from the DEEPSEEK_API_KEY environment variable.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def settings_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "ArchAgent"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "archagent"


def bundled_examples_dir() -> Path:
    """Folder with the example inputs.

    When frozen, the files live in PyInstaller's temporary _MEIxxxx folder,
    which changes on every launch - so they are copied once to a permanent
    location next to the settings, and that path is what the GUI shows.
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parents[1] / "examples"
    src = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "examples"
    dst = settings_dir() / "examples"
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.glob("*.md"):
            if not (dst / f.name).is_file():
                shutil.copyfile(f, dst / f.name)
        return dst
    except OSError:
        return src


@dataclass
class Settings:
    api_key: str = ""
    remember_key: bool = False
    model: str = "deepseek-v4-pro"
    base_url: str = "https://api.deepseek.com"
    thinking: bool = True
    reasoning_effort: str = "high"
    budget_usd: float = 1.0
    max_turns: int = 60
    doc_path: str = ""
    views_path: str = ""
    output_dir: str = ""
    extra_instructions: str = ""

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        path = settings_dir() / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        if not s.api_key:
            s.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        ex = bundled_examples_dir()
        # fall back to the bundled examples when unset or the saved file has gone
        if not (s.doc_path and Path(s.doc_path).is_file()) and (ex / "Architecture_Documentation.md").is_file():
            s.doc_path = str(ex / "Architecture_Documentation.md")
        if not (s.views_path and Path(s.views_path).is_file()) and (ex / "Architecture_View.md").is_file():
            s.views_path = str(ex / "Architecture_View.md")
        if not s.output_dir:
            s.output_dir = str(Path.home() / "ArchAgent-output" / "space-fractions")
        return s

    def save(self) -> None:
        data = asdict(self)
        if not self.remember_key:
            data["api_key"] = ""
        d = settings_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "settings.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
