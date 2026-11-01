"""导入 PDF 元数据确认/查重对话框(E1 从 main_window 拆出,行为不变)。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from synthrecall.models import Category
from synthrecall.repositories import (
    UNCATEGORIZED_CATEGORY_NAME,
    PdfImportMetadata,
    PdfImportPreview,
)
from synthrecall.ui.widgets import NoWheelComboBox, NoWheelSpinBox


class PDFImportPreviewDialog(QDialog):
    ACTION_IMPORT = "import"
    ACTION_OPEN_EXISTING = "open_existing"
    ACTION_MERGE = "merge"

    def __init__(
        self,
        preview: PdfImportPreview,
        categories: list[Category],
        selected_category_id: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.preview = preview
        self.action = ""
        has_duplicate = preview.duplicate_paper_id is not None
        self.setWindowTitle("检测到重复文献" if has_duplicate else "导入 PDF 预览")
        self.resize(820, 720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("检测到重复文献，已暂停导入" if has_duplicate else "确认文献信息")
        title.setObjectName("DialogTitle")
        source = QLabel(
            f"文件：{preview.source_path.name}    "
            f"大小：{self._format_file_size(preview.file_size)}"
        )
        source.setObjectName("MutedText")
        source.setToolTip(str(preview.source_path))

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.title_edit = QLineEdit(preview.metadata.title)
        self.authors_edit = QLineEdit(preview.metadata.authors)
        self.year_edit = NoWheelSpinBox()
        self.year_edit.setRange(0, 2200)
        self.year_edit.setSpecialValueText("未知")
        self.year_edit.setValue(preview.metadata.year or 0)
        self.doi_edit = QLineEdit(preview.metadata.doi)
        self.arxiv_edit = QLineEdit(preview.metadata.arxiv_id)
        self.category_combo = NoWheelComboBox()
        self.category_combo.addItem("未分类", None)
        for category in categories:
            if category.name == UNCATEGORIZED_CATEGORY_NAME:
                continue
            self.category_combo.addItem(category.name, category.id)
        selected_index = self.category_combo.findData(selected_category_id)
        self.category_combo.setCurrentIndex(max(0, selected_index))
        self.abstract_edit = QTextEdit(preview.metadata.abstract)
        self.abstract_edit.setAcceptRichText(False)
        self.abstract_edit.setMinimumHeight(220)
        self.abstract_edit.setPlaceholderText("从 PDF 前两页提取的文本，可在导入前修正或清空。")

        form.addRow("标题", self.title_edit)
        form.addRow("作者", self.authors_edit)
        form.addRow("年份", self.year_edit)
        form.addRow("DOI", self.doi_edit)
        form.addRow("arXiv ID", self.arxiv_edit)
        form.addRow("分类", self.category_combo)
        form.addRow("摘要/首页文本", self.abstract_edit)

        layout.addWidget(title)
        layout.addWidget(source)
        if preview.duplicate_paper_id is not None:
            layout.addWidget(self._build_duplicate_panel())
        layout.addWidget(form_host, 1)
        layout.addWidget(self._build_buttons())

    def _build_duplicate_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("SubPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(7)

        warning = QLabel("检测到可能已经入库的文献")
        warning.setObjectName("FieldTitle")
        reason = QLabel(f"匹配依据：{self.preview.duplicate_reason}")
        reason.setWordWrap(True)
        existing = QLabel(
            f"库内已有：#{self.preview.duplicate_paper_id} "
            f"《{self.preview.duplicate_title or '未命名文献'}》\n"
            f"作者：{self.preview.duplicate_authors or '未填写'}"
        )
        existing.setWordWrap(True)
        hint = QLabel("“合并缺失信息”只补空字段和分类；不会覆盖现有 PDF、摘要或笔记。")
        hint.setObjectName("MutedText")
        hint.setWordWrap(True)
        layout.addWidget(warning)
        layout.addWidget(reason)
        layout.addWidget(existing)
        layout.addWidget(hint)
        return panel

    def _build_buttons(self) -> QDialogButtonBox:
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        if self.preview.duplicate_paper_id is None:
            import_button = buttons.addButton(
                "确认导入",
                QDialogButtonBox.ButtonRole.AcceptRole,
            )
            import_button.setObjectName("PrimaryButton")
            import_button.clicked.connect(lambda: self._accept_action(self.ACTION_IMPORT))
            return buttons

        open_button = buttons.addButton(
            "打开现有文献",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        open_button.clicked.connect(
            lambda: self._accept_action(self.ACTION_OPEN_EXISTING)
        )
        merge_button = buttons.addButton(
            "合并缺失信息",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        merge_button.setObjectName("PrimaryButton")
        merge_button.clicked.connect(lambda: self._accept_action(self.ACTION_MERGE))
        return buttons

    def _accept_action(self, action: str) -> None:
        if action in {self.ACTION_IMPORT, self.ACTION_MERGE} and not self.title_edit.text().strip():
            QMessageBox.information(self, "缺少标题", "请填写文献标题。")
            return
        self.action = action
        self.accept()

    def metadata(self) -> PdfImportMetadata:
        return PdfImportMetadata(
            title=self.title_edit.text().strip(),
            authors=self.authors_edit.text().strip(),
            year=self.year_edit.value() or None,
            doi=self.doi_edit.text().strip(),
            arxiv_id=self.arxiv_edit.text().strip(),
            abstract=self.abstract_edit.toPlainText().strip(),
        )

    def selected_category_id(self) -> int | None:
        value = self.category_combo.currentData()
        return int(value) if value is not None else None

    @staticmethod
    def _format_file_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"
