from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from synthrecall.models import Base, PaperText

SessionFactory = sessionmaker[Session]


FTS_TABLE = "paper_texts_fts"


def _enable_sqlite_foreign_keys(
    dbapi_connection, _connection_record
) -> None:
    # SQLite 默认关闭外键约束;开启后删除文献时 dataset_metric_entries 的
    # paper_id 才会按 ondelete=SET NULL 置空(B4 语义),purge 级联同理
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_session_factory(database_path: Path) -> SessionFactory:
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    event.listens_for(engine, "connect")(_enable_sqlite_foreign_keys)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_database(session_factory: SessionFactory) -> None:
    engine = session_factory.kw["bind"]
    # B10:先探测这是"既有旧库"(需要数据迁移)还是"全新库"(直接建表)
    with engine.connect() as connection:
        legacy_database = _is_legacy_database(connection)
    Base.metadata.create_all(engine)
    migrate_database(engine, legacy_database=legacy_database)


def _is_legacy_database(connection) -> bool:
    """是否存在 papers 表且尚未装过迁移框架 → 需要执行数据迁移的旧库。"""
    tables = {
        row[0]
        for row in connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    return "papers" in tables and "schema_migrations" not in tables


def migrate_database(engine, *, legacy_database: bool = False) -> None:
    """B10 迁移框架:按序应用 MIGRATIONS 中未应用的迁移并登记。

    schema_migrations(version PK, name, applied_at) 是唯一事实来源;
    PRAGMA user_version 同步为最大已应用版本,兼容 B6 时代的旧判断。
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
        )
        applied = {
            int(row[0])
            for row in connection.execute(
                text("SELECT version FROM schema_migrations")
            ).fetchall()
        }
        user_version = int(connection.execute(text("PRAGMA user_version")).scalar_one())
        if user_version >= 1:
            applied.add(TIMESTAMP_UTC_VERSION)  # B6 时代旧标记兼容

        for version, name, migrate in MIGRATIONS:
            if version in applied:
                continue
            migrate(connection, legacy_database=legacy_database)
            connection.execute(
                text(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (:version, :name, :applied_at)"
                ),
                {
                    "version": version,
                    "name": name,
                    "applied_at": datetime.now(UTC).isoformat(),
                },
            )
            applied.add(version)

        if applied:
            connection.execute(text(f"PRAGMA user_version = {max(applied)}"))
        _ensure_full_text_index(connection)


def _migration_01_add_missing_columns(connection, *, legacy_database: bool) -> None:
    columns = {
        row[1]
        for row in connection.execute(text("PRAGMA table_info(paper_details)")).fetchall()
    }
    paper_columns = {
        row[1]
        for row in connection.execute(text("PRAGMA table_info(papers)")).fetchall()
    }
    if "deleted_at" not in paper_columns:
        connection.execute(
            text("ALTER TABLE papers ADD COLUMN deleted_at DATETIME NULL")
        )
    if "memory_anchor" not in columns:
        connection.execute(
            text("ALTER TABLE paper_details ADD COLUMN memory_anchor TEXT DEFAULT ''")
        )
    if "dataset_metrics" not in columns:
        connection.execute(
            text("ALTER TABLE paper_details ADD COLUMN dataset_metrics TEXT DEFAULT ''")
        )
    metric_columns = {
        row[1]
        for row in connection.execute(
            text("PRAGMA table_info(dataset_metric_entries)")
        ).fetchall()
    }
    if "unit" not in metric_columns:
        connection.execute(
            text("ALTER TABLE dataset_metric_entries ADD COLUMN unit VARCHAR(40) DEFAULT ''")
        )
    if "direction" not in metric_columns:
        connection.execute(
            text(
                "ALTER TABLE dataset_metric_entries "
                "ADD COLUMN direction VARCHAR(20) DEFAULT 'higher'"
            )
        )


def _migration_02_secondary_indexes(connection, *, legacy_database: bool) -> None:
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_papers_updated_at "
            "ON papers(updated_at DESC, created_at DESC)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_papers_last_opened_at "
            "ON papers(last_opened_at DESC, updated_at DESC)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_papers_importance "
            "ON papers(importance DESC, updated_at DESC)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_paper_categories_category_paper "
            "ON paper_categories(category_id, paper_id)"
        )
    )


def _migration_03_timestamps_to_utc(connection, *, legacy_database: bool) -> None:
    """B6:把旧库按本地时间写入的 naive 时间列整体平移为 UTC(naive)。

    仅对"迁移框架启用前就存在数据"的旧库执行;全新库直接登记为已应用。
    """
    if not legacy_database:
        return
    offset_seconds = int(datetime.now(UTC).astimezone().utcoffset().total_seconds())
    targets = {
        "papers": ["created_at", "updated_at", "last_opened_at", "deleted_at"],
        "paper_texts": ["updated_at"],
        "dataset_metric_entries": ["created_at", "updated_at"],
    }
    for table, columns in targets.items():
        existing = {
            row[1]
            for row in connection.execute(text(f"PRAGMA table_info({table})")).fetchall()
        }
        for column in columns:
            if column not in existing or offset_seconds == 0:
                continue
            connection.execute(
                text(
                    f"UPDATE {table} SET {column} = "
                    f"strftime('%Y-%m-%d %H:%M:%f', {column}, '{-offset_seconds} seconds') "
                    f"WHERE {column} IS NOT NULL"
                )
            )
    connection.execute(text("PRAGMA user_version = 1"))


def _migration_04_saved_searches(connection, *, legacy_database: bool) -> None:
    """F20 智能文件夹:保存的搜索规则表。"""
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS saved_searches ("
            " id INTEGER PRIMARY KEY,"
            " name VARCHAR(120) NOT NULL,"
            " rule_json TEXT NOT NULL DEFAULT '{}',"
            " sort_order INTEGER DEFAULT 0,"
            " created_at DATETIME"
            ")"
        )
    )


# B10:新增 schema 变更时在列表末尾追加 (version, name, fn) 即可,勿修改历史项。
TIMESTAMP_UTC_VERSION = 3


def _migration_05_add_papers_is_favorite(connection, *, legacy_database: bool) -> None:
    """F21 收藏星标:papers 增加 is_favorite 布尔列。"""
    paper_columns = {
        row[1]
        for row in connection.execute(text("PRAGMA table_info(papers)")).fetchall()
    }
    if "is_favorite" not in paper_columns:
        connection.execute(
            text("ALTER TABLE papers ADD COLUMN is_favorite BOOLEAN NOT NULL DEFAULT 0")
        )


def _migration_06_create_reading_log(connection, *, legacy_database: bool) -> None:
    """F22 阅读时长:每文献每日一行的秒数累计表。"""
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS reading_log ("
            " id INTEGER PRIMARY KEY,"
            " paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,"
            " day VARCHAR(10) NOT NULL,"
            " seconds INTEGER NOT NULL DEFAULT 0,"
            " UNIQUE(paper_id, day))"
        )
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_reading_log_day ON reading_log(day)")
    )


MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "add-missing-columns", _migration_01_add_missing_columns),
    (2, "create-secondary-indexes", _migration_02_secondary_indexes),
    (3, "timestamps-to-utc", _migration_03_timestamps_to_utc),
    (4, "create-saved-searches", _migration_04_saved_searches),
    (5, "add-papers-is-favorite", _migration_05_add_papers_is_favorite),
    (6, "create-reading-log", _migration_06_create_reading_log),
]


def _ensure_full_text_index(connection) -> None:
    """创建 FTS5 trigram 全文索引;rowid 即 paper_id。

    trigram 分词器支持中英文子串匹配(等价原 ILIKE 语义),由 ORM 事件自动同步;
    若索引行数与正文表不一致(外部改动/旧库),启动时全量重建自愈。
    """
    connection.execute(
        text(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} "
            "USING fts5(content, tokenize='trigram')"
        )
    )
    indexed = connection.execute(text(f"SELECT COUNT(*) FROM {FTS_TABLE}")).scalar_one()
    total = connection.execute(text("SELECT COUNT(*) FROM paper_texts")).scalar_one()
    if indexed != total:
        connection.execute(text(f"DELETE FROM {FTS_TABLE}"))
        connection.execute(
            text(
                f"INSERT INTO {FTS_TABLE}(rowid, content) "
                "SELECT paper_id, content FROM paper_texts"
            )
        )


def _fts_delete(connection, paper_id: int) -> None:
    connection.execute(
        text(f"DELETE FROM {FTS_TABLE} WHERE rowid = :paper_id"),
        {"paper_id": paper_id},
    )


@event.listens_for(PaperText, "after_insert")
def _sync_fts_after_insert(mapper, connection, target) -> None:
    _fts_delete(connection, target.paper_id)
    connection.execute(
        text(f"INSERT INTO {FTS_TABLE}(rowid, content) VALUES (:paper_id, :content)"),
        {"paper_id": target.paper_id, "content": target.content or ""},
    )


@event.listens_for(PaperText, "after_update")
def _sync_fts_after_update(mapper, connection, target) -> None:
    _fts_delete(connection, target.paper_id)
    connection.execute(
        text(f"INSERT INTO {FTS_TABLE}(rowid, content) VALUES (:paper_id, :content)"),
        {"paper_id": target.paper_id, "content": target.content or ""},
    )


@event.listens_for(PaperText, "after_delete")
def _sync_fts_after_delete(mapper, connection, target) -> None:
    _fts_delete(connection, target.paper_id)
