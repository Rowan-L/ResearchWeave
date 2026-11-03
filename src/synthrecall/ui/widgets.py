"""共享基础控件:文献卡片、平滑列表、无滚轮下拉/微调框、密码可见按钮、自适应文本框。

从 main_window.py 拆出(E1),行为不变;main_window 重新导出保持原导入路径可用。
"""

from __future__ import annotations

import html
import re

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QSignalBlocker,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QResizeEvent,
    QTextBlockFormat,
    QTextCursor,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QToolButton,
    QWidget,
)

from synthrecall.models import Paper
from synthrecall.ui import theme


class PaperCard(QFrame):
    doubleClicked = Signal(int)
    favoriteToggled = Signal(int, bool)
    # F28:Ctrl/Shift 点击卡片时发出(卡片/子控件会吞修饰键点击,
    # 交给主窗口按 Qt 选择语义切换列表选中,确定性优于事件转发)
    listToggleRequested = Signal(int, object)

    def __init__(
        self,
        paper: Paper,
        *,
        search_query: str = "",
        match_fields: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.paper_id = paper.id
        self._is_favorite = False
        self.setObjectName("PaperCard")

        self.setMinimumHeight(theme.card_min_height())
        self.setMaximumHeight(theme.card_row_height())
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QGridLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(4)
        layout.setColumnStretch(0, 1)

        self.title_label = QLabel()
        self.title_label.setObjectName("CardTitle")
        self.title_label.setTextFormat(Qt.TextFormat.RichText)
        self.title_label.setWordWrap(True)

        self.meta_label = QLabel()
        self.meta_label.setObjectName("CardMeta")
        self.meta_label.setTextFormat(Qt.TextFormat.RichText)
        self.meta_label.setWordWrap(True)

        self.chips_container = QWidget()
        self.chips_layout = QHBoxLayout(self.chips_container)
        self.chips_layout.setContentsMargins(0, 0, 0, 0)
        self.chips_layout.setSpacing(4)

        self.summary_label = QLabel()
        self.summary_label.setObjectName("CardSummary")
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.summary_label.setWordWrap(True)

        self.year_label = QLabel()
        self.year_label.setObjectName("CardColumn")
        self.status_label = QLabel()
        self.status_label.setObjectName("CardStatus")
        self.importance_label = QLabel()
        self.importance_label.setObjectName("CardImportance")

        # F21:收藏星标——亮星=已收藏,灰星=未收藏,点击即切换
        self.favorite_button = QToolButton()
        self.favorite_button.setObjectName("FavoriteStar")
        self.favorite_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.favorite_button.setFixedSize(26, 24)
        self.favorite_button.clicked.connect(self._on_favorite_clicked)

        layout.addWidget(self.title_label, 0, 0)
        layout.addWidget(self.year_label, 0, 1, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.status_label, 0, 2, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.importance_label, 0, 3, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.favorite_button, 0, 4, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.meta_label, 1, 0)
        layout.addWidget(self.chips_container, 1, 1, 1, 3)
        layout.addWidget(self.summary_label, 2, 0, 1, 4)

        self.update_content(paper, search_query=search_query, match_fields=match_fields)

    def update_content(
        self,
        paper: Paper,
        *,
        search_query: str = "",
        match_fields: list[str] | None = None,
    ) -> None:
        """原地刷新卡片内容(搜索词高亮、元数据、分类 chip),复用已有控件。"""
        self.paper_id = paper.id
        self.setAccessibleName(paper.title or "未命名文献")
        self.setAccessibleDescription(
            f"{paper.authors or '未知作者'}，"
            f"{paper.year or '年份未知'}，"
            f"状态 {paper.reading_status or '待读'}，重要度 {paper.importance}"
        )

        self.title_label.setText(
            self._highlight(paper.title or "未命名文献", search_query)
        )
        self._apply_favorite(bool(getattr(paper, "is_favorite", False)))
        meta_parts = [
            paper.authors or "未知作者",
            str(paper.year) if paper.year else "年份未知",
            paper.venue or "来源未填写",
        ]
        self.meta_label.setText(self._highlight(" · ".join(meta_parts), search_query))

        # B36:无条件重建 chips——此前的「保留最后一项」写法会留下上一张
        # 卡片的最后一个分类 chip(卡片被列表复用时与当前分类重复/串卡)。
        while self.chips_layout.count():
            taken = self.chips_layout.takeAt(0)
            widget = taken.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for category in paper.categories[:2]:
            self.chips_layout.addWidget(self._chip(category.name, "CoolChip"))
        if match_fields:
            self.chips_layout.addWidget(
                self._chip(f"命中：{'/'.join(match_fields)}", "CoolChip")
            )
        self.chips_layout.addStretch(1)

        detail = paper.detail
        summary = ""
        if detail is not None:
            summary = detail.memory_anchor or detail.summary or detail.datasets or detail.personal_notes
        summary = summary or paper.abstract or "还没有摘要或总结。可以先手动记录，后续再交给 AI 补全。"
        self.summary_label.setText(self._highlight(self._clip(summary, 132), search_query))

        self.year_label.setText(str(paper.year) if paper.year else "—")
        self.status_label.setText(paper.reading_status or "待读")
        self.importance_label.setText(f"重要度 {paper.importance}")

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit(self.paper_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # F28:Ctrl/Shift+左键 → 请求列表切换本卡片选中(QListWidget 的
        # ExtendedSelection 对落在卡片子控件上的修饰键点击不可靠)
        mods = event.modifiers()
        if (
            event.button() == Qt.MouseButton.LeftButton
            and mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        ):
            self.listToggleRequested.emit(self.paper_id, mods)
            event.accept()
            return
        super().mousePressEvent(event)

    def _apply_favorite(self, favorite: bool) -> None:
        """F21:星标视觉——亮金=已收藏,浅灰=未收藏。"""
        self._is_favorite = bool(favorite)
        self.favorite_button.setText("★" if self._is_favorite else "☆")
        color = "#f59e0b" if self._is_favorite else "#b7c0cd"
        self.favorite_button.setStyleSheet(
            f"QToolButton#FavoriteStar {{ color: {color}; background: transparent;"
            " border: none; font-size: 15px; padding: 0px; }"
        )
        self.favorite_button.setToolTip("取消收藏" if self._is_favorite else "加入收藏")

    def _on_favorite_clicked(self) -> None:
        new_value = not self._is_favorite
        self._apply_favorite(new_value)
        self.favoriteToggled.emit(self.paper_id, new_value)

    def _chip(self, text: str, object_name: str) -> QLabel:
        chip = QLabel(text)
        chip.setObjectName(object_name)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        return chip

    def _clip(self, text: str, limit: int) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 1] + "..."

    def _highlight(self, text: str, query: str) -> str:
        compact_query = query.strip()
        if not compact_query:
            return html.escape(text)
        highlight_bg = theme.color("card_highlight_bg")
        highlight_fg = theme.color("card_highlight_fg")
        parts = re.split(f"({re.escape(compact_query)})", text, flags=re.IGNORECASE)
        return "".join(
            (
                f"<span style='background:{highlight_bg};color:{highlight_fg}'>"
                f"{html.escape(part)}</span>"
                if part.casefold() == compact_query.casefold()
                else html.escape(part)
            )
            for part in parts
        )


class _SmoothScrollMixin:
    """U12:滚轮平滑滚动——动画逼近目标值,连续滚动在动画当前位置上叠加。"""

    _scroll_step = 42
    _scroll_duration_ms = 150

    def _animate_scroll(self, delta: float) -> None:
        scrollbar = self.verticalScrollBar()
        animation = getattr(self, "_scroll_animation", None)
        if animation is not None and animation.state() == QPropertyAnimation.State.Running:
            base = int(animation.currentValue())
        else:
            base = scrollbar.value()
        if animation is None:
            animation = QPropertyAnimation(scrollbar, b"value", self)
            animation.setDuration(self._scroll_duration_ms)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._scroll_animation = animation
        target = min(max(base - int(delta / 120 * self._scroll_step), scrollbar.minimum()), scrollbar.maximum())
        if target == scrollbar.value():
            return
        animation.stop()
        animation.setStartValue(scrollbar.value())
        animation.setEndValue(target)
        animation.start()


class SmoothPaperList(_SmoothScrollMixin, QListWidget):
    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        self._animate_scroll(delta)
        event.accept()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        overlay = self.findChild(QFrame, "PaperListEmptyState")
        if overlay is not None and overlay.isVisible():
            overlay.setGeometry(0, 0, self.viewport().width(), self.viewport().height())


class SmoothTagList(_SmoothScrollMixin, QListWidget):
    _scroll_step = 18

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        self._animate_scroll(delta)
        event.accept()


class NoWheelComboBox(QComboBox):
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.isEnabled()
            and not self.isEditable()
        ):
            self._open_popup_on_release = True
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and getattr(self, "_open_popup_on_release", False)
        ):
            self._open_popup_on_release = False
            if self.rect().contains(event.position().toPoint()):
                QTimer.singleShot(0, self._show_popup_if_available)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _show_popup_if_available(self) -> None:
        if self.isVisible() and self.isEnabled():
            self.showPopup()

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class PasswordVisibilityButton(QToolButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PasswordVisibilityButton")
        self.setCheckable(True)
        self.setFixedSize(38, 36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("显示 API Key")
        self.setToolTip("显示 API Key")

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self.palette().color(self.foregroundRole()), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        center_x = self.width() / 2
        center_y = self.height() / 2
        path = QPainterPath()
        path.moveTo(center_x - 10, center_y)
        path.cubicTo(
            center_x - 5, center_y - 7,
            center_x + 5, center_y - 7,
            center_x + 10, center_y,
        )
        path.cubicTo(
            center_x + 5, center_y + 7,
            center_x - 5, center_y + 7,
            center_x - 10, center_y,
        )
        painter.drawPath(path)
        painter.drawEllipse(int(center_x - 2.5), int(center_y - 2.5), 5, 5)
        if not self.isChecked():
            painter.drawLine(
                int(center_x - 9), int(center_y - 8),
                int(center_x + 9), int(center_y + 8),
            )


class AutoHeightJustifiedTextEdit(QTextEdit):
    def __init__(self, minimum_height: int = 84) -> None:
        super().__init__()
        self._minimum_auto_height = minimum_height
        self._height_update_pending = False
        self._applying_justification = False
        self.setMinimumHeight(minimum_height)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAlignment(Qt.AlignmentFlag.AlignJustify)
        self.textChanged.connect(self._on_content_changed)
        self.document().documentLayout().documentSizeChanged.connect(
            lambda _size: self._queue_height_update()
        )
        self._queue_height_update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._queue_height_update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._queue_height_update()

    def _on_content_changed(self) -> None:
        self._justify_all_blocks()
        self._queue_height_update()

    def _justify_all_blocks(self) -> None:
        if self._applying_justification:
            return
        self._applying_justification = True
        try:
            original_cursor = self.textCursor()
            cursor = QTextCursor(self.document())
            cursor.select(QTextCursor.SelectionType.Document)
            block_format = QTextBlockFormat()
            block_format.setAlignment(Qt.AlignmentFlag.AlignJustify)
            blocker = QSignalBlocker(self.document())
            cursor.mergeBlockFormat(block_format)
            del blocker
            self.setTextCursor(original_cursor)
        finally:
            self._applying_justification = False

    def _queue_height_update(self) -> None:
        if self._height_update_pending:
            return
        self._height_update_pending = True
        QTimer.singleShot(0, self._update_height_to_document)

    def _update_height_to_document(self) -> None:
        self._height_update_pending = False
        if self.viewport().width() <= 0:
            return
        document_height = self.document().documentLayout().documentSize().height()
        target_height = max(
            self._minimum_auto_height,
            int(document_height + 0.999) + self.frameWidth() * 2 + 14,
        )
        if abs(self.height() - target_height) > 1:
            self.setFixedHeight(target_height)


