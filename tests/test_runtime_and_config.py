"""Running generated code with the bundled interpreter; settings; prompts."""

import shutil
from pathlib import Path

from archagent.agent.prompts import BUNDLED_STDLIB, system_prompt
from archagent.config import Settings
from archagent.runtime import runner

FIXTURE = Path(__file__).parent / "fixtures" / "sample_project"


def test_run_tests_on_fixture(tmp_path):
    proj = tmp_path / "p"
    shutil.copytree(FIXTURE, proj)
    ok, out = runner.run_tests(proj)
    assert ok, out
    assert "Ran 3 tests" in out


def test_run_tests_without_package_init(tmp_path):
    proj = tmp_path / "p"
    shutil.copytree(FIXTURE, proj)
    (proj / "tests" / "__init__.py").unlink()
    ok, out = runner.run_tests(proj)
    assert ok, out


def test_syntax_errors_reported_before_running(tmp_path):
    proj = tmp_path / "p"
    shutil.copytree(FIXTURE, proj)
    (proj / "quiz" / "broken.py").write_text("def f(:\n    pass\n")
    ok, out = runner.run_tests(proj)
    assert not ok and out.startswith("SYNTAX ERRORS") and "broken.py:1" in out


def test_no_tests_found(tmp_path):
    (tmp_path / "tests").mkdir()
    ok, out = runner.run_tests(tmp_path)
    assert not ok and "NO TESTS FOUND" in out


def test_run_script_selftest_and_timeout(tmp_path):
    proj = tmp_path / "p"
    shutil.copytree(FIXTURE, proj)
    code, out = runner.run_script(proj, "main.py", ["--selftest"])
    assert code == 0 and "SELFTEST OK" in out
    (proj / "hang.py").write_text("import time\ntime.sleep(30)\n")
    code, out = runner.run_script(proj, "hang.py", timeout=2)
    assert code == -9 and "TIMEOUT" in out


def test_run_script_refuses_outside_project(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (tmp_path / "outside.py").write_text("print('pwned')")
    code, out = runner.run_script(proj, "../outside.py")
    assert code == 2 and "pwned" not in out


def test_settings_roundtrip_does_not_leak_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    s = Settings.load()
    assert s.model == "deepseek-v4-pro" and s.budget_usd == 1.0
    assert s.doc_path.endswith("Architecture_Documentation.md")  # bundled example preselected
    s.api_key, s.remember_key = "sk-secret", False
    s.save()
    assert "sk-secret" not in next(tmp_path.rglob("settings.json")).read_text()
    s.remember_key = True
    s.save()
    assert Settings.load().api_key == "sk-secret"


def test_env_key_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    assert Settings.load().api_key == "sk-env"


def test_system_prompt_pins_desktop_target():
    p = system_prompt("Use a space theme.")
    assert "desktop application" in p and "Tkinter" in p and "Standard library only" in p
    assert "unittest" in p and "--selftest" in p and "Use a space theme." in p
    assert "sqlite3" in BUNDLED_STDLIB and "tkinter.ttk" in BUNDLED_STDLIB


def test_spec_file_bundles_every_allowed_module():
    spec = (Path(__file__).parents[1] / "packaging" / "ArchAgent.spec").read_text()
    assert "BUNDLED_STDLIB" in spec and "examples" in spec


def test_stale_saved_paths_fall_back_to_examples(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    s = Settings.load()
    s.doc_path = str(tmp_path / "_MEI123" / "gone.md")  # e.g. an old PyInstaller temp dir
    s.save()
    assert Path(Settings.load().doc_path).is_file()


def test_keys_are_kept_per_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    for var in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    s = Settings.load()
    s.remember_key = True
    s.provider, s.api_key = "openrouter", "sk-or-1"
    s.save()
    loaded = Settings.load()
    assert loaded.provider == "openrouter" and loaded.api_key == "sk-or-1"
    from archagent.config import key_for
    assert key_for("deepseek", loaded) == "" and key_for("openrouter", loaded) == "sk-or-1"
