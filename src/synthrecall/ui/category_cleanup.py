"""F19 分类整理向导:把多分类时代收敛为「每篇一个主分类 + 其余转标签」。

表格逐篇指定保留的主分类(默认当前第一个正式分类),其余分类名将转为
自由标签;下方可勾选要整体降级为标签的分类节点(如「数据集与基准」这类
维度错位的分类),从树移除且关联文献自动获得同名标签。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from synthrecall.models import Paper
from synthrecall.repositories import LibraryRepository

COLUMNS = ("标题", "现有分类", "保留主分类", "将转为标签")


class CategoryCleanupDialog(QDialog):
    def __init__(self, repository: LibraryRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.repository = repository
        self.setWindowTitle("分类整理向导")
        self.resize(880, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        intro = QLabel(
            "整理为「每篇一个主分类,其余分类转为标签」:"
            "分类树保持精简,细节特征(任务/数据集/方法)交给标签系统。"
            "确认后执行,标签可随时在文献编辑页增删。"
        )
        intro.setObjectName("MutedText")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.paper_table = QTableWidget(0, len(COLUMNS))
        self.paper_table.setHorizontalHeaderLabels(COLUMNS)
        self.paper_table.verticalHeader().setVisible(False)
        self.paper_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.paper_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.paper_table.setAlternatingRowColors(True)
        header = self.paper_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.paper_table.setColumnWidth(0, 230)
        # 「现有分类」是用户判断保留哪个主分类的依据,给最宽并自动提示全名
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.paper_table, 0)

        node_box = QLabel("把这些分类整体降级为标签(从分类树移除,文献自动获得同名标签):")
        node_box.setObjectName("MutedText")
        root.addWidget(node_box)
        self.node_list = QListWidget()
        root.addWidget(self.node_list, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setObjectName("MutedText")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        execute_button = QPushButton("执行整理")
        execute_button.setObjectName("PrimaryButton")
        execute_button.clicked.connect(self.execute_cleanup)
        footer.addWidget(execute_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        footer.addWidget(close_button)
        root.addLayout(footer)

        self._primary_combos: dict[int, object] = {}
        self._papers: list[Paper] = []
        self.refresh()

    def refresh(self) -> None:
        self._papers = self.repository.list_papers(sort_by="created")
        categories = sorted(
            self.repository.list_categories(),
            key=lambda category: (category.parent_id or 0, category.sort_order, category.name),
        )
        parent_names = {c.parent_id for c in categories if c.parent_id is not None}

        self.paper_table.setRowCount(len(self._papers))
        self._primary_combos = {}
        from synthrecall.ui.widgets import NoWheelComboBox

        for row, paper in enumerate(self._papers):
            names = [c.name for c in paper.categories]
            formal = [name for name in names if name != "未分类"]
            primary_default = formal[0] if formal else "未分类"
            title_item = QTableWidgetItem(paper.title or "未命名文献")
            title_item.setToolTip(paper.title or "")
            self.paper_table.setItem(row, 0, title_item)
            self.paper_table.setItem(row, 1, QTableWidgetItem("、".join(names) or "未分类"))

            combo = NoWheelComboBox()
            for category in categories:
                combo.addItem(category.name, category.id)
            index = combo.findText(primary_default)
            if index >= 0:
                combo.setCurrentIndex(index)
            self.paper_table.setCellWidget(row, 2, combo)
            self._primary_combos[paper.id] = combo

            others = [name for name in formal if name != primary_default]
            self.paper_table.setItem(
                row, 3, QTableWidgetItem("、".join(others) if others else "—")
            )

        # 降级候选:排除父组节点(有子分类的)与未分类
        self.node_list.clear()
        for category in categories:
            if category.id in parent_names or category.name == "未分类":
                continue
            item = QListWidgetItem(f"{category.name}({self._count_papers(category.id)} 篇)")
            item.setData(Qt.ItemDataRole.UserRole, category.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.node_list.addItem(item)

    def _count_papers(self, category_id: int) -> int:
        return sum(
            1
            for paper in self._papers
            if any(c.id == category_id for c in paper.categories)
        )

    def execute_cleanup(self) -> None:
        tag_count = 0
        for paper in self._papers:
            combo = self._primary_combos.get(paper.id)
            if combo is None:
                continue
            category_id = combo.currentData()
            formal_names = [
                c.name for c in paper.categories if c.name != "未分类"
            ]
            keep_name = ""
            if category_id is not None:
                keep_name = next(
                    (c.name for c in self.repository.list_categories() if c.id == category_id),
                    "",
                )
            for name in formal_names:
                if (
                    name
                    and name != keep_name
                    and name != "未分类"
                    and self.repository.add_tag_to_paper(paper.id, name)
                ):
                    tag_count += 1
            self.repository.set_primary_category(paper.id, category_id)

        demoted_names: list[str] = []
        demote_ids = []
        for row in range(self.node_list.count()):
            item = self.node_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                demote_ids.append(int(item.data(Qt.ItemDataRole.UserRole)))
                demoted_names.append(item.text().split("(")[0])
        if demote_ids:
            self.repository.demote_categories_to_tags(demote_ids)

        message = (
            f"整理完成:{len(self._papers)} 篇文献已收敛为主分类,"
            f"新增 {tag_count} 个标签"
        )
        if demoted_names:
            message += f";已降级分类:{'、'.join(demoted_names)}"
        self.status_label.setText(message)
        self.refresh()
