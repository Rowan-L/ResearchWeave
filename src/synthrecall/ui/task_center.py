from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from synthrecall.tasks import (
    TASK_CANCELLED,
    TASK_CANCELLING,
    TASK_FAILED,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    TaskManager,
)


def to_local(value: datetime) -> datetime:
    """B6:库内时间为 UTC(naive),展示前转本地时区。"""
    return value.replace(tzinfo=UTC).astimezone()


STATE_LABELS = {
    TASK_QUEUED: "排队中",
    TASK_RUNNING: "运行中",
    TASK_CANCELLING: "正在取消",
    TASK_SUCCEEDED: "已完成",
    TASK_FAILED: "失败",
    TASK_CANCELLED: "已取消",
}


class TaskCenterDialog(QDialog):
    def __init__(self, manager: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("任务中心")
        self.resize(860, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["任务", "状态", "详情", "开始时间", "结束时间"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.itemSelectionChanged.connect(self.update_actions)

        action_row = QHBoxLayout()
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.retry_button = QPushButton("重试")
        self.retry_button.clicked.connect(self.retry_selected)
        self.copy_error_button = QPushButton("复制错误")
        self.copy_error_button.clicked.connect(self.copy_selected_error)
        self.clear_button = QPushButton("清除已完成")
        self.clear_button.clicked.connect(self.manager.clear_finished)
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.retry_button)
        action_row.addWidget(self.copy_error_button)
        action_row.addStretch(1)
        action_row.addWidget(self.clear_button)

        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close_buttons.rejected.connect(self.reject)

        layout.addWidget(self.table, 1)
        layout.addLayout(action_row)
        layout.addWidget(close_buttons)

        self.manager.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        selected_id = self.selected_task_id()
        records = self.manager.records()
        self.table.setRowCount(len(records))
        selected_row = -1
        for row, record in enumerate(records):
            values = [
                record.name,
                STATE_LABELS.get(record.state, record.state),
                record.error or record.detail,
                to_local(record.created_at).strftime("%H:%M:%S"),
                to_local(record.finished_at).strftime("%H:%M:%S") if record.finished_at else "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record.id)
                if record.error:
                    item.setToolTip(record.error)
                self.table.setItem(row, column, item)
            if record.id == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        elif records:
            self.table.selectRow(0)
        self.update_actions()

    def selected_task_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        task_id = item.data(Qt.ItemDataRole.UserRole)
        return int(task_id) if task_id is not None else None

    def update_actions(self) -> None:
        record = self.manager.get(self.selected_task_id())
        self.cancel_button.setEnabled(bool(record and record.can_cancel))
        self.retry_button.setEnabled(bool(record and record.can_retry))
        self.copy_error_button.setEnabled(bool(record and record.error))

    def cancel_selected(self) -> None:
        self.manager.request_cancel(self.selected_task_id())

    def retry_selected(self) -> None:
        self.manager.retry(self.selected_task_id())

    def copy_selected_error(self) -> None:
        record = self.manager.get(self.selected_task_id())
        if record is None or not record.error:
            return
        QApplication.clipboard().setText(record.error)
        QMessageBox.information(self, "已复制", "错误详情已复制到剪贴板。")
