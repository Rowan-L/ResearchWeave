import json
import os
import subprocess
import sys
from pathlib import Path

import pymupdf as fitz

from synthrecall.cli import main


def run_json(capsys, root, *arguments):
    exit_code = main(["--root", str(root), "--json", *arguments])
    output = capsys.readouterr()
    payload = json.loads(output.out or output.err)
    return exit_code, payload


def write_pdf(path, *, text, title, authors):
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.set_metadata({"title": title, "author": authors})
    document.save(path)
    document.close()


def test_cli_create_list_update_show_and_delete(tmp_path, capsys):
    exit_code, created = run_json(
        capsys,
        tmp_path,
        "create",
        "--title",
        "CLI Controlled Paper",
        "--authors",
        "Ada Example",
        "--year",
        "2026",
        "--memory-anchor",
        "cli-marker",
    )
    assert exit_code == 0
    paper_id = created["id"]

    exit_code, papers = run_json(
        capsys,
        tmp_path,
        "list",
        "--search",
        "CLI Controlled",
    )
    assert exit_code == 0
    assert [paper["id"] for paper in papers] == [paper_id]

    exit_code, updated = run_json(
        capsys,
        tmp_path,
        "update",
        str(paper_id),
        "--title",
        "Updated from CLI",
        "--clear-year",
        "--reading-status",
        "精读",
    )
    assert exit_code == 0
    assert updated["title"] == "Updated from CLI"
    assert updated["year"] is None
    assert updated["reading_status"] == "精读"

    exit_code, shown = run_json(capsys, tmp_path, "show", str(paper_id))
    assert exit_code == 0
    assert shown["memory_anchor"] == "cli-marker"

    exit_code, error = run_json(capsys, tmp_path, "delete", str(paper_id))
    assert exit_code == 1
    assert "--yes" in error["error"]

    exit_code, deleted = run_json(
        capsys,
        tmp_path,
        "delete",
        str(paper_id),
        "--yes",
    )
    assert exit_code == 0
    assert deleted["deleted"] is True


def test_cli_import_reports_duplicate_and_does_not_copy_it(tmp_path, capsys):
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    write_pdf(
        first_pdf,
        text="First PDF representation",
        title="A Reliable CLI Import Test Paper",
        authors="Alice Example; Bob Example",
    )
    write_pdf(
        second_pdf,
        text="Second PDF representation",
        title="A RELIABLE CLI IMPORT TEST PAPER!",
        authors="Bob Example, Alice Example",
    )

    # B34:全新库不再种入演示分类树,先显式建一个子分类再验证导入挂分类
    from synthrecall.config import AppPaths
    from synthrecall.database import create_session_factory, init_database
    from synthrecall.repositories import LibraryRepository

    cli_paths = AppPaths.from_root(tmp_path)
    cli_paths.ensure()
    cli_factory = create_session_factory(cli_paths.database_path)
    init_database(cli_factory)
    cli_repo = LibraryRepository(cli_factory)
    root_category = cli_repo.add_category("CLI 主题")
    child_category = cli_repo.add_category("CLI 子分类", parent_id=root_category.id)

    exit_code, categories = run_json(capsys, tmp_path, "categories")
    assert exit_code == 0
    requested_id = next(
        category["id"]
        for category in categories
        if category["name"] != "未分类" and category["parent_id"] is not None
    )
    assert requested_id == child_category.id
    exit_code, first = run_json(
        capsys,
        tmp_path,
        "import",
        str(first_pdf),
        "--category-id",
        str(requested_id),
    )
    assert exit_code == 0
    assert first["created"] is True
    assert [category["id"] for category in first["paper"]["categories"]] == [requested_id]

    exit_code, second = run_json(capsys, tmp_path, "import", str(second_pdf))
    assert exit_code == 0
    assert second["created"] is False
    assert second["paper"]["id"] == first["paper"]["id"]
    assert len(list((tmp_path / "data" / "papers").rglob("*.pdf"))) == 1


def test_cli_subprocess_uses_utf8_on_windows_legacy_code_page(tmp_path, capsys):
    exit_code, created = run_json(
        capsys,
        tmp_path,
        "create",
        "--title",
        "Unicode ∗ Paper",
        "--abstract",
        "公式符号 ∗ 与中文摘要",
    )
    assert exit_code == 0

    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("PYTHONIOENCODING", None)
    environment.pop("PYTHONUTF8", None)
    existing_python_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in [str(project_root / "src"), existing_python_path] if value
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "synthrecall",
            "--root",
            str(tmp_path),
            "--json",
            "show",
            str(created["id"]),
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    payload = json.loads(result.stdout.decode("utf-8"))
    assert payload["title"] == "Unicode ∗ Paper"
    assert payload["abstract"] == "公式符号 ∗ 与中文摘要"


def test_cli_json_mode_keeps_unexpected_errors_machine_readable(
    tmp_path,
    capsys,
    monkeypatch,
):
    monkeypatch.setattr(
        "synthrecall.cli._run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )

    exit_code = main(["--root", str(tmp_path), "--json", "list"])
    output = capsys.readouterr()
    payload = json.loads(output.out or output.err)

    assert exit_code == 2
    assert payload == {"error": "内部错误：unexpected"}
