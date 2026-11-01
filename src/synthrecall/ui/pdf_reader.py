from __future__ import annotations

import shutil
from pathlib import Path

import pymupdf as fitz
from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QCursor,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QColorDialog,
    QFrame,
    QInputDialog,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from synthrecall.ui.confirmation import ConfirmationDialog, DialogAction


class FlowLayout(QLayout):
    """流式布局:B29——窗口宽度不足时工具栏按钮自动换行,不再挤压重叠。"""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self._spacing = spacing
        self._items: list[QLayoutItem] = []

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        placed: list[tuple[QLayoutItem, int, int]] = []
        line: list[tuple[QLayoutItem, int]] = []
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > effective.right() + 1 and line_height > 0:
                self._place_line(placed, line, y, line_height)
                line = []
                x = effective.x()
                y = y + line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            line.append((item, x))
            x = next_x
            line_height = max(line_height, hint.height())
        self._place_line(placed, line, y, line_height)
        total = y + line_height - rect.y() + margins.bottom()
        if not test_only:
            for item, item_x, item_y in placed:
                item.setGeometry(QRect(QPoint(item_x, item_y), item.sizeHint()))
        return total

    def _place_line(
        self,
        placed: list[tuple[QLayoutItem, int, int]],
        line: list[tuple[QLayoutItem, int]],
        y: int,
        line_height: int,
    ) -> None:
        # B29:同一行内矮项(标签/状态文字)相对按钮行高垂直居中
        for item, item_x in line:
            offset = y + max(0, (line_height - item.sizeHint().height()) // 2)
            placed.append((item, item_x, offset))


class ReaderPageLabel(QLabel):
    annotationGesture = Signal(int, str, object)
    annotationDoubleClicked = Signal(int, object)
    selectionGesture = Signal(int, object)
    selectionContextRequested = Signal(int, object)

    def __init__(self, page_index: int) -> None:
        super().__init__(f"第 {page_index + 1} 页")
        self.page_index = page_index
        self.annotation_tool = "browse"
        self.rendered_zoom = 0.0
        self._gesture_points: list[QPointF] = []
        self.setObjectName("ReaderPdfPage")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMouseTracking(True)

    def set_annotation_tool(self, tool: str) -> None:
        self.annotation_tool = tool
        cursors = {
            "browse": Qt.CursorShape.ArrowCursor,
            "highlight": Qt.CursorShape.CrossCursor,
            "pen": Qt.CursorShape.CrossCursor,
            "note": Qt.CursorShape.PointingHandCursor,
            "delete": self._eraser_cursor(),
        }
        self.setCursor(cursors.get(tool, Qt.CursorShape.ArrowCursor))
        self._gesture_points = []
        self.update()

    @staticmethod
    def _eraser_cursor() -> QCursor:
        pixmap = QPixmap(26, 26)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#475569"), 2))
        painter.setBrush(QColor("#f8fafc"))
        painter.drawRoundedRect(4, 8, 18, 11, 3, 3)
        painter.drawLine(12, 8, 19, 19)
        painter.end()
        return QCursor(pixmap, 13, 13)

    def clear_render(self) -> None:
        self.rendered_zoom = 0.0
        self.setPixmap(QPixmap())
        self.setText(f"第 {self.page_index + 1} 页")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self.pixmap().isNull()
        ):
            self._gesture_points = [event.position()]
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._gesture_points and event.buttons() & Qt.MouseButton.LeftButton:
            point = event.position()
            if self.annotation_tool == "pen" or len(self._gesture_points) == 1:
                self._gesture_points.append(point)
            else:
                self._gesture_points[-1] = point
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._gesture_points:
            if len(self._gesture_points) == 1:
                self._gesture_points.append(event.position())
            elif self.annotation_tool != "pen":
                self._gesture_points[-1] = event.position()
            points = list(self._gesture_points)
            self._gesture_points = []
            self.update()
            if self.annotation_tool == "browse":
                self.selectionGesture.emit(self.page_index, points)
            else:
                self.annotationGesture.emit(self.page_index, self.annotation_tool, points)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.annotation_tool == "browse"
            and not self.pixmap().isNull()
        ):
            self.annotationDoubleClicked.emit(self.page_index, event.position())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if self.annotation_tool == "browse" and not self.pixmap().isNull():
            self.selectionContextRequested.emit(self.page_index, event.pos())
            event.accept()
            return
        super().contextMenuEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._gesture_points:
            return
        painter = QPainter(self)
        if self.annotation_tool in {"browse", "highlight"} and len(self._gesture_points) > 1:
            rectangle = QRectF(self._gesture_points[0], self._gesture_points[-1]).normalized()
            if self.annotation_tool == "browse":
                painter.setPen(QPen(QColor(37, 99, 235), 1))
                painter.setBrush(QColor(59, 130, 246, 65))
            else:
                painter.setPen(QPen(QColor(224, 174, 0), 1))
                painter.setBrush(QColor(255, 220, 45, 90))
            painter.drawRect(rectangle)
        elif self.annotation_tool == "pen" and len(self._gesture_points) > 1:
            painter.setPen(
                QPen(
                    QColor(214, 66, 66, 210),
                    2.2,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            for start, end in zip(self._gesture_points, self._gesture_points[1:]):
                painter.drawLine(start, end)


class PageSpinBox(QSpinBox):
    """页码跳转框：箭头/滚轮/上下键按阅读方向步进，键入页码按回车跳转。"""

    pageStepped = Signal()

    def stepBy(self, steps: int) -> None:
        # 阅读语义:▲/↑/滚轮上 = 往文档开头翻(页码-1),与 QSpinBox 默认的
        # 数值递增方向相反,统一在这里反转。
        super().stepBy(-steps)
        self.pageStepped.emit()


class ContinuousPDFReader(QWidget):
    annotationDirtyChanged = Signal(bool)
    statusMessage = Signal(str)
    pageChanged = Signal(int)
    annotationsChanged = Signal()
    translateRequested = Signal()

    MODES = (
        ("original", "原文"),
        ("mono", "译文"),
        ("dual", "双语对照"),
    )

    def __init__(self) -> None:
        super().__init__()
        self._paths: dict[str, Path | None] = {key: None for key, _label in self.MODES}
        self._documents: dict[str, fitz.Document] = {}
        self._dirty_modes: set[str] = set()
        self._active_mode = "original"
        self._annotation_tool = "browse"
        self._page_labels: list[ReaderPageLabel] = []
        self._search_pages: list[int] = []
        self._search_position = -1
        self._selected_page = -1
        self._selected_text = ""
        self._selected_rects: list[fitz.Rect] = []
        self.page_index = 0
        self.page_count = 0
        self.zoom = 1.0
        self._rebuilding = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        toolbar = FlowLayout(spacing=6)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons: dict[str, QPushButton] = {}
        for key, label in self.MODES:
            button = QPushButton(label)
            button.setObjectName("ReaderModeButton")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, value=key: self._change_mode(value)
            )
            self.mode_group.addButton(button)
            self.mode_buttons[key] = button
        self.mode_buttons["original"].setChecked(True)

        self.zoom_out_button = QPushButton("−")
        self.zoom_out_button.setObjectName("TinyButton")
        self.zoom_out_button.clicked.connect(lambda: self.change_zoom(-0.15))
        self.zoom_in_button = QPushButton("+")
        self.zoom_in_button.setObjectName("TinyButton")
        self.zoom_in_button.clicked.connect(lambda: self.change_zoom(0.15))
        self.reset_zoom_button = QPushButton("100%")
        self.reset_zoom_button.setObjectName("TinyButton")
        self.reset_zoom_button.setToolTip("恢复 PDF 的正常页面大小。")
        self.reset_zoom_button.clicked.connect(self.reset_zoom)
        self.fit_width_button = QPushButton("适合宽度")
        self.fit_width_button.setObjectName("TinyButton")
        self.fit_width_button.clicked.connect(self.fit_width)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("MutedText")
        self.page_label = QLabel("未加载 PDF")
        self.page_label.setObjectName("MutedText")

        for key, _label in self.MODES:
            toolbar.addWidget(self.mode_buttons[key])
        toolbar.addWidget(self.zoom_out_button)
        toolbar.addWidget(self.zoom_in_button)
        toolbar.addWidget(self.reset_zoom_button)
        toolbar.addWidget(self.fit_width_button)
        toolbar.addWidget(self.zoom_label)
        self.page_spin = PageSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setPrefix("第 ")
        self.page_spin.setSuffix(" 页")
        self.page_spin.setToolTip("输入页码后按回车跳转；也可用箭头按钮或滚轮逐页翻。")
        self.page_spin.editingFinished.connect(self._go_to_spin_page)
        self.page_spin.pageStepped.connect(self._go_to_spin_page)
        toolbar.addWidget(self.page_spin)
        toolbar.addWidget(self.page_label)
        self.translate_button = QPushButton("翻译 PDF")
        self.translate_button.setObjectName("PrimaryButton")
        self.translate_button.setToolTip("翻译当前 PDF，完成后可切换到译文或双语对照。")
        self.translate_button.clicked.connect(self.translateRequested.emit)
        toolbar.addWidget(self.translate_button)

        annotation_bar = FlowLayout(spacing=6)
        self.annotation_group = QButtonGroup(self)
        self.annotation_group.setExclusive(False)
        self.annotation_buttons: dict[str, QPushButton] = {}
        for tool, label, tooltip in (
            ("browse", "浏览", "使用滑轮连续滚动阅读。"),
            ("highlight", "高亮", "拖动鼠标高亮文本区域。"),
            ("pen", "画笔", "按住鼠标自由勾画。"),
            ("note", "文字批注", "单击或拖出文本框，添加浮在 PDF 上的文字。"),
            ("delete", "橡皮擦", "单击已有批注将其擦除。"),
        ):
            button = QPushButton(label)
            button.setObjectName("TinyButton")
            button.setCheckable(True)
            button.setToolTip(tooltip)
            button.clicked.connect(
                lambda _checked=False, value=tool: self.toggle_annotation_tool(value)
            )
            self.annotation_group.addButton(button)
            self.annotation_buttons[tool] = button
            annotation_bar.addWidget(button)
        self.annotation_buttons["browse"].setChecked(True)
        self.highlight_color = QColor("#ffe04b")
        self.note_color = QColor("#2563eb")
        self.highlight_color_button = QPushButton("高亮色")
        self.highlight_color_button.setObjectName("TinyButton")
        self.highlight_color_button.clicked.connect(self.choose_highlight_color)
        self.note_color_button = QPushButton("批注色")
        self.note_color_button.setObjectName("TinyButton")
        self.note_color_button.clicked.connect(self.choose_note_color)
        self._update_color_buttons()
        self.save_button = QPushButton("保存 PDF")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.setToolTip("保存当前版本的 PDF 批注（Ctrl+S）。")
        self.save_button.clicked.connect(self.save_annotations)
        self.annotation_status = QLabel("PDF 批注已保存")
        self.annotation_status.setObjectName("MutedText")
        annotation_bar.addWidget(self.annotation_status)
        annotation_bar.addWidget(self.highlight_color_button)
        annotation_bar.addWidget(self.note_color_button)
        annotation_bar.addWidget(self.save_button)

        search_bar = FlowLayout(spacing=6)
        self.search_edit = QLineEdit()
        self.search_edit.setMinimumWidth(260)
        self.search_edit.setPlaceholderText("在 PDF 全文中查找")
        self.search_edit.returnPressed.connect(self.find_text)
        self.search_button = QPushButton("查找")
        self.search_button.setObjectName("TinyButton")
        self.search_button.clicked.connect(self.find_text)
        self.previous_match_button = QPushButton("上一个")
        self.previous_match_button.setObjectName("TinyButton")
        self.previous_match_button.clicked.connect(self.previous_match)
        self.next_match_button = QPushButton("下一个")
        self.next_match_button.setObjectName("TinyButton")
        self.next_match_button.clicked.connect(self.next_match)
        self.copy_text_button = QPushButton("复制本页文本")
        self.copy_text_button.setObjectName("TinyButton")
        self.copy_text_button.clicked.connect(self.copy_current_page_text)
        self.search_status = QLabel("")
        self.search_status.setObjectName("MutedText")
        search_bar.addWidget(self.search_edit)
        search_bar.addWidget(self.search_button)
        search_bar.addWidget(self.previous_match_button)
        search_bar.addWidget(self.next_match_button)
        search_bar.addWidget(self.copy_text_button)
        search_bar.addWidget(self.search_status)

        self.pages_container = QWidget()
        self.pages_container.setObjectName("ReaderPagesContainer")
        self.pages_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self.pages_layout = QVBoxLayout(self.pages_container)
        self.pages_layout.setContentsMargins(24, 16, 24, 24)
        self.pages_layout.setSpacing(18)
        self.pages_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("ReaderPdfScroll")
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll.setWidget(self.pages_container)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.scroll.viewport().installEventFilter(self)

        self.zoom_in_shortcut = QShortcut(QKeySequence.StandardKey.ZoomIn, self)
        self.zoom_in_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.zoom_in_shortcut.activated.connect(lambda: self.change_zoom(0.15))
        self.zoom_out_shortcut = QShortcut(QKeySequence.StandardKey.ZoomOut, self)
        self.zoom_out_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.zoom_out_shortcut.activated.connect(lambda: self.change_zoom(-0.15))
        self.reset_zoom_shortcut = QShortcut(QKeySequence("Ctrl+0"), self)
        self.reset_zoom_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.reset_zoom_shortcut.activated.connect(self.reset_zoom)

        root.addLayout(toolbar)
        root.addLayout(annotation_bar)
        root.addLayout(search_bar)
        root.addWidget(self.scroll, 1)
        self._update_controls()

    @property
    def pdf_path(self) -> Path | None:
        return self._paths.get(self._active_mode)

    @property
    def has_unsaved_annotations(self) -> bool:
        return bool(self._dirty_modes)

    def set_pdf_path(self, value: str) -> None:
        path = Path(value.strip()) if value.strip() else None
        self.set_documents(original=path)

    def set_documents(
        self,
        *,
        original: Path | None,
        mono: Path | None = None,
        dual: Path | None = None,
    ) -> None:
        self.close_documents()
        self._paths = {
            "original": original if original and original.exists() else None,
            "mono": mono if mono and mono.exists() else None,
            "dual": dual if dual and dual.exists() else None,
        }
        self._dirty_modes.clear()
        self._search_pages = []
        self._search_position = -1
        self._clear_text_selection(render=False)
        self.search_status.clear()
        self.annotationDirtyChanged.emit(False)
        available_mode = next((key for key, _label in self.MODES if self._paths[key]), "original")
        self._active_mode = available_mode
        self.mode_buttons[available_mode].setChecked(True)
        self.page_index = 0
        self._rebuild_pages()

    def close_documents(self) -> None:
        for document in self._documents.values():
            document.close()
        self._documents.clear()

    def _document(self, mode: str | None = None) -> fitz.Document | None:
        key = mode or self._active_mode
        path = self._paths.get(key)
        if path is None:
            return None
        if key not in self._documents:
            try:
                self._documents[key] = fitz.open(path)
            except Exception as exc:
                self.statusMessage.emit(f"PDF 打开失败：{exc}")
                return None
        return self._documents[key]

    def _change_mode(self, requested: str) -> None:
        if self._rebuilding:
            return
        if self._paths.get(requested) is None:
            label = dict(self.MODES).get(requested, requested)
            self.statusMessage.emit(f"当前文献还没有{label} PDF")
            self.mode_buttons[self._active_mode].setChecked(True)
            return
        shared_page = self.current_visible_page()
        self._active_mode = requested
        self.mode_buttons[requested].setChecked(True)
        self.page_index = shared_page
        self._rebuild_pages()
        QTimer.singleShot(0, lambda: self.jump_to_page(shared_page))

    def _clear_page_widgets(self) -> None:
        while self.pages_layout.count():
            item = self.pages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._page_labels.clear()

    def _rebuild_pages(self) -> None:
        self._rebuilding = True
        self._clear_page_widgets()
        document = self._document()
        self.page_count = document.page_count if document is not None else 0
        if document is None or not self.page_count:
            empty = QLabel("当前版本没有可阅读的 PDF。")
            empty.setObjectName("MutedText")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(480)
            self.pages_layout.addWidget(empty)
            self.page_index = 0
        else:
            self.page_index = max(0, min(self.page_index, self.page_count - 1))
            for index in range(self.page_count):
                page = document.load_page(index)
                label = ReaderPageLabel(index)
                label.set_annotation_tool(self._annotation_tool)
                label.annotationGesture.connect(self._apply_annotation_gesture)
                label.annotationDoubleClicked.connect(self._edit_annotation_at)
                label.selectionGesture.connect(self._apply_text_selection)
                label.selectionContextRequested.connect(self._show_selection_menu)
                label.setFixedSize(self._page_pixel_size(page))
                self.pages_layout.addWidget(label, 0, Qt.AlignmentFlag.AlignHCenter)
                self._page_labels.append(label)
        self._rebuilding = False
        self.pages_container.adjustSize()
        self._update_controls()
        QTimer.singleShot(0, self._render_visible_pages)
        QTimer.singleShot(0, lambda: self.jump_to_page(self.page_index))
        self.annotationsChanged.emit()

    def _page_pixel_size(self, page: fitz.Page) -> QSize:
        rectangle = page.rect
        return QSize(
            max(1, round(rectangle.width * self.zoom)),
            max(1, round(rectangle.height * self.zoom)),
        )

    def _render_page(self, page_index: int) -> None:
        document = self._document()
        if document is None or not 0 <= page_index < len(self._page_labels):
            return
        label = self._page_labels[page_index]
        if abs(label.rendered_zoom - self.zoom) < 0.001 and not label.pixmap().isNull():
            return
        try:
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(self.zoom, self.zoom),
                alpha=False,
                annots=True,
            )
            from PySide6.QtGui import QImage

            image = QImage(
                pixmap.samples,
                pixmap.width,
                pixmap.height,
                pixmap.stride,
                QImage.Format.Format_RGB888,
            ).copy()
            query = self.search_edit.text().strip()
            if query:
                matrix = fitz.Matrix(self.zoom, self.zoom)
                bbox = page.rect * matrix
                painter = QPainter(image)
                painter.setPen(QPen(QColor(214, 158, 0, 180), 1))
                painter.setBrush(QColor(255, 220, 45, 95))
                for rectangle in page.search_for(query):
                    transformed = rectangle * matrix
                    painter.drawRect(
                        QRectF(
                            transformed.x0 - bbox.x0,
                            transformed.y0 - bbox.y0,
                            transformed.width,
                            transformed.height,
                        )
                    )
                painter.end()
            if page_index == self._selected_page and self._selected_rects:
                matrix = fitz.Matrix(self.zoom, self.zoom)
                bbox = page.rect * matrix
                painter = QPainter(image)
                painter.setPen(QPen(QColor(37, 99, 235, 190), 1))
                painter.setBrush(QColor(59, 130, 246, 70))
                for rectangle in self._selected_rects:
                    transformed = rectangle * matrix
                    painter.drawRect(
                        QRectF(
                            transformed.x0 - bbox.x0,
                            transformed.y0 - bbox.y0,
                            transformed.width,
                            transformed.height,
                        )
                    )
                painter.end()
        except Exception as exc:
            label.setText(f"第 {page_index + 1} 页渲染失败：{exc}")
            return
        label.setPixmap(QPixmap.fromImage(image))
        label.setFixedSize(image.size())
        label.rendered_zoom = self.zoom

    def _render_visible_pages(self) -> None:
        if not self._page_labels:
            return
        viewport_top = self.scroll.verticalScrollBar().value()
        viewport_height = max(1, self.scroll.viewport().height())
        render_top = viewport_top - viewport_height
        render_bottom = viewport_top + viewport_height * 2
        retain_top = viewport_top - viewport_height * 2
        retain_bottom = viewport_top + viewport_height * 3
        rendered_any = False
        for label in self._page_labels:
            top = label.mapTo(self.pages_container, QPoint(0, 0)).y()
            bottom = top + label.height()
            if bottom >= render_top and top <= render_bottom:
                self._render_page(label.page_index)
                rendered_any = True
            elif (bottom < retain_top or top > retain_bottom) and not label.pixmap().isNull():
                label.clear_render()
        if not rendered_any:
            self._render_page(self.page_index)

    def _on_scroll(self, _value: int) -> None:
        if self._rebuilding:
            return
        self.page_index = self.current_visible_page()
        self._update_page_label()
        self.pageChanged.emit(self.page_index)
        QTimer.singleShot(0, self._render_visible_pages)

    def current_visible_page(self) -> int:
        if not self._page_labels:
            return 0
        center = self.scroll.verticalScrollBar().value() + self.scroll.viewport().height() // 2
        best = min(
            self._page_labels,
            key=lambda label: abs(
                label.mapTo(self.pages_container, QPoint(0, 0)).y()
                + label.height() // 2
                - center
            ),
        )
        return best.page_index

    def jump_to_page(self, page_index: int) -> None:
        if not self._page_labels:
            return
        target = max(0, min(page_index, len(self._page_labels) - 1))
        self.page_index = target
        label = self._page_labels[target]
        top = label.mapTo(self.pages_container, QPoint(0, 0)).y()
        # 页高小于视口时把目标页居中：滚动回调用"视口中心所在页"判定当前页，
        # 只贴顶会导致跳转后页码被回读成相邻页。
        margin = max(0, (self.scroll.viewport().height() - label.height()) // 2)
        self.scroll.verticalScrollBar().setValue(max(0, top - margin))
        self._render_page(target)
        self._update_page_label()

    def render_page(self) -> None:
        self._render_page(self.page_index)

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self.scroll.viewport()
            and event.type() == QEvent.Type.Wheel
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.change_zoom(0.15 if event.angleDelta().y() > 0 else -0.15)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def change_zoom(self, delta: float) -> None:
        self._set_zoom(self.zoom + delta)

    def reset_zoom(self) -> None:
        self._set_zoom(1.0)

    def fit_width(self) -> None:
        document = self._document()
        if document is None or not self.page_count:
            return
        page = document.load_page(max(0, min(self.page_index, self.page_count - 1)))
        available = max(320, self.scroll.viewport().width() - 64)
        self._set_zoom(available / max(1.0, page.rect.width))

    def _set_zoom(self, value: float) -> None:
        new_zoom = max(0.5, min(3.0, round(value, 2)))
        if abs(new_zoom - self.zoom) < 0.001:
            return
        shared_page = self.current_visible_page()
        self.zoom = new_zoom
        document = self._document()
        if document is not None:
            for index, label in enumerate(self._page_labels):
                label.clear_render()
                label.setFixedSize(self._page_pixel_size(document.load_page(index)))
        self.pages_container.adjustSize()
        self._update_controls()
        QTimer.singleShot(0, self._render_visible_pages)
        QTimer.singleShot(0, lambda: self.jump_to_page(shared_page))

    def choose_highlight_color(self) -> None:
        color = QColorDialog.getColor(
            self.highlight_color,
            self,
            "选择高亮颜色",
        )
        if color.isValid():
            self.highlight_color = color
            self._update_color_buttons()

    def choose_note_color(self) -> None:
        color = QColorDialog.getColor(
            self.note_color,
            self,
            "选择批注文字颜色",
        )
        if color.isValid():
            self.note_color = color
            self._update_color_buttons()

    def _update_color_buttons(self) -> None:
        for button, color in (
            (self.highlight_color_button, self.highlight_color),
            (self.note_color_button, self.note_color),
        ):
            foreground = "#111827" if color.lightness() > 145 else "#ffffff"
            button.setStyleSheet(
                f"background: {color.name()}; color: {foreground};"
                "border: 1px solid rgba(15, 23, 42, 0.22);"
            )

    @staticmethod
    def _color_tuple(color: QColor) -> tuple[float, float, float]:
        return (color.redF(), color.greenF(), color.blueF())

    def toggle_annotation_tool(self, tool: str) -> None:
        if tool == self._annotation_tool and tool != "browse":
            self.set_annotation_tool("browse")
        else:
            self.set_annotation_tool(tool)

    def set_annotation_tool(self, tool: str) -> None:
        if tool not in self.annotation_buttons:
            tool = "browse"
        self._annotation_tool = tool
        for label in self._page_labels:
            label.set_annotation_tool(tool)
        for name, button in self.annotation_buttons.items():
            button.blockSignals(True)
            button.setChecked(name == tool)
            button.blockSignals(False)
        hints = {
            "browse": "浏览模式：滑轮上下连续滚动",
            "highlight": "拖动鼠标添加高亮",
            "pen": "按住鼠标自由勾画",
            "note": "单击或拖出区域，添加浮动文字批注",
            "delete": "用橡皮擦单击已有批注",
        }
        self.annotation_status.setText(hints.get(tool, ""))

    def _canvas_to_page_point(
        self,
        page: fitz.Page,
        position: QPointF,
    ) -> fitz.Point:
        matrix = fitz.Matrix(self.zoom, self.zoom)
        bbox = page.rect * matrix
        transformed = fitz.Point(position.x() + bbox.x0, position.y() + bbox.y0)
        return transformed * ~matrix


    def _clear_text_selection(self, *, render: bool = True) -> None:
        previous_page = self._selected_page
        self._selected_page = -1
        self._selected_text = ""
        self._selected_rects = []
        if render and 0 <= previous_page < len(self._page_labels):
            self._page_labels[previous_page].rendered_zoom = 0.0
            self._render_page(previous_page)

    def _apply_text_selection(
        self,
        page_index: int,
        canvas_points: list[QPointF],
    ) -> None:
        document = self._document()
        if document is None or len(canvas_points) < 2:
            self._clear_text_selection()
            return
        page = document.load_page(page_index)
        points = [self._canvas_to_page_point(page, point) for point in canvas_points]
        selection = fitz.Rect(points[0], points[-1]).normalize()
        if selection.width < 2 or selection.height < 2:
            self._clear_text_selection()
            return
        selected = []
        for word in page.get_text("words"):
            rectangle = fitz.Rect(word[:4])
            if rectangle.intersects(selection):
                selected.append((int(word[5]), int(word[6]), int(word[7]), str(word[4]), rectangle))
        selected.sort(key=lambda item: item[:3])
        if not selected:
            self._clear_text_selection()
            self.annotation_status.setText("选区内没有可复制文字")
            return
        parts: list[str] = []
        previous_line: tuple[int, int] | None = None
        for block, line, _word, text, _rectangle in selected:
            current_line = (block, line)
            if parts and current_line != previous_line:
                parts.append("\n")
            elif parts:
                parts.append(" ")
            parts.append(text)
            previous_line = current_line
        old_page = self._selected_page
        self._selected_page = page_index
        self._selected_text = "".join(parts).strip()
        self._selected_rects = [item[4] for item in selected]
        if 0 <= old_page < len(self._page_labels) and old_page != page_index:
            self._page_labels[old_page].rendered_zoom = 0.0
            self._render_page(old_page)
        self._page_labels[page_index].rendered_zoom = 0.0
        self._render_page(page_index)
        self.annotation_status.setText(f"已选择 {len(self._selected_text)} 个字符；右键可复制或高亮")

    def copy_selected_text(self) -> None:
        if self._selected_text:
            QApplication.clipboard().setText(self._selected_text)
            self.annotation_status.setText("选中文字已复制")

    def highlight_selected_text(self) -> None:
        document = self._document()
        if document is None or self._selected_page < 0 or not self._selected_rects:
            return
        page = document.load_page(self._selected_page)
        try:
            annotation = page.add_highlight_annot(self._selected_rects)
            annotation.set_colors(stroke=self._color_tuple(self.highlight_color))
            annotation.set_opacity(0.38)
            annotation.update()
        except Exception as exc:
            self.statusMessage.emit(f"高亮选中文字失败：{exc}")
            return
        page_index = self._selected_page
        self._clear_text_selection(render=False)
        self._mark_document_dirty(page_index)

    def add_note_to_selection(self) -> None:
        document = self._document()
        if document is None or self._selected_page < 0 or not self._selected_rects:
            return
        page_index = self._selected_page
        page = document.load_page(page_index)
        union = fitz.Rect(self._selected_rects[0])
        for rectangle in self._selected_rects[1:]:
            union |= rectangle
        width = max(180.0, union.width)
        rectangle = fitz.Rect(
            union.x0,
            min(page.rect.y1 - 64, union.y1 + 6),
            min(page.rect.x1, union.x0 + width),
            min(page.rect.y1, union.y1 + 70),
        )
        text, accepted = QInputDialog.getMultiLineText(
            self, "添加 PDF 批注", "批注内容："
        )
        if not accepted or not text.strip():
            return
        annotation = page.add_freetext_annot(
            rectangle,
            text.strip(),
            fontsize=11,
            fontname="china-s",
            text_color=self._color_tuple(self.note_color),
            fill_color=None,
            align=0,
        )
        annotation.set_opacity(0.94)
        annotation.update()
        self._clear_text_selection(render=False)
        self._mark_document_dirty(page_index)

    def _show_selection_menu(self, page_index: int, canvas_position: QPoint) -> None:
        if page_index != self._selected_page or not self._selected_text:
            self.annotation_status.setText("先用左键拖动选择 PDF 文字")
            return
        menu = QMenu(self)
        copy_action = menu.addAction("复制")
        highlight_action = menu.addAction("高亮选中文字")
        note_action = menu.addAction("为选中文字添加批注")
        search_action = menu.addAction("在全文中查找此文本")
        menu.addSeparator()
        clear_action = menu.addAction("清除选择")
        label = self._page_labels[page_index]
        chosen = menu.exec(label.mapToGlobal(canvas_position))
        if chosen is copy_action:
            self.copy_selected_text()
        elif chosen is highlight_action:
            self.highlight_selected_text()
        elif chosen is note_action:
            self.add_note_to_selection()
        elif chosen is search_action:
            self.search_edit.setText(self._selected_text[:120].replace("\n", " "))
            self.find_text()
        elif chosen is clear_action:
            self._clear_text_selection()

    def _annotation_at(self, page: fitz.Page, point: fitz.Point):
        tolerance = max(4.0, 9.0 / max(self.zoom, 0.1))
        for annotation in list(page.annots() or []):
            hit_box = fitz.Rect(annotation.rect)
            hit_box.x0 -= tolerance
            hit_box.y0 -= tolerance
            hit_box.x1 += tolerance
            hit_box.y1 += tolerance
            if point in hit_box:
                return annotation
        return None

    def _edit_annotation_at(self, page_index: int, canvas_position: QPointF) -> None:
        document = self._document()
        if document is None:
            return
        page = document.load_page(page_index)
        point = self._canvas_to_page_point(page, canvas_position)
        annotation = self._annotation_at(page, point)
        if annotation is None:
            self.annotation_status.setText("双击位置没有批注")
            return
        action = ConfirmationDialog(
            window_title="修改批注",
            title="如何处理这条 PDF 批注？",
            message="你可以继续编辑批注内容，或将它从当前 PDF 中移除。",
            detail="删除会先保留在当前编辑会话中，保存 PDF 后才会写入文件。",
            eyebrow="PDF 批注",
            tone="question",
            actions=(
                DialogAction("cancel", "取消"),
                DialogAction("delete", "删除批注", "danger-soft"),
                DialogAction("modify", "修改批注", "primary", default=True),
            ),
            parent=self,
        ).exec_action()
        if action == "delete":
            page.delete_annot(annotation)
            self._mark_document_dirty(page_index)
            return
        if action != "modify":
            return
        if annotation.type[1] == "FreeText":
            current = str(annotation.info.get("content") or "")
            text, accepted = QInputDialog.getMultiLineText(
                self, "修改文字批注", "批注内容：", current
            )
            if not accepted or not text.strip():
                return
            rectangle = fitz.Rect(annotation.rect)
            page.delete_annot(annotation)
            replacement = page.add_freetext_annot(
                rectangle,
                text.strip(),
                fontsize=11,
                fontname="china-s",
                text_color=self._color_tuple(self.note_color),
                fill_color=None,
                align=0,
            )
            replacement.set_opacity(0.94)
            replacement.update()
        else:
            initial = self.highlight_color if annotation.type[1] == "Highlight" else QColor("#d64242")
            color = QColorDialog.getColor(initial, self, "修改批注颜色")
            if not color.isValid():
                return
            annotation.set_colors(stroke=self._color_tuple(color))
            annotation.update()
        self._mark_document_dirty(page_index)

    def _mark_document_dirty(self, page_index: int) -> None:
        self._dirty_modes.add(self._active_mode)
        self.annotationDirtyChanged.emit(True)
        self.annotation_status.setText("PDF 有未保存批注")
        self.annotationsChanged.emit()
        if 0 <= page_index < len(self._page_labels):
            self._page_labels[page_index].rendered_zoom = 0.0
            self._render_page(page_index)
        self._update_controls()

    def annotations_summary(self) -> list[dict]:
        """当前文档全部批注摘要,按页排序。

        返回 [{page: 从 1 起, kind: Highlight/FreeText/Ink, text: 批注内容或高亮覆盖的文字}]。
        """
        document = self._document()
        if document is None:
            return []
        items: list[dict] = []
        try:
            for index in range(document.page_count):
                page = document.load_page(index)
                for annot in page.annots() or []:
                    kind = annot.type[1]
                    content = str((annot.info or {}).get("content") or "").strip()
                    if kind == "Highlight":
                        try:
                            covered = " ".join(page.get_textbox(annot.rect).split())
                        except Exception:
                            covered = ""
                        text = content or covered or "(高亮区域)"
                    else:
                        text = content or ("(手绘批注)" if kind == "Ink" else f"({kind})")
                    items.append({"page": index + 1, "kind": kind, "text": text})
        except Exception as exc:
            self.statusMessage.emit(f"读取 PDF 批注失败：{exc}")
            return []
        return items

    def find_text(self) -> None:
        query = self.search_edit.text().strip()
        self._search_pages = []
        self._search_position = -1
        document = self._document()
        if not query or document is None:
            self.search_status.clear()
            self._invalidate_rendered_pages()
            self._update_controls()
            return
        try:
            self._search_pages = [
                index
                for index in range(document.page_count)
                if document.load_page(index).search_for(query)
            ]
        except Exception as exc:
            self.search_status.setText(f"查找失败：{exc}")
            return
        self._invalidate_rendered_pages()
        if not self._search_pages:
            self.search_status.setText("没有匹配")
            self._update_controls()
            return
        self._search_position = 0
        self._show_search_position()
        self._update_controls()

    def next_match(self) -> None:
        if not self._search_pages:
            return
        self._search_position = (self._search_position + 1) % len(self._search_pages)
        self._show_search_position()

    def previous_match(self) -> None:
        if not self._search_pages:
            return
        self._search_position = (self._search_position - 1) % len(self._search_pages)
        self._show_search_position()

    def _show_search_position(self) -> None:
        page = self._search_pages[self._search_position]
        self.search_status.setText(
            f"{self._search_position + 1} / {len(self._search_pages)} 个匹配页"
        )
        self.jump_to_page(page)

    def _invalidate_rendered_pages(self) -> None:
        for label in self._page_labels:
            label.rendered_zoom = 0.0
        self._render_visible_pages()

    def copy_current_page_text(self) -> None:
        document = self._document()
        if document is None or not self.page_count:
            return
        text = document.load_page(self.current_visible_page()).get_text("text").strip()
        if text:
            QApplication.clipboard().setText(text)
            self.search_status.setText("本页文本已复制")

    def _apply_annotation_gesture(
        self,
        page_index: int,
        tool: str,
        canvas_points: list[QPointF],
    ) -> None:
        document = self._document()
        if document is None or not canvas_points:
            return
        page = document.load_page(page_index)
        points = [self._canvas_to_page_point(page, point) for point in canvas_points]
        try:
            if tool == "highlight":
                rectangle = fitz.Rect(points[0], points[-1]).normalize()
                if rectangle.width < 2 or rectangle.height < 2:
                    return
                annotation = page.add_highlight_annot(rectangle)
                annotation.set_colors(stroke=self._color_tuple(self.highlight_color))
                annotation.set_opacity(0.38)
                annotation.update()
            elif tool == "pen":
                if len(points) < 2:
                    return
                ink_paths = [[[float(point.x), float(point.y)] for point in points]]
                annotation = page.add_ink_annot(ink_paths)
                annotation.set_colors(stroke=(0.84, 0.18, 0.18))
                annotation.set_border(width=1.6)
                annotation.set_opacity(0.9)
                annotation.update()
            elif tool == "note":
                text, accepted = QInputDialog.getMultiLineText(
                    self,
                    "添加 PDF 批注",
                    "批注内容：",
                )
                if not accepted or not text.strip():
                    return
                rectangle = fitz.Rect(points[0], points[-1]).normalize()
                if rectangle.width < 24 or rectangle.height < 18:
                    anchor = points[0]
                    rectangle = fitz.Rect(
                        anchor.x,
                        anchor.y,
                        min(page.rect.x1, anchor.x + 220),
                        min(page.rect.y1, anchor.y + 72),
                    )
                color = self._color_tuple(self.note_color)
                annotation = page.add_freetext_annot(
                    rectangle,
                    text.strip(),
                    fontsize=11,
                    fontname="china-s",
                    text_color=color,
                    fill_color=None,
                    align=0,
                )
                annotation.set_opacity(0.94)
                annotation.update()
            elif tool == "delete":
                target = points[0]
                tolerance = max(4.0, 9.0 / max(self.zoom, 0.1))
                for annotation in list(page.annots() or []):
                    hit_box = fitz.Rect(annotation.rect)
                    hit_box.x0 -= tolerance
                    hit_box.y0 -= tolerance
                    hit_box.x1 += tolerance
                    hit_box.y1 += tolerance
                    if target in hit_box:
                        page.delete_annot(annotation)
                        break
                else:
                    self.annotation_status.setText("这里没有可删除的批注")
                    return
            else:
                return
        except Exception as exc:
            self.annotation_status.setText(f"批注失败：{exc}")
            self.statusMessage.emit(f"PDF 批注失败：{exc}")
            return
        self._mark_document_dirty(page_index)

    def save_annotations(self) -> bool:
        mode = self._active_mode
        if mode not in self._dirty_modes:
            self.annotation_status.setText("PDF 批注已保存")
            return True
        document = self._document(mode)
        path = self._paths.get(mode)
        if document is None or path is None:
            return False
        # 按用户要求:批注直接写入原文,只保留带痕迹的版本,不再备份原版 PDF。
        try:
            try:
                document.saveIncr()
            except Exception:
                # 被修复过(repaired)的 PDF 不允许增量写,回退为整体重存并原子替换,
                # 顺带把文件修回可继续增量写的干净结构。
                tmp_path = path.with_name(f"{path.stem}.saving.tmp")
                try:
                    document.save(str(tmp_path), garbage=3, deflate=True)
                    document.close()
                    try:
                        tmp_path.replace(path)
                    except PermissionError:
                        # Windows:仍被其他句柄占用时,rename 会被拒,退化为原地覆写。
                        shutil.copyfile(tmp_path, path)
                finally:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._documents[mode] = fitz.open(path)
        except Exception as exc:
            self.annotation_status.setText(f"保存失败：{exc}")
            self.statusMessage.emit(f"PDF 批注保存失败：{exc}")
            return False
        self._dirty_modes.discard(mode)
        self.annotationDirtyChanged.emit(bool(self._dirty_modes))
        self.annotation_status.setText("PDF 批注已保存")
        message = f"{dict(self.MODES)[mode]} PDF 批注已保存"
        self.statusMessage.emit(message)
        self._update_controls()
        self.annotationsChanged.emit()
        return True

    def save_all_annotations(self) -> bool:
        active_mode = self._active_mode
        for mode in list(self._dirty_modes):
            self._active_mode = mode
            if not self.save_annotations():
                self._active_mode = active_mode
                return False
        self._active_mode = active_mode
        self._update_controls()
        return True

    def discard_annotations(self) -> None:
        modes = list(self._dirty_modes)
        for mode in modes:
            document = self._documents.pop(mode, None)
            if document is not None:
                document.close()
        self._dirty_modes.clear()
        self.annotationDirtyChanged.emit(False)
        self.annotation_status.setText("已放弃未保存的 PDF 批注")
        self._rebuild_pages()
        self.annotationsChanged.emit()

    def _update_page_label(self) -> None:
        if not self.page_count:
            self.page_label.setText("未加载 PDF")
            self.page_spin.setRange(1, 1)
            self._resize_page_spin()
            self.page_spin.setEnabled(False)
            return
        mode_name = dict(self.MODES).get(self._active_mode, "")
        self.page_label.setText(
            f"{mode_name} · 第 {self.page_index + 1} / {self.page_count} 页"
        )
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1, self.page_count)
        self.page_spin.setValue(self.page_index + 1)
        self.page_spin.blockSignals(False)
        self._resize_page_spin()
        self.page_spin.setEnabled(True)

    def _resize_page_spin(self) -> None:
        # 前缀“第 ”+后缀“ 页”随字号占宽，固定窄宽会截断文字和箭头；
        # 按当前页数范围的最宽取值自适应。
        self.page_spin.setFixedWidth(self.page_spin.sizeHint().width() + 10)

    def _go_to_spin_page(self) -> None:
        if not self.page_count:
            return
        target = max(0, min(self.page_spin.value() - 1, self.page_count - 1))
        if target != self.page_index:
            self.jump_to_page(target)

    def _update_controls(self) -> None:
        has_pdf = self.page_count > 0
        self.zoom_out_button.setEnabled(has_pdf and self.zoom > 0.5)
        self.zoom_in_button.setEnabled(has_pdf and self.zoom < 3.0)
        self.reset_zoom_button.setEnabled(has_pdf and abs(self.zoom - 1.0) > 0.001)
        self.fit_width_button.setEnabled(has_pdf)
        self.translate_button.setEnabled(has_pdf)
        self.save_button.setEnabled(self._active_mode in self._dirty_modes)
        self.copy_text_button.setEnabled(has_pdf)
        has_matches = bool(self._search_pages)
        self.previous_match_button.setEnabled(has_matches)
        self.next_match_button.setEnabled(has_matches)
        self.zoom_label.setText(f"{self.zoom * 100:.0f}%")
        self._update_page_label()
        for mode, _label in self.MODES:
            button = self.mode_buttons[mode]
            button.setEnabled(self._paths.get(mode) is not None)
            button.setChecked(mode == self._active_mode)
