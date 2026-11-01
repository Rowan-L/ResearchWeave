"""界面设置对话框(F10):主题(浅色/深色)、字号、界面密度。

改动即实时应用(回调 appearance_changed),由主窗口重刷样式与文献列表;
设置持久化在 ui_state.ini(theme 模块负责读写)。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from synthrecall.ui import theme
from synthrecall.ui.widgets import NoWheelComboBox


class AppearanceDialog(QDialog):
    appearance_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("界面设置")
        self.setObjectName("AppearanceDialog")
        self.setModal(False)
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        self.theme_combo = NoWheelComboBox()
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.addItem("深色", "dark")
        form.addRow("主题", self.theme_combo)

        self.font_combo = NoWheelComboBox()
        for key in ("small", "standard", "large"):
            self.font_combo.addItem(theme.font_size_label(key), key)
        form.addRow("界面字号", self.font_combo)

        self.density_combo = NoWheelComboBox()
        for key in ("compact", "standard", "relaxed"):
            self.density_combo.addItem(theme.density_label(key), key)
        form.addRow("列表密度", self.density_combo)
        layout.addLayout(form)

        hint = QLabel(
            "密度影响文献卡片行高;改动立即生效并记住,"
            "已打开的对话框在重新打开后应用新主题。"
        )
        hint.setObjectName("AppearanceHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.setObjectName("SoftButton")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._loading = True
        self._select(self.theme_combo, theme.theme_mode())
        self._select(self.font_combo, theme.font_size_key())
        self._select(self.density_combo, theme.density_key())
        self._loading = False

        self.theme_combo.currentIndexChanged.connect(self._apply)
        self.font_combo.currentIndexChanged.connect(self._apply)
        self.density_combo.currentIndexChanged.connect(self._apply)

    @staticmethod
    def _select(combo: NoWheelComboBox, key: str) -> None:
        index = combo.findData(key)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _apply(self) -> None:
        if self._loading:
            return
        theme.set_theme_mode(self.theme_combo.currentData())
        theme.set_font_size(self.font_combo.currentData())
        theme.set_density(self.density_combo.currentData())
        self.appearance_changed.emit()
