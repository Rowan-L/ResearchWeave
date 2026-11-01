"""E4 性能基线:固定合成数据集上的核心查询场景,输出/对比基线。

数据集确定性(random.Random 固定种子):2000 篇文献,含分类/标签/状态/
年份/重要度/摘要/全文,通过仓库层 ORM 写入以触发 FTS 同步事件。
每个场景重复 --runs 次取中位数;结果写入 tools/perf_baseline.json,
若已有基线则打印逐项变化(百分比)。用法:

    <目标环境>/python.exe tools/perf_baseline.py [--papers 2000] [--runs 5]
"""

from __future__ import annotations

import argparse
import gc
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from synthrecall.database import create_session_factory, init_database
from synthrecall.models import (
    Category,
    Paper,
    PaperDetail,
    PaperText,
    Tag,
    utc_now,
)
from synthrecall.repositories import LibraryRepository

BASELINE_PATH = PROJECT_ROOT / "tools" / "perf_baseline.json"
SEED = 20261028

STATUS_CYCLE = ["待读", "浏览", "已读", "精读", "复现中", "已复现"]
CATEGORY_NAMES = ["深度学习", "计算机视觉", "自然语言处理", "机器人", "数据集与基准", "综述"]
TAG_NAMES = ["复现必看", "ImageNet", "Transformer", "卷积网络", "数据增强", "消融实验", "代码开源", "综述笔记"]

EN_SNIPPET = (
    "We study transformer attention mechanisms for vision backbones. "
    "The proposed architecture improves transfer learning on ImageNet "
    "classification and robustness benchmarks under distribution shift."
)
ZH_SNIPPET = (
    "本文研究卷积神经网络在图像识别中的注意力机制,提出一种轻量的迁移学习"
    "框架,并在 ImageNet 与自建基准上验证了鲁棒性与消融实验结论。"
)


def build_dataset(session_factory, paper_count: int) -> int:
    rng = random.Random(SEED)
    now = utc_now()
    with session_factory() as session:
        categories = [
            Category(name=name, sort_order=index)
            for index, name in enumerate(CATEGORY_NAMES)
        ]
        session.add_all(categories)
        tags = [Tag(name=name) for name in TAG_NAMES]
        session.add_all(tags)
        session.flush()
        for index in range(paper_count):
            zh_title = index % 3 == 0
            paper = Paper(
                title=(
                    f"卷积注意力基准研究 {index}"
                    if zh_title
                    else f"Transformer Benchmark Study {index}"
                ),
                authors=f"Author {index % 97}, Author {(index + 13) % 97}",
                year=2020 + index % 7,
                venue=f"Venue {index % 24}",
                reading_status=STATUS_CYCLE[index % len(STATUS_CYCLE)],
                importance=rng.randint(1, 5),
                abstract=(ZH_SNIPPET if zh_title else EN_SNIPPET) + f"(#{index})",
                created_at=now,
                updated_at=now,
                detail=PaperDetail(
                    summary=ZH_SNIPPET,
                    datasets="ImageNet, CIFAR-100" if index % 2 else "COCO, ImageNet",
                    dataset_metrics=f"Top-1 {70 + index % 25}%",
                    memory_anchor=ZH_SNIPPET,
                ),
            )
            paper.categories.append(categories[index % len(categories)])
            paper.tags.append(tags[index % len(tags)])
            if index % 3 == 0:
                paper.tags.append(tags[(index * 5 + 1) % len(tags)])
            paper.text = PaperText(
                content=(ZH_SNIPPET if zh_title else EN_SNIPPET) * 6,
                page_count=8 + index % 12,
            )
            session.add(paper)
        session.commit()
    return paper_count


def run_scenarios(repository: LibraryRepository, runs: int) -> dict[str, dict[str, float]]:
    papers = repository.list_papers()
    anchor_id = papers[len(papers) // 2].id
    scenarios: dict[str, dict[str, object]] = {
        "list_all": {"fn": lambda: repository.list_papers()},
        "fts_chinese": {
            "fn": lambda: repository.list_papers("卷积", search_fields={"title", "abstract", "full_text"})
        },
        "fts_english": {
            "fn": lambda: repository.list_papers("transformer", search_fields={"title", "abstract", "full_text"})
        },
        "ilike_short_fallback": {
            "fn": lambda: repository.list_papers("tr", search_fields={"title"})
        },
        "filter_tag": {"fn": lambda: repository.list_papers(tag_id=1)},
        "filter_category": {"fn": lambda: repository.list_papers(category_id=1)},
        "filter_status_year": {
            "fn": lambda: repository.list_papers(reading_status="待读", year=2024)
        },
        "combined_smart_folder": {
            "fn": lambda: repository.list_papers("ImageNet", reading_status="待读", min_importance=3)
        },
        "library_statistics": {"fn": lambda: repository.library_statistics()},
        "related_papers": {"fn": lambda: repository.related_papers(anchor_id)},
    }

    results: dict[str, dict[str, float]] = {}
    for name, spec in scenarios.items():
        samples: list[float] = []
        for _ in range(runs):
            gc.collect()
            started = time.perf_counter()
            spec["fn"]()
            samples.append((time.perf_counter() - started) * 1000)
        results[name] = {
            "median_ms": round(statistics.median(samples), 2),
            "p95_ms": round(statistics.quantiles(samples, n=20)[-1], 2)
            if len(samples) >= 2
            else round(samples[0], 2),
        }
    return results


def compare(previous: dict | None, current: dict) -> list[str]:
    if not previous or "scenarios" not in previous:
        return []
    lines = []
    for name, metrics in current["scenarios"].items():
        before = previous["scenarios"].get(name, {}).get("median_ms")
        if before:
            delta = (metrics["median_ms"] - before) / before * 100
            lines.append(f"  {name}: {before}ms -> {metrics['median_ms']}ms ({delta:+.0f}%)")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--papers", type=int, default=2000)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(argv)


    with tempfile.TemporaryDirectory(prefix="synthrecall-baseline-") as temp_dir:
        database_path = Path(temp_dir) / "baseline.db"
        session_factory = create_session_factory(database_path)
        init_database(session_factory)
        build_dataset(session_factory, args.papers)
        repository = LibraryRepository(session_factory)
        started = time.perf_counter()
        scenarios = run_scenarios(repository, args.runs)
        wall_seconds = round(time.perf_counter() - started, 2)
        engine = session_factory.kw["bind"]
        database_mb = round(database_path.stat().st_size / (1024 * 1024), 2)
        engine.dispose()

    report = {
        "created_at": time.strftime("%Y-%m-%d %H:%M"),
        "papers": args.papers,
        "runs": args.runs,
        "database_mb": database_mb,
        "setup_wall_seconds": wall_seconds,
        "scenarios": scenarios,
    }

    previous = None
    if BASELINE_PATH.exists():
        try:
            previous = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None

    print(f"E4 性能基线({args.papers} 篇 · {args.runs} 次取中位数)")
    print(f"{'场景':<24}{'中位 ms':>10}{'p95 ms':>10}")
    for name, metrics in scenarios.items():
        print(f"{name:<24}{metrics['median_ms']:>10}{metrics['p95_ms']:>10}")
    deltas = compare(previous, report)
    if deltas:
        print("与上次基线对比:")
        print("\n".join(deltas))

    BASELINE_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已写入 {BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
