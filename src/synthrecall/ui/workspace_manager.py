"""F11 工作区管理对话框:注册/移除多个库目录,切换当前工作区(重启生效)。

切换动作本身不在此执行——对话框发出 switchRequested(name),
由 MainWindow 负责未保存确认、更新注册表并重启进程。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


def _path_key(path: Path) -> str:
    """路径相等判定键:统一分隔符与大小写(Windows 路径大小写不敏感)。"""
    return str(path.resolve()).replace("\\", "/").casefold()


from synthrecall.workspaces import (
    WorkspaceEntry,
    add_workspace,
    list_workspaces,
    remove_workspace,
)


class WorkspaceManagerDialog(QDialog):
    switchRequested = Signal(str)

    def __init__(
        self,
        *,
        current_root: Path,
        default_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.current_root = current_root
        self.default_root = default_root
        self.setWindowTitle("工作区管理")
        self.resize(640, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        # 手动断行:语义完整处换行,避免自动换行把尾巴两三个字挤成孤行
        intro = QLabel(
            "每个工作区是一个独立的文献库目录(数据库、PDF 与设置),"
            + chr(10)
            + "新建工作区不影响当前库。"
        )
        intro.setObjectName("MutedText")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["名称", "库目录", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.table, 1)

        # 操作按钮与关闭同排(左操作右关闭),避免下盘松散
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        create_button = QPushButton("新建工作区…")
        create_button.setObjectName("PrimaryButton")
        create_button.clicked.connect(self.create_workspace)
        add_button = QPushButton("添加已有目录…")
        add_button.clicked.connect(self.add_existing_directory)
        self.switch_button = QPushButton("切换到此工作区")
        self.switch_button.setObjectName("SoftButton")
        self.switch_button.clicked.connect(self.request_switch)
        remove_button = QPushButton("从列表移除")
        remove_button.clicked.connect(self.remove_selected)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(create_button)
        buttons.addWidget(add_button)
        buttons.addWidget(self.switch_button)
        buttons.addWidget(remove_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        root.addLayout(buttons)

        self.status_label = QLabel(
            "切换工作区会自动重启软件;列表外库目录的数据不受影响。"
        )
        self.status_label.setObjectName("MutedText")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self._entries: list[WorkspaceEntry] = []
        self.refresh()

    def refresh(self) -> None:
        # 内置「默认工作区」(安装目录)恒在列表头;已注册工作区跟在后面。
        # 当前判定按 data 目录的规范小写路径比较,避免大小写/分隔符差异误判。
        registered = list_workspaces()
        builtin = WorkspaceEntry(name="默认工作区", root=self.default_root)
        if any(entry.root == builtin.root for entry in registered):
            self._entries = registered
        else:
            self._entries = [builtin, *registered]
        current_key = _path_key(self.current_root)
        self.table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            name_item = QTableWidgetItem(entry.name)
            name_item.setData(Qt.ItemDataRole.UserRole, entry.name)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(str(entry.root)))
            status = "当前使用" if _path_key(entry.root) == current_key else ""
            self.table.setItem(row, 2, QTableWidgetItem(status))
        self.table.selectRow(0)

    def _selected_entry(self) -> WorkspaceEntry | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def create_workspace(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择新工作区的目录(将为它创建 data 子目录)"
        )
        if not directory:
            return
        suggested = Path(directory).name or "新工作区"
        name, ok = QInputDialog.getText(
            self, "新建工作区", "工作区名称:", text=suggested
        )
        name = name.strip()
        if not ok or not name:
            return
        try:
            add_workspace(name, Path(directory))
        except (OSError, ValueError) as exc:
            self.status_label.setText(f"创建失败:{exc}")
            return
        self.status_label.setText(f"已注册工作区「{name}」;切换后才会使用它的库。")
        self.refresh()

    def add_existing_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择已有库目录(其下应已有或将为它创建 data 子目录)"
        )
        if not directory:
            return
        name, ok = QInputDialog.getText(
            self, "添加已有目录", "工作区名称:", text=Path(directory).name
        )
        name = name.strip()
        if not ok or not name:
            return
        try:
            add_workspace(name, Path(directory))
        except (OSError, ValueError) as exc:
            self.status_label.setText(f"添加失败:{exc}")
            return
        self.status_label.setText(f"已添加工作区「{name}」。")
        self.refresh()

    def request_switch(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        if _path_key(entry.root) == _path_key(self.current_root):
            self.status_label.setText("「" + entry.name + "」已是当前工作区。")
            return
        self.switchRequested.emit(entry.name)

    def remove_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        if _path_key(entry.root) == _path_key(self.current_root):
            self.status_label.setText("当前使用的工作区不能从列表移除。")
            return
        if entry.name == "默认工作区" and _path_key(entry.root) == _path_key(
            self.default_root
        ):
            self.status_label.setText("内置默认工作区不可移除。")
            return
        remove_workspace(entry.name)
        self.status_label.setText(f"已从列表移除「{entry.name}」(磁盘数据未动)。")
        self.refresh()
