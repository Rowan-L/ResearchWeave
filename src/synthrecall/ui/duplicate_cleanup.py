"""F12 重复文献清理向导:扫描库内疑似重复文献,选择保留的文献并合并。

判定依据由仓库层 find_duplicate_groups 提供(与导入查重同语义);
向导负责展示分组、指定保留的文献、合并缺失信息并把其余重复文献移入回收站(可恢复)。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from synthrecall.models import Paper
from synthrecall.repositories import DuplicateGroup, LibraryRepository
from synthrecall.ui.confirmation import confirm_action
from synthrecall.ui.task_center import to_local

COLUMNS = ("保留", "标题", "作者", "年份", "来源", "DOI / arXiv", "PDF", "收录时间")


def _format_date(value) -> str:
    if value is None:
        return "—"
    return to_local(value).strftime("%Y-%m-%d")


class DuplicateCleanupDialog(QDialog):
    """库内重复文献扫描与合并向导。合并动作完成后发出 duplicatesChanged。"""

    duplicatesChanged = Signal()

    def __init__(self, repository: LibraryRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.repository = repository
        self.setWindowTitle("重复文献清理向导")
        self.resize(1000, 620)

        self._groups: list[DuplicateGroup] = []
        self._papers_by_id: dict[int, Paper] = {}
        self._keeper_radios: dict[int, QRadioButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        intro = QLabel(
            "扫描库内疑似重复的文献(文件内容 / DOI / arXiv ID / 标题作者等依据)。"
            "每组选一篇保留,其余重复文献缺失的信息会并入保留的文献,然后移入回收站(可恢复)。"
        )
        intro.setObjectName("MutedText")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("DuplicateCleanupSplitter")

        self.group_list = QListWidget()
        self.group_list.setObjectName("DuplicateGroupList")
        self.group_list.currentRowChanged.connect(self._load_group)
        self.splitter.addWidget(self.group_list)
        # 组名可能较长,右侧留出内边距避免贴边(QSS 一旦写 padding 需四边齐全,
        # 否则左右内边距全部归零)
        self.group_list.setStyleSheet(
            "QListWidget#DuplicateGroupList::item { padding: 6px 10px 6px 8px; }"
        )

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(10)
        self.group_reason_label = QLabel("")
        self.group_reason_label.setObjectName("MutedText")
        self.group_reason_label.setWordWrap(True)
        detail_layout.addWidget(self.group_reason_label)

        self.paper_table = QTableWidget(0, len(COLUMNS))
        self.paper_table.setHorizontalHeaderLabels(COLUMNS)
        self.paper_table.verticalHeader().setVisible(False)
        self.paper_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.paper_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.paper_table.setAlternatingRowColors(True)
        header = self.paper_table.horizontalHeader()
        # 文本列表头左对齐与内容一致;窄列(保留/PDF)保持居中
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for center_column in (0, 6):
            header_item = QTableWidgetItem(COLUMNS[center_column])
            header_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.paper_table.setHorizontalHeaderItem(center_column, header_item)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        for column, width in ((0, 44), (2, 130), (3, 48), (4, 110), (5, 130), (6, 40), (7, 88)):
            self.paper_table.setColumnWidth(column, width)
        detail_layout.addWidget(self.paper_table, 1)

        self.merge_checkbox = QCheckBox("把重复文献中缺失的信息并入保留的文献(元数据/摘要/分类/标签/PDF)")
        self.merge_checkbox.setChecked(True)
        detail_layout.addWidget(self.merge_checkbox)
        self.splitter.addWidget(detail_panel)
        self.splitter.setSizes([210, 770])
        root.addWidget(self.splitter, 1)

        self.empty_state = QFrame()
        self.empty_state.setObjectName("PaperListEmptyState")
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title = QLabel("未发现重复文献")
        empty_title.setObjectName("EmptyStateTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_hint = QLabel("库内没有按文件内容、DOI、arXiv ID\n或标题作者判定的疑似重复文献。")
        empty_hint.setObjectName("EmptyStateHint")
        empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_hint.setWordWrap(True)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_hint)
        root.addWidget(self.empty_state, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setObjectName("MutedText")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        self.merge_button = QPushButton("合并并移入回收站")
        self.merge_button.setObjectName("PrimaryButton")
        self.merge_button.setToolTip("把其余重复文献并入保留的文献,然后移入回收站(可随时恢复)。")
        self.merge_button.clicked.connect(self.merge_current_group)
        footer.addWidget(self.merge_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        footer.addWidget(close_button)
        root.addLayout(footer)

        self.rescan()

    def rescan(self) -> None:
        """重新扫描重复分组并重建左侧列表;无重复时展示内嵌空态。"""
        self._groups = self.repository.find_duplicate_groups()
        self._papers_by_id = {
            paper.id: paper for paper in self.repository.list_papers(sort_by="created")
        }
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for index, group in enumerate(self._groups, start=1):
            self.group_list.addItem(f"组 {index} · {len(group.paper_ids)} 篇 · {group.reason}")
        self.group_list.blockSignals(False)

        has_groups = bool(self._groups)
        self.splitter.setVisible(has_groups)
        self.empty_state.setVisible(not has_groups)
        self.merge_button.setEnabled(False)
        self.group_reason_label.setText("")
        self.paper_table.setRowCount(0)
        if has_groups:
            self.group_list.setCurrentRow(0)

    def _load_group(self, row: int) -> None:
        if not (0 <= row < len(self._groups)):
            self.merge_button.setEnabled(False)
            return
        group = self._groups[row]
        self.group_reason_label.setText(f"判定依据:{group.reason}")
        self._keeper_radios.clear()
        self.paper_table.setRowCount(len(group.paper_ids))
        # 默认保留最早收录的一篇(第 0 行),用户可点选第一列单选框更换
        for table_row, paper_id in enumerate(group.paper_ids):
            paper = self._papers_by_id.get(paper_id)
            radio = QRadioButton()
            if table_row == 0:
                radio.setChecked(True)
            self._keeper_radios[paper_id] = radio
            self.paper_table.setCellWidget(table_row, 0, radio)
            values = (
                (paper.title or "未命名文献") if paper else f"#{paper_id}(已删除)",
                (paper.authors or "—") if paper else "—",
                str(paper.year) if paper and paper.year else "—",
                (paper.venue or "—") if paper else "—",
                self._identifier_text(paper) if paper else "—",
                ("有" if paper and paper.pdf_path else "无") if paper else "—",
                _format_date(paper.created_at) if paper else "—",
            )
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setToolTip(value)
                if column == 6:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.paper_table.setItem(table_row, column, item)
        self.merge_button.setEnabled(True)

    @staticmethod
    def _identifier_text(paper: Paper | None) -> str:
        if paper is None:
            return "—"
        if paper.doi:
            return paper.doi
        if paper.arxiv_id:
            return f"arXiv:{paper.arxiv_id}"
        return "—"

    def _selected_keeper_id(self) -> int | None:
        for paper_id, radio in self._keeper_radios.items():
            if radio.isChecked():
                return paper_id
        return None

    def _current_group(self) -> DuplicateGroup | None:
        row = self.group_list.currentRow()
        if 0 <= row < len(self._groups):
            return self._groups[row]
        return None

    def _confirm_merge(self, keeper_title: str, duplicate_count: int) -> bool:
        """合并前确认;独立方法便于测试替换。"""
        return confirm_action(
            self,
            title=f"合并 {duplicate_count} 篇重复文献?",
            message=f"将保留《{keeper_title}》,其余 {duplicate_count} 篇重复文献移入回收站(可恢复)。",
            detail="缺失信息并入保留的文献;PDF 文件保留,直至从回收站彻底删除才会清理。",
            confirm_label="合并并移入回收站",
        )

    def merge_current_group(self) -> None:
        group = self._current_group()
        if group is None:
            return
        keeper_id = self._selected_keeper_id()
        if keeper_id is None:
            self.status_label.setText("请先在表格第一列选择要保留的文献。")
            return
        duplicate_ids = [paper_id for paper_id in group.paper_ids if paper_id != keeper_id]
        keeper = self._papers_by_id.get(keeper_id)
        keeper_title = (keeper.title if keeper else "") or "未命名文献"
        if not self._confirm_merge(keeper_title, len(duplicate_ids)):
            return
        counts = {"metadata": 0, "ai_fields": 0, "categories": 0, "tags": 0, "pdf": 0}
        if self.merge_checkbox.isChecked():
            counts = self.repository.merge_duplicate_papers(keeper_id, duplicate_ids)
        moved = self.repository.delete_papers(duplicate_ids)
        merged_bits = []
        if counts["metadata"]:
            merged_bits.append(f"{counts['metadata']} 项元数据")
        if counts["ai_fields"]:
            merged_bits.append(f"{counts['ai_fields']} 项摘要/笔记")
        if counts["categories"] or counts["tags"]:
            merged_bits.append(f"{counts['categories']} 个分类、{counts['tags']} 个标签")
        if counts["pdf"]:
            merged_bits.append("PDF 文件")
        merged_text = "、".join(merged_bits) if merged_bits else "无缺失信息需要补齐"
        self.status_label.setText(
            f"已并入 {merged_text};{moved} 篇重复文献移入回收站,可从左侧恢复。"
        )
        self.duplicatesChanged.emit()
        self.rescan()
