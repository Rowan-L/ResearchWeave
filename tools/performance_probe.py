from __future__ import annotations

import argparse
from datetime import datetime
import gc
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tracemalloc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"


def _rss_mb() -> float:
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except ImportError:
        return 0.0


def _child_import(module: str) -> int:
    sys.path.insert(0, str(SRC_DIR))
    started = time.perf_counter()
    importlib.import_module(module)
    result = {
        "module": module,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "rss_mb": round(_rss_mb(), 2),
        "loaded_modules": len(sys.modules),
    }
    print(json.dumps(result))
    return 0


def _measure_import(module: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child-import", module],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def _measure_query(paper_count: int, text_chars: int) -> dict[str, object]:
    sys.path.insert(0, str(SRC_DIR))
    from synthrecall.database import create_session_factory, init_database
    from synthrecall.models import Category, Paper, PaperDetail, PaperText, paper_categories
    from synthrecall.repositories import LibraryRepository

    with tempfile.TemporaryDirectory(prefix="synthrecall-perf-", dir=PROJECT_ROOT) as temp_dir:
        database_path = Path(temp_dir) / "probe.db"
        session_factory = create_session_factory(database_path)
        init_database(session_factory)
        engine = session_factory.kw["bind"]
        now = datetime.now()
        text_value = "neural performance corpus " + "x" * max(0, text_chars - 26)

        with engine.begin() as connection:
            category_id = connection.execute(
                Category.__table__.insert().values(name="Probe", sort_order=0)
            ).inserted_primary_key[0]
            for start in range(0, paper_count, 50):
                rows = [
                    {
                        "title": f"Probe paper {index}",
                        "authors": "Performance Author",
                        "year": 2026,
                        "reading_status": "to-read",
                        "importance": index % 5 + 1,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for index in range(start, min(start + 50, paper_count))
                ]
                connection.execute(Paper.__table__.insert(), rows)
            ids = list(connection.scalars(Paper.__table__.select().with_only_columns(Paper.id)))
            connection.execute(
                PaperDetail.__table__.insert(),
                [{"paper_id": paper_id, "summary": "summary"} for paper_id in ids],
            )
            connection.execute(
                PaperText.__table__.insert(),
                [
                    {"paper_id": paper_id, "content": text_value, "page_count": 10, "updated_at": now}
                    for paper_id in ids
                ],
            )
            connection.execute(
                paper_categories.insert(),
                [{"paper_id": paper_id, "category_id": category_id} for paper_id in ids],
            )

        repository = LibraryRepository(session_factory)
        results: dict[str, object] = {
            "papers": paper_count,
            "text_chars_per_paper": text_chars,
            "database_mb": round(database_path.stat().st_size / (1024 * 1024), 2),
        }
        for name, kwargs in (
            ("list_without_search", {}),
            ("full_text_search", {"search": "neural", "search_fields": {"full_text"}}),
        ):
            gc.collect()
            rss_before = _rss_mb()
            tracemalloc.start()
            started = time.perf_counter()
            papers = repository.list_papers(**kwargs)
            elapsed_ms = (time.perf_counter() - started) * 1000
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            results[name] = {
                "count": len(papers),
                "elapsed_ms": round(elapsed_ms, 2),
                "python_peak_mb": round(peak / (1024 * 1024), 2),
                "rss_delta_mb": round(max(0.0, _rss_mb() - rss_before), 2),
            }
            del papers
        engine.dispose()
        return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure ResearchWeave startup and repository hot paths.")
    parser.add_argument("--child-import", help=argparse.SUPPRESS)
    parser.add_argument("--papers", type=int, default=300)
    parser.add_argument("--text-chars", type=int, default=200_000)
    args = parser.parse_args(argv)
    if args.child_import:
        return _child_import(args.child_import)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    report = {
        "imports": [
            _measure_import("synthrecall.__main__"),
            _measure_import("synthrecall.cli"),
            _measure_import("synthrecall.app"),
        ],
        "repository": _measure_query(args.papers, args.text_chars),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
