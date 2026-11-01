"""数据集指标条目编辑对话框(E1 从 main_window 拆出,行为不变)。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from synthrecall.models import Paper
from synthrecall.repositories import DatasetMetricUpdate, parse_metric_number
from synthrecall.ui.widgets import NoWheelComboBox


class DatasetMetricDialog(QDialog):
    def __init__(
        self,
        papers: list[Paper],
        parent: QWidget | None = None,
        initial: dict[str, str | int | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("数据集指标")
        self.resize(620, 420)
        initial = initial or {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QLabel("手动添加排行榜条目")
        title.setObjectName("DialogTitle")

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(0)

        self.dataset_edit = QLineEdit(str(initial.get("dataset") or ""))
        self.metric_edit = QLineEdit(str(initial.get("metric") or ""))
        self.value_edit = QLineEdit(str(initial.get("value") or ""))
        self.unit_edit = QLineEdit(str(initial.get("unit") or ""))
        self.unit_edit.setPlaceholderText("例如 %、ms、M 参数")
        self.direction_combo = NoWheelComboBox()
        self.direction_combo.addItem("越高越好", "higher")
        self.direction_combo.addItem("越低越好", "lower")
        direction_index = self.direction_combo.findData(
            str(initial.get("direction") or "higher")
        )
        self.direction_combo.setCurrentIndex(max(0, direction_index))
        self.model_edit = QLineEdit(str(initial.get("model") or ""))
        self.task_edit = QLineEdit(str(initial.get("task") or ""))
        self.notes_edit = QTextEdit(str(initial.get("notes") or ""))
        self.notes_edit.setObjectName("FormulaText")
        self.notes_edit.setAcceptRichText(False)
        self.notes_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.notes_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.notes_edit.setMinimumHeight(100)
        self.paper_combo = NoWheelComboBox()
        self.paper_combo.addItem("不关联文献", None)
        for paper in papers:
            self.paper_combo.addItem(paper.title or f"Paper {paper.id}", paper.id)
        paper_id = initial.get("paper_id")
        if paper_id is not None:
            index = self.paper_combo.findData(int(paper_id))
            if index >= 0:
                self.paper_combo.setCurrentIndex(index)

        form.addRow("数据集", self.dataset_edit)
        form.addRow("指标", self.metric_edit)
        form.addRow("结果", self.value_edit)
        form.addRow("单位", self.unit_edit)
        form.addRow("指标方向", self.direction_combo)
        form.addRow("模型/方法", self.model_edit)
        form.addRow("任务", self.task_edit)
        form.addRow("关联文献", self.paper_combo)
        form.addRow("说明", self.notes_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout.addWidget(title)
        layout.addWidget(form_host)
        layout.addWidget(buttons)

    def validate_and_accept(self) -> None:
        if not self.dataset_edit.text().strip():
            QMessageBox.information(self, "缺少数据集", "请填写数据集名称。")
            return
        if not self.metric_edit.text().strip():
            QMessageBox.information(self, "缺少指标", "请填写指标名称。")
            return
        if not self.value_edit.text().strip():
            QMessageBox.information(self, "缺少结果", "请填写结果数值。")
            return
        if parse_metric_number(self.value_edit.text()) is None:
            QMessageBox.information(
                self,
                "结果格式无效",
                "结果中需要包含可排序的数值，例如 84.5%、12.3 ms 或 3.2 M。",
            )
            return
        if not self.model_edit.text().strip():
            QMessageBox.information(self, "缺少模型", "请填写模型或方法名称。")
            return
        self.accept()

    def update(self) -> DatasetMetricUpdate:
        paper_id = self.paper_combo.currentData()
        return DatasetMetricUpdate(
            dataset=self.dataset_edit.text(),
            metric=self.metric_edit.text(),
            value=self.value_edit.text(),
            model=self.model_edit.text(),
            task=self.task_edit.text(),
            notes=self.notes_edit.toPlainText(),
            paper_id=int(paper_id) if paper_id is not None else None,
            unit=self.unit_edit.text(),
            direction=str(self.direction_combo.currentData() or "higher"),
        )
