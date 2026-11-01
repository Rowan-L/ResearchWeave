"""PDF 页面渲染预览控件(E1 从 main_window 拆出,行为不变)。"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from synthrecall.ui.widgets import NoWheelSpinBox


class PDFPreviewWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.pdf_path: Path | None = None
        self.page_index = 0
        self.page_count = 0
        self.zoom = 1.45
        self.rotation = 0
        self.search_pages: list[int] = []
        self.search_position = -1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.prev_button = QPushButton("上一页")
        self.prev_button.setObjectName("TinyButton")
        self.prev_button.setToolTip("查看当前 PDF 预览的上一页。")
        self.prev_button.clicked.connect(self.previous_page)
        self.next_button = QPushButton("下一页")
        self.next_button.setObjectName("TinyButton")
        self.next_button.setToolTip("查看当前 PDF 预览的下一页。")
        self.next_button.clicked.connect(self.next_page)
        self.page_spin = NoWheelSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setFixedWidth(72)
        self.page_spin.setToolTip("输入页码并跳转。")
        self.page_spin.valueChanged.connect(self.go_to_page)
        self.zoom_out_button = QPushButton("−")
        self.zoom_out_button.setObjectName("TinyButton")
        self.zoom_out_button.setToolTip("缩小 PDF 预览。")
        self.zoom_out_button.clicked.connect(lambda: self.change_zoom(-0.2))
        self.zoom_in_button = QPushButton("+")
        self.zoom_in_button.setObjectName("TinyButton")
        self.zoom_in_button.setToolTip("放大 PDF 预览。")
        self.zoom_in_button.clicked.connect(lambda: self.change_zoom(0.2))
        self.fit_width_button = QPushButton("适合宽度")
        self.fit_width_button.setObjectName("TinyButton")
        self.fit_width_button.clicked.connect(self.fit_width)
        self.rotate_button = QPushButton("旋转")
        self.rotate_button.setObjectName("TinyButton")
        self.rotate_button.setToolTip("顺时针旋转当前 PDF 预览 90°。")
        self.rotate_button.clicked.connect(self.rotate_clockwise)
        self.copy_text_button = QPushButton("复制本页文本")
        self.copy_text_button.setObjectName("TinyButton")
        self.copy_text_button.clicked.connect(self.copy_current_page_text)
        self.zoom_label = QLabel("145%")
        self.zoom_label.setObjectName("MutedText")
        self.page_label = QLabel("未加载 PDF")
        self.page_label.setObjectName("MutedText")
        toolbar.addWidget(self.prev_button)
        toolbar.addWidget(self.next_button)
        toolbar.addWidget(self.page_spin)
        toolbar.addWidget(self.zoom_out_button)
        toolbar.addWidget(self.zoom_in_button)
        toolbar.addWidget(self.fit_width_button)
        toolbar.addWidget(self.rotate_button)
        toolbar.addWidget(self.copy_text_button)
        toolbar.addWidget(self.zoom_label)
        toolbar.addWidget(self.page_label, 1)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.search_edit = QLineEdit()
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
        self.search_status = QLabel("")
        self.search_status.setObjectName("MutedText")
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.search_button)
        search_row.addWidget(self.previous_match_button)
        search_row.addWidget(self.next_match_button)
        search_row.addWidget(self.search_status)

        self.image_label = QLabel("当前文献没有可预览的 PDF。")
        self.image_label.setObjectName("PdfPreview")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(420, 520)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(self.image_label)

        layout.addLayout(toolbar)
        layout.addLayout(search_row)
        layout.addWidget(self.scroll, 1)
        self._update_buttons()

    def set_pdf_path(self, value: str) -> None:
        path = Path(value.strip()) if value.strip() else None
        if path == self.pdf_path:
            return
        self.pdf_path = path
        self.page_index = 0
        self.rotation = 0
        self.search_pages = []
        self.search_position = -1
        self.search_status.setText("")
        self.render_page()

    def previous_page(self) -> None:
        if self.page_index > 0:
            self.page_index -= 1
            self.render_page()

    def next_page(self) -> None:
        if self.page_index + 1 < self.page_count:
            self.page_index += 1
            self.render_page()

    def go_to_page(self, page_number: int) -> None:
        if not self.page_count:
            return
        target = max(0, min(page_number - 1, self.page_count - 1))
        if target == self.page_index:
            return
        self.page_index = target
        self.render_page()

    def change_zoom(self, delta: float) -> None:
        self.zoom = max(0.5, min(3.0, round(self.zoom + delta, 2)))
        self.render_page()

    def fit_width(self) -> None:
        if self.pdf_path is None or not self.pdf_path.exists():
            return
        try:
            with fitz.open(self.pdf_path) as document:
                page = document.load_page(self.page_index)
                page_width = page.rect.height if self.rotation in {90, 270} else page.rect.width
        except Exception:
            return
        available_width = max(320, self.scroll.viewport().width() - 24)
        self.zoom = max(0.5, min(3.0, available_width / max(1, page_width)))
        self.render_page()

    def rotate_clockwise(self) -> None:
        self.rotation = (self.rotation + 90) % 360
        self.render_page()

    def find_text(self) -> None:
        query = self.search_edit.text().strip()
        self.search_pages = []
        self.search_position = -1
        if not query or self.pdf_path is None or not self.pdf_path.exists():
            self.search_status.setText("")
            self._update_buttons()
            self.render_page()
            return
        try:
            with fitz.open(self.pdf_path) as document:
                self.search_pages = [
                    page_index
                    for page_index, page in enumerate(document)
                    if page.search_for(query)
                ]
        except Exception as exc:
            self.search_status.setText(f"查找失败：{exc}")
            return
        if not self.search_pages:
            self.search_status.setText("没有匹配")
            self.render_page()
            return
        self.search_position = 0
        self.page_index = self.search_pages[0]
        self.search_status.setText(f"1 / {len(self.search_pages)} 个匹配页")
        self.render_page()

    def next_match(self) -> None:
        if not self.search_pages:
            return
        self.search_position = (self.search_position + 1) % len(self.search_pages)
        self._show_search_position()

    def previous_match(self) -> None:
        if not self.search_pages:
            return
        self.search_position = (self.search_position - 1) % len(self.search_pages)
        self._show_search_position()

    def _show_search_position(self) -> None:
        self.page_index = self.search_pages[self.search_position]
        self.search_status.setText(
            f"{self.search_position + 1} / {len(self.search_pages)} 个匹配页"
        )
        self.render_page()

    def copy_current_page_text(self) -> None:
        if self.pdf_path is None or not self.pdf_path.exists():
            return
        try:
            with fitz.open(self.pdf_path) as document:
                text = document.load_page(self.page_index).get_text("text").strip()
        except Exception:
            return
        if text:
            QApplication.clipboard().setText(text)
            self.search_status.setText("本页文本已复制")

    def render_page(self) -> None:
        if self.pdf_path is None or not self.pdf_path.exists():
            self.page_count = 0
            self.image_label.setText("当前文献没有可预览的 PDF。")
            self.image_label.setPixmap(QPixmap())
            self.page_label.setText("未加载 PDF")
            self._update_buttons()
            return
        try:
            with fitz.open(self.pdf_path) as document:
                self.page_count = document.page_count
                self.page_index = max(0, min(self.page_index, self.page_count - 1))
                page = document.load_page(self.page_index)
                query = self.search_edit.text().strip()
                if query:
                    for rectangle in page.search_for(query):
                        annotation = page.add_highlight_annot(rectangle)
                        annotation.update()
                matrix = fitz.Matrix(self.zoom, self.zoom).prerotate(self.rotation)
                pixmap = page.get_pixmap(matrix=matrix, alpha=False, annots=True)
                image = QImage(
                    pixmap.samples,
                    pixmap.width,
                    pixmap.height,
                    pixmap.stride,
                    QImage.Format.Format_RGB888,
                ).copy()
        except Exception as exc:
            self.image_label.setText(f"PDF 预览失败：{exc}")
            self.image_label.setPixmap(QPixmap())
            self._update_buttons()
            return
        self.image_label.setPixmap(QPixmap.fromImage(image))
        self.image_label.adjustSize()
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1, max(1, self.page_count))
        self.page_spin.setValue(self.page_index + 1)
        self.page_spin.blockSignals(False)
        self.zoom_label.setText(f"{self.zoom * 100:.0f}%")
        self.page_label.setText(f"{self.pdf_path.name} · 第 {self.page_index + 1} / {self.page_count} 页")
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_button.setEnabled(self.page_index > 0)
        self.next_button.setEnabled(self.page_index + 1 < self.page_count)
        has_pdf = self.page_count > 0
        self.page_spin.setEnabled(has_pdf)
        self.zoom_out_button.setEnabled(has_pdf and self.zoom > 0.5)
        self.zoom_in_button.setEnabled(has_pdf and self.zoom < 3.0)
        self.fit_width_button.setEnabled(has_pdf)
        self.rotate_button.setEnabled(has_pdf)
        self.copy_text_button.setEnabled(has_pdf)
        has_matches = bool(self.search_pages)
        self.previous_match_button.setEnabled(has_matches)
        self.next_match_button.setEnabled(has_matches)
