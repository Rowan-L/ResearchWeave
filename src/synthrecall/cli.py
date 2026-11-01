from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

from sqlalchemy.exc import NoResultFound

from synthrecall.config import AppPaths
from synthrecall.database import create_session_factory, init_database
from synthrecall.models import Paper
from synthrecall.repositories import LibraryRepository, PaperUpdate

EDITABLE_TEXT_FIELDS = (
    "title",
    "authors",
    "venue",
    "doi",
    "arxiv_id",
    "url",
    "abstract",
    "pdf_path",
    "reading_status",
    "summary",
    "datasets",
    "dataset_metrics",
    "methods",
    "contributions",
    "limitations",
    "experiment_notes",
    "reproducibility_notes",
    "memory_anchor",
    "personal_notes",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synthrecall",
        description="操控 ResearchWeave 本地文献库；不带参数时启动桌面界面。",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="文献库根目录，默认使用当前目录（数据库位于 ROOT/data）。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出，便于脚本和自动化读取。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="列出或搜索文献")
    list_parser.add_argument("--search", default="", help="搜索关键词")
    list_parser.add_argument("--category-id", type=int, help="按分类 ID 过滤")
    list_parser.add_argument("--limit", type=int, default=100, help="最多返回多少条，默认 100")

    show_parser = subparsers.add_parser("show", help="查看一篇文献的完整信息")
    show_parser.add_argument("paper_id", type=int)

    categories_parser = subparsers.add_parser("categories", help="列出分类")
    categories_parser.add_argument("--counts", action="store_true", help="附带各分类文献数")

    import_parser = subparsers.add_parser("import", help="导入一份 PDF")
    import_parser.add_argument("pdf", type=Path)
    import_parser.add_argument(
        "--category-id",
        type=int,
        help="兼容旧脚本；新导入始终先归入“未分类”，请之后用 update 手动指定。",
    )

    folder_parser = subparsers.add_parser("import-folder", help="批量导入文件夹内的 PDF")
    folder_parser.add_argument("folder", type=Path)
    folder_parser.add_argument(
        "--category-id",
        type=int,
        help="兼容旧脚本；批量导入的新文献统一归入“未分类”。",
    )

    create_parser = subparsers.add_parser("create", help="新建文献记录")
    _add_edit_arguments(create_parser)

    update_parser = subparsers.add_parser("update", help="更新文献字段")
    update_parser.add_argument("paper_id", type=int)
    _add_edit_arguments(update_parser)

    delete_parser = subparsers.add_parser("delete", help="删除文献记录")
    delete_parser.add_argument("paper_id", type=int)
    delete_parser.add_argument(
        "--yes",
        action="store_true",
        help="确认删除；CLI 不会删除 PDF 文件。",
    )

    subparsers.add_parser("auto-classify", help="对整个文献库执行本地自动分类")

    export_parser = subparsers.add_parser("export", help="导出引用为 BibTeX 或 RIS 文件")
    export_parser.add_argument("output", help="输出文件路径(建议 .bib 或 .ris 后缀)")
    export_parser.add_argument(
        "--format",
        choices=["bibtex", "ris"],
        default=None,
        help="导出格式;缺省按输出文件后缀推断(.ris→ris,其余 bibtex)。",
    )
    export_parser.add_argument(
        "--paper-id",
        type=int,
        action="append",
        help="只导出指定文献;可重复传入。缺省导出全部文献。",
    )
    return parser


def _add_edit_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title")
    parser.add_argument("--authors")
    parser.add_argument("--year", type=int)
    parser.add_argument("--clear-year", action="store_true")
    parser.add_argument("--venue")
    parser.add_argument("--doi")
    parser.add_argument("--arxiv-id")
    parser.add_argument("--url")
    parser.add_argument("--abstract")
    parser.add_argument("--pdf-path")
    parser.add_argument("--reading-status")
    parser.add_argument("--importance", type=int, choices=range(1, 6))
    parser.add_argument("--summary")
    parser.add_argument("--datasets")
    parser.add_argument("--dataset-metrics")
    parser.add_argument("--methods")
    parser.add_argument("--contributions")
    parser.add_argument("--limitations")
    parser.add_argument("--experiment-notes")
    parser.add_argument("--reproducibility-notes")
    parser.add_argument("--memory-anchor")
    parser.add_argument("--personal-notes")
    parser.add_argument(
        "--category-id",
        type=int,
        action="append",
        help="替换为指定分类；可重复传入。",
    )
    parser.add_argument("--clear-categories", action="store_true")


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        paths = AppPaths.from_root(args.root) if args.root else AppPaths.from_cwd()
        paths.ensure()
        session_factory = create_session_factory(paths.database_path)
        init_database(session_factory)
        repository = LibraryRepository(session_factory)
        repository.ensure_default_categories()
        return _run_command(args, repository, paths)
    except (OSError, ValueError, NoResultFound) as exc:
        _write_error(str(exc), as_json=args.json)
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI 顶层兜底,防止异常栈泄漏给调用方
        _write_error(f"内部错误：{exc}", as_json=args.json)
        return 2


def _iso_local(value):
    """B6:库内时间为 UTC(naive),CLI 输出本地时区 ISO(带偏移,机器可判读)。"""
    if value is None:
        return None
    return value.replace(tzinfo=UTC).astimezone().isoformat()


def _configure_utf8_streams() -> None:
    """Keep CLI output machine-readable on legacy Windows code pages."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            # Embedded runtimes and a few test capture streams cannot be reconfigured.
            continue


def _run_command(args, repository: LibraryRepository, paths: AppPaths) -> int:
    if args.command == "list":
        papers = repository.list_papers(
            search=args.search,
            category_id=args.category_id,
        )
        limit = max(0, args.limit)
        payload = [_paper_to_dict(paper) for paper in papers[:limit]]
        _write_output(payload, args.json, _format_paper_list)
        return 0

    if args.command == "show":
        paper = repository.get_paper(args.paper_id)
        payload = _paper_to_dict(paper, include_detail=True)
        _write_output(payload, args.json, _format_paper_detail)
        return 0

    if args.command == "categories":
        counts = repository.category_paper_counts() if args.counts else {}
        payload = [
            {
                "id": category.id,
                "name": category.name,
                "parent_id": category.parent_id,
                "sort_order": category.sort_order,
                **({"paper_count": counts.get(category.id, 0)} if args.counts else {}),
            }
            for category in repository.list_categories()
        ]
        _write_output(payload, args.json, _format_categories)
        return 0

    if args.command == "import":
        _validate_category_ids(repository, [args.category_id] if args.category_id else [])
        result = repository.import_pdf(
            args.pdf,
            category_id=args.category_id,
            library_paper_dir=paths.paper_dir,
        )
        paper = repository.get_paper(result.paper.id)
        payload = {
            "created": result.created,
            "duplicate_reason": result.duplicate_reason,
            "paper": _paper_to_dict(paper),
        }
        _write_output(payload, args.json, _format_import_result)
        return 0

    if args.command == "import-folder":
        if not args.folder.is_dir():
            raise ValueError(f"PDF 文件夹不存在：{args.folder}")
        _validate_category_ids(repository, [args.category_id] if args.category_id else [])
        result = repository.import_pdf_folder(
            args.folder,
            category_id=args.category_id,
            library_paper_dir=paths.paper_dir,
        )
        payload = {
            "total": result.total,
            "added": result.added,
            "skipped": result.skipped,
            "failed": result.failed,
        }
        _write_output(payload, args.json, _format_folder_result)
        return 0 if result.failed == 0 else 1

    if args.command == "create":
        _validate_edit_args(repository, args)
        paper = repository.create_blank_paper()
        paper = repository.get_paper(paper.id)
        _update_from_args(repository, paper, args)
        updated = repository.get_paper(paper.id)
        payload = _paper_to_dict(updated, include_detail=True)
        _write_output(payload, args.json, _format_paper_detail)
        return 0

    if args.command == "update":
        _validate_edit_args(repository, args)
        paper = repository.get_paper(args.paper_id)
        _update_from_args(repository, paper, args)
        updated = repository.get_paper(paper.id)
        payload = _paper_to_dict(updated, include_detail=True)
        _write_output(payload, args.json, _format_paper_detail)
        return 0

    if args.command == "delete":
        if not args.yes:
            raise ValueError("删除操作需要显式传入 --yes；PDF 文件不会被删除。")
        paper = repository.get_paper(args.paper_id)
        repository.delete_paper(paper.id)
        payload = {"deleted": True, "paper_id": paper.id, "title": paper.title}
        _write_output(
            payload,
            args.json,
            lambda value: f"已删除记录 #{value['paper_id']}：《{value['title']}》",
        )
        return 0

    if args.command == "auto-classify":
        result = repository.auto_classify_all()
        payload = {
            "total": result.total,
            "updated": result.updated,
            "unchanged": result.unchanged,
            "created_categories": result.created_categories,
        }
        _write_output(payload, args.json, _format_classify_result)
        return 0

    if args.command == "export":
        from synthrecall.citation_export import papers_to_bibtex, papers_to_ris, write_export

        if args.paper_id:
            papers = [
                repository.get_paper(paper_id, include_text_content=False)
                for paper_id in args.paper_id
            ]
        else:
            papers = repository.list_papers()
        output_path = Path(args.output)
        export_format = args.format
        if export_format is None:
            export_format = "ris" if output_path.suffix.casefold() == ".ris" else "bibtex"
        content = (
            papers_to_ris(papers) if export_format == "ris" else papers_to_bibtex(papers)
        )
        write_export(output_path, content)
        payload = {
            "format": export_format,
            "count": len(papers),
            "output": str(output_path),
        }
        _write_output(payload, args.json, lambda value: (
            f"已导出 {value['count']} 篇文献为 {value['format'].upper()}: {value['output']}"
        ))
        return 0

    raise ValueError(f"不支持的命令：{args.command}")


def _update_from_args(repository: LibraryRepository, paper: Paper, args) -> None:
    detail = paper.detail
    values: dict[str, Any] = {
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "venue": paper.venue,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "url": paper.url,
        "abstract": paper.abstract,
        "pdf_path": paper.pdf_path,
        "reading_status": paper.reading_status,
        "importance": paper.importance,
        "summary": detail.summary if detail else "",
        "datasets": detail.datasets if detail else "",
        "dataset_metrics": detail.dataset_metrics if detail else "",
        "methods": detail.methods if detail else "",
        "contributions": detail.contributions if detail else "",
        "limitations": detail.limitations if detail else "",
        "experiment_notes": detail.experiment_notes if detail else "",
        "reproducibility_notes": detail.reproducibility_notes if detail else "",
        "memory_anchor": detail.memory_anchor if detail else "",
        "personal_notes": detail.personal_notes if detail else "",
    }
    for field in EDITABLE_TEXT_FIELDS:
        argument_value = getattr(args, field)
        if argument_value is not None:
            values[field] = argument_value
    if args.clear_year:
        values["year"] = None
    elif args.year is not None:
        values["year"] = args.year
    if args.importance is not None:
        values["importance"] = args.importance

    if args.clear_categories:
        category_ids: list[int] = []
    elif args.category_id is not None:
        category_ids = args.category_id
    else:
        category_ids = [category.id for category in paper.categories]

    repository.update_paper(
        paper.id,
        PaperUpdate(**values),
        category_ids=category_ids,
    )


def _validate_edit_args(repository: LibraryRepository, args) -> None:
    if args.clear_year and args.year is not None:
        raise ValueError("--clear-year 不能和 --year 同时使用。")
    if args.clear_categories and args.category_id:
        raise ValueError("--clear-categories 不能和 --category-id 同时使用。")
    _validate_category_ids(repository, args.category_id or [])


def _validate_category_ids(repository: LibraryRepository, category_ids: list[int]) -> None:
    if not category_ids:
        return
    existing_ids = {category.id for category in repository.list_categories()}
    missing_ids = sorted(set(category_ids) - existing_ids)
    if missing_ids:
        joined = ", ".join(str(category_id) for category_id in missing_ids)
        raise ValueError(f"分类不存在：{joined}")


def _paper_to_dict(paper: Paper, include_detail: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": paper.id,
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "venue": paper.venue,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "url": paper.url,
        "pdf_path": paper.pdf_path,
        "file_hash": paper.file_hash,
        "reading_status": paper.reading_status,
        "importance": paper.importance,
        "categories": [
            {"id": category.id, "name": category.name}
            for category in sorted(paper.categories, key=lambda item: (item.sort_order, item.name))
        ],
        "created_at": _iso_local(paper.created_at),
        "updated_at": _iso_local(paper.updated_at),
    }
    if include_detail:
        detail = paper.detail
        payload.update(
            {
                "abstract": paper.abstract,
                "summary": detail.summary if detail else "",
                "datasets": detail.datasets if detail else "",
                "dataset_metrics": detail.dataset_metrics if detail else "",
                "methods": detail.methods if detail else "",
                "contributions": detail.contributions if detail else "",
                "limitations": detail.limitations if detail else "",
                "experiment_notes": detail.experiment_notes if detail else "",
                "reproducibility_notes": detail.reproducibility_notes if detail else "",
                "memory_anchor": detail.memory_anchor if detail else "",
                "personal_notes": detail.personal_notes if detail else "",
                "page_count": paper.text.page_count if paper.text else 0,
            }
        )
    return payload


def _write_output(value: Any, as_json: bool, formatter) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(formatter(value))


def _write_error(message: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
    else:
        print(f"错误：{message}", file=sys.stderr)


def _format_paper_list(papers: list[dict[str, Any]]) -> str:
    if not papers:
        return "没有匹配的文献。"
    return "\n".join(
        f"{paper['id']:>5}  {paper['year'] or '-':<4}  {paper['title']}  [{paper['authors'] or '-'}]"
        for paper in papers
    )


def _format_paper_detail(paper: dict[str, Any]) -> str:
    categories = ", ".join(item["name"] for item in paper["categories"]) or "-"
    fields = [
        ("ID", paper["id"]),
        ("标题", paper["title"]),
        ("作者", paper["authors"] or "-"),
        ("年份", paper["year"] or "-"),
        ("来源", paper["venue"] or "-"),
        ("DOI", paper["doi"] or "-"),
        ("arXiv ID", paper["arxiv_id"] or "-"),
        ("状态", paper["reading_status"]),
        ("重要度", paper["importance"]),
        ("分类", categories),
        ("PDF", paper["pdf_path"] or "-"),
        ("摘要", paper.get("abstract") or "-"),
        ("总结", paper.get("summary") or "-"),
        ("数据集", paper.get("datasets") or "-"),
        ("方法", paper.get("methods") or "-"),
        ("记忆锚点", paper.get("memory_anchor") or "-"),
        ("个人批注", paper.get("personal_notes") or "-"),
    ]
    return "\n".join(f"{label}：{value}" for label, value in fields)


def _format_categories(categories: list[dict[str, Any]]) -> str:
    if not categories:
        return "没有分类。"
    return "\n".join(
        f"{item['id']:>4}  parent={item['parent_id'] or '-':>4}  {item['name']}"
        + (f"  ({item['paper_count']} 篇)" if "paper_count" in item else "")
        for item in categories
    )


def _format_import_result(result: dict[str, Any]) -> str:
    paper = result["paper"]
    if result["created"]:
        return f"已导入 #{paper['id']}：《{paper['title']}》\nPDF：{paper['pdf_path']}"
    return (
        f"未重复导入：{result['duplicate_reason']}\n"
        f"现有记录 #{paper['id']}：《{paper['title']}》"
    )


def _format_folder_result(result: dict[str, int]) -> str:
    return (
        f"共 {result['total']} 份 PDF：新增 {result['added']}，"
        f"跳过 {result['skipped']}，失败 {result['failed']}。"
    )


def _format_classify_result(result: dict[str, int]) -> str:
    return (
        f"共 {result['total']} 篇：更新 {result['updated']}，"
        f"未变化 {result['unchanged']}，新增分类 {result['created_categories']}。"
    )
