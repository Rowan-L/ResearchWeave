"""F5 数据备份与恢复:把 data 目录全部用户数据打包为 ZIP,并可从备份恢复。

备份内容:
- synthrecall_demo.db(先经 sqlite3 backup API 做 WAL 合并,保证完整)
- llm_settings.json / ui_state.ini 等顶层设置文件
- papers/(全部受管 PDF)
- translations/(译文 PDF——花 LLM token 生成,必须随库走)
- pdf_backups/、exports/(原件安全副本与用户导出)
- manifest.json(时间、文件数、应用版本)

排除:data/babeldoc_home(排版模型/字体缓存,可重新下载)、data/backups
(备份自身,避免递归)、WAL/SHM 临时态。
恢复:先 checkpoint 当前库并对当前状态做安全备份,再解压覆盖;恢复后需重启软件。
"""
from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from synthrecall.branding import APP_NAME

MANIFEST_NAME = "manifest.json"
DATABASE_SUFFIX = ".db"
# 允许进入备份/恢复的用户数据子目录(全量;babeldoc_home=缓存、backups=备份
# 自身,刻意排除)
_SAFE_TOP_LEVELS = {"papers", "translations", "pdf_backups", "exports"}


def _checkpoint_sqlite(database_path: Path) -> None:
    """WAL 合并:确保 .db 主文件包含全部已提交写入(B22 教训)。"""
    if not database_path.exists():
        return
    connection = sqlite3.connect(str(database_path))
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def _collect_files(data_dir: Path) -> list[Path]:
    files: list[Path] = []
    for name in os.listdir(data_dir):
        path = data_dir / name
        if path.is_file() and path.suffix in {DATABASE_SUFFIX, "-wal", "-shm", ".json", ".ini"}:
            if path.suffix in {"-wal", "-shm"} or name.endswith(("-wal", "-shm")):
                continue  # WAL/SHM 是临时态,checkpoint 后不需要
            files.append(path)
        elif path.is_dir() and name in _SAFE_TOP_LEVELS:
            files.extend(path.rglob("*"))
    return [path for path in files if path.is_file()]


def create_backup(data_dir: Path, backup_path: Path, *, app_version: str = "") -> Path:
    """创建整库备份 ZIP;返回实际写出的路径。"""
    data_dir = data_dir.resolve()
    if not data_dir.exists():
        raise FileNotFoundError(f"数据目录不存在:{data_dir}")
    database_path = next(
        (path for path in data_dir.glob(f"*{DATABASE_SUFFIX}")), None
    )
    if database_path is None:
        raise FileNotFoundError(f"数据目录下没有数据库文件:{data_dir}")

    _checkpoint_sqlite(database_path)
    backup_path = backup_path.resolve()
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    files = _collect_files(data_dir)
    entries = []
    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            arcname = path.relative_to(data_dir).as_posix()
            archive.write(path, arcname)
            entries.append({"path": arcname, "size": path.stat().st_size})
        manifest = {
            "app": APP_NAME,
            "version": app_version,
            "created_at": datetime.now(UTC)
            .astimezone()
            .isoformat(),
            "file_count": len(entries),
            "files": entries,
        }
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
    return backup_path


@dataclass(frozen=True)
class RestoreReport:
    restored_files: int
    safety_backup: Path | None


def read_manifest(backup_path: Path) -> dict:
    with zipfile.ZipFile(backup_path) as archive:
        if MANIFEST_NAME not in archive.namelist():
            raise ValueError("该文件不是有效的软件备份(缺少 manifest.json)。")
        return json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))


def restore_backup(
    backup_path: Path,
    data_dir: Path,
    *,
    session_factory=None,
) -> RestoreReport:
    """从备份 ZIP 恢复到 data 目录。

    恢复前:1) checkpoint 当前库;2) 把当前状态备份为 data/backups/pre-restore-*.zip
    (session_factory 可选传入,用于先释放/合并 WAL)。
    恢复的顶层条目仅允许数据库、设置文件与用户数据目录(papers/translations/
    pdf_backups/exports),防路径穿越。
    """
    backup_path = backup_path.resolve()
    data_dir = data_dir.resolve()
    if not backup_path.exists():
        raise FileNotFoundError(f"备份文件不存在:{backup_path}")
    read_manifest(backup_path)  # 结构校验,不合法直接抛错

    database_path = next((path for path in data_dir.glob(f"*{DATABASE_SUFFIX}")), None)
    if database_path is not None:
        _checkpoint_sqlite(database_path)

    safety_backup: Path | None = None
    backups_dir = data_dir / "backups"
    try:
        safety_backup = create_backup(
            data_dir,
            backups_dir / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip",
        )
    except (OSError, FileNotFoundError):
        safety_backup = None

    restored = 0
    data_root = data_dir.as_posix().casefold().rstrip("/") + "/"
    with zipfile.ZipFile(backup_path) as archive:
        for member in archive.infolist():
            if member.is_dir() or member.filename == MANIFEST_NAME:
                continue
            target = (data_dir / member.filename).resolve()
            target_str = target.as_posix().casefold()
            if not target_str.startswith(data_root):
                raise ValueError(f"备份内含非法路径条目:{member.filename}")
            top = target.relative_to(data_dir).parts[0]
            if top not in _SAFE_TOP_LEVELS and target.suffix not in {
                DATABASE_SUFFIX,
                ".json",
                ".ini",
            }:
                continue  # 只恢复数据库/设置/用户数据目录,忽略其他顶层条目
            target.parent.mkdir(parents=True, exist_ok=True)
            archive.extract(member, data_dir)
            restored += 1
    return RestoreReport(restored_files=restored, safety_backup=safety_backup)
