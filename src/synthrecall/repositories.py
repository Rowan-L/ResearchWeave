from __future__ import annotations

import itertools
import json
import re
import shutil
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rapidfuzz import fuzz
from sqlalchemy import case, false, func, literal_column, or_, select, text
from sqlalchemy.orm import joinedload, with_expression

from synthrecall.ai import PaperAIAnalysis
from synthrecall.classification import DEFAULT_CATEGORY_RULES
from synthrecall.database import SessionFactory
from synthrecall.models import (
    Category,
    DatasetMetricEntry,
    Paper,
    PaperDetail,
    PaperText,
    ReadingLog,
    SavedSearch,
    Tag,
    TokenUsageRecord,
    paper_categories,
    utc_now,
)
from synthrecall.pdf_utils import (
    extract_paper_identifiers,
    extract_pdf_text,
    extract_text_preview,
    read_pdf_metadata,
    sha256_file,
)

UNCATEGORIZED_CATEGORY_NAME = "未分类"


def _normalize_for_verification(text: str) -> str:
    """F22:原文核对的归一化——去全部空白、小写,容忍 PDF 换行/空格差异。"""
    return "".join((text or "").lower().split())


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class PaperUpdate:
    title: str
    authors: str
    year: int | None
    venue: str
    doi: str
    arxiv_id: str
    url: str
    abstract: str
    pdf_path: str
    reading_status: str
    importance: int
    summary: str
    datasets: str
    dataset_metrics: str
    methods: str
    contributions: str
    limitations: str
    experiment_notes: str
    reproducibility_notes: str
    memory_anchor: str
    personal_notes: str


@dataclass(frozen=True)
class FolderImportResult:
    added: int
    skipped: int
    failed: int
    total: int


@dataclass(frozen=True)
class PdfImportResult:
    paper: Paper
    created: bool
    duplicate_reason: str = ""


@dataclass(frozen=True)
class PdfImportMetadata:
    title: str
    authors: str = ""
    year: int | None = None
    doi: str = ""
    arxiv_id: str = ""
    abstract: str = ""


@dataclass(frozen=True)
class PdfImportPreview:
    source_path: Path
    metadata: PdfImportMetadata
    file_hash: str
    file_size: int
    duplicate_paper_id: int | None = None
    duplicate_title: str = ""
    duplicate_authors: str = ""
    duplicate_reason: str = ""


@dataclass(frozen=True)
class AutoClassifyResult:
    updated: int
    unchanged: int
    total: int
    created_categories: int


@dataclass(frozen=True)
class DatasetMetricRow:
    entry_id: int | None
    dataset: str
    metric: str
    value: str
    numeric_value: float | None
    unit: str
    direction: str
    model: str
    task: str
    notes: str
    paper_id: int | None
    paper_title: str


