"""F20 智能文件夹新建/编辑对话框:规则字段 + 实时匹配数预览。

规则字段全部对应仓库层 list_papers 的既有过滤参数;「不限」即 None。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from synthrecall.repositories import LibraryRepository, SavedSearchRule
from synthrecall.ui.widgets import NoWheelComboBox, NoWheelSpinBox

READING_STATUSES = ["待读", "浏览", "已读", "精读", "复现中", "已复现"]


class SavedSearchDialog(QDialog):
    def __init__(
        self,
        repository: LibraryRepository,
        *,
        name: str = "",
        rule: SavedSearchRule | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.setWindowTitle("智能文件夹")
        self.setMinimumWidth(420)
        self.rule = rule or SavedSearchRule()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("例如:2026 待读含 ImageNet")
        form.addRow("名称", self.name_edit)

        self.keyword_edit = QLineEdit(self.rule.search)
        self.keyword_edit.setPlaceholderText("在标题/锚点/摘要/全文等字段中查找(可空)")
        form.addRow("关键词", self.keyword_edit)

        self.tag_combo = NoWheelComboBox()
        self.tag_combo.addItem("不限", None)
        for tag in repository.list_tags():
            self.tag_combo.addItem(tag.name, tag.id)
        self._select_combo(self.tag_combo, self.rule.tag_id)
        form.addRow("标签", self.tag_combo)

        self.category_combo = NoWheelComboBox()
        self.category_combo.addItem("不限", None)
        for category in repository.list_categories():
            if category.name == "未分类":
                continue
            self.category_combo.addItem(category.name, category.id)
        self._select_combo(self.category_combo, self.rule.category_id)
        form.addRow("主分类", self.category_combo)

        self.status_combo = NoWheelComboBox()
        self.status_combo.addItem("不限", None)
        for status in READING_STATUSES:
            self.status_combo.addItem(status, status)
        self._select_combo(self.status_combo, self.rule.reading_status)
        form.addRow("阅读状态", self.status_combo)

        self.year_spin = NoWheelSpinBox()
        self.year_spin.setRange(0, 2100)
        self.year_spin.setSpecialValueText("不限")
        self.year_spin.setValue(self.rule.year or 0)
        form.addRow("年份", self.year_spin)

        self.importance_spin = NoWheelSpinBox()
        self.importance_spin.setRange(0, 5)
        self.importance_spin.setSpecialValueText("不限")
        self.importance_spin.setValue(self.rule.min_importance or 0)
        form.addRow("重要度不低于", self.importance_spin)
        root.addLayout(form)

        self.preview_label = QLabel("")
        self.preview_label.setObjectName("MutedText")
        self.preview_label.setWordWrap(True)
        root.addWidget(self.preview_label)

        buttons = QHBoxLayout()
        clear_button = QPushButton("全部不限")
        clear_button.setObjectName("TinyButton")
        clear_button.clicked.connect(self._reset_rule)
        buttons.addWidget(clear_button)
        buttons.addStretch(1)
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        buttons.addWidget(button_box)
        root.addLayout(buttons)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(220)
        self._preview_timer.timeout.connect(self._update_preview)
        for widget in (
            self.keyword_edit,
            self.tag_combo,
            self.category_combo,
            self.status_combo,
            self.year_spin,
            self.importance_spin,
        ):
            if isinstance(widget, QComboBox):
                signal = widget.currentIndexChanged
            elif isinstance(widget, QLineEdit):
                signal = widget.textChanged
            else:
                signal = widget.valueChanged
            signal.connect(lambda *_a: self._preview_timer.start())
        self._update_preview()

    @staticmethod
    def _select_combo(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(max(index, 0))

    def _reset_rule(self) -> None:
        self.keyword_edit.clear()
        self._select_combo(self.tag_combo, None)
        self._select_combo(self.category_combo, None)
        self._select_combo(self.status_combo, None)
        self.year_spin.setValue(0)
        self.importance_spin.setValue(0)
        self._update_preview()

    def build_rule(self) -> SavedSearchRule:
        return SavedSearchRule(
            search=self.keyword_edit.text().strip(),
            search_fields=None,
            tag_id=self.tag_combo.currentData(),
            category_id=self.category_combo.currentData(),
            reading_status=self.status_combo.currentData(),
            year=self.year_spin.value() or None,
            min_importance=self.importance_spin.value() or None,
        )

    def _update_preview(self) -> None:
        papers = self.repository.list_papers(**self.build_rule().to_kwargs())
        self.preview_label.setText(f"当前匹配 {len(papers)} 篇文献(实时预览)")

    def accept(self) -> None:
        if not self.name_edit.text().strip():
            self.preview_label.setText("请先填写智能文件夹名称。")
            return
        super().accept()
