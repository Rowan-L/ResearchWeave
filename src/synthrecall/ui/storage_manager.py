"""F20 存储管理:分类统计 data 目录占用,提供安全的垃圾清理。

安全边界——只清「可再生成的派生物」:
- 翻译中间产物 translations/<id>/_working(BabelDOC 临时文件);
- 孤儿译文目录(translations 下非数字命名、无文献引用的目录);
- 链接导入临时目录 .url-imports。
数据库 / 原文 papers / BabelDOC 模型缓存只展示,不提供删除入口。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from synthrecall.ui.confirmation import confirm_action

# 分类顺序即展示顺序;data_dir 顶层条目按名字归类,未识别的进「其他」
_DB_SUFFIXES = {".db", ".db-wal", ".db-shm"}


@dataclass
class StorageCategory:
    key: str
    label: str
    note: str
    size: int = 0
    deletable: bool = False
    paths: list[Path] = field(default_factory=list)


def dir_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _classify_child(name: str) -> str | None:
    if name == "papers":
        return "papers"
    if name == "translations":
        return "translations"
    if name == "babeldoc_home":
        return "babeldoc"
    if name == "backups":
        return "backups"
    if name == ".url-imports":
        return "url_imports"
    if Path(name).suffix in _DB_SUFFIXES:
        return "db"
    return "other"


def scan_storage(data_dir: Path) -> list[StorageCategory]:
    """扫描 data 目录,把顶层条目归入各类并累计体积。"""
    categories = {
        "papers": StorageCategory("papers", "原文 PDF", "受管文献原件"),
        "translation_pdfs": StorageCategory(
            "translation_pdfs", "译文 PDF", "翻译成品(可再生成)"
        ),
        "working": StorageCategory(
            "working", "翻译中间产物", "_working 临时文件,翻译完成后无用", deletable=True
        ),
        "babeldoc": StorageCategory(
            "babeldoc", "BabelDOC 缓存", "排版模型/字体,全库共享,不建议删除"
        ),
        "db": StorageCategory("db", "数据库", "文献库本体,勿动"),
        "backups": StorageCategory("backups", "整库备份", "手动备份的 zip"),
        "url_imports": StorageCategory(
            "url_imports", "链接导入临时", "URL 导入残留", deletable=True
        ),
        "other": StorageCategory("other", "其他", "工作区/日志等其余文件"),
    }
    by_key = {
        "papers": "papers",
        "babeldoc_home": "babeldoc",
        "backups": "backups",
        ".url-imports": "url_imports",
    }
    translations_root = data_dir / "translations"
    for child in data_dir.iterdir():
        if not child.exists():
            continue
        key = by_key.get(child.name) or _classify_child(child.name)
        if child.name == "translations":
            # 译文目录拆两类:_working 中间产物 vs 译文成品;
            # 非数字命名的孤儿目录单独归为「孤儿译文目录」
            if child.is_dir():
                for paper_dir in child.iterdir():
                    if not paper_dir.is_dir() or not paper_dir.name.isdigit():
                        continue
                    working = paper_dir / "_working"
                    if working.exists():
                        categories["working"].size += dir_size(working)
                        categories["working"].paths.append(working)
                    for item in paper_dir.iterdir():
                        if item.name == "_working":
                            continue
                        size = dir_size(item)
                        categories["translation_pdfs"].size += size
                        if item.is_dir():
                            categories["translation_pdfs"].paths.append(item)
        else:
            categories[key].size += dir_size(child)
            if key == "url_imports" and child.is_dir():
                categories[key].paths.append(child)
    # 孤儿译文目录:translations 下非数字命名的目录(整目录可删)
    orphan = StorageCategory(
        "orphan_translations",
        "孤儿译文目录",
        "对应文献已不存在的译文目录",
        deletable=True,
    )
    if translations_root.exists():
        for paper_dir in translations_root.iterdir():
            if paper_dir.is_dir() and not paper_dir.name.isdigit():
                orphan.size += dir_size(paper_dir)
                orphan.paths.append(paper_dir)
    ordered = [
        categories["papers"],
        categories["translation_pdfs"],
        categories["working"],
        orphan,
        categories["babeldoc"],
        categories["db"],
        categories["backups"],
        categories["url_imports"],
        categories["other"],
    ]
    return ordered


def find_working_dirs(data_dir: Path) -> list[Path]:
    root = data_dir / "translations"
    if not root.exists():
        return []
    return [
        path
        for paper_dir in root.iterdir()
        if paper_dir.is_dir()
        for path in [paper_dir / "_working"]
        if path.exists()
    ]


def find_orphan_translation_dirs(data_dir: Path) -> list[Path]:
    root = data_dir / "translations"
    if not root.exists():
        return []
    return [
        path
        for path in root.iterdir()
        if path.is_dir() and not path.name.isdigit()
    ]


def find_url_import_temp(data_dir: Path) -> list[Path]:
    root = data_dir / ".url-imports"
    if not root.exists():
        return []
    return list(root.iterdir())


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class StorageManagerDialog(QDialog):
    def __init__(self, data_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.data_dir = data_dir
        self.setWindowTitle("存储管理")
        self.resize(720, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        intro = QLabel(
            f"数据目录:{data_dir}\n"
            "原文与数据库不会被清理;可清理项只包含可再生成或已失效的文件。"
        )
        intro.setObjectName("MutedText")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["类别", "占用", "说明"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        self.total_label = QLabel("")
        self.total_label.setObjectName("MutedText")
        layout.addWidget(self.total_label)

        buttons = QHBoxLayout()
        self.clean_working_button = QPushButton("清理翻译中间产物")
        self.clean_working_button.clicked.connect(self.clean_working_dirs)
        self.clean_orphan_button = QPushButton("清理孤儿译文目录")
        self.clean_orphan_button.clicked.connect(self.clean_orphan_translations)
        self.clean_url_import_button = QPushButton("清理链接导入临时")
        self.clean_url_import_button.clicked.connect(self.clean_url_imports)
        open_dir_button = QPushButton("打开数据目录")
        open_dir_button.clicked.connect(self.open_data_dir)
        rescan_button = QPushButton("重新扫描")
        rescan_button.setObjectName("SoftButton")
        rescan_button.clicked.connect(self.rescan)
        close_button = QPushButton("关闭")
        close_button.setObjectName("SoftButton")
        close_button.clicked.connect(self.accept)
        for widget in (
            self.clean_working_button,
            self.clean_orphan_button,
            self.clean_url_import_button,
        ):
            buttons.addWidget(widget)
        buttons.addStretch(1)
        for widget in (open_dir_button, rescan_button, close_button):
            buttons.addWidget(widget)
        layout.addLayout(buttons)

        self.rescan()

    def rescan(self) -> None:
        self._categories = scan_storage(self.data_dir)
        total = sum(category.size for category in self._categories)
        self.table.setRowCount(len(self._categories))
        for row, category in enumerate(self._categories):
            label_item = QTableWidgetItem(category.label)
            label_item.setData(Qt.ItemDataRole.UserRole, category.key)
            values = (
                label_item,
                QTableWidgetItem(format_size(category.size)),
                QTableWidgetItem(category.note),
            )
            for column, item in enumerate(values):
                self.table.setItem(row, column, item)
        self.total_label.setText(f"总占用:{format_size(total)}")
        self.clean_working_button.setEnabled(bool(find_working_dirs(self.data_dir)))
        self.clean_orphan_button.setEnabled(
            bool(find_orphan_translation_dirs(self.data_dir))
        )
        self.clean_url_import_button.setEnabled(bool(find_url_import_temp(self.data_dir)))

    # ------------------------------------------------------------------
    # 清理动作:每次先确认,成功后重扫
    # ------------------------------------------------------------------

    def _confirm_cleanup(
        self, title: str, paths: list[Path], extra_detail: str = ""
    ) -> bool:
        total = sum(dir_size(path) for path in paths)
        detail = "\n".join(str(path) for path in paths[:12])
        if len(paths) > 12:
            detail += f"\n……共 {len(paths)} 项"
        if extra_detail:
            detail = f"{detail}\n{extra_detail}" if detail else extra_detail
        return confirm_action(
            self,
            title=title,
            message=f"将删除 {len(paths)} 项,共 {format_size(total)}。此操作不可撤销。",
            detail=detail,
            confirm_label="删除",
            destructive=True,
        )

    def clean_working_dirs(self) -> None:
        paths = find_working_dirs(self.data_dir)
        if not paths:
            return
        if not self._confirm_cleanup("清理翻译中间产物?", paths):
            return
        for path in paths:
            shutil.rmtree(path, ignore_errors=True)
        self.rescan()

    def clean_orphan_translations(self) -> None:
        paths = find_orphan_translation_dirs(self.data_dir)
        if not paths:
            return
        if not self._confirm_cleanup(
            "清理孤儿译文目录?",
            paths,
            extra_detail="这些目录不对应任何文献(目录名不是文献编号)。",
        ):
            return
        for path in paths:
            shutil.rmtree(path, ignore_errors=True)
        self.rescan()

    def clean_url_imports(self) -> None:
        paths = find_url_import_temp(self.data_dir)
        if not paths:
            return
        if not self._confirm_cleanup("清理链接导入临时文件?", paths):
            return
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        self.rescan()

    def open_data_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir)))
