"""长任务加载进度对话框(E1 从 main_window 拆出,行为不变)。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout, QWidget


class LoadingDialog(QDialog):
    cancelRequested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        title: str = "AI 解析中",
        message: str = "正在解析论文，请稍候...",
        hint: str = "大模型正在生成文献信息。",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.setFixedSize(360, 230)
        self._frames = ["◐", "◓", "◑", "◒"]
        self._index = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        self.spinner = QLabel(self._frames[0])
        self.spinner.setObjectName("SpinnerLabel")
        self.spinner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel(message)
        text.setObjectName("DialogTitle")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("MutedText")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label.setWordWrap(True)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("MutedText")
        self.progress_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_label.hide()
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.setObjectName("TinyButton")
        self.cancel_button.clicked.connect(self._request_cancel)

        layout.addWidget(self.spinner)
        layout.addWidget(text)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.cancel_button, alignment=Qt.AlignmentFlag.AlignCenter)

        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._tick)

    def showEvent(self, event) -> None:
        self.timer.start()
        super().showEvent(event)

    def closeEvent(self, event) -> None:
        self.timer.stop()
        super().closeEvent(event)

    def _tick(self) -> None:
        self._index = (self._index + 1) % len(self._frames)
        self.spinner.setText(self._frames[self._index])

    def _request_cancel(self) -> None:
        self.set_cancelling()
        self.cancelRequested.emit()

    def set_progress(self, text: str) -> None:
        """B23:流式进度(已接收字符数);空文本时隐藏恢复原样。"""
        if text:
            self.progress_label.setText(text)
            self.progress_label.show()
        else:
            self.progress_label.hide()

    def set_cancelling(self) -> None:
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("正在取消")
        self.hint_label.setText("正在安全取消；若已有网络请求，会在当前请求返回后停止。")
