from pathlib import Path

import run_demo


def test_run_demo_uses_project_root_as_working_directory(tmp_path, monkeypatch):
    observed: dict[str, object] = {}

    def fake_main(arguments):
        observed["arguments"] = arguments
        observed["cwd"] = Path.cwd()
        return 0

    monkeypatch.setattr("synthrecall.__main__.main", fake_main)
    monkeypatch.setattr(run_demo.sys, "argv", ["run_demo.py"])
    monkeypatch.chdir(tmp_path)

    assert run_demo._main() == 0
    assert observed == {"arguments": [], "cwd": run_demo.ROOT}


def test_run_demo_writes_startup_traceback_inside_project(monkeypatch, tmp_path):
    monkeypatch.setattr(run_demo, "ROOT", tmp_path)

    try:
        raise RuntimeError("startup failure")
    except RuntimeError:
        log_path = run_demo._write_startup_error()

    assert log_path == tmp_path / "data" / "startup_error.log"
    assert "RuntimeError: startup failure" in log_path.read_text(encoding="utf-8")
