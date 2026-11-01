from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """B6:全库统一 UTC(naive)存储;展示层用 to_local() 转本地。"""
    return datetime.now(UTC).replace(tzinfo=None)

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, query_expression, relationship


class Base(DeclarativeBase):
    pass


paper_categories = Table(
    "paper_categories",
    Base.metadata,
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("category_id", ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_paper_categories_category_paper", "category_id", "paper_id"),
)


paper_tags = Table(
    "paper_tags",
    Base.metadata,
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    authors: Mapped[str] = mapped_column(String(500), default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    venue: Mapped[str] = mapped_column(String(300), default="")
    doi: Mapped[str] = mapped_column(String(200), default="")
    arxiv_id: Mapped[str] = mapped_column(String(100), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    abstract: Mapped[str] = mapped_column(Text, default="")
    pdf_path: Mapped[str] = mapped_column(String(1000), default="")
    file_hash: Mapped[str] = mapped_column(String(128), default="")
    reading_status: Mapped[str] = mapped_column(String(50), default="待读")
    importance: Mapped[int] = mapped_column(Integer, default=3)
    # F21:收藏星标;布尔标记,不参与 updated_at 语义
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Populated only by search-list queries. This keeps large PaperText.content values
    # out of memory while still allowing the UI to show an exact "full text" match badge.
    full_text_match: Mapped[bool] = query_expression()

    detail: Mapped[PaperDetail] = relationship(
        back_populates="paper",
        cascade="all, delete-orphan",
        uselist=False,
    )
    text: Mapped[PaperText] = relationship(
        back_populates="paper",
        cascade="all, delete-orphan",
        uselist=False,
    )
    categories: Mapped[list[Category]] = relationship(
        secondary=paper_categories,
        back_populates="papers",
    )
    tags: Mapped[list[Tag]] = relationship(
        secondary=paper_tags,
        back_populates="papers",
    )
    dataset_metric_entries: Mapped[list[DatasetMetricEntry]] = relationship(
        back_populates="paper",
        # 删除文献时保留排行榜条目(DB 侧 ondelete=SET NULL),paper_id 置空,
        # 指标历史不随文献删除而丢失。
        passive_deletes=True,
    )


class SavedSearch(Base):
    """F20 智能文件夹:命名的过滤规则(关键词/标签/主分类/状态/年份/重要度)。"""

    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    rule_json: Mapped[str] = mapped_column(Text, default="{}")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("name", "parent_id", name="uq_category_name_parent"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    preset_keywords: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    parent: Mapped[Category | None] = relationship(
        remote_side=[id],
        back_populates="children",
    )
    children: Mapped[list[Category]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )
    papers: Mapped[list[Paper]] = relationship(
        secondary=paper_categories,
        back_populates="categories",
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    color: Mapped[str] = mapped_column(String(20), default="#6b7280")

    papers: Mapped[list[Paper]] = relationship(
        secondary=paper_tags,
        back_populates="tags",
    )


class PaperDetail(Base):
    __tablename__ = "paper_details"

    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    datasets: Mapped[str] = mapped_column(Text, default="")
    dataset_metrics: Mapped[str] = mapped_column(Text, default="")
    methods: Mapped[str] = mapped_column(Text, default="")
    contributions: Mapped[str] = mapped_column(Text, default="")
    limitations: Mapped[str] = mapped_column(Text, default="")
    experiment_notes: Mapped[str] = mapped_column(Text, default="")
    reproducibility_notes: Mapped[str] = mapped_column(Text, default="")
    memory_anchor: Mapped[str] = mapped_column(Text, default="")
    personal_notes: Mapped[str] = mapped_column(Text, default="")
    raw_ai_output: Mapped[str] = mapped_column(Text, default="")

    paper: Mapped[Paper] = relationship(back_populates="detail")


class PaperText(Base):
    __tablename__ = "paper_texts"

    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    paper: Mapped[Paper] = relationship(back_populates="text")


class DatasetMetricEntry(Base):
    __tablename__ = "dataset_metric_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(String(200), default="")
    metric: Mapped[str] = mapped_column(String(120), default="")
    value: Mapped[str] = mapped_column(String(120), default="")
    numeric_value: Mapped[float | None] = mapped_column(nullable=True)
    unit: Mapped[str] = mapped_column(String(40), default="")
    direction: Mapped[str] = mapped_column(String(20), default="higher")
    model: Mapped[str] = mapped_column(String(240), default="")
    task: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    paper_id: Mapped[int | None] = mapped_column(ForeignKey("papers.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    paper: Mapped[Paper | None] = relationship(back_populates="dataset_metric_entries")


class TokenUsageRecord(Base):
    """F20:AI 调用 token 用量流水。

    自研层(LLMClient)逐次请求记录一条;BabelDOC 翻译按次记录一条
    (source="babeldoc",从子进程日志解析,缺失时按字符估算)。
    只统计经过本应用的调用;行数=调用次数,单行极小,无需清理策略。
    """

    __tablename__ = "token_usages"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    feature: Mapped[str] = mapped_column(String(50), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    paper_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="app")
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)


class ReadingLog(Base):
    """F22:真实阅读时长流水——每文献每日一行,秒数累计。"""

    __tablename__ = "reading_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[str] = mapped_column(String(10), index=True)
    seconds: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("paper_id", "day"),)
