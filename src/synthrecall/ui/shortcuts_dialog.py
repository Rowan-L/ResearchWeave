"""快捷键一览对话框(U16):分組列出全部键盘/鼠标操作,支持 F1 打开。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

# (分组, [(操作, 快捷键/鼠标), ...])
SHORTCUT_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "全局",
        [
            ("聚焦搜索框", "Ctrl+F"),
            ("刷新文献库", "F5 或 Ctrl+R"),
            ("打开/添加 PDF", "Ctrl+O"),
            ("新建文献", "Ctrl+N"),
            ("保存当前编辑", "Ctrl+S"),
            ("导出引用(BibTeX/RIS)", "Ctrl+E"),
            ("立即备份数据", "Ctrl+B"),
            ("界面设置(主题/字号/密度)", "Ctrl+,"),
            ("任务中心", "Ctrl+Shift+T"),
            ("切换页面:文献库/数据集/发现", "Alt+1 / Alt+2 / Alt+3"),
            ("返回上一页(按浏览顺序)", "鼠标后退键 或 Alt+←"),
            ("快捷键一览", "F1"),
        ],
    ),
    (
        "文献列表",
        [
            ("上下移动选择", "↑ / ↓"),
            ("全选文献", "Ctrl+A"),
            ("把选中文献移入回收站", "Delete"),
            ("打开阅读页", "双击卡片"),
            ("操作菜单(打开 PDF/AI 解析/复制…)", "右键卡片"),
            ("平滑滚动列表", "滚轮"),
        ],
    ),
    (
        "搜索与筛选",
        [
            ("清空搜索词", "Esc(搜索框内)"),
            ("点击分类/标签/智能文件夹筛选", "鼠标左键"),
            ("把当前筛选存为智能文件夹", "右键左栏空白处"),
        ],
    ),
    (
        "阅读器",
        [
            ("放大 / 缩小", "Ctrl++ / Ctrl+-"),
            ("恢复默认缩放", "Ctrl+0"),
            ("跳转到上一页 / 下一页", "▲ / ▼ 或 ↑ / ↓"),
            ("添加高亮/批注", "工具栏选择后拖选文字"),
        ],
    ),
    (
        "编辑页",
        [
            ("在字段间移动", "Tab / Shift+Tab"),
            ("按 DOI / arXiv 补全元数据", "编辑页按钮"),
        ],
    ),
]


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("快捷键一览")
        self.setObjectName("ShortcutsDialog")
        self.setModal(False)
        self.resize(540, 540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        hint = QLabel("常用键盘与鼠标操作;阅读器内的快捷键在阅读页激活时生效。")
        hint.setObjectName("MutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.tree = QTreeWidget()
        self.tree.setObjectName("ShortcutsTree")
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["操作", "快捷键"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.header().setStretchLastSection(False)
        # 两列总宽须为竖滚动条留位,否则内容溢出 ~15px 触发横向滚动条
        self.tree.header().resizeSection(0, 286)
        self.tree.header().resizeSection(1, 180)
        self.tree.setIndentation(14)
        for group_name, items in SHORTCUT_GROUPS:
            group_item = QTreeWidgetItem([group_name, ""])
            group_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled  # type: ignore[operator]
            )
            for action, keys in items:
                child = QTreeWidgetItem([action, keys])
                child.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                group_item.addChild(child)
            self.tree.addTopLevelItem(group_item)
        self.tree.expandAll()
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.setObjectName("SoftButton")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
