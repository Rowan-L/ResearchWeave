"""论文对话(QA)Worker 与对话框(E1 从 main_window 拆出,行为不变)。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from synthrecall.ai import LLMClient
from synthrecall.repositories import LibraryRepository
from synthrecall.settings import LLMSettings, get_llm_api_key
from synthrecall.ui.external_request import confirm_external_request


class PaperChatWorker(QObject):
    finished = Signal(str)
    failed = Signal(str)
    delta = Signal(str)

    def __init__(
        self,
        settings_path: Path,
        source: dict[str, str],
        history: list[dict[str, str]],
        question: str,
        *,
        usage_recorder=None,
    ) -> None:
        super().__init__()
        self.settings_path = settings_path
        self.source = source
        self.history = history
        self.question = question
        self.usage_recorder = usage_recorder

    def _report_usage(self, usage: dict) -> None:
        if self.usage_recorder is not None:
            self.usage_recorder(usage)

    def run(self) -> None:
        try:
            settings = LLMSettings.load(self.settings_path)
            client = LLMClient(
                settings=settings,
                api_key=get_llm_api_key(settings.base_url),
                usage_callback=self._report_usage,
            )
            answer = client.discuss_paper(
                self.source,
                self.history,
                self.question,
                on_delta=self.delta.emit,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(answer)

class PaperChatDialog(QDialog):
    def __init__(
        self,
        repository: LibraryRepository,
        settings_path: Path,
        paper_id: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.settings_path = settings_path
        self.paper_id = paper_id
        self.history: list[dict[str, str]] = []
        self.chat_thread: QThread | None = None
        self.chat_worker: PaperChatWorker | None = None
        # B23 流式回答的占位气泡与已累积文本
        self._streaming_bubble: QLabel | None = None
        self._streaming_text = ""

        paper = repository.get_paper(paper_id)
        self.setWindowTitle(f"与 AI 讨论 · {paper.title or '未命名文献'}")
        self.resize(760, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("与 AI 讨论这篇论文")
        title.setObjectName("DialogTitle")
        hint = QLabel(
            "AI 会读取当前论文的标题、摘要、个人笔记和正文。"
            "回答只应基于这篇论文；每次发送前遵循外部请求确认设置。"
        )
        hint.setObjectName("MutedText")
        hint.setWordWrap(True)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setObjectName("PaperChatHistory")
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.chat_messages = QWidget()
        self.chat_messages.setObjectName("PaperChatMessages")
        self.chat_layout = QVBoxLayout(self.chat_messages)
        self.chat_layout.setContentsMargins(12, 12, 12, 12)
        self.chat_layout.setSpacing(10)
        self.chat_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.chat_scroll.setWidget(self.chat_messages)
        self.empty_chat_hint = QLabel(
            "可以询问：方法为什么有效、实验是否充分、与某类方法的区别、"
            "如何复现，或让 AI 解释某个公式与结论。"
        )
        self.empty_chat_hint.setObjectName("MutedText")
        self.empty_chat_hint.setWordWrap(True)
        self.empty_chat_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chat_layout.addWidget(self.empty_chat_hint)

        self.question_edit = QTextEdit()
        self.question_edit.setObjectName("PaperChatInput")
        self.question_edit.setAcceptRichText(False)
        self.question_edit.setPlaceholderText("输入你想讨论的问题…")
        self.question_edit.setMaximumHeight(120)

        actions = QHBoxLayout()
        self.clear_button = QPushButton("清空对话")
        self.clear_button.setObjectName("GhostButton")
        self.clear_button.clicked.connect(self.clear_chat)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("PrimaryButton")
        self.send_button.clicked.connect(self.send_question)
        actions.addWidget(self.clear_button)
        actions.addStretch(1)
        actions.addWidget(self.send_button)

        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self.chat_scroll, 1)
        layout.addWidget(self.question_edit)
        layout.addLayout(actions)

    def _append_message(self, speaker: str, content: str) -> tuple[QLabel, QWidget]:
        self.empty_chat_hint.hide()
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        bubble = QLabel(content.strip())
        bubble.setWordWrap(True)
        bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bubble.setMaximumWidth(520)
        if speaker == "你":
            bubble.setObjectName("UserChatBubble")
            row_layout.addStretch(1)
            row_layout.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight)
        else:
            bubble.setObjectName("AIChatBubble" if speaker == "AI" else "SystemChatBubble")
            row_layout.addWidget(bubble, 0, Qt.AlignmentFlag.AlignLeft)
            row_layout.addStretch(1)
        self.chat_layout.addWidget(row)
        QTimer.singleShot(
            0,
            lambda: self.chat_scroll.verticalScrollBar().setValue(
                self.chat_scroll.verticalScrollBar().maximum()
            ),
        )
        return bubble, row

    def send_question(self) -> None:
        if self.chat_thread is not None:
            return
        question = self.question_edit.toPlainText().strip()
        if not question:
            return
        settings = LLMSettings.load(self.settings_path)
        source = self.repository.get_ai_source(
            self.paper_id,
            max_chars=settings.max_chars,
        )
        sent_chars = sum(len(value or "") for value in source.values())
        if not confirm_external_request(
            self.settings_path,
            operation="与 AI 讨论当前论文",
            content_summary=(
                f"论文标题、摘要、个人笔记、正文和当前问题，"
                f"最多约 {sent_chars + len(question):,} 个字符"
            ),
            output_summary="回答仅显示在当前对话框，不自动修改论文资料",
            parent=self,
        ):
            return
        prior_history = list(self.history)
        self.history.append({"role": "user", "content": question})
        self._append_message("你", question)
        self.question_edit.clear()
        self.send_button.setEnabled(False)
        self.send_button.setText("思考中…")

        self.chat_thread = QThread(self)
        self.chat_worker = PaperChatWorker(
            self.settings_path,
            source,
            prior_history,
            question,
            usage_recorder=lambda usage, pid=self.paper_id: self.repository.record_token_usage(
                feature="论文对话", paper_id=pid, source="app", **usage
            ),
        )
        self.chat_worker.moveToThread(self.chat_thread)
        self.chat_thread.started.connect(self.chat_worker.run)
        self._streaming_bubble, _ = self._append_message("AI", "…")
        self._streaming_text = ""
        self.chat_worker.delta.connect(self._on_delta)
        self.chat_worker.finished.connect(self._on_answer)
        self.chat_worker.failed.connect(self._on_error)
        self.chat_worker.finished.connect(self.chat_thread.quit)
        self.chat_worker.failed.connect(self.chat_thread.quit)
        self.chat_thread.finished.connect(self.chat_worker.deleteLater)
        self.chat_thread.finished.connect(self._clear_thread)
        self.chat_thread.start()

    def _on_delta(self, piece: str) -> None:
        """B23:流式增量——逐段填充占位气泡并滚动跟随。"""
        if self._streaming_bubble is None:
            return
        self._streaming_text += piece
        self._streaming_bubble.setText(self._streaming_text)
        QTimer.singleShot(
            0,
            lambda: self.chat_scroll.verticalScrollBar().setValue(
                self.chat_scroll.verticalScrollBar().maximum()
            ),
        )

    def _on_answer(self, answer: str) -> None:
        self._streaming_bubble = None
        self._streaming_text = ""
        self.history.append({"role": "assistant", "content": answer})
        self._append_message("AI", answer)
        self._set_ready()

    def _on_error(self, message: str) -> None:
        if self._streaming_bubble is not None:
            # 流式中途失败:占位气泡换成分隔提示,不展示半截内容误导
            self._streaming_bubble.setText("(回答被中断)")
            self._streaming_bubble = None
            self._streaming_text = ""
        self._append_message("系统", f"请求失败：{message}")
        self._set_ready()

    def _set_ready(self) -> None:
        self.send_button.setEnabled(True)
        self.send_button.setText("发送")
        self.question_edit.setFocus()

    def _clear_thread(self) -> None:
        self.chat_thread = None
        self.chat_worker = None

    def clear_chat(self) -> None:
        if self.chat_thread is not None:
            return
        self.history.clear()
        while self.chat_layout.count():
            item = self.chat_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.empty_chat_hint = QLabel(
            "可以询问论文方法、实验、复现细节，或让 AI 解释公式与结论。"
        )
        self.empty_chat_hint.setObjectName("MutedText")
        self.empty_chat_hint.setWordWrap(True)
        self.empty_chat_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chat_layout.addWidget(self.empty_chat_hint)
        self.question_edit.clear()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.chat_thread is not None:
            QMessageBox.information(
                self,
                "AI 正在回答",
                "请等待当前回答完成后再关闭对话框。",
            )
            event.ignore()
            return
        super().closeEvent(event)