@dataclass(frozen=True)
class SavedSearchRule:
    """F20 智能文件夹规则:全部对应 list_papers 的既有过滤参数。"""

    search: str = ""
    search_fields: tuple[str, ...] | None = None  # None = 全字段
    tag_id: int | None = None
    category_id: int | None = None
    reading_status: str | None = None
    year: int | None = None
    min_importance: int | None = None

    def to_kwargs(self) -> dict:
        return {
            "search": self.search,
            "search_fields": set(self.search_fields) if self.search_fields else None,
            "tag_id": self.tag_id,
            "category_id": self.category_id,
            "reading_status": self.reading_status,
            "year": self.year,
            "min_importance": self.min_importance,
        }

    def to_json(self) -> str:
        return json_dumps(
            {
                "search": self.search,
                "search_fields": list(self.search_fields) if self.search_fields else None,
                "tag_id": self.tag_id,
                "category_id": self.category_id,
                "reading_status": self.reading_status,
                "year": self.year,
                "min_importance": self.min_importance,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> SavedSearchRule:
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        fields = payload.get("search_fields")
        return cls(
            search=str(payload.get("search") or ""),
            search_fields=tuple(fields) if isinstance(fields, list) and fields else None,
            tag_id=int(payload["tag_id"]) if payload.get("tag_id") is not None else None,
            category_id=int(payload["category_id"]) if payload.get("category_id") is not None else None,
            reading_status=str(payload["reading_status"]) if payload.get("reading_status") else None,
            year=int(payload["year"]) if payload.get("year") is not None else None,
            min_importance=int(payload["min_importance"]) if payload.get("min_importance") is not None else None,
        )


@dataclass(frozen=True)
class SavedSearchEntry:
    saved_search_id: int
    name: str
    rule: SavedSearchRule


@dataclass(frozen=True)
class DuplicateGroup:
    """库内疑似重复的一组文献(>=2 篇),paper_ids 按收录时间先后排列。"""

    reason: str
    paper_ids: tuple[int, ...]


# 重复判定依据按强度排序;find_duplicate_groups 的 reason 按此顺序合并展示。
DUPLICATE_REASON_ORDER = (
    "PDF 文件内容相同",
    "DOI 相同",
    "arXiv ID 相同",
    "PDF 路径相同",
    "标题和作者相同",
    "标题相同(缺作者元数据)",
    "标题和作者高度相似",
)


@dataclass(frozen=True)
class LibraryStatistics:
    """F8 统计面板所需的库级聚合;趋势月份键为本地时区 YYYY-MM(B6 展示语义)。"""

    total_papers: int
    pdf_count: int
    trashed_count: int
    deep_reading_count: int
    reading_status_counts: tuple[tuple[str, int], ...]
    category_counts: tuple[tuple[str, int], ...]
    year_counts: tuple[tuple[int, int], ...]
    import_month_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class DatasetMetricUpdate:
    dataset: str
    metric: str
    value: str
    model: str
    task: str = ""
    notes: str = ""
    paper_id: int | None = None
    unit: str = ""
    direction: str = "higher"


DATASET_ALIAS_GROUPS = {
    "ImageNet-1K": {
        "imagenet",
        "imagenet1k",
        "imagenet-1k",
        "imagenet_1k",
        "ilsvrc",
        "ilsvrc2012",
        "ilsvrc-2012",
    },
    "CIFAR-10": {"cifar10", "cifar-10", "cifar_10"},
    "CIFAR-100": {"cifar100", "cifar-100", "cifar_100"},
    "MS COCO": {"coco", "mscoco", "ms-coco", "ms coco"},
}


class LibraryRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        engine = session_factory.kw.get("bind")
        database_path = Path(engine.url.database).resolve() if engine is not None else None
        self.data_dir = database_path.parent if database_path is not None else Path.cwd() / "data"
        self.migrate_managed_pdf_paths()

    def resolve_pdf_path(self, value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return (self.data_dir / path).resolve()

    def stored_pdf_path(self, value: str | Path) -> str:
        path = Path(value)
        resolved = path if path.is_absolute() else self.resolve_pdf_path(path)
        try:
            return resolved.resolve().relative_to(self.data_dir.resolve()).as_posix()
        except ValueError:
            return str(resolved.resolve())

    def migrate_managed_pdf_paths(self) -> int:
        """Rebase legacy absolute paths into portable paths relative to data/."""
        migrated = 0
        with self._session_factory() as session:
            for paper in session.scalars(select(Paper)).all():
                value = (paper.pdf_path or "").strip()
                if not value:
                    continue
                path = Path(value)
                if not path.is_absolute():
                    normalized = Path(value).as_posix()
                    if paper.pdf_path != normalized:
                        paper.pdf_path = normalized
                        migrated += 1
                    continue
                relative = self._relative_managed_pdf_path(path)
                if relative is not None:
                    paper.pdf_path = relative.as_posix()
                    migrated += 1
            if migrated:
                session.commit()
        return migrated

    def _relative_managed_pdf_path(self, path: Path) -> Path | None:
        try:
            return path.resolve().relative_to(self.data_dir.resolve())
        except ValueError:
            pass
        parts = path.parts
        data_indexes = [
            index for index, part in enumerate(parts) if part.casefold() == "data"
        ]
        for index in reversed(data_indexes):
            if index + 1 >= len(parts):
                continue
            relative = Path(*parts[index + 1 :])
            if (self.data_dir / relative).exists():
                return relative
        return None

    def list_categories(self) -> list[Category]:
        with self._session_factory() as session:
            statement = select(Category).order_by(Category.parent_id, Category.sort_order, Category.name)
            return list(session.scalars(statement).all())

    def add_category(self, name: str, parent_id: int | None = None) -> Category:
        with self._session_factory() as session:
            max_order = session.scalar(
                select(func.max(Category.sort_order)).where(Category.parent_id == parent_id)
            )
            category = Category(
                name=name.strip(),
                parent_id=parent_id,
                sort_order=(int(max_order) if max_order is not None else 0) + 10,
            )
            session.add(category)
            session.commit()
            session.refresh(category)
            return category

    def rename_category(self, category_id: int, name: str) -> None:
        with self._session_factory() as session:
            category = session.get(Category, category_id)
            if category is None:
                raise ValueError(f"Category {category_id} does not exist")
            if category.name == UNCATEGORIZED_CATEGORY_NAME:
                raise ValueError("“未分类”是系统类别，不能重命名")
            category.name = name.strip()
            session.commit()

    def delete_category(self, category_id: int) -> None:
        with self._session_factory() as session:
            category = session.get(Category, category_id)
            if category is not None:
                if category.name == UNCATEGORIZED_CATEGORY_NAME:
                    raise ValueError("“未分类”是系统类别，不能删除")
                session.delete(category)
                session.commit()

    def move_category(self, category_id: int, direction: int) -> None:
        with self._session_factory() as session:
            category = session.get(Category, category_id)
            if category is None or direction == 0:
                return
            siblings = list(
                session.scalars(
                    select(Category)
                    .where(Category.parent_id == category.parent_id)
                    .order_by(Category.sort_order, Category.name)
                )
            )
            index = next((i for i, item in enumerate(siblings) if item.id == category_id), -1)
            target_index = index + (-1 if direction < 0 else 1)
            if index < 0 or target_index < 0 or target_index >= len(siblings):
                return
            other = siblings[target_index]
            category.sort_order, other.sort_order = other.sort_order, category.sort_order
            session.commit()

    def ensure_default_categories(self) -> int:
        with self._session_factory() as session:
            created = self._ensure_default_categories(session)
            session.commit()
            return created

    def list_tags(self) -> list[Tag]:
        with self._session_factory() as session:
            return list(
                session.scalars(select(Tag).order_by(Tag.name)).all()
            )

    def tag_paper_counts(self) -> dict[int, int]:
        with self._session_factory() as session:
            rows = session.execute(
                select(Tag.id, func.count(Paper.id))
                .join(Tag.papers, isouter=True)
                .where(Paper.deleted_at.is_(None))
                .group_by(Tag.id)
            ).all()
            return {int(tag_id): int(count) for tag_id, count in rows}

    def create_tag(self, name: str, color: str = "#6b7280") -> Tag:
        normalized = name.strip()
        if not normalized:
            raise ValueError("标签名不能为空")
        with self._session_factory() as session:
            existing = session.scalars(
                select(Tag).where(Tag.name == normalized)
            ).first()
            if existing is not None:
                return existing
            tag = Tag(name=normalized, color=color)
            session.add(tag)
            session.commit()
            session.refresh(tag)
            return tag

    def rename_tag(self, tag_id: int, name: str) -> None:
        normalized = name.strip()
        if not normalized:
            raise ValueError("标签名不能为空")
        with self._session_factory() as session:
            tag = session.get(Tag, tag_id)
            if tag is None:
                return
            tag.name = normalized
            session.commit()

    def set_tag_color(self, tag_id: int, color: str) -> None:
        with self._session_factory() as session:
            tag = session.get(Tag, tag_id)
            if tag is None:
                return
            tag.color = color
            session.commit()

    def delete_tag(self, tag_id: int) -> None:
        with self._session_factory() as session:
            tag = session.get(Tag, tag_id)
            if tag is not None:
                session.delete(tag)
                session.commit()

    def category_paper_counts(self) -> dict[int, int]:
        with self._session_factory() as session:
            rows = session.execute(
                select(Category.id, func.count(Paper.id))
                .join(Category.papers, isouter=True)
                .where(Paper.deleted_at.is_(None))
                .group_by(Category.id)
            ).all()
            return {int(category_id): int(count) for category_id, count in rows}

    def list_papers(
        self,
        search: str = "",
        category_id: int | None = None,
        search_fields: set[str] | None = None,
        sort_by: str = "updated",
        *,
        trashed: bool = False,
        tag_id: int | None = None,
        reading_status: str | None = None,
        year: int | None = None,
        min_importance: int | None = None,
    ) -> list[Paper]:
        with self._session_factory() as session:
            alive_filter = (
                Paper.deleted_at.is_not(None) if trashed else Paper.deleted_at.is_(None)
            )
            order_by = {
                "title": (func.lower(Paper.title).asc(), Paper.updated_at.desc()),
                "year": (
                    Paper.year.is_(None),
                    Paper.year.desc(),
                    Paper.updated_at.desc(),
                ),
                "importance": (
                    Paper.importance.desc(),
                    Paper.updated_at.desc(),
                ),
                "last_opened": (
                    Paper.last_opened_at.is_(None),
                    Paper.last_opened_at.desc(),
                    Paper.updated_at.desc(),
                ),
                "created": (Paper.created_at.desc(), Paper.title.asc()),
                "updated": (Paper.updated_at.desc(), Paper.created_at.desc()),
            }.get(sort_by, (Paper.updated_at.desc(), Paper.created_at.desc()))
            statement = select(Paper).options(
                joinedload(Paper.detail),
                joinedload(Paper.categories),
            ).where(alive_filter).order_by(*order_by)
            if category_id is not None:
                statement = statement.join(Paper.categories).where(Category.id == category_id)
            if tag_id is not None:
                statement = statement.join(Paper.tags).where(Tag.id == tag_id)
            if reading_status:
                statement = statement.where(Paper.reading_status == reading_status)
            if year is not None:
                statement = statement.where(Paper.year == year)
            if min_importance is not None:
                statement = statement.where(Paper.importance >= min_importance)
            if search.strip():
                keyword = f"%{search.strip()}%"
                fields = search_fields or {
                    "title",
                    "authors",
                    "abstract",
                    "memory_anchor",
                    "summary",
                    "notes",
                    "full_text",
                    "metadata",
                }
                conditions = []
                if "title" in fields:
                    conditions.append(Paper.title.ilike(keyword))
                if "authors" in fields:
                    conditions.append(Paper.authors.ilike(keyword))
                if "abstract" in fields:
                    conditions.append(Paper.abstract.ilike(keyword))
                if "memory_anchor" in fields:
                    conditions.append(PaperDetail.memory_anchor.ilike(keyword))
                if "summary" in fields:
                    conditions.extend(
                        [
                            PaperDetail.summary.ilike(keyword),
                            PaperDetail.datasets.ilike(keyword),
                            PaperDetail.dataset_metrics.ilike(keyword),
                            PaperDetail.methods.ilike(keyword),
                            PaperDetail.contributions.ilike(keyword),
                        ]
                    )
                if "notes" in fields:
                    conditions.extend(
                        [
                            PaperDetail.personal_notes.ilike(keyword),
                            PaperDetail.experiment_notes.ilike(keyword),
                            PaperDetail.reproducibility_notes.ilike(keyword),
                        ]
                    )
                if "metadata" in fields:
                    conditions.extend(
                        [
                            Paper.venue.ilike(keyword),
                            Paper.reading_status.ilike(keyword),
                            Paper.doi.ilike(keyword),
                            Paper.arxiv_id.ilike(keyword),
                        ]
                    )
                needs_text_join = False
                if "full_text" in fields:
                    keyword_term = search.strip()
                    if len(keyword_term) >= 3:
                        # FTS5 trigram 子串匹配(中英文等价于原 ILIKE 语义,走索引)
                        fts_phrase = '"' + keyword_term.replace('"', '""') + '"'
                        fts_condition = Paper.id.in_(
                            select(literal_column("rowid"))
                            .select_from(text("paper_texts_fts"))
                            .where(text("paper_texts_fts MATCH :full_text_fts_query"))
                        )
                        conditions.append(fts_condition)
                        statement = statement.options(
                            with_expression(Paper.full_text_match, fts_condition)
                        )
                    else:
                        needs_text_join = True
                        conditions.append(PaperText.content.ilike(keyword))
                        statement = statement.options(
                            with_expression(
                                Paper.full_text_match,
                                PaperText.content.ilike(keyword),
                            )
                        )
                statement = statement.where(or_(*conditions) if conditions else false())
                statement = statement.join(Paper.detail, isouter=True)
                if needs_text_join:
                    statement = statement.join(Paper.text, isouter=True)
                execute_params = (
                    {"full_text_fts_query": fts_phrase}
                    if "full_text" in fields and len(search.strip()) >= 3
                    else {}
                )
                return list(
                    session.scalars(statement, execute_params).unique().all()
                )
            return list(session.scalars(statement).unique().all())

    def get_paper(self, paper_id: int, *, include_text_content: bool = True) -> Paper:
        with self._session_factory() as session:
            text_option = joinedload(Paper.text)
            if not include_text_content:
                text_option = text_option.load_only(PaperText.page_count)
            statement = (
                select(Paper)
                .where(Paper.id == paper_id)
                .options(
                    joinedload(Paper.detail),
                    text_option,
                    joinedload(Paper.categories),
                    joinedload(Paper.tags),
                )
            )
            paper = session.scalars(statement).unique().one()
            if paper.detail is None:
                paper.detail = PaperDetail(paper_id=paper.id)
            return paper

    def create_blank_paper(self, category_id: int | None = None) -> Paper:
        with self._session_factory() as session:
            paper = Paper(title="未命名文献", detail=PaperDetail())
            category = session.get(Category, category_id) if category_id is not None else None
            paper.categories.append(
                category if category is not None else self._uncategorized_category(session)
            )
            session.add(paper)
            session.commit()
            session.refresh(paper)
            return paper

    def delete_paper(self, paper_id: int) -> None:
        """移入回收站(软删除);彻底删除用 purge_papers。"""
        with self._session_factory() as session:
            paper = session.get(Paper, paper_id)
            if paper is not None:
                paper.deleted_at = utc_now()
                session.commit()

    def list_trashed_papers(self) -> list[Paper]:
        return self.list_papers(trashed=True)

    def restore_papers(self, paper_ids: list[int]) -> int:
        unique_ids = {int(paper_id) for paper_id in paper_ids}
        if not unique_ids:
            return 0
        restored = 0
        with self._session_factory() as session:
            papers = list(
                session.scalars(
                    select(Paper).where(Paper.id.in_(unique_ids))
                )
            )
            for paper in papers:
                if paper.deleted_at is not None:
                    paper.deleted_at = None
                    paper.updated_at = utc_now()
                    restored += 1
            session.commit()
        return restored

    def purge_papers(self, paper_ids: list[int]) -> int:
        """彻底删除(硬删除记录,级联清理正文/详情/指标,FTS 由事件自动清理)。"""
        unique_ids = {int(paper_id) for paper_id in paper_ids}
        if not unique_ids:
            return 0
        with self._session_factory() as session:
            papers = list(session.scalars(select(Paper).where(Paper.id.in_(unique_ids))))
            for paper in papers:
                session.delete(paper)
            session.commit()
            return len(papers)

    def find_duplicate_groups(self) -> list[DuplicateGroup]:
        """扫描正常库(不含回收站)中的疑似重复文献。

        判定语义与导入查重 _find_duplicate_paper 一致:文件哈希/DOI/arXiv ID/
        PDF 路径完全一致,或标题作者匹配(含「缺作者元数据」「高相似标题」两种
        降级规则)。组内多重依据同时命中时 reason 按强度顺序合并展示。
        """
        with self._session_factory() as session:
            papers = list(
                session.scalars(
                    select(Paper).where(Paper.deleted_at.is_(None))
                ).all()
            )
        if len(papers) < 2:
            return []
        papers.sort(key=lambda paper: (paper.created_at or utc_now(), paper.id))

        parent = list(range(len(papers)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        group_reasons: dict[int, set[str]] = {}

        def record_union(left: int, right: int, reason: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root == right_root:
                group_reasons.setdefault(left_root, set()).add(reason)
                return
            merged = group_reasons.pop(left_root, set()) | group_reasons.pop(right_root, set())
            merged.add(reason)
            parent[max(left_root, right_root)] = min(left_root, right_root)
            group_reasons[min(left_root, right_root)] = merged

        buckets: dict[str, list[int]] = {}
        bucket_reason = {
            "hash": "PDF 文件内容相同",
            "path": "PDF 路径相同",
            "doi": "DOI 相同",
            "arxiv": "arXiv ID 相同",
        }
        for index, paper in enumerate(papers):
            if paper.file_hash:
                buckets.setdefault(f"hash:{paper.file_hash}", []).append(index)
            if paper.pdf_path:
                normalized_path = self._normalize_pdf_path(paper.pdf_path)
                buckets.setdefault(f"path:{normalized_path}", []).append(index)
            doi_key = self._normalize_doi(paper.doi)
            if doi_key:
                buckets.setdefault(f"doi:{doi_key}", []).append(index)
            arxiv_key = self._normalize_arxiv_id(paper.arxiv_id)
            if arxiv_key:
                buckets.setdefault(f"arxiv:{arxiv_key}", []).append(index)
        for key, members in buckets.items():
            if len(members) < 2:
                continue
            reason = bucket_reason[key.split(":", 1)[0]]
            for other in members[1:]:
                record_union(members[0], other, reason)

        # 标题/作者匹配是文本判定,只对尚无更强依据相连的文献两两比较。
        title_indexed: list[tuple[int, str]] = []
        for index, paper in enumerate(papers):
            title_key = self._normalize_identity_text(paper.title)
            if self._is_meaningful_title(paper.title, title_key):
                title_indexed.append((index, title_key))
        for (left, key_left), (right, key_right) in itertools.combinations(title_indexed, 2):
            if find(left) == find(right):
                continue
            paper_left, paper_right = papers[left], papers[right]
            authors_match = self._authors_match(paper_left.authors, paper_right.authors)
            both_have_authors = bool(
                self._normalize_identity_text(paper_left.authors)
                and self._normalize_identity_text(paper_right.authors)
            )
            if key_left == key_right:
                if authors_match:
                    record_union(left, right, "标题和作者相同")
                elif not both_have_authors and self._title_is_distinctive(paper_left.title):
                    record_union(left, right, "标题相同(缺作者元数据)")
            elif (
                both_have_authors
                and authors_match
                and self._titles_are_highly_similar(paper_left.title, paper_right.title)
            ):
                record_union(left, right, "标题和作者高度相似")

        groups: dict[int, list[int]] = {}
        for index in range(len(papers)):
            groups.setdefault(find(index), []).append(index)
        results: list[DuplicateGroup] = []
        for root, members in groups.items():
            if len(members) < 2:
                continue
            reasons = sorted(
                group_reasons.get(root, set()),
                key=DUPLICATE_REASON_ORDER.index,
            )
            results.append(
                DuplicateGroup(
                    reason="、".join(reasons),
                    paper_ids=tuple(papers[member].id for member in members),
                )
            )
        return results

    def merge_duplicate_papers(self, keep_id: int, duplicate_ids: list[int]) -> dict[str, int]:
        """把重复文献里「保留记录缺失」的信息并入保留记录(不覆盖已填字段)。

        覆盖:基本元数据(作者/年份/来源/DOI/arXiv/URL/摘要,标题仅在未命名时
        补)、AI 字段与个人笔记、分类与标签取并集、PDF 文件(保留记录没有时收编
        对方的,正文全文随迁)。其余重复记录由调用方决定去留(向导移入回收站)。
        返回各类补齐数量,供 UI 生成提示。
        """
        counts = {
            "metadata": 0,
            "ai_fields": 0,
            "categories": 0,
            "tags": 0,
            "pdf": 0,
        }
        unique_ids = {int(paper_id) for paper_id in duplicate_ids if int(paper_id) != int(keep_id)}
        if not unique_ids:
            return counts
        with self._session_factory() as session:
            keeper = session.get(Paper, keep_id)
            if keeper is None:
                raise ValueError(f"Paper {keep_id} does not exist")
            duplicates = [
                paper
                for paper in session.scalars(select(Paper).where(Paper.id.in_(unique_ids))).all()
                if paper.deleted_at is None
            ]
            if not duplicates:
                return counts

            if keeper.title.strip() in ("", "未命名文献"):
                for duplicate in duplicates:
                    if duplicate.title.strip():
                        keeper.title = duplicate.title.strip()
                        counts["metadata"] += 1
                        break
            for attr in ("authors", "venue", "doi", "arxiv_id", "url", "abstract"):
                if getattr(keeper, attr, ""):
                    continue
                for duplicate in duplicates:
                    value = (getattr(duplicate, attr) or "").strip()
                    if value:
                        setattr(keeper, attr, value)
                        counts["metadata"] += 1
                        break
            if keeper.year is None:
                for duplicate in duplicates:
                    if duplicate.year is not None:
                        keeper.year = duplicate.year
                        counts["metadata"] += 1
                        break

            if keeper.detail is None:
                keeper.detail = PaperDetail(paper_id=keeper.id)
            for attr in (
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
            ):
                if getattr(keeper.detail, attr, ""):
                    continue
                for duplicate in duplicates:
                    if duplicate.detail is None:
                        continue
                    value = (getattr(duplicate.detail, attr) or "").strip()
                    if value:
                        setattr(keeper.detail, attr, value)
                        counts["ai_fields"] += 1
                        break

            keeper_category_ids = {category.id for category in keeper.categories}
            for duplicate in duplicates:
                for category in duplicate.categories:
                    if category.id in keeper_category_ids:
                        continue
                    keeper_category_ids.add(category.id)
                    keeper.categories.append(category)
                    counts["categories"] += 1
            if any(
                category.name != UNCATEGORIZED_CATEGORY_NAME
                for category in keeper.categories
            ):
                keeper.categories = [
                    category
                    for category in keeper.categories
                    if category.name != UNCATEGORIZED_CATEGORY_NAME
                ]

            keeper_tag_ids = {tag.id for tag in keeper.tags}
            for duplicate in duplicates:
                for tag in duplicate.tags:
                    if tag.id in keeper_tag_ids:
                        continue
                    keeper_tag_ids.add(tag.id)
                    keeper.tags.append(tag)
                    counts["tags"] += 1

            if not keeper.pdf_path:
                for duplicate in duplicates:
                    if duplicate.pdf_path:
                        keeper.pdf_path = duplicate.pdf_path
                        keeper.file_hash = duplicate.file_hash
                        counts["pdf"] += 1
                        # 收编 PDF 时正文全文一并随迁,避免全文搜索出现空档
                        if duplicate.text is not None and (
                            keeper.text is None or not keeper.text.content
                        ):
                            if keeper.text is None:
                                keeper.text = PaperText(paper_id=keeper.id)
                            keeper.text.content = duplicate.text.content
                            keeper.text.page_count = duplicate.text.page_count
                        break

            keeper.updated_at = utc_now()
            session.commit()
            return counts

    def set_primary_category(self, paper_id: int, category_id: int | None) -> None:
        """F19 主分类:每篇文献恰好一个主分类(None → 未分类)。

        多分类时代的其余关联由调用方(整理向导)负责先转标签。
        """
        with self._session_factory() as session:
            paper = session.get(Paper, paper_id)
            if paper is None:
                raise ValueError(f"Paper {paper_id} does not exist")
            category = session.get(Category, category_id) if category_id is not None else None
            if category is not None and category.name != UNCATEGORIZED_CATEGORY_NAME:
                paper.categories = [category]
            else:
                paper.categories = [self._uncategorized_category(session)]
            paper.updated_at = utc_now()
            session.commit()

    def add_tag_to_paper(self, paper_id: int, tag_name: str) -> bool:
        """F19:给文献打一个自由标签(同名复用,幂等)。返回是否新增。"""
        name = tag_name.strip()
        if not name:
            return False
        with self._session_factory() as session:
            paper = session.get(Paper, paper_id)
            if paper is None:
                raise ValueError(f"Paper {paper_id} does not exist")
            tag = session.scalars(select(Tag).where(Tag.name == name)).first()
            if tag is None:
                tag = Tag(name=name, color="#6b7280")
                session.add(tag)
                session.flush()
            if any(existing.id == tag.id for existing in paper.tags):
                return False
            paper.tags.append(tag)
            paper.updated_at = utc_now()
            session.commit()
            return True

    def demote_categories_to_tags(self, category_ids: list[int]) -> dict[int, list[str]]:
        """F19:分类节点降级为标签——关联文献先获得同名标签,再解除关联;
        无子节点的分类从树删除(有子节点则保留节点仅解绑文献)。
        返回 {paper_id: [新获得的标签名]}。
        """
        unique_ids = {int(value) for value in category_ids}
        applied: dict[int, list[str]] = {}
        with self._session_factory() as session:
            categories = {
                category.id: category
                for category in session.scalars(
                    select(Category).where(Category.id.in_(unique_ids))
                ).all()
            }
            uncategorized_ids: set[int] = set()
            for category_id in sorted(unique_ids):
                category = categories.get(category_id)
                if category is None or category.name == UNCATEGORIZED_CATEGORY_NAME:
                    continue
                tag = session.scalars(select(Tag).where(Tag.name == category.name)).first()
                if tag is None:
                    tag = Tag(name=category.name, color="#6b7280")
                    session.add(tag)
                    session.flush()
                papers = list(
                    session.scalars(
                        select(Paper).join(Paper.categories).where(Category.id == category_id)
                    ).all()
                )
                for paper in papers:
                    if not any(existing.id == tag.id for existing in paper.tags):
                        paper.tags.append(tag)
                        applied.setdefault(paper.id, []).append(category.name)
                    paper.categories = [
                        current for current in paper.categories if current.id != category_id
                    ]
                    if not paper.categories:
                        paper.categories = [self._uncategorized_category(session)]
                has_children = (
                    session.scalar(
                        select(func.count())
                        .select_from(Category)
                        .where(Category.parent_id == category_id)
                    )
                    or 0
                )
                if not has_children:
                    session.delete(category)
                    uncategorized_ids.add(category_id)
            session.commit()
        return applied

    def create_identifier_paper(self, metadata) -> tuple[Paper | None, Paper | None]:
        """魔棒添加:按 DOI/arXiv 元数据建条目;命中重复时返回 (None, existing)。"""
        from synthrecall.metadata_enrichment import PaperMetadata

        if not isinstance(metadata, PaperMetadata):
            raise TypeError("metadata 必须是 PaperMetadata")
        with self._session_factory() as session:
            duplicate = self._find_duplicate_paper(
                session,
                file_hash="",
                pdf_paths=set(),
                title=metadata.title,
                authors=metadata.authors,
                doi=metadata.doi,
                arxiv_id=metadata.arxiv_id,
            )
            if duplicate is not None:
                existing_paper, _reason = duplicate
                return None, existing_paper
            paper = Paper(
                title=metadata.title.strip() or "未命名文献",
                authors=metadata.authors.strip(),
                year=metadata.year,
                venue=metadata.venue.strip(),
                doi=metadata.doi.strip(),
                arxiv_id=metadata.arxiv_id.strip(),
                url=metadata.url.strip(),
                abstract=metadata.abstract.strip(),
            )
            paper.detail = PaperDetail(paper_id=paper.id)
            session.add(paper)
            session.flush()
            paper.categories.append(self._uncategorized_category(session))
            session.commit()
            return paper, None

    def related_papers(
        self, paper_id: int, limit: int = 5
    ) -> list[tuple[Paper, int, list[str]]]:
        """F13:基于标签/主分类/数据集/记忆锚点重叠的库内相关文献推荐。

        返回 [(paper, score, reasons)],score>0 按分数降序;排除自身与回收站。
        reasons 是可解释的命中依据(如「标签:语义分割」)。
        """
        with self._session_factory() as session:
            papers = list(
                session.scalars(
                    select(Paper)
                    .options(joinedload(Paper.detail), joinedload(Paper.tags), joinedload(Paper.categories))
                    .where(Paper.deleted_at.is_(None))
                )
                .unique()
                .all()
            )
        target = next((p for p in papers if p.id == paper_id), None)
        if target is None:
            return []

        separators = "[" + chr(10) + ";；,，、/|]"
        def _norm_items(text: str) -> set[str]:
            items = set()
            for chunk in re.split(separators, text or ""):
                cleaned = chunk.strip().casefold()
                if cleaned and len(cleaned) >= 2:
                    items.add(cleaned)
            return items

        newline = chr(10)
        target_tags = {tag.id for tag in target.tags}
        target_datasets = _norm_items(
            (target.detail.datasets if target.detail else "")
            + newline
            + (target.detail.dataset_metrics if target.detail else "")
        )
        target_anchor = _norm_items(target.detail.memory_anchor if target.detail else "")
        target_primary = next(
            (
                c.name
                for c in target.categories
                if c.name != UNCATEGORIZED_CATEGORY_NAME
            ),
            "",
        )

        results: list[tuple[Paper, int, list[str]]] = []
        for candidate in papers:
            if candidate.id == paper_id:
                continue
            score = 0
            reasons: list[str] = []
            shared_tags = [tag.name for tag in candidate.tags if tag.id in target_tags]
            if shared_tags:
                score += 3 * len(shared_tags)
                reasons.append("标签:" + "、".join(shared_tags[:3]))
            candidate_primary = next(
                (c.name for c in candidate.categories if c.name != UNCATEGORIZED_CATEGORY_NAME),
                "",
            )
            if target_primary and candidate_primary == target_primary:
                score += 4
                reasons.append(f"同主分类:{target_primary}")
            candidate_datasets = _norm_items(
                (candidate.detail.datasets if candidate.detail else "")
                + newline
                + (candidate.detail.dataset_metrics if candidate.detail else "")
            )
            shared_datasets = sorted(target_datasets & candidate_datasets)[:3]
            if shared_datasets:
                score += 2 * len(shared_datasets)
                reasons.append("数据集:" + "、".join(shared_datasets))
            candidate_anchor = _norm_items(candidate.detail.memory_anchor if candidate.detail else "")
            shared_anchor = sorted(target_anchor & candidate_anchor)[:2]
            if shared_anchor:
                score += len(shared_anchor)
                reasons.append("锚点:" + "、".join(shared_anchor))
            if score > 0:
                results.append((candidate, score, reasons))
        results.sort(key=lambda item: (-item[1], item[0].title))
        return results[:limit]

    def list_saved_searches(self) -> list[SavedSearchEntry]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(SavedSearch).order_by(SavedSearch.sort_order, SavedSearch.id)
            ).all()
            return [
                SavedSearchEntry(
                    saved_search_id=row.id,
                    name=row.name,
                    rule=SavedSearchRule.from_json(row.rule_json),
                )
                for row in rows
            ]

    def create_saved_search(self, name: str, rule: SavedSearchRule) -> SavedSearchEntry:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("智能文件夹名称不能为空。")
        with self._session_factory() as session:
            max_order = (
                session.scalar(select(func.max(SavedSearch.sort_order))) or 0
            )
            row = SavedSearch(name=cleaned, rule_json=rule.to_json(), sort_order=max_order + 1)
            session.add(row)
            session.commit()
            return SavedSearchEntry(saved_search_id=row.id, name=row.name, rule=rule)

    def update_saved_search(self, saved_search_id: int, name: str, rule: SavedSearchRule) -> None:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("智能文件夹名称不能为空。")
        with self._session_factory() as session:
            row = session.get(SavedSearch, saved_search_id)
            if row is None:
                raise ValueError(f"Saved search {saved_search_id} does not exist")
            row.name = cleaned
            row.rule_json = rule.to_json()
            session.commit()

    def delete_saved_search(self, saved_search_id: int) -> None:
        with self._session_factory() as session:
            row = session.get(SavedSearch, saved_search_id)
            if row is not None:
                session.delete(row)
                session.commit()

    def library_statistics(
        self, *, trend_months: int = 12, deep_reading_statuses: frozenset[str] = frozenset({"精读", "已复现"})
    ) -> LibraryStatistics:
        """F8:库级统计聚合(正常库;回收站只计总数)。

        导入趋势按本地时区月份分桶(B6:库内时间为 UTC naive,展示须转本地),
        并补齐近 trend_months 个月的零值月份,保证趋势图连续。
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    Paper.created_at,
                    Paper.year,
                    Paper.reading_status,
                    Paper.pdf_path,
                ).where(Paper.deleted_at.is_(None))
            ).all()
            trashed_count = (
                session.scalar(
                    select(func.count())
                    .select_from(Paper)
                    .where(Paper.deleted_at.is_not(None))
                )
                or 0
            )
            category_rows = session.execute(
                select(Category.name, func.count(Paper.id))
                .join(paper_categories, paper_categories.c.category_id == Category.id)
                .join(Paper, Paper.id == paper_categories.c.paper_id)
                .where(Paper.deleted_at.is_(None))
                .group_by(Category.id)
                .order_by(func.count(Paper.id).desc(), Category.name.asc())
            ).all()

        status_counter: Counter[str] = Counter(row.reading_status or "待读" for row in rows)
        year_counter: Counter[int] = Counter(row.year for row in rows if row.year is not None)
        month_counter: Counter[str] = Counter(
            row.created_at.replace(tzinfo=UTC).astimezone().strftime("%Y-%m")
            for row in rows
            if row.created_at is not None
        )

        canonical_order = ["待读", "浏览", "已读", "精读", "复现中", "已复现"]
        status_items = [
            (status, status_counter.pop(status))
            for status in canonical_order
            if status_counter.get(status)
        ]
        status_items.extend(sorted(status_counter.items(), key=lambda item: -item[1]))

        today = datetime.now(UTC).astimezone()
        month_keys = []
        year_i, month_i = today.year, today.month
        for _ in range(trend_months):
            month_keys.append(f"{year_i:04d}-{month_i:02d}")
            month_i -= 1
            if month_i == 0:
                month_i = 12
                year_i -= 1
        month_keys.reverse()

        return LibraryStatistics(
            total_papers=len(rows),
            pdf_count=sum(1 for row in rows if row.pdf_path),
            trashed_count=trashed_count,
            deep_reading_count=sum(
                1 for row in rows if (row.reading_status or "") in deep_reading_statuses
            ),
            reading_status_counts=tuple(status_items),
            category_counts=tuple((name, count) for name, count in category_rows),
            year_counts=tuple(sorted(year_counter.items())),
            import_month_counts=tuple((key, month_counter.get(key, 0)) for key in month_keys),
        )

    def set_papers_reading_status(self, paper_ids: list[int], reading_status: str) -> int:
        unique_ids = {int(paper_id) for paper_id in paper_ids}
        if not unique_ids:
            return 0
        updated = 0
        with self._session_factory() as session:
            papers = list(session.scalars(select(Paper).where(Paper.id.in_(unique_ids))))
            for paper in papers:
                paper.reading_status = reading_status.strip() or "待读"
                paper.updated_at = utc_now()
                updated += 1
            session.commit()
        return updated

    def add_category_to_papers(self, paper_ids: list[int], category_id: int) -> int:
        unique_ids = {int(paper_id) for paper_id in paper_ids}
        if not unique_ids:
            return 0
        updated = 0
        with self._session_factory() as session:
            category = session.get(Category, category_id)
            if category is None:
                raise ValueError(f"Category {category_id} does not exist")
            papers = list(
                session.scalars(
                    select(Paper)
                    .where(Paper.id.in_(unique_ids))
                    .options(joinedload(Paper.categories))
                ).unique()
            )
            for paper in papers:
                if all(item.id != category_id for item in paper.categories):
                    if category.name != UNCATEGORIZED_CATEGORY_NAME:
                        paper.categories = [
                            item
                            for item in paper.categories
                            if item.name != UNCATEGORIZED_CATEGORY_NAME
                        ]
                    elif any(
                        item.name != UNCATEGORIZED_CATEGORY_NAME
                        for item in paper.categories
                    ):
                        continue
                    paper.categories.append(category)
                    paper.updated_at = utc_now()
                    updated += 1
            session.commit()
        return updated

    def delete_papers(self, paper_ids: list[int]) -> int:
        """批量移入回收站(软删除)。"""
        unique_ids = {int(paper_id) for paper_id in paper_ids}
        if not unique_ids:
            return 0
        with self._session_factory() as session:
            papers = list(
                session.scalars(
                    select(Paper).where(Paper.id.in_(unique_ids))
                )
            )
            now = utc_now()
            for paper in papers:
                paper.deleted_at = now
            session.commit()
            return len(papers)

    def store_pdf_file(
        self,
        pdf_path: Path,
        category_id: int | None,
        library_paper_dir: Path,
    ) -> Path:
        source_pdf_path = pdf_path.resolve()
        file_hash = sha256_file(source_pdf_path)
        with self._session_factory() as session:
            managed_pdf_path = self._managed_pdf_path(
                session=session,
                source_pdf_path=source_pdf_path,
                category_id=category_id,
                library_paper_dir=library_paper_dir,
                file_hash=file_hash,
            )
            if managed_pdf_path != source_pdf_path:
                managed_pdf_path.parent.mkdir(parents=True, exist_ok=True)
                if not managed_pdf_path.exists():
                    shutil.copy2(source_pdf_path, managed_pdf_path)
            return managed_pdf_path

    def create_from_pdf(
        self,
        pdf_path: Path,
        category_id: int | None = None,
        library_paper_dir: Path | None = None,
    ) -> Paper:
        return self.import_pdf(
            pdf_path,
            category_id=category_id,
            library_paper_dir=library_paper_dir,
        ).paper

    def preview_pdf_import(
        self,
        pdf_path: Path,
        category_id: int | None = None,
        library_paper_dir: Path | None = None,
    ) -> PdfImportPreview:
        source_pdf_path = pdf_path.resolve()
        if not source_pdf_path.is_file():
            raise FileNotFoundError(f"PDF 文件不存在：{source_pdf_path}")
        if source_pdf_path.suffix.lower() != ".pdf":
            raise ValueError(f"不是 PDF 文件：{source_pdf_path}")
        metadata = read_pdf_metadata(source_pdf_path)
        file_hash = sha256_file(source_pdf_path)
        title = self._clean_pdf_title(metadata["title"], source_pdf_path)
        authors = metadata["authors"].strip()
        abstract = extract_text_preview(source_pdf_path)
        identifiers = extract_paper_identifiers(abstract)
        import_metadata = PdfImportMetadata(
            title=title,
            authors=authors,
            doi=identifiers["doi"],
            arxiv_id=identifiers["arxiv_id"],
            abstract=abstract,
        )
        duplicate_paper_id: int | None = None
        duplicate_title = ""
        duplicate_authors = ""
        duplicate_reason = ""
        with self._session_factory() as session:
            managed_pdf_path = self._managed_pdf_path(
                session=session,
                source_pdf_path=source_pdf_path,
                category_id=category_id,
                library_paper_dir=library_paper_dir,
                file_hash=file_hash,
            )
            duplicate = self._find_duplicate_paper(
                session,
                file_hash=file_hash,
                pdf_paths={str(managed_pdf_path), str(source_pdf_path)},
                title=title,
                authors=authors,
                doi=identifiers["doi"],
                arxiv_id=identifiers["arxiv_id"],
            )
            if duplicate is not None:
                existing, duplicate_reason = duplicate
                duplicate_paper_id = existing.id
                duplicate_title = existing.title
                duplicate_authors = existing.authors
        return PdfImportPreview(
            source_path=source_pdf_path,
            metadata=import_metadata,
            file_hash=file_hash,
            file_size=source_pdf_path.stat().st_size,
            duplicate_paper_id=duplicate_paper_id,
            duplicate_title=duplicate_title,
            duplicate_authors=duplicate_authors,
            duplicate_reason=duplicate_reason,
        )

    def import_pdf(
        self,
        pdf_path: Path,
        category_id: int | None = None,
        library_paper_dir: Path | None = None,
        metadata: PdfImportMetadata | None = None,
    ) -> PdfImportResult:
        source_pdf_path = pdf_path.resolve()
        preview = self.preview_pdf_import(
            source_pdf_path,
            category_id=category_id,
            library_paper_dir=library_paper_dir,
        )
        file_hash = sha256_file(source_pdf_path)
        import_metadata = metadata or preview.metadata
        title = self._clean_pdf_title(import_metadata.title, source_pdf_path)
        authors = import_metadata.authors.strip()
        abstract = import_metadata.abstract.strip()
        doi = import_metadata.doi.strip()
        arxiv_id = import_metadata.arxiv_id.strip()
        with self._session_factory() as session:
            managed_pdf_path = self._managed_pdf_path(
                session=session,
                source_pdf_path=source_pdf_path,
                category_id=category_id,
                library_paper_dir=library_paper_dir,
                file_hash=file_hash,
            )
            duplicate = self._find_duplicate_paper(
                session,
                file_hash=file_hash,
                pdf_paths={str(managed_pdf_path), str(source_pdf_path)},
                title=title,
                authors=authors,
                doi=doi,
                arxiv_id=arxiv_id,
            )
            if duplicate is not None:
                existing, reason = duplicate
                return PdfImportResult(
                    paper=existing,
                    created=False,
                    duplicate_reason=reason,
                )
            if managed_pdf_path != source_pdf_path:
                managed_pdf_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_pdf_path, managed_pdf_path)
            full_text, page_count = extract_pdf_text(managed_pdf_path)

            paper = Paper(
                title=title,
                authors=authors,
                year=import_metadata.year,
                doi=doi,
                arxiv_id=arxiv_id,
                pdf_path=self.stored_pdf_path(managed_pdf_path),
                file_hash=file_hash,
                abstract=abstract,
                detail=PaperDetail(
                    personal_notes=self._filename_note(source_pdf_path),
                ),
                text=PaperText(content=full_text, page_count=page_count),
            )
            if category_id is None:
                category = self._uncategorized_category(session)
            else:
                category = session.get(Category, category_id)
                if category is None:
                    raise ValueError(f"Category {category_id} does not exist")
            paper.categories.append(category)
            session.add(paper)
            session.commit()
            paper_id = paper.id
        return PdfImportResult(paper=self.get_paper(paper_id), created=True)

    def merge_pdf_import(
        self,
        pdf_path: Path,
        paper_id: int,
        metadata: PdfImportMetadata,
        category_id: int | None = None,
        library_paper_dir: Path | None = None,
    ) -> Paper:
        """Fill missing fields on an existing record without overwriting notes or its PDF."""
        source_pdf_path = pdf_path.resolve()
        file_hash = sha256_file(source_pdf_path)
        with self._session_factory() as session:
            paper = session.get(Paper, paper_id)
            if paper is None:
                raise ValueError(f"Paper {paper_id} does not exist")
            if paper.detail is None:
                paper.detail = PaperDetail(paper_id=paper_id)

            if not self._is_meaningful_title(
                paper.title,
                self._normalize_identity_text(paper.title),
            ):
                paper.title = self._clean_pdf_title(metadata.title, source_pdf_path)
            if not paper.authors.strip():
                paper.authors = metadata.authors.strip()
            if paper.year is None:
                paper.year = metadata.year
            if not paper.doi.strip():
                paper.doi = metadata.doi.strip()
            if not paper.arxiv_id.strip():
                paper.arxiv_id = metadata.arxiv_id.strip()
            if not paper.abstract.strip():
                paper.abstract = metadata.abstract.strip()

            current_pdf = self.resolve_pdf_path(paper.pdf_path) if paper.pdf_path else None
            if current_pdf is None or not current_pdf.exists():
                managed_pdf_path = self._managed_pdf_path(
                    session=session,
                    source_pdf_path=source_pdf_path,
                    category_id=category_id,
                    library_paper_dir=library_paper_dir,
                    file_hash=file_hash,
                )
                if managed_pdf_path != source_pdf_path:
                    managed_pdf_path.parent.mkdir(parents=True, exist_ok=True)
                    if not managed_pdf_path.exists():
                        shutil.copy2(source_pdf_path, managed_pdf_path)
                full_text, page_count = extract_pdf_text(managed_pdf_path)
                paper.pdf_path = self.stored_pdf_path(managed_pdf_path)
                paper.file_hash = file_hash
                paper.text = PaperText(content=full_text, page_count=page_count)

            if category_id is not None and all(
                category.id != category_id for category in paper.categories
            ):
                category = session.get(Category, category_id)
                if category is not None:
                    if category.name != UNCATEGORIZED_CATEGORY_NAME:
                        paper.categories = [
                            item
                            for item in paper.categories
                            if item.name != UNCATEGORIZED_CATEGORY_NAME
                        ]
                    paper.categories.append(category)
            filename_note = self._filename_note(source_pdf_path)
            if filename_note and filename_note not in paper.detail.personal_notes:
                paper.detail.personal_notes = (
                    f"{paper.detail.personal_notes}\n\n{filename_note}".strip()
                )
            paper.updated_at = utc_now()
            session.commit()
        return self.get_paper(paper_id)

    def import_pdf_folder(
        self,
        folder: Path,
        category_id: int | None = None,
        library_paper_dir: Path | None = None,
    ) -> FolderImportResult:
        pdf_paths = sorted(
            {path.resolve() for path in [*folder.glob("*.pdf"), *folder.glob("*.PDF")]},
            key=lambda path: path.name.lower(),
        )
        added = 0
        skipped = 0
        failed = 0
        for pdf_path in pdf_paths:
            try:
                result = self.import_pdf(
                    pdf_path,
                    category_id=category_id,
                    library_paper_dir=library_paper_dir,
                )
                if result.created:
                    added += 1
                else:
                    skipped += 1
            except Exception:  # noqa: BLE001 — 单篇导入失败计数,不阻塞批量
                failed += 1
        return FolderImportResult(
            added=added,
            skipped=skipped,
            failed=failed,
            total=len(pdf_paths),
        )

    def record_token_usage(
        self,
        *,
        feature: str,
        model: str = "",
        paper_id: int | None = None,
        source: str = "app",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        estimated: bool = False,
    ) -> None:
        """F20:记录一次 AI 调用的 token 用量;统计失败绝不影响主流程。"""
        try:
            with self._session_factory() as session:
                session.add(
                    TokenUsageRecord(
                        feature=feature,
                        model=model,
                        paper_id=paper_id,
                        source=source,
                        prompt_tokens=int(prompt_tokens or 0),
                        completion_tokens=int(completion_tokens or 0),
                        total_tokens=int(total_tokens or 0),
                        estimated=bool(estimated),
                    )
                )
                session.commit()
        except Exception:  # noqa: BLE001, S110 — 用量统计是旁路,失败静默
            pass

    def token_usage_summary(self) -> dict:
        """F20:今日/近7天/近30天/累计四桶 + 累计按功能细分。

        今日按本地时区零点起算(库内时间为 UTC naive,换算后比较);
        数值可能持续增大,列类型为 64 位整数,SQL 端聚合不逐行加载。
        """
        with self._session_factory() as session:
            now_utc = datetime.now(UTC).replace(tzinfo=None)
            local_offset = datetime.now().astimezone().utcoffset()
            today_start_utc = (
                datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                - (local_offset or timedelta(0))
            )

            def _bucket(since: datetime | None) -> dict:
                stmt = select(
                    func.coalesce(func.sum(TokenUsageRecord.prompt_tokens), 0),
                    func.coalesce(func.sum(TokenUsageRecord.completion_tokens), 0),
                    func.coalesce(func.sum(TokenUsageRecord.total_tokens), 0),
                    func.coalesce(
                        func.sum(
                            case(
                                (TokenUsageRecord.estimated, TokenUsageRecord.total_tokens),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                )
                if since is not None:
                    stmt = stmt.where(TokenUsageRecord.created_at >= since)
                row = session.execute(stmt).one()
                return {
                    "prompt": int(row[0]),
                    "completion": int(row[1]),
                    "total": int(row[2]),
                    "estimated": int(row[3]),
                }

            features = {
                str(name or "未标注"): int(total)
                for name, total in session.execute(
                    select(TokenUsageRecord.feature, func.sum(TokenUsageRecord.total_tokens))
                    .group_by(TokenUsageRecord.feature)
                    .order_by(func.sum(TokenUsageRecord.total_tokens).desc())
                ).all()
            }
            return {
                "today": _bucket(today_start_utc),
                "week": _bucket(now_utc - timedelta(days=7)),
                "month": _bucket(now_utc - timedelta(days=30)),
                "total": _bucket(None),
                "features": features,
            }

    def set_paper_favorite(self, paper_id: int, favorite: bool) -> None:
        """F21:收藏/取消收藏;只改标记,不触碰 updated_at(不算内容修改)。"""
        with self._session_factory() as session:
            paper = session.get(Paper, paper_id)
            if paper is None:
                return
            paper.is_favorite = bool(favorite)
            session.commit()

    def apply_ai_experiment_metrics(
        self, paper_id: int, rows: list[dict], source_text: str
    ) -> tuple[int, int]:
        """F22:AI 解析抽取的实验数据落库到数据集排行榜。

        正确性优先:
        - 每条先做「原文核对」——value/dataset/model(非空者)必须能在论文
          全文中找到(去空白、不区分大小写的子串匹配);核对不到的照样收录,
          但 notes 明确标注「原文未核对到,请人工确认」;
        - 与该文献已有条目按 (dataset, metric, model, value) 去重,手工录入
          的条目永远不会被覆盖;
        - 返回 (新增条数, 其中已核对条数)。
        """
        normalized_source = _normalize_for_verification(source_text)
        added = 0
        verified = 0
        with self._session_factory() as session:
            existing = {
                (
                    entry.dataset.lower(),
                    entry.metric.lower(),
                    entry.model.lower(),
                    entry.value,
                )
                for entry in session.scalars(
                    select(DatasetMetricEntry).where(
                        DatasetMetricEntry.paper_id == paper_id
                    )
                ).all()
            }
        for row in rows[:30]:
            dataset = str(row.get("dataset") or "").strip()
            metric = str(row.get("metric") or "").strip()
            value = str(row.get("value") or "").strip()
            model = str(row.get("model") or "").strip()
            if not dataset or not metric or not value:
                continue
            key = (
                canonical_dataset_name(dataset).lower(),
                metric.lower(),
                model.lower(),
                value,
            )
            if key in existing:
                continue
            existing.add(key)
            checks = [value, dataset] + ([model] if model else [])
            is_verified = all(
                _normalize_for_verification(fragment) in normalized_source
                for fragment in checks
            )
            note = "AI抽取·已核对原文" if is_verified else "AI抽取·原文未核对到,请人工确认"
            self.add_dataset_metric_entry(
                DatasetMetricUpdate(
                    dataset=dataset,
                    metric=metric,
                    value=value,
                    model=model,
                    task=str(row.get("task") or "").strip(),
                    unit=str(row.get("unit") or "").strip(),
                    direction="higher",
                    notes=note,
                    paper_id=paper_id,
                )
            )
            added += 1
            if is_verified:
                verified += 1
        return added, verified

    def add_reading_seconds(self, paper_id: int, seconds: int) -> None:
        """F22:按本地日期累计真实阅读秒数(同文献同日一行)。"""
        if seconds <= 0:
            return
        day = datetime.now().astimezone().strftime("%Y-%m-%d")
        with self._session_factory() as session:
            row = session.scalars(
                select(ReadingLog).where(
                    ReadingLog.paper_id == paper_id, ReadingLog.day == day
                )
            ).first()
            if row is None:
                row = ReadingLog(paper_id=paper_id, day=day, seconds=0)
                session.add(row)
            row.seconds += int(seconds)
            session.commit()

    def reading_time_summary(self) -> dict:
        """F22:今日/近7天/近30天/累计阅读时长(秒)+ 近14天逐日序列。

        day 为本地日期字符串,直接按字典序比较。
        """
        today = datetime.now().astimezone().date()

        def _cutoff(days: int) -> str:
            return (today - timedelta(days=days)).strftime("%Y-%m-%d")

        with self._session_factory() as session:
            rows = session.scalars(select(ReadingLog)).all()
        by_day: dict[str, int] = {}
        for row in rows:
            by_day[row.day] = by_day.get(row.day, 0) + int(row.seconds)

        def _sum(since: str) -> int:
            return sum(seconds for day, seconds in by_day.items() if day >= since)

        series = []
        for offset in range(13, -1, -1):
            day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            series.append((day[5:], by_day.get(day, 0)))
        return {
            "today": by_day.get(today.strftime("%Y-%m-%d"), 0),
            "week": _sum(_cutoff(6)),
            "month": _sum(_cutoff(29)),
            "total": sum(by_day.values()),
            "series": series,
        }

    def auto_classify_all(self) -> AutoClassifyResult:
        with self._session_factory() as session:
            created_categories = self._ensure_default_categories(session)
            categories_by_name = {
                category.name: category
                for category in session.scalars(select(Category)).all()
            }
            papers = list(
                session.scalars(
                    select(Paper).options(
                        joinedload(Paper.detail),
                        joinedload(Paper.text),
                        joinedload(Paper.categories),
                    )
                )
                .unique()
                .all()
            )

            updated = 0
            unchanged = 0
            for paper in papers:
                managed_names = {rule.name for rule in DEFAULT_CATEGORY_RULES}
                original = {category.id for category in paper.categories}
                # F19 单主分类:规则命中取得分最高者作为唯一主分类;
                # 未命中时保留手动指定的分类(没有则挂未分类)
                manual_categories = [
                    category
                    for category in paper.categories
                    if category.name not in managed_names
                    and category.name != UNCATEGORIZED_CATEGORY_NAME
                ]
                # B34:只匹配库中真实存在的分类(演示种子树已不再自动创建,
                # 关键词命中但库里没有同名分类时按未命中处理)
                matched_scores = {
                    name: score
                    for name, score in self._match_category_names(paper).items()
                    if name in categories_by_name
                }
                if matched_scores:
                    best_name = max(matched_scores, key=matched_scores.get)
                    best = categories_by_name.get(best_name)
                    paper.categories = [best] if best is not None else manual_categories
                else:
                    paper.categories = manual_categories
                if not paper.categories:
                    paper.categories.append(self._uncategorized_category(session))
                after = {category.id for category in paper.categories}
                if original != after:
                    paper.updated_at = utc_now()
                    updated += 1
                else:
                    unchanged += 1

            session.commit()
            return AutoClassifyResult(
                updated=updated,
                unchanged=unchanged,
                total=len(papers),
                created_categories=created_categories,
            )

    def update_paper(
        self,
        paper_id: int,
        update: PaperUpdate,
        category_id: int | None = None,
        category_ids: list[int] | None = None,
        tag_ids: list[int] | None = None,
    ) -> None:
        with self._session_factory() as session:
            paper = session.get(Paper, paper_id)
            if paper is None:
                raise ValueError(f"Paper {paper_id} does not exist")
            if paper.detail is None:
                paper.detail = PaperDetail(paper_id=paper_id)

            paper.title = update.title.strip() or "未命名文献"
            paper.authors = update.authors.strip()
            paper.year = update.year
            paper.venue = update.venue.strip()
            paper.doi = update.doi.strip()
            paper.arxiv_id = update.arxiv_id.strip()
            paper.url = update.url.strip()
            paper.abstract = update.abstract.strip()
            paper.pdf_path = (
                self.stored_pdf_path(update.pdf_path) if update.pdf_path.strip() else ""
            )
            paper.reading_status = update.reading_status
            paper.importance = update.importance
            paper.updated_at = utc_now()

            paper.detail.summary = update.summary.strip()
            paper.detail.datasets = update.datasets.strip()
            paper.detail.dataset_metrics = update.dataset_metrics.strip()
            paper.detail.methods = update.methods.strip()
            paper.detail.contributions = update.contributions.strip()
            paper.detail.limitations = update.limitations.strip()
            paper.detail.experiment_notes = update.experiment_notes.strip()
            paper.detail.reproducibility_notes = update.reproducibility_notes.strip()
            paper.detail.memory_anchor = update.memory_anchor.strip()
            paper.detail.personal_notes = update.personal_notes.strip()

            requested_ids = (
                category_ids
                if category_ids is not None
                else [category_id] if category_id is not None else []
            )
            assigned: list[Category] = []
            seen_category_ids: set[int] = set()
            for assigned_category_id in requested_ids:
                if assigned_category_id in seen_category_ids:
                    continue
                seen_category_ids.add(assigned_category_id)
                category = session.get(Category, assigned_category_id)
                if category is not None:
                    assigned.append(category)
            formal_categories = [
                category
                for category in assigned
                if category.name != UNCATEGORIZED_CATEGORY_NAME
            ]
            paper.categories = formal_categories or [self._uncategorized_category(session)]

            if tag_ids is not None:
                seen_tag_ids: set[int] = set()
                assigned_tags: list[Tag] = []
                for tag_id in tag_ids:
                    if tag_id in seen_tag_ids:
                        continue
                    seen_tag_ids.add(tag_id)
                    tag = session.get(Tag, tag_id)
                    if tag is not None:
                        assigned_tags.append(tag)
                paper.tags = assigned_tags

            session.commit()

    def get_ai_source(self, paper_id: int, max_chars: int) -> dict[str, str]:
        paper = self.get_paper(paper_id)
        detail = paper.detail
        text = paper.text.content if paper.text else ""
        resolved_pdf = self.resolve_pdf_path(paper.pdf_path) if paper.pdf_path else None
        if not text and resolved_pdf is not None and resolved_pdf.exists():
            text, _ = extract_pdf_text(resolved_pdf, max_chars=max_chars)
        known_datasets = self.list_known_dataset_names(exclude_paper_id=paper_id)
        return {
            "title": paper.title,
            "authors": paper.authors,
            "venue": paper.venue,
            "abstract": paper.abstract,
            "pdf_path": paper.pdf_path,
            "known_datasets": "; ".join(known_datasets[:80]),
            "memory_anchor": detail.memory_anchor if detail else "",
            "personal_notes": detail.personal_notes if detail else "",
            "text": text[:max_chars],
        }

    def apply_ai_analysis(self, paper_id: int, analysis: PaperAIAnalysis) -> None:
        with self._session_factory() as session:
            paper = session.get(Paper, paper_id)
            if paper is None:
                raise ValueError(f"Paper {paper_id} does not exist")
            if paper.detail is None:
                paper.detail = PaperDetail(paper_id=paper_id)

            if analysis.title:
                paper.title = analysis.title
            if analysis.authors:
                paper.authors = analysis.authors
            if analysis.year:
                paper.year = analysis.year
            if analysis.venue:
                paper.venue = analysis.venue
            if analysis.abstract:
                paper.abstract = analysis.abstract

            if analysis.summary:
                paper.detail.summary = analysis.summary
            if analysis.datasets:
                paper.detail.datasets = analysis.datasets
            if analysis.methods:
                paper.detail.methods = analysis.methods
            if analysis.contributions:
                paper.detail.contributions = analysis.contributions
            if analysis.limitations:
                paper.detail.limitations = analysis.limitations
            if analysis.experiment_notes:
                paper.detail.experiment_notes = analysis.experiment_notes
            if analysis.reproducibility_notes:
                paper.detail.reproducibility_notes = analysis.reproducibility_notes
            if analysis.memory_anchor:
                paper.detail.memory_anchor = analysis.memory_anchor
            if analysis.keywords:
                existing = paper.detail.personal_notes.strip()
                keyword_note = f"AI关键词：{analysis.keywords}"
                if keyword_note not in existing:
                    paper.detail.personal_notes = (
                        f"{existing}\n\n{keyword_note}".strip() if existing else keyword_note
                    )
                if not paper.detail.memory_anchor:
                    paper.detail.memory_anchor = analysis.keywords
            paper.detail.raw_ai_output = json_dumps(analysis.raw)
            paper.updated_at = utc_now()
            session.commit()

    def mark_opened(self, paper_id: int) -> None:
        with self._session_factory() as session:
            paper = session.get(Paper, paper_id)
            if paper is not None:
                paper.last_opened_at = utc_now()
                session.commit()

    def update_paper_pdf_path(self, paper_id: int, pdf_path: str) -> None:
        with self._session_factory() as session:
            paper = session.get(Paper, paper_id)
            if paper is None:
                raise ValueError(f"Paper {paper_id} does not exist")
            paper.pdf_path = self.stored_pdf_path(pdf_path) if pdf_path.strip() else ""
            paper.updated_at = utc_now()
            session.commit()

    def get_abstract_translation(self, paper_id: int) -> str:
        with self._session_factory() as session:
            detail = session.get(PaperDetail, paper_id)
            if detail is None or not detail.raw_ai_output.strip():
                return ""
            try:
                data = json.loads(detail.raw_ai_output)
            except json.JSONDecodeError:
                return ""
            if not isinstance(data, dict):
                return ""
            return str(data.get("_synthrecall_abstract_zh") or "").strip()

    def set_abstract_translation(self, paper_id: int, translation: str) -> None:
        with self._session_factory() as session:
            paper = session.get(Paper, paper_id)
            if paper is None:
                raise ValueError(f"Paper {paper_id} does not exist")
            if paper.detail is None:
                paper.detail = PaperDetail(paper_id=paper_id)
            data: dict = {}
            raw = paper.detail.raw_ai_output.strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                    data = parsed if isinstance(parsed, dict) else {"_legacy_raw_ai_output": raw}
                except json.JSONDecodeError:
                    data = {"_legacy_raw_ai_output": raw}
            data["_synthrecall_abstract_zh"] = translation.strip()
            paper.detail.raw_ai_output = json_dumps(data)
            paper.updated_at = utc_now()
            session.commit()

    def list_dataset_metrics(self) -> list[DatasetMetricRow]:
        with self._session_factory() as session:
            entries = list(
                session.scalars(
                    select(DatasetMetricEntry)
                    .options(joinedload(DatasetMetricEntry.paper))
                    .order_by(DatasetMetricEntry.dataset, DatasetMetricEntry.metric, DatasetMetricEntry.model)
                )
                .unique()
                .all()
            )
            rows: list[DatasetMetricRow] = []
            known_dataset_names = {entry.dataset for entry in entries if entry.dataset}
            known_datasets = sorted(known_dataset_names, key=lambda name: (-len(name), name.lower()))
            for entry in entries:
                dataset = canonical_dataset_name(entry.dataset, known_datasets)
                rows.append(
                    DatasetMetricRow(
                        entry_id=entry.id,
                        dataset=dataset,
                        metric=entry.metric,
                        value=entry.value,
                        numeric_value=entry.numeric_value,
                        unit=entry.unit,
                        direction=entry.direction or "higher",
                        model=entry.model,
                        task=entry.task,
                        notes=entry.notes,
                        paper_id=entry.paper_id,
                        paper_title=entry.paper.title if entry.paper else "",
                    )
                )
            return sorted(
                rows,
                key=lambda row: (
                    row.dataset.lower(),
                    row.metric.lower(),
                    -(row.numeric_value if row.numeric_value is not None else float("-inf")),
                    row.model.lower(),
                ),
            )

    def add_dataset_metric_entry(self, update: DatasetMetricUpdate) -> DatasetMetricEntry:
        with self._session_factory() as session:
            entry = DatasetMetricEntry(
                dataset=canonical_dataset_name(update.dataset.strip()),
                metric=update.metric.strip(),
                value=update.value.strip(),
                numeric_value=parse_metric_number(update.value),
                unit=update.unit.strip(),
                direction=(
                    update.direction if update.direction in {"higher", "lower"} else "higher"
                ),
                model=update.model.strip(),
                task=update.task.strip(),
                notes=update.notes.strip(),
                paper_id=update.paper_id,
            )
            session.add(entry)
            session.commit()
            session.refresh(entry)
            return entry

    def update_dataset_metric_entry(self, entry_id: int, update: DatasetMetricUpdate) -> None:
        with self._session_factory() as session:
            entry = session.get(DatasetMetricEntry, entry_id)
            if entry is None:
                raise ValueError(f"Dataset metric entry {entry_id} does not exist")
            entry.dataset = canonical_dataset_name(update.dataset.strip())
            entry.metric = update.metric.strip()
            entry.value = update.value.strip()
            entry.numeric_value = parse_metric_number(update.value)
            entry.unit = update.unit.strip()
            entry.direction = (
                update.direction if update.direction in {"higher", "lower"} else "higher"
            )
            entry.model = update.model.strip()
            entry.task = update.task.strip()
            entry.notes = update.notes.strip()
            entry.paper_id = update.paper_id
            entry.updated_at = utc_now()
            session.commit()

    def delete_dataset_metric_entry(self, entry_id: int) -> None:
        with self._session_factory() as session:
            entry = session.get(DatasetMetricEntry, entry_id)
            if entry is not None:
                session.delete(entry)
                session.commit()

    def list_known_dataset_names(self, exclude_paper_id: int | None = None) -> list[str]:
        with self._session_factory() as session:
            papers = list(
                session.scalars(select(Paper).options(joinedload(Paper.detail)))
                .unique()
                .all()
            )
            names: set[str] = set()
            for paper in papers:
                if exclude_paper_id is not None and paper.id == exclude_paper_id:
                    continue
                if paper.detail is None:
                    continue
                for parsed in parse_dataset_metrics_text(paper.detail.dataset_metrics):
                    if parsed.get("dataset"):
                        names.add(canonical_dataset_name(parsed["dataset"]))
                for raw_name in re.split(r"[;；,\n]", paper.detail.datasets or ""):
                    if raw_name.strip():
                        names.add(canonical_dataset_name(raw_name))
            entries = session.scalars(select(DatasetMetricEntry.dataset)).all()
            for dataset in entries:
                if dataset:
                    names.add(canonical_dataset_name(dataset))
            return sorted(names, key=str.lower)


    def _find_duplicate_paper(
        self,
        session,
        *,
        file_hash: str,
        pdf_paths: set[str],
        title: str,
        authors: str,
        doi: str = "",
        arxiv_id: str = "",
        exclude_paper_id: int | None = None,
    ) -> tuple[Paper, str] | None:
        papers = list(
            session.scalars(
                select(Paper).where(Paper.deleted_at.is_(None))
            ).all()
        )
        normalized_paths = {self._normalize_pdf_path(path) for path in pdf_paths if path}
        normalized_doi = self._normalize_doi(doi)
        normalized_arxiv_id = self._normalize_arxiv_id(arxiv_id)

        for paper in papers:
            if exclude_paper_id is not None and paper.id == exclude_paper_id:
                continue
            if file_hash and paper.file_hash == file_hash:
                return paper, "PDF 文件内容相同"
            if paper.pdf_path and self._normalize_pdf_path(paper.pdf_path) in normalized_paths:
                return paper, "PDF 路径相同"
            if normalized_doi and self._normalize_doi(paper.doi) == normalized_doi:
                return paper, f"DOI 相同（{doi}）"
            if (
                normalized_arxiv_id
                and self._normalize_arxiv_id(paper.arxiv_id) == normalized_arxiv_id
            ):
                return paper, f"arXiv ID 相同（{arxiv_id}）"

        title_key = self._normalize_identity_text(title)
        if not self._is_meaningful_title(title, title_key):
            return None
        for paper in papers:
            if exclude_paper_id is not None and paper.id == exclude_paper_id:
                continue
            existing_title_key = self._normalize_identity_text(paper.title)
            if not self._is_meaningful_title(paper.title, existing_title_key):
                continue

            exact_title = title_key == existing_title_key
            authors_match = self._authors_match(authors, paper.authors)
            both_have_authors = bool(
                self._normalize_identity_text(authors)
                and self._normalize_identity_text(paper.authors)
            )
            if exact_title and authors_match:
                return paper, "论文标题和作者相同"
            if exact_title and not both_have_authors and self._title_is_distinctive(title):
                return paper, "论文标题相同，且至少一份 PDF 缺少作者元数据"
            if (
                both_have_authors
                and authors_match
                and self._titles_are_highly_similar(title, paper.title)
            ):
                return paper, "论文标题和作者高度相似"
        return None

    def _normalize_identity_text(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value or "").casefold()
        return "".join(character for character in normalized if character.isalnum())

    def _normalize_words(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value or "").casefold()
        return " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))

    def _normalize_pdf_path(self, value: str) -> str:
        return str(self.resolve_pdf_path(value)).casefold()

    def _normalize_doi(self, value: str) -> str:
        normalized = (value or "").strip().casefold()
        normalized = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", normalized)
        return normalized.rstrip(".,;:)]}")

    def _normalize_arxiv_id(self, value: str) -> str:
        normalized = (value or "").strip().casefold()
        normalized = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv\s*:?\s*)", "", normalized)
        normalized = re.sub(r"\.pdf$", "", normalized)
        return re.sub(r"v\d+$", "", normalized)

    def _is_meaningful_title(self, title: str, title_key: str) -> bool:
        placeholders = {
            "document",
            "microsoftword",
            "paper",
            "untitled",
            "unknown",
            "未命名文献",
        }
        return bool(title_key) and title_key not in placeholders and len(title_key) >= 6

    def _title_is_distinctive(self, title: str) -> bool:
        title_key = self._normalize_identity_text(title)
        word_count = len(self._normalize_words(title).split())
        return len(title_key) >= 18 or word_count >= 4

    def _authors_match(self, left: str, right: str) -> bool:
        left_key = self._normalize_identity_text(left)
        right_key = self._normalize_identity_text(right)
        if not left_key or not right_key:
            return False
        if left_key == right_key:
            return True

        ignored = {"al", "and", "et", "the"}
        left_tokens = {
            token
            for token in self._normalize_words(left).split()
            if len(token) > 1 and token not in ignored
        }
        right_tokens = {
            token
            for token in self._normalize_words(right).split()
            if len(token) > 1 and token not in ignored
        }
        if not left_tokens or not right_tokens:
            return False
        shared = left_tokens & right_tokens
        coverage = len(shared) / min(len(left_tokens), len(right_tokens))
        token_similarity = fuzz.token_set_ratio(
            " ".join(sorted(left_tokens)),
            " ".join(sorted(right_tokens)),
        )
        return (bool(shared) and coverage >= 0.5) or token_similarity >= 80

    def _titles_are_highly_similar(self, left: str, right: str) -> bool:
        left_key = self._normalize_identity_text(left)
        right_key = self._normalize_identity_text(right)
        if min(len(left_key), len(right_key)) < 18:
            return False
        compact_similarity = fuzz.ratio(left_key, right_key)
        token_similarity = fuzz.token_set_ratio(
            self._normalize_words(left),
            self._normalize_words(right),
        )
        return compact_similarity >= 96 or token_similarity >= 96

    def _uncategorized_category(self, session) -> Category:
        category = session.scalar(
            select(Category).where(
                Category.name == UNCATEGORIZED_CATEGORY_NAME,
                Category.parent_id.is_(None),
            )
        )
        if category is None:
            category = Category(name=UNCATEGORIZED_CATEGORY_NAME, sort_order=0)
            session.add(category)
            session.flush()
        return category

    def _ensure_default_categories(self, session) -> int:
        """B34:默认分类只保证「未分类」存在,演示用的研究主题/阅读状态树不再
        种入(用户反馈:全新库不该被灌入一堆 0 篇的 CV 方向演示分类)。旧库已有
        的分类原样保留;用户删掉的分类也不会在下次启动时复活。"""
        names = {
            category.name for category in session.scalars(select(Category)).all()
        }
        if UNCATEGORIZED_CATEGORY_NAME in names:
            return 0
        self._uncategorized_category(session)
        return 1

    def _match_category_names(self, paper: Paper) -> dict[str, int]:
        """F19:返回 {分类名: 关键词得分}(>=2 视为命中);auto_classify 取最高分为唯一主分类。"""
        focus_corpus, weak_corpus = self._classification_corpus(paper)
        matched: dict[str, int] = {}
        for rule in DEFAULT_CATEGORY_RULES:
            score = 0
            for keyword in rule.keywords:
                normalized = keyword.lower()
                if not normalized:
                    continue
                if self._contains_keyword(focus_corpus, normalized):
                    score += 2
                elif self._contains_keyword(weak_corpus, normalized):
                    score += 1
            if score >= 2:
                matched[rule.name] = score
        return matched

    def _contains_keyword(self, corpus: str, keyword: str) -> bool:
        if re.fullmatch(r"[a-z0-9-]+", keyword):
            pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
            return re.search(pattern, corpus) is not None
        return keyword in corpus

    def _classification_corpus(self, paper: Paper) -> tuple[str, str]:
        detail = paper.detail
        weak_text = paper.text.content[:12_000] if paper.text else ""
        focus_parts = [
            paper.title,
            paper.authors,
            paper.venue,
            paper.abstract,
            paper.pdf_path,
            detail.summary if detail else "",
            detail.datasets if detail else "",
            detail.dataset_metrics if detail else "",
            detail.methods if detail else "",
            detail.contributions if detail else "",
            detail.memory_anchor if detail else "",
            detail.personal_notes if detail else "",
        ]
        focus = "\n".join(part for part in focus_parts if part).lower()
        return focus, weak_text.lower()

    def _clean_pdf_title(self, title: str, pdf_path: Path) -> str:
        cleaned = " ".join((title or "").split())
        if not cleaned or cleaned.lower() in {"untitled", "unknown"}:
            return pdf_path.stem
        if len(cleaned) < 5:
            return pdf_path.stem
        return cleaned

    def _filename_note(self, pdf_path: Path) -> str:
        stem = pdf_path.stem.strip()
        if not stem:
            return ""
        return f"文件名备注：{stem}"

    def _managed_pdf_path(
        self,
        session,
        source_pdf_path: Path,
        category_id: int | None,
        library_paper_dir: Path | None,
        file_hash: str,
    ) -> Path:
        if library_paper_dir is None:
            return source_pdf_path
        category_parts = self._category_path_parts(session, category_id) or ["未分类"]
        target_dir = library_paper_dir.joinpath(*category_parts)
        filename = self._safe_filename(source_pdf_path.stem) or file_hash[:12]
        suffix = source_pdf_path.suffix if source_pdf_path.suffix.lower() == ".pdf" else ".pdf"
        target_path = target_dir / f"{filename}{suffix}"
        try:
            source_pdf_path.relative_to(library_paper_dir.resolve())
            return source_pdf_path
        except ValueError:
            pass
        if not target_path.exists():
            return target_path
        hash_suffix = file_hash[:10]
        candidate = target_dir / f"{filename}-{hash_suffix}{suffix}"
        index = 2
        while candidate.exists():
            candidate = target_dir / f"{filename}-{hash_suffix}-{index}{suffix}"
            index += 1
        return candidate

    def _category_path_parts(self, session, category_id: int | None) -> list[str]:
        if category_id is None:
            return []
        parts: list[str] = []
        current = session.get(Category, category_id)
        while current is not None:
            parts.append(self._safe_filename(current.name) or f"category-{current.id}")
            current = session.get(Category, current.parent_id) if current.parent_id is not None else None
        return list(reversed(parts))

    def _safe_filename(self, value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return cleaned[:120]


def parse_dataset_metrics_text(text: str) -> list[dict[str, str]]:
    cleaned = text.strip()
    if not cleaned:
        return []

    rows: list[dict[str, str]] = []
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        data = data.get("dataset_metrics") or data.get("metrics") or data.get("rows")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            parsed = {
                normalize_metric_key(str(key)): str(value).strip()
                for key, value in item.items()
                if normalize_metric_key(str(key)) and str(value).strip()
            }
            if parsed.get("dataset") and parsed.get("metric") and parsed.get("value"):
                rows.append(parsed)
        return rows

    for raw_line in cleaned.splitlines():
        line = raw_line.strip().lstrip("-*•0123456789.、 ").strip()
        if not line:
            continue
        parsed: dict[str, str] = {}
        parts = [part.strip() for part in re.split(r"[;；|]", line) if part.strip()]
        for part in parts:
            match = re.match(r"([^:：=]+)[:：=](.+)", part)
            if not match:
                continue
            key = normalize_metric_key(match.group(1))
            value = match.group(2).strip()
            if key:
                parsed[key] = value
        if parsed.get("dataset") and parsed.get("metric") and parsed.get("value"):
            rows.append(parsed)
    return rows


def canonical_dataset_name(name: str, known_names: list[str] | None = None) -> str:
    cleaned = " ".join((name or "").strip().split())
    if not cleaned:
        return ""
    token = normalize_dataset_token(cleaned)
    for canonical, aliases in DATASET_ALIAS_GROUPS.items():
        if token in {normalize_dataset_token(alias) for alias in aliases | {canonical}}:
            return canonical
    if known_names:
        known_lookup: dict[str, str] = {}
        for known in sorted(known_names, key=lambda value: (-len(value), value.lower())):
            if known.strip():
                known_lookup.setdefault(normalize_dataset_token(known), known)
        if token in known_lookup:
            return known_lookup[token]
        for known_token, known in known_lookup.items():
            if token and known_token and (token in known_token or known_token in token):
                return known
    return cleaned


def normalize_dataset_token(name: str) -> str:
    normalized = name.lower()
    normalized = normalized.replace("dataset", "").replace("benchmark", "")
    return re.sub(r"[^a-z0-9]", "", normalized)


def is_primary_dataset_metric(row: dict[str, str]) -> bool:
    text = " ".join(
        [
            row.get("model", ""),
            row.get("task", ""),
            row.get("notes", ""),
        ]
    ).lower()
    excluded_markers = [
        "comparison",
        "baseline",
        "ablation",
        "contribution",
        "对比",
        "基线",
        "消融",
    ]
    return not any(marker in text for marker in excluded_markers)


def normalize_metric_key(key: str) -> str:
    normalized = key.strip().lower().replace(" ", "_")
    mapping = {
        "dataset": "dataset",
        "data": "dataset",
        "数据集": "dataset",
        "基准": "dataset",
        "benchmark": "dataset",
        "metric": "metric",
        "measure": "metric",
        "指标": "metric",
        "value": "value",
        "result": "value",
        "score": "value",
        "结果": "value",
        "数值": "value",
        "model": "model",
        "模型": "model",
        "method": "model",
        "方法": "model",
        "task": "task",
        "任务": "task",
        "notes": "notes",
        "note": "notes",
        "说明": "notes",
        "备注": "notes",
    }
    return mapping.get(normalized, "")


def parse_metric_number(value: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None
