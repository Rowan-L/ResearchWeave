from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pymupdf as fitz
from PySide6.QtCore import (
    QEvent,
    QProcess,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QCompleter,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from synthrecall.arxiv_discovery import (
    ArxivPaper,
)
from synthrecall.branding import APP_DISPLAY_NAME, APP_NAME, APP_NAME_ZH, app_icon_path
from synthrecall.metadata_enrichment import fetch_metadata
from synthrecall.models import Category, Paper
from synthrecall.paper_url import (
    ResolvedPaperPDF,
    arxiv_pdf_url,
)
from synthrecall.repositories import (
    UNCATEGORIZED_CATEGORY_NAME,
    LibraryRepository,
    PaperUpdate,
    SavedSearchRule,
    canonical_dataset_name,
    is_primary_dataset_metric,
    parse_dataset_metrics_text,
)
from synthrecall.settings import (
    LLMSettings,
    get_llm_api_key,
)
from synthrecall.tasks import TaskManager
from synthrecall.translation import (
    PDFTranslationResult,
    find_translation_result,
)
from synthrecall.ui import theme
from synthrecall.ui.appearance_settings import AppearanceDialog
from synthrecall.ui.confirmation import (
    ConfirmationDialog,
    DialogAction,
    confirm_action,
)
from synthrecall.ui.dialogs.dataset_metric import (
    DatasetMetricDialog,
)
from synthrecall.ui.dialogs.large_text import (
    LargeTextDialog,
)
from synthrecall.ui.dialogs.llm_settings import (
    LLMSettingsDialog,
)
from synthrecall.ui.dialogs.loading import (
    LoadingDialog,
)
from synthrecall.ui.dialogs.paper_chat import (
    PaperChatDialog,
    PaperChatWorker,
)
from synthrecall.ui.dialogs.pdf_import_preview import (
    PDFImportPreviewDialog,
)
from synthrecall.ui.discovery_view import DiscoveryView
from synthrecall.ui.duplicate_cleanup import DuplicateCleanupDialog
from synthrecall.ui.external_request import confirm_external_request
from synthrecall.ui.pdf_preview import (
    PDFPreviewWidget,
)
from synthrecall.ui.pdf_reader import ContinuousPDFReader
from synthrecall.ui.saved_search_dialog import SavedSearchDialog
from synthrecall.ui.shortcuts_dialog import ShortcutsDialog
from synthrecall.ui.statistics_panel import StatisticsDialog
from synthrecall.ui.storage_manager import StorageManagerDialog
from synthrecall.ui.task_center import TaskCenterDialog
from synthrecall.ui.ui_thread import run_on_ui_thread
from synthrecall.ui.widgets import (
    AutoHeightJustifiedTextEdit,
    NoWheelComboBox,
    NoWheelSpinBox,
    PaperCard,
    PasswordVisibilityButton,
    SmoothPaperList,
    SmoothTagList,
)
from synthrecall.ui.workers import (
    AbstractTranslateWorker,
    AIAnalyzeWorker,
    ArxivPDFDownloadWorker,
    CategorySuggestWorker,
    PDFTranslateWorker,
    URLPDFImportWorker,
)
from synthrecall.ui.workspace_manager import WorkspaceManagerDialog

# E1 拆分后本模块仍是这些公共名字的稳定导入面;__all__ 同时让 ruff 把
# re-export 视为有意为之(未在模块内使用也不报 F401)。
__all__ = [
    "AIAnalyzeWorker",
    "AbstractTranslateWorker",
    "ArxivPDFDownloadWorker",
    "AutoHeightJustifiedTextEdit",
    "DatasetMetricDialog",
    "LLMSettingsDialog",
    "LargeTextDialog",
    "LoadingDialog",
    "MainWindow",
    "NoWheelComboBox",
    "NoWheelSpinBox",
    "PDFImportPreviewDialog",
    "PDFPreviewWidget",
    "PDFTranslateWorker",
    "PaperCard",
    "PaperChatDialog",
    "PaperChatWorker",
    "PasswordVisibilityButton",
    "SmoothPaperList",
    "SmoothTagList",
    "URLPDFImportWorker",
    "format_memory_anchor",
]

READING_STATUSES = ["待读", "浏览", "已读", "精读", "复现中", "已复现"]

# 分类树「回收站」节点使用的哨兵 ID(正常分类 id 从 1 开始)
TRASH_CATEGORY_ID = -1

# 智能分组父节点的哨兵值;子节点用 ("smart", value) 元组
SMART_GROUP_NONE = "smart-group"
# F20 智能文件夹树节点哨兵:父节点(点击=取消筛选)与子节点 ("saved", id)
SAVED_SEARCH_NONE = "saved-group-none"


def format_memory_anchor(text: str) -> str:
    """把记忆锚点原文(中英文分号/换行混排)整理成逐条 "• " 列表，便于阅读。"""
    parts = [part.strip(" ;；,，、") for part in re.split(r"[;；\n]", text or "")]
    return "\n".join(f"• {part}" for part in parts if part)


























class MainWindow(QMainWindow):
    def __init__(self, repository: LibraryRepository, data_dir: Path) -> None:
        super().__init__()
        self.repository = repository
        self.data_dir = data_dir
        self.settings_path = data_dir / "llm_settings.json"
        self.ui_settings = QSettings(
            str(data_dir / "ui_state.ini"), QSettings.Format.IniFormat
        )
        self._splitters: dict[str, QSplitter] = {}
        self._splitter_save_timers: dict[str, QTimer] = {}
        self.tray_icon: QSystemTrayIcon | None = None
        self._tray_exit_requested = False
        self.task_manager = TaskManager(self)
        self.task_center_dialog: TaskCenterDialog | None = None
        self.paper_chat_dialog: PaperChatDialog | None = None
        self.ai_thread: QThread | None = None
        self.ai_worker: AIAnalyzeWorker | None = None
        self.ai_task_id: int | None = None
        self.translate_thread: QThread | None = None
        self.translate_worker: PDFTranslateWorker | None = None
        self.translate_task_id: int | None = None
        self.arxiv_pdf_thread: QThread | None = None
        self.arxiv_pdf_worker: ArxivPDFDownloadWorker | None = None
        self.arxiv_pdf_task_id: int | None = None
        self.url_import_thread: QThread | None = None
        self.url_import_worker: URLPDFImportWorker | None = None
        self.url_import_task_id: int | None = None
        self.abstract_translate_thread: QThread | None = None
        self.abstract_translate_worker: AbstractTranslateWorker | None = None
        self.abstract_translate_task_id: int | None = None
        # F21:真实阅读计时——阅读页可见且窗口在前台时逐秒累计,
        # 30 秒 待读→浏览,300 秒 浏览→已读;手动状态不会被降级。
        self._reading_paper_id: int | None = None
        self._reading_seconds = 0
        self._reading_promoted: set[str] = set()
        self._reading_unsaved_seconds = 0
        self._reading_tick_timer = QTimer(self)
        self._reading_tick_timer.setInterval(1000)
        self._reading_tick_timer.timeout.connect(self._on_reading_tick)
        self._reading_tick_timer.start()
        self.abstract_language = "en"
        self.abstract_source_text = ""
        self.loading_dialog: LoadingDialog | None = None
        self.current_paper_id: int | None = None
        self.current_category_id: int | None = None
        self.category_lookup: dict[int, Category] = {}
        self.selected_category_ids: list[int] = []
        self.detail_sections: list[tuple[QWidget, QTextEdit | QTableWidget]] = []
        self.detail_section_by_title: dict[str, QWidget] = {}
        self._dirty = False
        self._suppress_dirty_tracking = False
        self._updating_paper_list = False
        self._updating_reader_queue = False
        self._viewing_trash = False
        self.current_tag_id: int | None = None
        self.smart_filter: tuple[str, str] | None = None
        self.current_saved_search_id: int | None = None
        self._restoring_paper_selection = False

        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setMinimumSize(1180, 720)
        self._build_ui()
        self.task_manager.changed.connect(self.update_task_button)
        self._connect_dirty_tracking()
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.save_shortcut.activated.connect(self.save_active_content)
        self.shortcuts: dict[str, QShortcut] = {}
        self._register_shortcut("search", "Ctrl+F", self.focus_library_search)
        self._register_shortcut("import", "Ctrl+O", self.add_pdf)
        self._register_shortcut("new", "Ctrl+N", self.create_blank_paper)
        self._register_shortcut("library", "Alt+1", self.show_library_page)
        self._register_shortcut("dataset", "Alt+2", self.show_dataset_page)
        self._register_shortcut("discovery", "Alt+3", self.show_discovery_page)
        self._register_shortcut("tasks", "Ctrl+Shift+T", self.open_task_center)
        # U16:常用快捷键(全局)
        self._register_shortcut("refresh", "F5", self.refresh_all_views)
        self._register_shortcut("refresh2", "Ctrl+R", self.refresh_all_views)
        self._register_shortcut("export", "Ctrl+E", self.batch_export_citations)
        self._register_shortcut("backup", "Ctrl+B", self.backup_data_now)
        self._register_shortcut("appearance", "Ctrl+,", self.open_appearance_settings)
        self._register_shortcut("shortcuts_help", "F1", self.open_shortcuts_dialog)
        # U16:作用域受限的快捷键——只在对应控件获得焦点时触发,
        # 避免覆盖文本框内的 Esc/Delete/Ctrl+A 语义
        esc_shortcut = QShortcut(QKeySequence("Esc"), self.search_edit)
        esc_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc_shortcut.activated.connect(self.clear_search_with_esc)
        self.shortcuts["search_clear"] = esc_shortcut
        select_all_shortcut = QShortcut(
            QKeySequence("Ctrl+A"), self.paper_list
        )
        select_all_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        select_all_shortcut.activated.connect(self.select_all_papers)
        self.shortcuts["select_all"] = select_all_shortcut
        delete_shortcut = QShortcut(QKeySequence("Delete"), self.paper_list)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        delete_shortcut.activated.connect(self._trash_via_shortcut)
        self.shortcuts["trash"] = delete_shortcut
        # 浏览历史返回(用户需求):按浏览顺序回退上一页,非"上一层";
        # 触发方式 = 鼠标侧键后退(XButton1)或 Alt+←
        self._nav_history: list[tuple[str, int | None]] = []
        self._nav_back_in_progress = False
        self._register_shortcut("nav_back", "Alt+Left", self.go_back_previous_view)
        QApplication.instance().installEventFilter(self)
        self._apply_style()
        self._set_active_workspace("library")
        self.refresh_categories()
        self.refresh_tag_rail()
        self.refresh_papers()

    def _register_shortcut(self, name: str, sequence: str, callback) -> None:
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.activated.connect(callback)
        self.shortcuts[name] = shortcut

    def _register_splitter_state(
        self,
        key: str,
        splitter: QSplitter,
        default_sizes: list[int],
    ) -> None:
        splitter.setChildrenCollapsible(False)
        self._splitters[key] = splitter
        state = self.ui_settings.value(f"splitters/{key}")
        restored = False
        if state is not None:
            try:
                restored = splitter.restoreState(state)
            except (TypeError, ValueError):
                restored = False
        if not restored:
            splitter.setSizes(default_sizes)

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(250)
        timer.timeout.connect(lambda current_key=key: self._save_splitter_state(current_key))
        splitter.splitterMoved.connect(lambda *_args, current_timer=timer: current_timer.start())
        self._splitter_save_timers[key] = timer

    def _save_splitter_state(self, key: str) -> None:
        splitter = self._splitters.get(key)
        if splitter is not None:
            self.ui_settings.setValue(f"splitters/{key}", splitter.saveState())
            self.ui_settings.sync()

    def _save_all_splitter_states(self) -> None:
        for timer in self._splitter_save_timers.values():
            timer.stop()
        for key in self._splitters:
            self._save_splitter_state(key)
        self.ui_settings.sync()

    def focus_library_search(self) -> None:
        self.show_library_page()
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def refresh_all_views(self) -> None:
        """U16:F5/Ctrl+R 手动刷新全部视图(列表/左栏/标签)。"""
        self.refresh_categories()
        self.refresh_tag_rail()
        self.refresh_papers()
        self.statusBar().showMessage("已刷新", 2000)

    def clear_search_with_esc(self) -> None:
        """U16:搜索框内按 Esc 清空搜索词;已为空则不动其它筛选。"""
        if not self.search_edit.text().strip():
            return
        self.search_edit.clear()
        self.refresh_papers()
        self.statusBar().showMessage("已清空搜索词", 2000)

    def select_all_papers(self) -> None:
        """U16:Ctrl+A 全选当前文献列表。"""
        self.paper_list.selectAll()

    def _trash_via_shortcut(self) -> None:
        """U16:Delete 键=移入回收站;回收站视图下不响应,防止误彻底删除。"""
        if self._viewing_trash:
            self.statusBar().showMessage("回收站内请用批量菜单「彻底删除」", 3000)
            return
        self.batch_delete_papers()

    def open_storage_manager(self) -> None:
        """F20:存储管理——分类容量与安全垃圾清理。"""
        StorageManagerDialog(self.data_dir, parent=self).exec()

    def open_shortcuts_dialog(self) -> None:
        """U16:F1 快捷键一览。"""
        ShortcutsDialog(self).show()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.library_page = QSplitter(Qt.Orientation.Horizontal)
        self.library_page.setObjectName("MainSplitter")
        self.library_page.addWidget(self._build_left_panel())
        self.library_page.addWidget(self._build_middle_panel())
        self.library_page.addWidget(self._build_detail_panel())
        self.library_page.setStretchFactor(0, 0)
        self.library_page.setStretchFactor(1, 1)
        self.library_page.setStretchFactor(2, 0)
        self.main_stack = QStackedWidget()
        self.main_stack.addWidget(self.library_page)
        self.dataset_page = self._build_dataset_page()
        self.main_stack.addWidget(self.dataset_page)
        self.discovery_page = DiscoveryView(
            self.data_dir,
            self,
            task_manager=self.task_manager,
            # F20:arXiv 摘要翻译的 token 用量也入库统计(无对应文献 id)
            usage_recorder=lambda usage: self.repository.record_token_usage(
                feature="arXiv 摘要翻译", source="app", **usage
            ),
        )
        self.discovery_page.importRequested.connect(self.import_discovered_paper)
        self.discovery_page.deepReadRequested.connect(self.deep_read_discovered_paper)
        self.discovery_page.backRequested.connect(self.show_library_page)
        self.main_stack.addWidget(self.discovery_page)
        self.reader_page = self._build_reader_page()
        self.main_stack.addWidget(self.reader_page)
        self.main_stack.addWidget(self.editor_page)

        self.app_header = self._build_header()
        self._register_splitter_state(
            "library", self.library_page, [220, 800, 380]
        )
        self._register_splitter_state(
            "dataset", self.dataset_splitter, [260, 900]
        )
        self._register_splitter_state(
            "discovery", self.discovery_page.splitter, [220, 680, 420]
        )
        self._register_splitter_state(
            "reader", self.reader_splitter, [230, 850, 330]
        )
        root_layout.addWidget(self.app_header)
        root_layout.addWidget(self.main_stack, 1)

        self.setCentralWidget(root)
        self.statusBar().showMessage(f"{APP_DISPLAY_NAME} 已就绪")

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("AppHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(10)

        brand_icon = QLabel()
        brand_icon.setObjectName("BrandIcon")
        icon_path = app_icon_path()
        if icon_path.exists():
            brand_icon.setPixmap(
                QPixmap(str(icon_path)).scaled(
                    34,
                    34,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        brand_icon.setFixedSize(38, 38)
        brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_icon.setAccessibleName(f"{APP_NAME} 应用图标")

        brand_copy = QVBoxLayout()
        brand_copy.setContentsMargins(0, 0, 8, 0)
        brand_copy.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setObjectName("HeroTitle")
        title_zh = QLabel(APP_NAME_ZH)
        title_zh.setObjectName("BrandSubtitle")
        brand_copy.addWidget(title)
        brand_copy.addWidget(title_zh)
        layout.addWidget(brand_icon)
        layout.addLayout(brand_copy)

        # 顶部常驻页签:文献库/发现/数据集 在任何非阅读页都可直接互切
        header_nav = QFrame()
        header_nav.setObjectName("HeaderNav")
        nav_layout = QHBoxLayout(header_nav)
        nav_layout.setContentsMargins(2, 2, 2, 2)
        nav_layout.setSpacing(2)
        nav_callbacks = {
            "library": self.show_library_page,
            "dataset": self.show_dataset_page,
            "discovery": self.show_discovery_page,
        }
        self.header_nav_buttons = {}
        for key, text in (("library", "文献库"), ("discovery", "发现"), ("dataset", "数据集")):
            nav_button = QPushButton(text)
            nav_button.setObjectName("HeaderNavButton")
            nav_button.setCheckable(True)
            nav_button.setAutoExclusive(True)
            nav_button.setCursor(Qt.CursorShape.PointingHandCursor)
            nav_button.clicked.connect(
                lambda _checked=False, name=key: nav_callbacks[name]()
            )
            self.header_nav_buttons[key] = nav_button
            nav_layout.addWidget(nav_button)
        layout.addWidget(header_nav)
        layout.addStretch(1)

        self.header_count = QLabel("0 篇文献")
        self.header_count.setObjectName("HeaderMeta")
        self.search_edit.setMinimumWidth(320)
        self.search_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        api_settings = self._make_button(
            "API 设置",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            "TinyButton",
        )
        api_settings.setAccessibleName("API 设置")
        api_settings.setAccessibleDescription("配置大模型连接、API Key 和解析提示词")
        api_settings.setToolTip("配置大模型 Base URL、模型名、API Key，以及可编辑的解析提示词。")
        api_settings.clicked.connect(self.open_api_settings)
        self.api_settings_button = api_settings

        self.task_button = self._make_button(
            "任务",
            QStyle.StandardPixmap.SP_BrowserReload,
            "TinyButton",
        )
        self.task_button.setToolTip(
            "查看后台任务、取消任务、重试失败任务或复制错误（Ctrl+Shift+T）。"
        )
        self.task_button.clicked.connect(self.open_task_center)
        # F15:有后台任务运行时,循环图标旋转提示"正在执行"
        self._task_spin_angle = 0
        self._task_icon_base = self.task_button.icon().pixmap(64, 64)
        self._task_spin_timer = QTimer(self.task_button)
        self._task_spin_timer.setInterval(70)
        self._task_spin_timer.timeout.connect(self._spin_task_icon)

        import_menu_button = self._make_button(
            "导入",
            QStyle.StandardPixmap.SP_FileIcon,
            "PrimaryButton",
        )
        import_menu_button.setAccessibleName("导入或新建文献")
        import_menu_button.setToolTip("导入 PDF 或创建空白文献（导入快捷键 Ctrl+O）。")
        import_menu = QMenu(import_menu_button)
        magic_wand_action = import_menu.addAction("按 DOI / arXiv 添加…")
        magic_wand_action.setToolTip(
            "粘贴 DOI、arXiv ID 或其链接，自动拉取元数据创建文献；arXiv 文献会自动下载 PDF。"
        )
        magic_wand_action.triggered.connect(self.add_by_identifier)
        import_menu.addSeparator()
        ai_import_action = import_menu.addAction("AI 导入 PDF")
        ai_import_action.setToolTip("选择一个外部 PDF，复制到项目 data/papers 目录，并调用大模型生成文献信息。")
        ai_import_action.triggered.connect(self.ai_import_pdf)
        self.ai_import_url_action = import_menu.addAction("AI 链接导入")
        self.ai_import_url_action.setToolTip(
            "输入论文页面或在线 PDF 链接，自动查找并下载 PDF，归档后调用大模型解析。"
        )
        self.ai_import_url_action.triggered.connect(self.ai_import_url)
        add_pdf_action = import_menu.addAction("添加 PDF")
        add_pdf_action.setToolTip("选择一个 PDF 并复制到项目 data/papers 目录，不调用大模型解析。")
        add_pdf_action.triggered.connect(self.add_pdf)
        import_menu.addSeparator()
        new_paper_action = import_menu.addAction("新建文献")
        new_paper_action.setToolTip("创建一篇空白文献，适合先手动录入标题、摘要和批注。")
        new_paper_action.triggered.connect(self.create_blank_paper)
        import_menu_button.setMenu(import_menu)

        layout.addWidget(self.search_edit, 1)
        layout.addWidget(self.header_count)
        layout.addWidget(self.task_button)
        layout.addWidget(api_settings)
        layout.addWidget(import_menu_button)
        return header

    def update_task_button(self) -> None:
        active_count = self.task_manager.active_count()
        queued_count = self.task_manager.pending_count()
        total = active_count + queued_count
        self.task_button.setText(f"任务 {total}" if total else "任务")
        self.task_button.setProperty("active", active_count > 0)
        self.task_button.style().unpolish(self.task_button)
        self.task_button.style().polish(self.task_button)
        if active_count > 0:
            if not self._task_spin_timer.isActive():
                self._task_spin_timer.start()
        else:
            self._task_spin_timer.stop()
            self.task_button.setIcon(QIcon(self._task_icon_base))

    def _enqueue_task(self, name: str, detail: str, starter: Callable[[], None]) -> None:
        """F16:同类任务运行中时,新任务加入队列顺序执行,不再拒绝。"""
        self.task_manager.enqueue(name, detail=detail, start=starter)
        self.statusBar().showMessage(
            "已加入任务队列，将在当前任务完成后自动执行。", 5000
        )

    def _spin_task_icon(self) -> None:
        # 自绘 loading 圆弧:standardIcon 的 pixmap 实际尺寸与 iconSize 不一致,
        # 拿它旋转会圆心偏移、比例失真;直接画弧则圆心/粗细/边距完全可控。
        self._task_spin_angle = (self._task_spin_angle + 30) % 360
        size = max(16, self.task_button.iconSize().width())
        dpr = self.devicePixelRatioF()
        pixmap = QPixmap(max(2, round(size * dpr)), max(2, round(size * dpr)))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#0f9f91"), max(2.0, size / 11.0))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        margin = pen.widthF() / 2 + 1.5
        painter.drawArc(
            QRectF(margin, margin, size - margin * 2, size - margin * 2),
            self._task_spin_angle * 16,
            270 * 16,
        )
        painter.end()
        self.task_button.setIcon(QIcon(pixmap))

    def open_task_center(self) -> None:
        if self.task_center_dialog is None:
            self.task_center_dialog = TaskCenterDialog(self.task_manager, self)
            self.task_center_dialog.finished.connect(self._clear_task_center_dialog)
        self.task_center_dialog.show()
        self.task_center_dialog.raise_()
        self.task_center_dialog.activateWindow()

    def _clear_task_center_dialog(self) -> None:
        if self.task_center_dialog is not None:
            self.task_center_dialog.deleteLater()
            self.task_center_dialog = None

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("NavigationRail")
        panel.setMinimumWidth(190)
        panel.setMaximumWidth(250)
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 14, 12, 14)
        layout.setSpacing(8)

        self.workspace_buttons = {}
        workspace_actions = [
            ("library", "文献库", QStyle.StandardPixmap.SP_DirHomeIcon, self.show_library_page),
            ("dataset", "数据集", QStyle.StandardPixmap.SP_FileDialogContentsView, self.show_dataset_page),
            ("discovery", "发现", QStyle.StandardPixmap.SP_FileDialogInfoView, self.show_discovery_page),
        ]
        for key, text, icon, callback in workspace_actions:
            button = self._make_button(text, icon, "RailNavButton")
            button.setProperty("active", False)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            shortcut_hint = {"library": "Alt+1", "dataset": "Alt+2", "discovery": "Alt+3"}[key]
            button.setToolTip(f"切换到{text}工作区（{shortcut_hint}）")
            button.clicked.connect(callback)
            self.workspace_buttons[key] = button
            layout.addWidget(button)

        quick_label = QLabel("快捷排序")
        quick_label.setObjectName("RailHeading")
        layout.addWidget(quick_label)
        quick_views = [
            ("按更新时间", "updated", "按最近修改排序,显示全部文献。"),
            ("按最近打开", "last_opened", "按最近打开时间排序。"),
            ("按重要度", "importance", "重要度高的文献排前面。"),
        ]
        for text, view_name, tooltip in quick_views:
            button = QPushButton(text)
            button.setObjectName("QuickViewButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(tooltip)
            button.clicked.connect(
                lambda _checked=False, name=view_name: self.apply_library_quick_view(name)
            )
            layout.addWidget(button)

        tag_label = QLabel("标签")
        tag_label.setObjectName("RailHeading")
        layout.addWidget(tag_label)
        self.tag_rail_layout = QVBoxLayout()
        self.tag_rail_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_rail_layout.setSpacing(4)
        tag_rail_container = QWidget()
        tag_rail_container.setLayout(self.tag_rail_layout)
        layout.addWidget(tag_rail_container)

        header = QHBoxLayout()
        title = QLabel("分类")
        title.setObjectName("RailHeading")
        add_category = self._make_button(
            "新建",
            QStyle.StandardPixmap.SP_FileDialogNewFolder,
            "TinyButton",
        )
        add_category.setToolTip("在当前选中的分类下添加子分类；也可以右键分类进行更多操作。")
        add_category.clicked.connect(lambda: self.add_category())
        header.addWidget(title, 1)
        header.addWidget(add_category)

        hint = QLabel("")
        hint.setObjectName("MutedText")
        hint.setVisible(False)

        self.category_tree = QTreeWidget()
        self.category_tree.setObjectName("CategoryTree")
        self.category_tree.setHeaderHidden(True)
        self.category_tree.setIndentation(18)
        self.category_tree.itemSelectionChanged.connect(self.on_category_selected)
        self.category_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.category_tree.customContextMenuRequested.connect(self.open_category_menu)
        self.category_filter_edit = QLineEdit()
        self.category_filter_edit.setObjectName("SearchBox")
        self.category_filter_edit.setPlaceholderText("搜索分类")
        self.category_filter_edit.textChanged.connect(self.filter_category_tree)

        layout.addLayout(header)
        layout.addWidget(hint)
        layout.addWidget(self.category_filter_edit)
        layout.addWidget(self.category_tree, 1)
        # 左栏内容(导航/快捷排序/标签 chips/分类树)整体包进透明滚动容器:
        # 窗口偏矮或标签过多时滚动查看,而不是溢出叠压到「分类」区或被裁切
        scroll = QScrollArea()
        scroll.setObjectName("NavigationRailScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        return scroll

    def _build_middle_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("LibraryTablePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("全部文献")
        title.setObjectName("PanelTitle")
        self.paper_count = QLabel("0 条")
        self.paper_count.setObjectName("MutedText")
        self.paper_sort_combo = NoWheelComboBox()
        self.paper_sort_combo.addItem("最近更新", "updated")
        self.paper_sort_combo.addItem("最近打开", "last_opened")
        self.paper_sort_combo.addItem("重要度", "importance")
        self.paper_sort_combo.addItem("年份", "year")
        self.paper_sort_combo.addItem("标题", "title")
        self.paper_sort_combo.addItem("最近导入", "created")
        self.paper_sort_combo.setObjectName("PaperSortCombo")
        self.paper_sort_combo.setAccessibleName("文献排序")
        self.paper_sort_combo.setMinimumWidth(124)
        self.paper_sort_combo.setToolTip("调整当前文献列表的排序方式。")
        self.paper_sort_combo.currentIndexChanged.connect(self.refresh_papers)
        self.batch_button = QPushButton("批量")
        self.batch_button.setObjectName("TinyButton")
        self.batch_button.setToolTip("按住 Ctrl 或 Shift 多选文献，再批量修改状态、分类或移入回收站。")
        self._batch_menu = QMenu(self.batch_button)
        self._batch_menu.aboutToShow.connect(self._rebuild_batch_menu)
        self.batch_button.setMenu(self._batch_menu)
        tools_button = QPushButton("工具")
        tools_button.setObjectName("TinyButton")
        tools_button.setToolTip("库维护工具:孤儿文件扫描清理等。")
        tools_menu = QMenu(tools_button)
        tools_menu.addAction("存储管理…").triggered.connect(self.open_storage_manager)
        tools_menu.addAction("扫描未引用的 PDF 与译文…").triggered.connect(
            self.scan_orphan_files
        )
        tools_menu.addSeparator()
        tools_menu.addAction("重复文献清理向导…").triggered.connect(self.open_duplicate_cleanup)
        tools_menu.addAction("库统计…").triggered.connect(self.open_statistics_panel)
        tools_menu.addAction("界面设置…").triggered.connect(self.open_appearance_settings)
        tools_menu.addAction("快捷键一览…").triggered.connect(self.open_shortcuts_dialog)
        tools_menu.addAction("工作区管理…").triggered.connect(self.open_workspace_manager)
        tools_menu.addAction("分类整理向导…").triggered.connect(self.open_category_cleanup)
        tools_menu.addAction("AI 建议主分类(未分类文献)…").triggered.connect(
            self.suggest_categories_for_uncategorized
        )
        tools_menu.addAction("新建智能文件夹…").triggered.connect(self.create_saved_search_blank)
        tools_menu.addSeparator()
        tools_menu.addAction("立即备份数据…").triggered.connect(self.backup_data_now)
        tools_menu.addAction("从备份恢复…").triggered.connect(self.restore_data_from_backup)
        tools_button.setMenu(tools_menu)
        title_row.addWidget(title, 1)
        title_row.addWidget(self.paper_sort_combo)
        title_row.addWidget(self.batch_button)
        title_row.addWidget(tools_button)
        title_row.addWidget(self.paper_count)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("SearchBox")
        self.search_edit.setPlaceholderText("搜索标题、作者、记忆锚点、摘要、总结、数据集、批注")
        self.search_edit.textChanged.connect(self.schedule_search)

        self.search_checkboxes: dict[str, QCheckBox] = {}
        search_scope = QGridLayout()
        search_scope.setContentsMargins(0, 0, 0, 0)
        search_scope.setHorizontalSpacing(6)
        search_scope.setVerticalSpacing(4)
        for index, (field, label, checked) in enumerate([
            ("title", "标题", True),
            ("memory_anchor", "记忆锚点", True),
            ("abstract", "摘要", True),
            ("summary", "总结/方法", True),
            ("notes", "批注", True),
            ("authors", "作者", False),
            ("metadata", "来源/状态", False),
            ("full_text", "全文", False),
        ]):
            checkbox = QCheckBox(label)
            checkbox.setObjectName("SearchScope")
            checkbox.setChecked(checked)
            checkbox.stateChanged.connect(self.schedule_search)
            checkbox.stateChanged.connect(self._hint_when_no_search_fields)
            self.search_checkboxes[field] = checkbox
            search_scope.addWidget(checkbox, index // 2, index % 2)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(220)
        self.search_timer.timeout.connect(self.refresh_papers)

        self.paper_list = SmoothPaperList()
        self.paper_list.setObjectName("PaperList")
        self.paper_list.setSpacing(0)
        self.paper_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.paper_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.paper_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.paper_list.itemSelectionChanged.connect(self.on_paper_selected)
        self.paper_list.itemDoubleClicked.connect(self.open_paper_in_reader)
        self.paper_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.paper_list.customContextMenuRequested.connect(self.open_paper_menu)

        self.paper_list_empty_state = QFrame(self.paper_list)
        self.paper_list_empty_state.setObjectName("PaperListEmptyState")
        empty_layout = QVBoxLayout(self.paper_list_empty_state)
        empty_layout.setContentsMargins(24, 24, 24, 24)
        empty_layout.setSpacing(8)
        empty_layout.addStretch(1)
        self.paper_list_empty_title = QLabel("未找到相关文献")
        self.paper_list_empty_title.setObjectName("EmptyStateTitle")
        self.paper_list_empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.paper_list_empty_hint = QLabel("换个关键词，或清除筛选条件再试。")
        self.paper_list_empty_hint.setObjectName("EmptyStateHint")
        self.paper_list_empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.paper_list_empty_hint.setWordWrap(True)
        self.clear_filters_button = QPushButton("清除搜索与筛选")
        self.clear_filters_button.setObjectName("EmptyStateClearButton")
        self.clear_filters_button.clicked.connect(self.clear_library_filters)
        empty_button_row = QHBoxLayout()
        empty_button_row.addStretch(1)
        empty_button_row.addWidget(self.clear_filters_button)
        empty_button_row.addStretch(1)
        empty_layout.addWidget(self.paper_list_empty_title)
        empty_layout.addWidget(self.paper_list_empty_hint)
        empty_layout.addLayout(empty_button_row)
        empty_layout.addStretch(1)
        self.paper_list_empty_state.hide()

        column_header = QFrame()
        column_header.setObjectName("PaperListHeader")
        column_layout = QHBoxLayout(column_header)
        column_layout.setContentsMargins(12, 6, 12, 6)
        column_layout.setSpacing(12)
        column_layout.addWidget(QLabel("标题 / 作者"), 1)
        for text in ("年份", "状态", "重要度 / 分类"):
            label = QLabel(text)
            label.setObjectName("MutedText")
            column_layout.addWidget(label)

        layout.addLayout(title_row)
        layout.addLayout(search_scope)
        layout.addWidget(column_header)
        layout.addWidget(self.paper_list, 1)
        return panel

    def _build_dataset_page(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title = QLabel("数据集排行")
        title.setObjectName("PanelTitle")
        hint = QLabel("左侧选择数据集，右侧按 IoU、F1、参数量等指标查看本文方法排行榜。")
        hint.setObjectName("MutedText")
        # U6:库中还没有数据集指标时,页面中央显示空态引导(内嵌,不弹窗)
        self.dataset_empty_state = QFrame()
        self.dataset_empty_state.setObjectName("PaperListEmptyState")
        empty_state_layout = QVBoxLayout(self.dataset_empty_state)
        empty_state_layout.setContentsMargins(24, 24, 24, 24)
        empty_state_layout.setSpacing(8)
        empty_state_layout.addStretch(1)
        dataset_empty_title = QLabel("还没有数据集指标")
        dataset_empty_title.setObjectName("EmptyStateTitle")
        dataset_empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dataset_empty_hint = QLabel(
            "在文献详情的「研究总结」页填写数据集与指标,或让 AI 解析自动提取。"
        )
        dataset_empty_hint.setObjectName("EmptyStateHint")
        dataset_empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dataset_empty_hint.setWordWrap(True)
        self.dataset_empty_hint_button = QPushButton("去文献添加数据集指标")
        self.dataset_empty_hint_button.setObjectName("EmptyStateClearButton")
        self.dataset_empty_hint_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dataset_empty_hint_button.setToolTip("回到文献库,在文献详情中手动或由 AI 填写数据集指标。")
        self.dataset_empty_hint_button.clicked.connect(self._go_add_dataset_metrics)
        empty_button_row = QHBoxLayout()
        empty_button_row.addStretch(1)
        empty_button_row.addWidget(self.dataset_empty_hint_button)
        empty_button_row.addStretch(1)
        empty_state_layout.addWidget(dataset_empty_title)
        empty_state_layout.addWidget(dataset_empty_hint)
        empty_state_layout.addLayout(empty_button_row)
        empty_state_layout.addStretch(1)
        self.dataset_empty_state.hide()
        title_block.addWidget(title)
        title_block.addWidget(hint)

        back = self._make_button(
            "返回文献库",
            QStyle.StandardPixmap.SP_ArrowBack,
            "SoftButton",
        )
        back.setToolTip("返回文献库主页面。")
        back.clicked.connect(self.show_library_page)
        header.addWidget(back)
        header.addLayout(title_block, 1)

        self.dataset_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.dataset_splitter.setObjectName("DatasetSplitter")

        dataset_panel = QFrame()
        dataset_panel.setObjectName("SubPanel")
        dataset_layout = QVBoxLayout(dataset_panel)
        dataset_layout.setContentsMargins(14, 14, 14, 14)
        dataset_layout.setSpacing(10)
        dataset_title = QLabel("数据集")
        dataset_title.setObjectName("FieldTitle")
        self.dataset_filter_edit = QLineEdit()
        self.dataset_filter_edit.setObjectName("SearchBox")
        self.dataset_filter_edit.setPlaceholderText("筛选数据集")
        self.dataset_filter_edit.textChanged.connect(self.refresh_dataset_metrics)
        self.dataset_list = QListWidget()
        self.dataset_list.setObjectName("DatasetList")
        self.dataset_list.setSpacing(6)
        self.dataset_list.itemSelectionChanged.connect(self.on_dataset_selected)
        dataset_layout.addWidget(dataset_title)
        dataset_layout.addWidget(self.dataset_filter_edit)
        dataset_layout.addWidget(self.dataset_list, 1)

        ranking_panel = QFrame()
        ranking_panel.setObjectName("SubPanel")
        ranking_layout = QVBoxLayout(ranking_panel)
        ranking_layout.setContentsMargins(14, 14, 14, 14)
        ranking_layout.setSpacing(10)
        ranking_header = QHBoxLayout()
        self.dataset_ranking_title = QLabel("选择一个数据集")
        self.dataset_ranking_title.setObjectName("PanelTitle")
        self.metric_combo = NoWheelComboBox()
        self.metric_combo.currentIndexChanged.connect(self.populate_dataset_ranking)
        self.sort_combo = NoWheelComboBox()
        self.sort_combo.addItem("按指标方向", "auto")
        self.sort_combo.addItem("数值从高到低", "desc")
        self.sort_combo.addItem("数值从低到高", "asc")
        self.sort_combo.currentIndexChanged.connect(self.populate_dataset_ranking)
        add_metric = QPushButton("添加")
        add_metric.setObjectName("PrimaryButton")
        add_metric.setToolTip("手动新增一条排行榜记录：数据集、指标、结果、模型和可选关联文献。")
        add_metric.clicked.connect(self.add_ranking_entry)
        edit_metric = QPushButton("编辑")
        edit_metric.setObjectName("TinyButton")
        edit_metric.setToolTip("编辑当前选中的排行榜记录。")
        edit_metric.clicked.connect(self.edit_ranking_entry)
        delete_metric = QPushButton("删除")
        delete_metric.setObjectName("TinyButton")
        delete_metric.setToolTip("删除当前选中的排行榜记录。")
        delete_metric.clicked.connect(self.delete_ranking_entry)
        ranking_header.addWidget(self.dataset_ranking_title, 1)
        ranking_header.addWidget(QLabel("指标"))
        ranking_header.addWidget(self.metric_combo)
        ranking_header.addWidget(QLabel("排序"))
        ranking_header.addWidget(self.sort_combo)
        ranking_header.addWidget(add_metric)
        ranking_header.addWidget(edit_metric)
        ranking_header.addWidget(delete_metric)

        self.dataset_table = QTableWidget(0, 6)
        self.dataset_table.setObjectName("DatasetTable")
        self.dataset_table.setHorizontalHeaderLabels(["排名", "模型/方法", "指标值", "任务", "来源文献", "说明"])
        self.dataset_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dataset_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.dataset_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.dataset_table.setAlternatingRowColors(True)
        self.dataset_table.setWordWrap(True)
        self.dataset_table.setSortingEnabled(False)
        self.dataset_table.verticalHeader().setVisible(False)
        self.dataset_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.dataset_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.dataset_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.dataset_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.dataset_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.dataset_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.dataset_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.dataset_table.cellClicked.connect(self.on_dataset_cell_clicked)
        self.dataset_table.itemDoubleClicked.connect(self.open_metric_paper)

        ranking_layout.addLayout(ranking_header)
        ranking_layout.addWidget(self.dataset_table, 1)
        self.dataset_splitter.addWidget(dataset_panel)
        self.dataset_splitter.addWidget(ranking_panel)
        self.dataset_splitter.setStretchFactor(0, 1)
        self.dataset_splitter.setStretchFactor(1, 4)
        self.dataset_splitter.setSizes([260, 900])

        layout.addLayout(header)
        layout.addWidget(self.dataset_splitter, 1)
        layout.addWidget(self.dataset_empty_state, 1)
        return panel

    def _build_detail_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("InspectorPanel")
        self.inspector_panel = panel
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(560)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self.detail_stack = QStackedWidget()

        summary_page = QWidget()
        summary_layout = QVBoxLayout(summary_page)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.setSpacing(12)

        title_row = QHBoxLayout()
        section_title = QLabel("文献详情")
        section_title.setObjectName("PanelTitle")
        self.detail_hint = QLabel("选择一篇文献开始编辑")
        self.detail_hint.setObjectName("MutedText")
        self.detail_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        title_row.addWidget(section_title, 1)
        title_row.addWidget(self.detail_hint)

        self.inspector_title_label = QLabel("尚未选择文献")
        self.inspector_title_label.setObjectName("InspectorTitle")
        self.inspector_title_label.setWordWrap(True)
        self.inspector_title_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.inspector_meta_label = QLabel("")
        self.inspector_meta_label.setObjectName("MutedText")
        self.inspector_meta_label.setWordWrap(True)

        quick_actions = QHBoxLayout()
        quick_actions.setSpacing(8)

        self.open_pdf_button = self._make_button(
            "打开原文",
            QStyle.StandardPixmap.SP_DialogOpenButton,
            "SoftButton",
        )
        self.open_pdf_button.setToolTip("用系统默认 PDF 阅读器打开当前文献的 PDF 原件。")
        self.open_pdf_button.clicked.connect(self.open_current_pdf)
        self.enter_reader_button = self._make_button(
            "进入阅读",
            QStyle.StandardPixmap.SP_FileDialogContentsView,
            "PrimaryButton",
        )
        self.enter_reader_button.setToolTip("进入沉浸阅读界面：左侧阅读队列，中间 PDF，右侧要点与批注。")
        self.enter_reader_button.clicked.connect(self.enter_reader)
        self.home_translate_button = self._make_button(
            "翻译 PDF", QStyle.StandardPixmap.SP_BrowserReload, "GhostButton"
        )
        self.home_translate_button.setToolTip("翻译当前文献 PDF。")
        self.home_translate_button.clicked.connect(self.translate_current_pdf)
        quick_actions.addWidget(self.open_pdf_button)
        quick_actions.addWidget(self.enter_reader_button)
        quick_actions.addWidget(self.home_translate_button)
        quick_actions.addStretch(1)

        memory_title = QLabel("我的记忆锚点")
        memory_title.setObjectName("FieldTitle")
        self.inspector_memory_label = QLabel("这里会显示最值得记住的一句话。")
        self.inspector_memory_label.setObjectName("InspectorExcerpt")
        self.inspector_memory_label.setWordWrap(True)
        self.inspector_memory_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        abstract_title = QLabel("摘要")
        abstract_title.setObjectName("FieldTitle")
        self.inspector_abstract_label = QLabel("选择文献后显示摘要。")
        self.inspector_abstract_label.setObjectName("InspectorExcerpt")
        self.inspector_abstract_label.setWordWrap(True)
        self.inspector_abstract_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.inspector_categories_label = QLabel("")
        self.inspector_categories_label.setObjectName("InspectorTags")
        self.inspector_categories_label.setWordWrap(True)

        # F13:相关文献推荐(基于标签/主分类/数据集/锚点重叠)
        related_title = QLabel("相关文献")
        related_title.setObjectName("FieldTitle")
        self.related_papers_list = QListWidget()
        self.related_papers_list.setObjectName("RelatedPapersList")
        self.related_papers_list.setMaximumHeight(120)
        self.related_papers_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.related_papers_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.related_papers_list.itemClicked.connect(self._open_related_paper)
        self.related_papers_list.hide()

        summary_footer = QHBoxLayout()
        edit_details = self._make_button(
            "编辑资料",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            "GhostButton",
        )
        edit_details.clicked.connect(self.show_detail_editor)
        ai_parse_quick = self._make_button(
            "AI 解析",
            QStyle.StandardPixmap.SP_BrowserReload,
            "GhostButton",
        )
        ai_parse_quick.clicked.connect(self.ai_analyze_current)
        summary_footer.addWidget(edit_details)
        summary_footer.addWidget(ai_parse_quick)
        summary_footer.addStretch(1)

        # F19:AI 主分类建议条(有建议时显示,应用/忽略)
        self.category_suggestion_bar = QFrame()
        self.category_suggestion_bar.setObjectName("CategorySuggestionBar")
        suggestion_layout = QHBoxLayout(self.category_suggestion_bar)
        suggestion_layout.setContentsMargins(10, 6, 10, 6)
        suggestion_layout.setSpacing(8)
        self.category_suggestion_label = QLabel("")
        self.category_suggestion_label.setObjectName("MutedText")
        self.category_suggestion_label.setWordWrap(True)
        self.category_suggestion_apply = QPushButton("应用")
        self.category_suggestion_apply.setObjectName("TinyButton")
        self.category_suggestion_apply.clicked.connect(self._apply_suggested_category)
        self.category_suggestion_dismiss = QPushButton("忽略")
        self.category_suggestion_dismiss.setObjectName("TinyButton")
        self.category_suggestion_dismiss.clicked.connect(self._clear_suggestion)
        suggestion_layout.addWidget(self.category_suggestion_label, 1)
        suggestion_layout.addWidget(self.category_suggestion_apply)
        suggestion_layout.addWidget(self.category_suggestion_dismiss)
        self.category_suggestion_bar.hide()
        self._pending_suggestion: tuple[int, str] | None = None

        summary_layout.addLayout(title_row)
        summary_layout.addWidget(self.inspector_title_label)
        summary_layout.addWidget(self.inspector_meta_label)
        summary_layout.addLayout(quick_actions)
        summary_layout.addWidget(self.category_suggestion_bar)
        summary_layout.addWidget(memory_title)
        summary_layout.addWidget(self.inspector_memory_label)
        summary_layout.addWidget(abstract_title)
        summary_layout.addWidget(self.inspector_abstract_label)
        summary_layout.addWidget(self.inspector_categories_label)
        summary_layout.addWidget(related_title)
        summary_layout.addWidget(self.related_papers_list)
        summary_layout.addLayout(summary_footer)
        summary_layout.addStretch(1)

        self.editor_page = QFrame()
        self.editor_page.setObjectName("EditorWorkspace")
        editor_layout = QVBoxLayout(self.editor_page)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)

        editor_header = QFrame()
        editor_header.setObjectName("EditorWorkspaceHeader")
        editor_header_layout = QHBoxLayout(editor_header)
        editor_header_layout.setContentsMargins(0, 0, 14, 0)
        editor_header_layout.setSpacing(0)

        header_back_zone = QFrame()
        header_back_zone.setObjectName("EditorHeaderBackZone")
        header_back_zone.setFixedWidth(220)
        header_back_layout = QHBoxLayout(header_back_zone)
        header_back_layout.setContentsMargins(12, 9, 10, 9)
        self.editor_back_button = self._make_button(
            "返回文献库",
            QStyle.StandardPixmap.SP_ArrowBack,
            "EditorBackButton",
        )
        self.editor_back_button.setToolTip("返回文献库；如有未保存修改会先提示。")
        self.editor_back_button.clicked.connect(self.return_from_editor)
        header_back_layout.addWidget(self.editor_back_button)
        header_back_layout.addStretch(1)

        header_context = QFrame()
        header_context.setObjectName("EditorHeaderContext")
        header_context_layout = QHBoxLayout(header_context)
        header_context_layout.setContentsMargins(18, 9, 0, 9)
        header_context_layout.setSpacing(9)
        editor_title = QLabel(f"编辑资料  ·  {APP_NAME}")
        editor_title.setObjectName("EditorWorkspaceTitle")
        self.editor_save_state_label = QLabel("已保存")
        self.editor_save_state_label.setObjectName("EditorSaveState")
        header_context_layout.addWidget(editor_title)
        header_context_layout.addWidget(self.editor_save_state_label)
        header_context_layout.addStretch(1)

        translate_pdf = self._make_button(
            "翻译 PDF",
            QStyle.StandardPixmap.SP_BrowserReload,
            "EditorToolbarButton",
        )
        translate_pdf.setToolTip("调用 BabelDOC 翻译当前 PDF，并尽量保留原始排版。")
        translate_pdf.clicked.connect(self.translate_current_pdf)
        ai_parse = self._make_button(
            "AI 解析",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            "EditorToolbarButton",
        )
        ai_parse.setToolTip("调用大模型补全摘要、方法、贡献、局限和记忆锚点。")
        ai_parse.clicked.connect(self.ai_analyze_current)
        export_bibtex = self._make_button(
            "导出 BibTeX",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            "EditorToolbarButton",
        )
        export_bibtex.setToolTip("把当前文献的 BibTeX 复制到剪贴板。")
        export_bibtex.clicked.connect(self.copy_current_bibtex)

        self.compact_detail_button = QPushButton("只看已填写")
        self.compact_detail_button.setCheckable(True)
        self.compact_detail_button.hide()
        self.compact_detail_button.toggled.connect(self.update_detail_section_visibility)

        more_actions = self._make_button(
            "更多操作",
            QStyle.StandardPixmap.SP_TitleBarUnshadeButton,
            "EditorToolbarButton",
        )
        more_actions.setToolTip("打开当前文献的更多操作。")
        detail_menu = QMenu(more_actions)
        compact_action = detail_menu.addAction("只看已填写")
        compact_action.setCheckable(True)
        compact_action.toggled.connect(self.compact_detail_button.setChecked)
        detail_menu.addSeparator()
        open_translation_action = detail_menu.addAction("打开单语译文")
        open_translation_action.triggered.connect(self.open_current_translation_pdf)
        open_dual_translation_action = detail_menu.addAction("打开双语对照")
        open_dual_translation_action.triggered.connect(self.open_current_dual_translation_pdf)
        detail_menu.addAction("打开原文").triggered.connect(self.open_current_pdf)
        more_actions.setMenu(detail_menu)

        header_context_layout.addWidget(translate_pdf)
        header_context_layout.addWidget(ai_parse)
        header_context_layout.addWidget(export_bibtex)
        header_context_layout.addWidget(more_actions)
        editor_header_layout.addWidget(header_back_zone)
        editor_header_layout.addWidget(header_context, 1)

        overview_page = self._build_overview_tab()
        knowledge_page = self._build_knowledge_tab()
        notes_page = self._build_notes_tab()
        self.pdf_preview = PDFPreviewWidget()

        # Keep the existing four-section state model for shortcuts and compatibility,
        # while presenting the editor as one continuous document.
        self.tabs = QTabWidget(self.editor_page)
        for label in ("基本信息", "研究总结", "阅读批注", "文件与原文"):
            self.tabs.addTab(QWidget(), label)
        self.tabs.hide()

        outline_panel = QFrame()
        outline_panel.setObjectName("EditorOutline")
        outline_panel.setFixedWidth(220)
        outline_layout = QVBoxLayout(outline_panel)
        outline_layout.setContentsMargins(10, 14, 10, 12)
        outline_layout.setSpacing(8)
        outline_title_row = QHBoxLayout()
        outline_title = QLabel("内容大纲")
        outline_title.setObjectName("EditorOutlineTitle")
        outline_menu_icon = QLabel()
        outline_menu_icon.setPixmap(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogListView).pixmap(14, 14)
        )
        outline_title_row.addWidget(outline_title)
        outline_title_row.addStretch(1)
        outline_title_row.addWidget(outline_menu_icon)

        self.editor_outline = QListWidget()
        self.editor_outline.setObjectName("EditorOutlineList")
        self.editor_outline.setFrameShape(QFrame.Shape.NoFrame)
        self.editor_outline.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outline_labels = (
            "文献信息",
            "摘要",
            "研究问题与工作",
            "创新点",
            "个人批注",
        )
        outline_icons = (
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            QStyle.StandardPixmap.SP_FileDialogInfoView,
            QStyle.StandardPixmap.SP_MessageBoxInformation,
            QStyle.StandardPixmap.SP_DialogApplyButton,
            QStyle.StandardPixmap.SP_FileDialogContentsView,
        )
        self.editor_outline.setIconSize(QSize(14, 14))
        for label, icon in zip(outline_labels, outline_icons, strict=True):
            item = QListWidgetItem(QApplication.style().standardIcon(icon), label)
            item.setSizeHint(QSize(0, 34))
            self.editor_outline.addItem(item)
        outline_layout.addLayout(outline_title_row)
        outline_layout.addWidget(self.editor_outline, 1)
        outline_footer = QLabel("Ctrl+S  保存修改")
        outline_footer.setObjectName("EditorOutlineFooter")
        outline_layout.addWidget(outline_footer)

        self.editor_document_scroll = QScrollArea()
        self.editor_document_scroll.setObjectName("EditorDocumentScroll")
        self.editor_document_scroll.setWidgetResizable(True)
        self.editor_document_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.editor_document_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        document_shell = QWidget()
        document_shell.setObjectName("EditorDocumentShell")
        document_shell_layout = QHBoxLayout(document_shell)
        document_shell_layout.setContentsMargins(20, 22, 34, 30)
        document_shell_layout.setSpacing(0)
        document = QFrame()
        document.setObjectName("EditorDocument")
        document.setMaximumWidth(1000)
        document.setMinimumWidth(720)
        document_layout = QVBoxLayout(document)
        document_layout.setContentsMargins(30, 22, 30, 30)
        document_layout.setSpacing(0)

        identity = QFrame()
        identity.setObjectName("EditorIdentity")
        identity_layout = QVBoxLayout(identity)
        identity_layout.setContentsMargins(0, 0, 0, 18)
        identity_layout.setSpacing(5)
        self.title_edit.setObjectName("EditorPaperTitleEdit")
        self.title_edit.setMinimumHeight(42)
        self.title_edit.setMaximumHeight(76)
        self.title_edit.setPlaceholderText("未命名文献")
        self.authors_edit.setObjectName("EditorPaperAuthorsEdit")
        self.authors_edit.setMinimumHeight(30)
        self.authors_edit.setMaximumHeight(52)
        self.authors_edit.setPlaceholderText("作者未知")
        self.editor_paper_title_label = self.title_edit
        identity_layout.addWidget(self.title_edit)
        identity_layout.addWidget(self.authors_edit)
        document_layout.addWidget(identity)
        document_layout.addWidget(overview_page)
        for section_title in (
            "研究问题与工作",
            "创新点",
            "个人批注",
        ):
            document_layout.addWidget(self.detail_section_by_title[section_title])

        # Keep advanced data alive for AI analysis, import/export and existing records,
        # but remove it from the everyday detail-editing experience.
        knowledge_page.setParent(self.editor_page)
        knowledge_page.hide()
        notes_page.setParent(self.editor_page)
        notes_page.hide()

        pdf_section = QFrame()
        pdf_section.setObjectName("EditorPdfSection")
        pdf_section_layout = QVBoxLayout(pdf_section)
        pdf_section_layout.setContentsMargins(0, 16, 0, 0)
        pdf_title = QLabel("文件与原文")
        pdf_title.setObjectName("DocumentSectionTitle")
        pdf_section_layout.addWidget(pdf_title)
        pdf_section_layout.addWidget(self.pdf_preview)
        pdf_section.setParent(self.editor_page)
        pdf_section.hide()
        self.editor_navigation_spacer = QWidget()
        self.editor_navigation_spacer.setObjectName("EditorNavigationSpacer")
        self.editor_navigation_spacer.setFixedHeight(32)
        document_layout.addWidget(self.editor_navigation_spacer)
        document_layout.addStretch(1)

        document_shell_layout.addWidget(document, 1)
        self.editor_document_scroll.setWidget(document_shell)

        editor_body = QFrame()
        editor_body.setObjectName("EditorWorkspaceBody")
        editor_body_layout = QHBoxLayout(editor_body)
        editor_body_layout.setContentsMargins(0, 0, 0, 0)
        editor_body_layout.setSpacing(0)
        editor_body_layout.addWidget(outline_panel)
        editor_body_layout.addWidget(self.editor_document_scroll, 1)

        self.editor_outline_targets = (
            identity,
            self.detail_section_by_title["摘要"],
            self.detail_section_by_title["研究问题与工作"],
            self.detail_section_by_title["创新点"],
            self.detail_section_by_title["个人批注"],
        )
        self.editor_outline_tab_indices = (0, 0, 1, 1, 2)
        self.editor_outline.currentRowChanged.connect(self._navigate_editor_outline)
        self.editor_outline.itemClicked.connect(
            lambda item: self._navigate_editor_outline(self.editor_outline.row(item))
        )
        self.editor_outline.setCurrentRow(0)

        action_bar = QFrame()
        action_bar.setObjectName("EditorActionBar")
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(220, 8, 18, 8)
        action_layout.setSpacing(10)
        self.editor_bottom_state_label = QLabel("所有修改已保存")
        self.editor_bottom_state_label.setObjectName("EditorBottomState")
        self.save_button = self._make_button(
            "保存修改",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            "PrimaryButton",
        )
        self.save_button.setToolTip("保存当前文献的所有编辑内容（Ctrl+S）。")
        self.save_button.setAccessibleDescription("保存当前文献的所有编辑内容，快捷键 Ctrl+S")
        self.save_button.clicked.connect(self.save_current_paper)
        action_layout.addStretch(1)
        action_layout.addWidget(self.editor_bottom_state_label)
        action_layout.addWidget(self.save_button)

        editor_layout.addWidget(editor_header)
        editor_layout.addWidget(editor_body, 1)
        editor_layout.addWidget(action_bar)
        self.detail_stack.addWidget(summary_page)
        layout.addWidget(self.detail_stack, 1)
        return panel

    def _build_reader_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("ReaderShell")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("ReaderHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 10, 14, 10)
        header_layout.setSpacing(10)
        back = self._make_button(
            "返回文献库",
            QStyle.StandardPixmap.SP_ArrowBack,
            "GhostButton",
        )
        back.setToolTip("返回检索工作台；如有未保存批注会先提示。")
        back.clicked.connect(self.leave_reader)
        self.reader_title_label = QLabel("沉浸阅读")
        self.reader_title_label.setObjectName("ReaderTitle")
        self.reader_title_label.setWordWrap(True)
        self.reader_meta_label = QLabel("")
        self.reader_meta_label.setObjectName("MutedText")
        # F21:阅读页手动标记阅读状态(与列表右键菜单、编辑页同一套取值)
        self.reader_status_combo = NoWheelComboBox()
        self.reader_status_combo.addItems(READING_STATUSES)
        self.reader_status_combo.setToolTip("手动标记本文阅读状态")
        self.reader_status_combo.currentIndexChanged.connect(
            self._on_reader_status_changed
        )
        self.reader_status_combo.setCurrentText("待读")
        open_original = self._make_button(
            "系统阅读器",
            QStyle.StandardPixmap.SP_DialogOpenButton,
            "GhostButton",
        )
        open_original.clicked.connect(self.open_current_pdf)
        header_layout.addWidget(back)
        header_layout.addWidget(self.reader_title_label, 1)
        header_layout.addWidget(self.reader_meta_label)
        header_layout.addWidget(QLabel("状态"))
        header_layout.addWidget(self.reader_status_combo)
        header_layout.addWidget(open_original)

        self.reader_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.reader_splitter.setObjectName("ReaderSplitter")

        queue_panel = QFrame()
        queue_panel.setObjectName("ReaderQueue")
        queue_panel.setMinimumWidth(210)
        queue_panel.setMaximumWidth(360)
        queue_layout = QVBoxLayout(queue_panel)
        queue_layout.setContentsMargins(12, 12, 12, 12)
        queue_layout.setSpacing(8)
        queue_title_row = QHBoxLayout()
        queue_title = QLabel("阅读队列")
        queue_title.setObjectName("FieldTitle")
        self.reader_queue_count = QLabel("0 篇")
        self.reader_queue_count.setObjectName("MutedText")
        queue_title_row.addWidget(queue_title, 1)
        queue_title_row.addWidget(self.reader_queue_count)
        self.reader_paper_list = QListWidget()
        self.reader_paper_list.setObjectName("ReaderPaperList")
        self.reader_paper_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.reader_paper_list.setWordWrap(True)
        self.reader_paper_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.reader_paper_list.itemSelectionChanged.connect(self.on_reader_paper_selected)
        queue_layout.addLayout(queue_title_row)
        queue_layout.addWidget(self.reader_paper_list, 1)

        canvas_panel = QFrame()
        canvas_panel.setObjectName("ReaderCanvas")
        canvas_layout = QVBoxLayout(canvas_panel)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        self.reader_pdf_preview = ContinuousPDFReader()
        self.reader_pdf_preview.statusMessage.connect(
            lambda message: self.statusBar().showMessage(message, 5000)
        )
        self.reader_pdf_preview.annotationDirtyChanged.connect(
            self._on_pdf_annotation_dirty_changed
        )
        self.reader_pdf_preview.annotationsChanged.connect(self.refresh_pdf_annotations)
        self.reader_pdf_preview.translateRequested.connect(self.translate_current_pdf)
        canvas_layout.addWidget(self.reader_pdf_preview)

        notes_panel = QFrame()
        notes_panel.setObjectName("ReaderNotes")
        notes_panel.setMinimumWidth(300)
        notes_panel.setMaximumWidth(560)
        notes_layout = QVBoxLayout(notes_panel)
        notes_layout.setContentsMargins(12, 12, 12, 12)
        notes_layout.setSpacing(8)
        self.reader_tabs = QTabWidget()
        self.reader_tabs.setObjectName("ReaderTabs")

        points_page = QWidget()
        points_layout = QVBoxLayout(points_page)
        points_layout.setContentsMargins(8, 10, 8, 8)
        points_layout.setSpacing(10)
        memory_title = QLabel("记忆锚点")
        memory_title.setObjectName("FieldTitle")
        self.reader_memory_label = QTextEdit("选择文献后显示。")
        self.reader_memory_label.setObjectName("ReaderExcerpt")
        self.reader_memory_label.setReadOnly(True)
        self.reader_memory_label.setAcceptRichText(False)
        self.reader_memory_label.setMinimumHeight(96)
        self.reader_memory_label.setMaximumHeight(170)
        summary_title = QLabel("全文总结")
        summary_title.setObjectName("FieldTitle")
        self.reader_summary_label = QTextEdit()
        self.reader_summary_label.setObjectName("ReaderSummary")
        self.reader_summary_label.setReadOnly(True)
        self.reader_summary_label.setAcceptRichText(False)
        self.reader_summary_label.setPlainText("尚未生成总结。")
        generate_points = self._make_button(
            "生成论文要点",
            QStyle.StandardPixmap.SP_BrowserReload,
            "GhostButton",
        )
        generate_points.clicked.connect(self.ai_analyze_current)
        points_layout.addWidget(memory_title)
        points_layout.addWidget(self.reader_memory_label)
        points_layout.addWidget(summary_title)
        points_layout.addWidget(self.reader_summary_label, 1)
        points_layout.addWidget(generate_points)

        notes_page = QWidget()
        reader_notes_layout = QVBoxLayout(notes_page)
        reader_notes_layout.setContentsMargins(8, 10, 8, 8)
        reader_notes_layout.setSpacing(6)

        pdf_annotations_header = QHBoxLayout()
        pdf_annotations_title = QLabel("PDF 批注")
        pdf_annotations_title.setObjectName("FieldTitle")
        self.pdf_annotations_count = QLabel("")
        self.pdf_annotations_count.setObjectName("MutedText")
        export_notes_button = self._make_button(
            "导出 Markdown",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            "TinyButton",
        )
        export_notes_button.setToolTip("把 PDF 批注摘录与我的笔记导出为 Markdown 文件。")
        export_notes_button.clicked.connect(self.export_reader_notes_markdown)
        pdf_annotations_header.addWidget(pdf_annotations_title)
        pdf_annotations_header.addWidget(self.pdf_annotations_count)
        pdf_annotations_header.addStretch(1)
        pdf_annotations_header.addWidget(export_notes_button)

        self.pdf_annotations_list = QListWidget()
        self.pdf_annotations_list.setObjectName("PdfAnnotationsList")
        self.pdf_annotations_list.setWordWrap(True)
        self.pdf_annotations_list.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection
        )
        self.pdf_annotations_list.itemClicked.connect(self._jump_to_pdf_annotation)
        self.pdf_annotations_list.itemDoubleClicked.connect(self._jump_to_pdf_annotation)
        self.pdf_annotations_list.setToolTip("点击任意批注跳转到 PDF 对应页面。")

        notes_title = QLabel("我的笔记")
        notes_title.setObjectName("FieldTitle")
        self.reader_notes_edit = QTextEdit()
        self.reader_notes_edit.setObjectName("ReaderNotesEdit")
        self.reader_notes_edit.setAcceptRichText(False)
        self.reader_notes_edit.setPlaceholderText("记录可复用的观点、疑问、实验想法或与其他论文的联系")
        self.reader_notes_edit.textChanged.connect(self.sync_reader_notes_to_detail)
        save_notes = self._make_button(
            "保存到笔记",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            "PrimaryButton",
        )
        save_notes.clicked.connect(self.save_reader_notes)

        reader_notes_layout.addLayout(pdf_annotations_header)
        reader_notes_layout.addWidget(self.pdf_annotations_list, 2)
        reader_notes_layout.addWidget(notes_title)
        reader_notes_layout.addWidget(self.reader_notes_edit, 1)
        reader_notes_layout.addWidget(save_notes)

        ai_page = QWidget()
        ai_layout = QVBoxLayout(ai_page)
        ai_layout.setContentsMargins(8, 10, 8, 8)
        ai_layout.setSpacing(10)
        ai_title = QLabel("围绕当前论文继续提炼")
        ai_title.setObjectName("FieldTitle")
        ai_hint = QLabel(
            "AI 解析会更新摘要、方法、贡献、局限和记忆锚点。发送前仍会显示内容范围与隐私确认。"
        )
        ai_hint.setObjectName("ReaderExcerpt")
        ai_hint.setWordWrap(True)
        analyze = self._make_button(
            "AI 解析当前论文",
            QStyle.StandardPixmap.SP_BrowserReload,
            "PrimaryButton",
        )
        analyze.clicked.connect(self.ai_analyze_current)
        discuss = self._make_button(
            "与 AI 讨论这篇文章",
            QStyle.StandardPixmap.SP_MessageBoxQuestion,
            "PrimaryButton",
        )
        discuss.clicked.connect(self.open_paper_chat)
        ai_layout.addWidget(ai_title)
        ai_layout.addWidget(ai_hint)
        ai_layout.addStretch(1)
        ai_layout.addWidget(discuss)
        ai_layout.addWidget(analyze)

        self.reader_tabs.addTab(points_page, "要点")
        self.reader_tabs.addTab(notes_page, "批注")
        self.reader_tabs.addTab(ai_page, "AI 助手")
        self.reader_tabs.currentChanged.connect(
            lambda index: (
                self.refresh_pdf_annotations()
                if self.reader_tabs.tabText(index) == "批注"
                else None
            )
        )
        notes_layout.addWidget(self.reader_tabs)

        self.reader_splitter.addWidget(queue_panel)
        self.reader_splitter.addWidget(canvas_panel)
        self.reader_splitter.addWidget(notes_panel)
        self.reader_splitter.setStretchFactor(0, 0)
        self.reader_splitter.setStretchFactor(1, 1)
        self.reader_splitter.setStretchFactor(2, 0)
        self.reader_splitter.setSizes([230, 850, 330])

        layout.addWidget(header)
        layout.addWidget(self.reader_splitter, 1)
        return page

    def _build_overview_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("EditorOverviewSection")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.title_edit = QTextEdit()
        self.authors_edit = QTextEdit()
        self.year_edit = NoWheelSpinBox()
        self.year_edit.setRange(0, 2200)
        self.year_edit.setSpecialValueText("未知")
        self.venue_edit = QTextEdit()
        self.doi_edit = QLineEdit()
        self.arxiv_edit = QLineEdit()
        self.url_edit = QTextEdit()
        self.pdf_path_edit = QTextEdit()
        self.category_combo = NoWheelComboBox()
        self.category_combo.setEditable(True)
        self.category_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.category_combo.lineEdit().setPlaceholderText("搜索或输入新分类")
        self.category_combo.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.category_combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.category_combo.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        # F19 单主分类:下拉即主分类,不再有 chip 列表与添加/移除按钮
        self._suppress_category_signal = False
        self.category_combo.currentIndexChanged.connect(self._on_primary_category_changed)
        self.status_combo = NoWheelComboBox()
        self.status_combo.addItems(READING_STATUSES)
        self.importance_edit = NoWheelSpinBox()
        self.importance_edit.setRange(1, 5)
        self.abstract_edit = AutoHeightJustifiedTextEdit(108)
        self.abstract_edit.setMinimumHeight(108)
        self.abstract_edit.setPlaceholderText("记录原始摘要，或先留空，后续可由 PDF / AI 自动补全。")
        self.abstract_lang_button = QPushButton("中 / 英")
        self.abstract_lang_button.setObjectName("GhostButton")
        self.abstract_lang_button.setToolTip("在原始摘要和中文译文之间切换；中文译文不会覆盖原文。")
        self.abstract_lang_button.clicked.connect(self.toggle_abstract_language)
        self._prepare_compact_text_edits(
            self.title_edit,
            self.authors_edit,
            self.venue_edit,
            self.url_edit,
            self.pdf_path_edit,
        )
        self._prepare_line_edits(self.doi_edit, self.arxiv_edit)
        self._prepare_text_edits(self.abstract_edit)
        self.pdf_path_edit.textChanged.connect(self.update_pdf_action_state)
        self.url_edit.textChanged.connect(self.update_pdf_action_state)
        self.arxiv_edit.textChanged.connect(self.update_pdf_action_state)

        metadata_title = QLabel("文献信息")
        metadata_title.setObjectName("DocumentSectionTitle")
        self.enrich_metadata_button = QPushButton("按 DOI / arXiv 补全")
        self.enrich_metadata_button.setObjectName("TinyButton")
        self.enrich_metadata_button.setToolTip(
            "根据 DOI(CrossRef)或 arXiv ID 联网查询官方元数据,只填写空缺字段,不覆盖已填写内容。"
        )
        self.enrich_metadata_button.clicked.connect(self.enrich_metadata_from_public_api)
        metadata_title_row = QHBoxLayout()
        metadata_title_row.addWidget(metadata_title)
        metadata_title_row.addStretch(1)
        metadata_title_row.addWidget(self.enrich_metadata_button)
        metadata_card = QFrame()
        metadata_card.setObjectName("EditorMetadataCard")
        grid = QGridLayout(metadata_card)
        grid.setContentsMargins(0, 10, 0, 14)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        metadata_rows = (
            ("年份", self.year_edit, "来源", self.venue_edit),
            ("DOI", self.doi_edit, "arXiv ID", self.arxiv_edit),
            ("原文链接", self.url_edit, "PDF 文件", self.pdf_path_edit),
            ("阅读状态", self.status_combo, "重要程度", self.importance_edit),
        )
        for row, (left_label, left_widget, right_label, right_widget) in enumerate(metadata_rows):
            left = QLabel(left_label)
            left.setObjectName("MetadataLabel")
            right = QLabel(right_label)
            right.setObjectName("MetadataLabel")
            grid.addWidget(left, row, 0)
            grid.addWidget(left_widget, row, 1)
            grid.addWidget(right, row, 2)
            grid.addWidget(right_widget, row, 3)

        layout.addLayout(metadata_title_row)
        layout.addWidget(metadata_card)
        layout.addWidget(self._category_tag_area())
        layout.addWidget(self._tag_area())
        layout.addWidget(self._text_area("摘要", self.abstract_edit, self.abstract_lang_button))
        return tab

    def _tag_area(self) -> QWidget:
        """自由标签编辑区:输入回车即创建并附加,列表点 × 移除。"""
        field = QWidget()
        field.setObjectName("TextAreaField")
        field.setMinimumWidth(0)
        layout = QVBoxLayout(field)
        layout.setContentsMargins(0, 4, 0, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        label = QLabel("标签")
        label.setObjectName("FieldTitle")
        header.addWidget(label, 1)

        self.tag_input = QLineEdit()
        self.tag_input.setObjectName("TagInput")
        self.tag_input.setPlaceholderText("输入标签名,回车添加(如:复现必看、方法借鉴)")
        self.tag_input.returnPressed.connect(self.add_tag_from_input)
        header.addWidget(self.tag_input, 2)

        layout.addLayout(header)
        self.tag_chips_flow = QWidget()
        self.tag_chips_layout = QHBoxLayout(self.tag_chips_flow)
        self.tag_chips_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_chips_layout.setSpacing(6)
        self.tag_chips_layout.addStretch(1)
        layout.addWidget(self.tag_chips_flow)
        return field

    def add_tag_from_input(self) -> None:
        if self.current_paper_id is None:
            return
        name = self.tag_input.text().strip()
        if not name:
            return
        tag = self.repository.create_tag(name)
        ids = [t.id for t in self._current_paper_tags()]
        if tag.id not in ids:
            ids.append(tag.id)
            self._save_paper_tags(ids)
        self.tag_input.clear()

    def _current_paper_tags(self) -> list:
        if self.current_paper_id is None:
            return []
        paper = self.repository.get_paper(self.current_paper_id, include_text_content=False)
        return list(paper.tags) if paper is not None else []

    def _save_paper_tags(self, tag_ids: list[int]) -> None:
        paper = (
            self.repository.get_paper(self.current_paper_id, include_text_content=False)
            if self.current_paper_id is not None
            else None
        )
        if paper is None:
            return
        update = PaperUpdate(
            title=paper.title,
            authors=paper.authors,
            year=paper.year,
            venue=paper.venue,
            doi=paper.doi,
            arxiv_id=paper.arxiv_id,
            url=paper.url,
            abstract=paper.abstract,
            pdf_path=paper.pdf_path or "",
            reading_status=paper.reading_status,
            importance=paper.importance,
            summary=paper.detail.summary if paper.detail else "",
            datasets=paper.detail.datasets if paper.detail else "",
            dataset_metrics=paper.detail.dataset_metrics if paper.detail else "",
            methods=paper.detail.methods if paper.detail else "",
            contributions=paper.detail.contributions if paper.detail else "",
            limitations=paper.detail.limitations if paper.detail else "",
            experiment_notes=paper.detail.experiment_notes if paper.detail else "",
            reproducibility_notes=paper.detail.reproducibility_notes if paper.detail else "",
            memory_anchor=paper.detail.memory_anchor if paper.detail else "",
            personal_notes=paper.detail.personal_notes if paper.detail else "",
        )
        self.repository.update_paper(
            self.current_paper_id,
            update,
            category_ids=[c.id for c in paper.categories],
            tag_ids=tag_ids,
        )
        self.refresh_tag_chips()
        self.refresh_tag_rail()
        self.refresh_categories()

    def refresh_tag_chips(self) -> None:
        while self.tag_chips_layout.count() > 1:
            taken = self.tag_chips_layout.takeAt(0)
            widget = taken.widget()
            if widget is not None:
                widget.deleteLater()
        tags = self._current_paper_tags()
        for tag in tags:
            chip = QPushButton(f"{tag.name} ×")
            chip.setObjectName("TagChip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip("点击移除该标签(不影响标签本身)")
            chip.clicked.connect(
                lambda _checked=False, tag_id=tag.id: self._save_paper_tags(
                    [t.id for t in self._current_paper_tags() if t.id != tag_id]
                )
            )
            self.tag_chips_layout.insertWidget(
                self.tag_chips_layout.count() - 1, chip
            )
        if not tags:
            hint = QLabel("还没有标签;在上方输入并回车即可添加。")
            hint.setObjectName("MutedText")
            self.tag_chips_layout.insertWidget(0, hint)

    def refresh_tag_rail(self) -> None:
        """刷新左侧「标签」区(全部标签+计数,点击筛选)。"""
        layout = getattr(self, "tag_rail_layout", None)
        if layout is None:
            return
        while layout.count():
            taken = layout.takeAt(0)
            widget = taken.widget()
            if widget is not None:
                widget.deleteLater()
        counts = self.repository.tag_paper_counts()
        tags = self.repository.list_tags()
        if not tags:
            empty = QLabel("暂无标签")
            empty.setObjectName("MutedText")
            layout.addWidget(empty)
        for tag in tags:
            count = counts.get(tag.id, 0)
            button = QPushButton(f"{tag.name}  {count}")
            button.setObjectName("QuickViewButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(
                "点击筛选带此标签的文献;再次点击取消筛选。\n右键可重命名、改颜色或删除标签。"
            )
            button.setCheckable(True)
            button.setChecked(self.current_tag_id == tag.id)
            if tag.color and tag.color != "#6b7280":
                button.setStyleSheet(
                    f"QPushButton#QuickViewButton {{ border-left: 3px solid {tag.color}; }}"
                )
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda _pos, tag_id=tag.id, sender=button: self.open_tag_menu(tag_id, sender)
            )
            button.clicked.connect(
                lambda checked, tag_id=tag.id: self.toggle_tag_filter(tag_id)
            )
            layout.addWidget(button)

    def _exec_tag_menu(self, menu: QMenu, button: QPushButton):
        """菜单弹出点(单独提出便于测试替换;QMenu.exec 是 C++ 绑定,无法 monkeypatch)。"""
        return menu.exec(button.mapToGlobal(button.rect().center()))

    def open_tag_menu(self, tag_id: int, button: QPushButton) -> None:
        """左侧标签栏右键菜单:重命名 / 改颜色 / 删除标签本身。"""
        tag = next((t for t in self.repository.list_tags() if t.id == tag_id), None)
        if tag is None:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("重命名")
        color_action = menu.addAction("改颜色…")
        menu.addSeparator()
        count = self.repository.tag_paper_counts().get(tag_id, 0)
        delete_label = (
            f"删除标签(从 {count} 篇文献上移除)" if count else "删除标签"
        )
        delete_action = menu.addAction(delete_label)
        chosen = self._exec_tag_menu(menu, button)
        if chosen is rename_action:
            new_name, accepted = QInputDialog.getText(
                self, "重命名标签", "新的标签名：", text=tag.name
            )
            if not accepted or not new_name.strip() or new_name.strip() == tag.name:
                return
            try:
                self.repository.rename_tag(tag_id, new_name)
            except Exception as exc:
                QMessageBox.warning(self, "重命名失败", f"无法重命名标签：{exc}")
                return
            self.refresh_tag_rail()
            self.refresh_tag_chips()
        elif chosen is color_action:
            candidate = QColor(tag.color)
            initial = candidate if candidate.isValid() else QColor("#6b7280")
            color = QColorDialog.getColor(initial, self, "标签颜色")
            if not color.isValid():
                return
            self.repository.set_tag_color(tag_id, color.name())
            self.refresh_tag_rail()
        elif chosen is delete_action:
            if not confirm_action(
                self,
                title=f"删除标签「{tag.name}」",
                message=(
                    f"这个标签挂在 {count} 篇文献上,删除后这些文献将不再带此标签。"
                    if count
                    else "这个标签还没有挂在任何文献上。"
                ),
                detail="文献本身不受影响,只移除标签与关联。",
                confirm_label="删除标签",
                destructive=True,
            ):
                return
            self.repository.delete_tag(tag_id)
            if self.current_tag_id == tag_id:
                self.current_tag_id = None
                self.refresh_papers()
            self.refresh_tag_rail()
            self.refresh_tag_chips()

    def toggle_tag_filter(self, tag_id: int) -> None:
        self.current_tag_id = None if self.current_tag_id == tag_id else tag_id
        self.refresh_papers()
        self.refresh_tag_rail()
    def _build_knowledge_tab(self) -> QWidget:
        tab = QWidget()
        form = self._make_form(tab)

        self.summary_edit = AutoHeightJustifiedTextEdit(108)
        self.datasets_edit = QTextEdit()
        self.dataset_metrics_edit = QTextEdit()
        self.dataset_metrics_edit.setVisible(False)
        self.dataset_metrics_table = QTableWidget(0, 6)
        self.dataset_metrics_table.setObjectName("MetricEditTable")
        self.dataset_metrics_table.setHorizontalHeaderLabels(["数据集", "指标", "结果", "模型/方法", "任务", "说明"])
        self.dataset_metrics_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.dataset_metrics_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.dataset_metrics_table.setAlternatingRowColors(True)
        self.dataset_metrics_table.setWordWrap(True)
        self.dataset_metrics_table.verticalHeader().setVisible(False)
        self.dataset_metrics_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.dataset_metrics_table.setMinimumHeight(190)
        self.dataset_metrics_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.dataset_metrics_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.dataset_metrics_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.dataset_metrics_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.dataset_metrics_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.dataset_metrics_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.methods_edit = QTextEdit()
        self.contributions_edit = AutoHeightJustifiedTextEdit(96)
        self.limitations_edit = QTextEdit()

        placeholders = {
            self.summary_edit: "说明这篇文献属于什么领域、针对什么任务、解决什么问题，以及具体做了什么。",
            self.datasets_edit: "例如：ImageNet; COCO; Cityscapes",
            self.methods_edit: "例如：Transformer; Low-rank; Distillation",
            self.contributions_edit: "记录真正值得回忆的创新点。",
            self.limitations_edit: "记录适用边界、失败场景或你不认同的地方。",
        }
        for editor, placeholder in placeholders.items():
            editor.setPlaceholderText(placeholder)
        self.dataset_metrics_edit.setPlaceholderText(
            "每行一条实验结果，例如：dataset: ImageNet; metric: Top-1 Acc; value: 84.5%; model: ResearchWeaveNet; task: classification; notes: Table 1"
        )
        self.summary_edit.setMinimumHeight(108)
        self.datasets_edit.setMinimumHeight(72)
        self.datasets_edit.setMaximumHeight(112)
        self.dataset_metrics_edit.setMinimumHeight(160)
        self.methods_edit.setMinimumHeight(96)
        self.methods_edit.setMaximumHeight(152)
        self.contributions_edit.setMinimumHeight(96)
        self.limitations_edit.setMinimumHeight(96)
        self.limitations_edit.setMaximumHeight(152)
        self._prepare_text_edits(
            self.summary_edit,
            self.datasets_edit,
            self.dataset_metrics_edit,
            self.methods_edit,
            self.contributions_edit,
            self.limitations_edit,
        )

        form.addRow(self._text_area("研究问题与工作", self.summary_edit))
        form.addRow(self._text_area("数据集", self.datasets_edit))
        form.addRow(self._metric_table_area("数据集指标", self.dataset_metrics_table))
        form.addRow(self._text_area("方法", self.methods_edit))
        form.addRow(self._text_area("创新点", self.contributions_edit))
        form.addRow(self._text_area("局限性", self.limitations_edit))
        return tab

    def _build_notes_tab(self) -> QWidget:
        tab = QWidget()
        form = self._make_form(tab)

        self.memory_anchor_edit = QTextEdit()
        self.experiment_notes_edit = QTextEdit()
        self.reproducibility_notes_edit = QTextEdit()
        self.personal_notes_edit = AutoHeightJustifiedTextEdit(220)

        self.memory_anchor_edit.setMinimumHeight(76)
        self.memory_anchor_edit.setMaximumHeight(120)
        self.memory_anchor_edit.setPlaceholderText("写下未来可能凭什么想起这篇文献，例如：动态生成卷积核；attention map 做卷积；上采样低通滤波。")
        self.experiment_notes_edit.setMinimumHeight(116)
        self.experiment_notes_edit.setMaximumHeight(180)
        self.experiment_notes_edit.setPlaceholderText("实验设置、指标、对比方法、表格结论。")
        self.reproducibility_notes_edit.setMinimumHeight(116)
        self.reproducibility_notes_edit.setMaximumHeight(180)
        self.reproducibility_notes_edit.setPlaceholderText("代码仓库、环境、命令、踩坑点。")
        self.personal_notes_edit.setMinimumHeight(220)
        self.personal_notes_edit.setPlaceholderText("这里放最自由的阅读批注、灵感和后续想法。")
        self._prepare_text_edits(
            self.memory_anchor_edit,
            self.experiment_notes_edit,
            self.reproducibility_notes_edit,
            self.personal_notes_edit,
        )

        form.addRow(self._text_area("记忆锚点", self.memory_anchor_edit))
        form.addRow(self._text_area("实验记录", self.experiment_notes_edit))
        form.addRow(self._text_area("复现记录", self.reproducibility_notes_edit))
        form.addRow(self._text_area("个人批注", self.personal_notes_edit))
        return tab

    def _text_area(self, title: str, editor: QTextEdit, extra_button: QPushButton | None = None) -> QWidget:
        field = QWidget()
        field.setObjectName("TextAreaField")
        field.setMinimumWidth(0)
        layout = QVBoxLayout(field)
        layout.setContentsMargins(0, 4, 0, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("FieldTitle")
        header.addWidget(label, 1)
        if extra_button is not None:
            extra_button.setCursor(Qt.CursorShape.PointingHandCursor)
            extra_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            header.addWidget(extra_button)
        if not isinstance(editor, AutoHeightJustifiedTextEdit):
            expand = QPushButton("展开")
            expand.setObjectName("GhostButton")
            expand.setToolTip("在更大的弹窗里编辑这段长文本。")
            expand.setCursor(Qt.CursorShape.PointingHandCursor)
            expand.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            expand.clicked.connect(lambda: self.open_large_text_editor(title, editor))
            header.addWidget(expand)

        layout.addLayout(header)
        layout.addWidget(editor)
        self.detail_sections.append((field, editor))
        self.detail_section_by_title[title] = field
        return field

    def _category_tag_area(self) -> QWidget:
        field = QWidget()
        field.setObjectName("TextAreaField")
        field.setMinimumWidth(0)
        layout = QVBoxLayout(field)
        layout.setContentsMargins(0, 4, 0, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        label = QLabel("主分类")
        label.setObjectName("FieldTitle")
        header.addWidget(label, 1)

        selector = QHBoxLayout()
        selector.setSpacing(8)
        self.category_combo.setToolTip("当前文献的主分类(每篇唯一);切换后 Ctrl+S 保存生效。")
        selector.addWidget(self.category_combo, 1)

        layout.addLayout(header)
        layout.addLayout(selector)
        return field
    def _metric_table_area(self, title: str, table: QTableWidget) -> QWidget:
        field = QWidget()
        field.setObjectName("TextAreaField")
        field.setMinimumWidth(0)
        layout = QVBoxLayout(field)
        layout.setContentsMargins(0, 4, 0, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("FieldTitle")
        add_row = QPushButton("添加行")
        add_row.setObjectName("TinyButton")
        add_row.setToolTip("在当前文献的数据集指标表中新增一行手动记录。")
        add_row.setCursor(Qt.CursorShape.PointingHandCursor)
        add_row.clicked.connect(self.add_dataset_metric_row)
        remove_row = QPushButton("删除行")
        remove_row.setObjectName("TinyButton")
        remove_row.setToolTip("删除当前选中的数据集指标行；只影响当前文献详情表。")
        remove_row.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_row.clicked.connect(self.remove_dataset_metric_row)
        header.addWidget(label, 1)
        header.addWidget(add_row)
        header.addWidget(remove_row)

        layout.addLayout(header)
        layout.addWidget(table)
        self.detail_sections.append((field, table))
        self.detail_section_by_title[title] = field
        return field

    def update_detail_section_visibility(self, *_args) -> None:
        compact = self.compact_detail_button.isChecked()
        for field, editor in self.detail_sections:
            if isinstance(editor, QTextEdit):
                has_content = bool(editor.toPlainText().strip())
            else:
                has_content = editor.rowCount() > 0
            field.setVisible(not compact or has_content)

    def add_dataset_metric_row(self) -> None:
        self.dataset_metrics_table.insertRow(self.dataset_metrics_table.rowCount())
        self._mark_dirty()

    def remove_dataset_metric_row(self) -> None:
        row = self.dataset_metrics_table.currentRow()
        if row >= 0:
            self.dataset_metrics_table.removeRow(row)
            self._mark_dirty()

    def _connect_dirty_tracking(self) -> None:
        text_edits = [
            self.title_edit,
            self.authors_edit,
            self.venue_edit,
            self.url_edit,
            self.pdf_path_edit,
            self.abstract_edit,
            self.summary_edit,
            self.datasets_edit,
            self.dataset_metrics_edit,
            self.methods_edit,
            self.contributions_edit,
            self.limitations_edit,
            self.memory_anchor_edit,
            self.experiment_notes_edit,
            self.reproducibility_notes_edit,
            self.personal_notes_edit,
        ]
        for editor in text_edits:
            editor.textChanged.connect(self._mark_dirty)
        for editor in (self.doi_edit, self.arxiv_edit):
            editor.textChanged.connect(self._mark_dirty)
        for spin_box in (self.year_edit, self.importance_edit):
            spin_box.valueChanged.connect(self._mark_dirty)
        self.status_combo.currentIndexChanged.connect(self._mark_dirty)
        self.dataset_metrics_table.itemChanged.connect(self._mark_dirty)
        self.title_edit.textChanged.connect(self.update_editor_workspace_identity)
        self.authors_edit.textChanged.connect(self.update_editor_workspace_identity)

    def _mark_dirty(self, *_args) -> None:
        if self._suppress_dirty_tracking or self.current_paper_id is None:
            return
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        changed = self._dirty != dirty
        self._dirty = dirty
        pdf_dirty = (
            hasattr(self, "reader_pdf_preview")
            and self.reader_pdf_preview.has_unsaved_annotations
        )
        self.setWindowTitle(
            f"{APP_DISPLAY_NAME} *" if dirty or pdf_dirty else APP_DISPLAY_NAME
        )
        if hasattr(self, "editor_save_state_label"):
            self.editor_save_state_label.setText("有未保存修改" if dirty else "已保存")
            self.editor_save_state_label.setProperty("dirty", dirty)
            self.editor_save_state_label.style().unpolish(self.editor_save_state_label)
            self.editor_save_state_label.style().polish(self.editor_save_state_label)
        if hasattr(self, "editor_bottom_state_label"):
            self.editor_bottom_state_label.setText(
                "有未保存修改 · Ctrl+S 保存" if dirty else "所有修改已保存"
            )
            self.editor_bottom_state_label.setProperty("dirty", dirty)
            self.editor_bottom_state_label.style().unpolish(self.editor_bottom_state_label)
            self.editor_bottom_state_label.style().polish(self.editor_bottom_state_label)
        if changed and dirty:
            self.statusBar().showMessage("有未保存的修改")

    def update_editor_workspace_identity(self, *_args) -> None:
        if not hasattr(self, "title_edit"):
            return
        title = self._field_text(self.title_edit).strip() or "未命名文献"
        authors = self._field_text(self.authors_edit).strip() or "作者未知"
        self.title_edit.setAccessibleName(f"文献标题：{title}")
        self.authors_edit.setAccessibleName(f"文献作者：{authors}")

    def _navigate_editor_outline(self, row: int) -> None:
        if row < 0 or row >= len(self.editor_outline_targets):
            return
        tab_index = self.editor_outline_tab_indices[row]
        self.tabs.setCurrentIndex(tab_index)
        target = self.editor_outline_targets[row]
        viewport_height = self.editor_document_scroll.viewport().height()
        spacer_height = max(32, viewport_height - 48)
        self.editor_navigation_spacer.setFixedHeight(spacer_height)
        self.editor_navigation_spacer.updateGeometry()
        QTimer.singleShot(
            0,
            lambda target=target: self._scroll_editor_target_to_top(target),
        )

    def _scroll_editor_target_to_top(self, target: QWidget) -> None:
        self._align_editor_target_to_top(target)
        QTimer.singleShot(0, lambda target=target: self._align_editor_target_to_top(target))

    def _align_editor_target_to_top(self, target: QWidget) -> None:
        viewport = self.editor_document_scroll.viewport()
        scroll_bar = self.editor_document_scroll.verticalScrollBar()
        visible_top = target.mapTo(viewport, target.rect().topLeft()).y()
        desired = scroll_bar.value() + visible_top - 24
        scroll_bar.setValue(max(0, min(desired, scroll_bar.maximum())))

    def copy_current_bibtex(self) -> None:
        if self.current_paper_id is None:
            return
        paper = self.repository.get_paper(self.current_paper_id)
        if paper is None:
            return
        from synthrecall.citation_export import paper_to_bibtex

        bibtex = paper_to_bibtex(paper, set())
        QApplication.clipboard().setText(bibtex)
        self.statusBar().showMessage("BibTeX 已复制到剪贴板", 3000)

    @contextmanager
    def _suspend_dirty_tracking(self):
        previous = self._suppress_dirty_tracking
        self._suppress_dirty_tracking = True
        try:
            yield
        finally:
            self._suppress_dirty_tracking = previous

    def _prompt_unsaved_changes(self) -> QMessageBox.StandardButton:
        action = ConfirmationDialog(
            title="当前修改还没有保存",
            message=f"先处理这些修改，再继续切换页面或关闭 {APP_NAME}。",
            detail="保存会写入当前文献；放弃修改会恢复到上一次保存的内容。",
            eyebrow="未保存修改",
            tone="warning",
            actions=(
                DialogAction("cancel", "取消"),
                DialogAction("discard", "放弃修改", "danger-soft"),
                DialogAction("save", "保存并继续", "primary", default=True),
            ),
            parent=self,
        )
        return {
            "save": QMessageBox.StandardButton.Save,
            "discard": QMessageBox.StandardButton.Discard,
            "cancel": QMessageBox.StandardButton.Cancel,
        }[action.exec_action()]

    def _confirm_pending_changes(self) -> bool:
        pdf_dirty = (
            hasattr(self, "reader_pdf_preview")
            and self.reader_pdf_preview.has_unsaved_annotations
        )
        if not self._dirty and not pdf_dirty:
            return True
        choice = self._prompt_unsaved_changes()
        if choice == QMessageBox.StandardButton.Save:
            return self.save_active_content(refresh=False)
        if choice == QMessageBox.StandardButton.Discard:
            paper_id = self.current_paper_id
            if pdf_dirty:
                self.reader_pdf_preview.discard_annotations()
            self._set_dirty(False)
            if paper_id is not None:
                self.load_paper(paper_id)
            return True
        return False
    def _prepare_line_edits(self, *edits: QLineEdit) -> None:
        for edit in edits:
            edit.setMinimumWidth(0)
            edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def _prepare_compact_text_edits(self, *edits: QTextEdit) -> None:
        for edit in edits:
            edit.setMinimumWidth(0)
            edit.setAcceptRichText(False)
            edit.setObjectName("CompactTextEdit")
            edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            edit.setTabChangesFocus(True)
            edit.setMinimumHeight(54)
            edit.setMaximumHeight(92)
            edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def _prepare_text_edits(self, *edits: QTextEdit) -> None:
        for edit in edits:
            edit.setMinimumWidth(0)
            edit.setAcceptRichText(False)
            edit.setObjectName("FormulaText")
            edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            if isinstance(edit, AutoHeightJustifiedTextEdit):
                edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            else:
                edit.setSizePolicy(
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.MinimumExpanding,
                )

    def _field_text(self, widget: QLineEdit | QTextEdit) -> str:
        if isinstance(widget, QTextEdit):
            return widget.toPlainText()
        return widget.text()

    def _set_field_text(self, widget: QLineEdit | QTextEdit, text: str) -> None:
        if isinstance(widget, QTextEdit):
            widget.setPlainText(text)
            return
        widget.setText(text)

    def open_large_text_editor(self, title: str, editor: QTextEdit) -> None:
        dialog = LargeTextDialog(title, editor.toPlainText(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            editor.setPlainText(dialog.text())

    def set_dataset_metrics_table_text(self, text: str) -> None:
        self.dataset_metrics_table.setRowCount(0)
        rows = [
            row
            for row in parse_dataset_metrics_text(text)
            if is_primary_dataset_metric(row)
        ]
        for row_data in rows:
            row_index = self.dataset_metrics_table.rowCount()
            self.dataset_metrics_table.insertRow(row_index)
            values = [
                canonical_dataset_name(row_data.get("dataset", "")),
                row_data.get("metric", ""),
                row_data.get("value", ""),
                row_data.get("model", ""),
                row_data.get("task", ""),
                row_data.get("notes", ""),
            ]
            for column, value in enumerate(values):
                self.dataset_metrics_table.setItem(row_index, column, QTableWidgetItem(value))
        self.dataset_metrics_table.resizeRowsToContents()

    def dataset_metrics_table_text(self) -> str:
        lines: list[str] = []
        keys = ["dataset", "metric", "value", "model", "task", "notes"]
        for row in range(self.dataset_metrics_table.rowCount()):
            values = []
            for column, key in enumerate(keys):
                item = self.dataset_metrics_table.item(row, column)
                text = item.text().strip() if item is not None else ""
                values.append((key, text))
            row_data = {key: value for key, value in values}
            if not row_data["dataset"] or not row_data["metric"] or not row_data["value"]:
                continue
            row_data["dataset"] = canonical_dataset_name(row_data["dataset"])
            values[0] = ("dataset", row_data["dataset"])
            lines.append("; ".join(f"{key}: {value}" for key, value in values if value))
        return "\n".join(lines)

    def _make_form(self, parent: QWidget) -> QFormLayout:
        form = QFormLayout(parent)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(0)
        form.setVerticalSpacing(0)
        return form

    def _make_button(
        self,
        text: str,
        icon: QStyle.StandardPixmap,
        object_name: str,
    ) -> QPushButton:
        style = QApplication.style()
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setIcon(style.standardIcon(icon))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        return button

    def _set_active_workspace(self, workspace: str) -> None:
        for name, button in self.workspace_buttons.items():
            button.setProperty("active", name == workspace)
            button.style().unpolish(button)
            button.style().polish(button)
        nav_buttons = getattr(self, "header_nav_buttons", None)
        if nav_buttons:
            for name, button in nav_buttons.items():
                button.setChecked(name == workspace)

    def _apply_style(self) -> None:
        assets = Path(__file__).resolve().parent.parent / "assets"
        spin_icon = {
            "%SPIN_UP%": assets / "spin_up.png",
            "%SPIN_UP_HOVER%": assets / "spin_up_hover.png",
            "%SPIN_DOWN%": assets / "spin_down.png",
            "%SPIN_DOWN_HOVER%": assets / "spin_down_hover.png",
        }
        style = """
            QMainWindow {
                background: #fff7ed;
            }
            * {
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "SimSun";
                font-size: %BASE_FONT_SIZE%;
            }
            QWidget#Root {
                background: #fff7ed;
            }
            QFrame#AppHeader {
                background: #fffdf8;
                border: 1px solid #f4d6c6;
                border-radius: 14px;
            }
            QFrame#HeaderNav {
                background: #f1ece3;
                border-radius: 9px;
            }
            QPushButton#HeaderNavButton {
                background: transparent;
                border: none;
                border-radius: 7px;
                padding: 5px 14px;
                color: #52606f;
                font-weight: 600;
            }
            QPushButton#HeaderNavButton:hover {
                background: #e6dfd2;
            }
            QPushButton#HeaderNavButton:checked {
                background: #fffdf8;
                color: #0f766e;
            }
            QLabel#HeroTitle {
                color: #243447;
                font-size: 28px;
                font-weight: 800;
                letter-spacing: 0;
            }
            QLabel#HeroSubtitle {
                color: #6b7280;
                font-size: 13px;
            }
            QLabel#HeaderPill {
                background: #ecfeff;
                color: #0f766e;
                border: 1px solid #99f6e4;
                border-radius: 12px;
                padding: 7px 12px;
                font-weight: 700;
            }
            QFrame#Panel {
                background: #fffdf8;
                border: 1px solid #f1d9ca;
                border-radius: 14px;
            }
            QFrame#SubPanel {
                background: #ffffff;
                border: 1px solid #e5edf3;
                border-radius: 12px;
            }
            QLabel#PanelTitle {
                color: #243447;
                font-size: 18px;
                font-weight: 750;
                letter-spacing: 0;
            }
            QLabel#MutedText {
                color: #7c8490;
                font-size: 12px;
            }
            QLabel#FieldTitle, QLabel#DialogTitle {
                color: #243447;
                font-size: 14px;
                font-weight: 800;
            }
            QLabel#SpinnerLabel {
                color: #14b8a6;
                font-size: 42px;
                font-weight: 900;
            }
            QLabel#PdfPreview {
                background: #f8fafc;
                color: #64748b;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
                padding: 12px;
            }
            QScrollArea#ReaderPdfScroll, QWidget#ReaderPagesContainer {
                background: #e9eef4;
                border: none;
            }
            QLabel#ReaderPdfPage {
                background: #ffffff;
                color: #64748b;
                border: 1px solid #d8dee8;
            }
            QPushButton#ReaderModeButton {
                min-width: 76px;
                background: #f8fafc;
                color: #64748b;
                border: 1px solid #d8e0e8;
            }
            QPushButton#ReaderModeButton:hover {
                color: #0f766e;
                border-color: #8bd2c8;
            }
            QPushButton#ReaderModeButton:checked {
                background: #dff5f1;
                color: #0f766e;
                border-color: #69c3b7;
                font-weight: 800;
            }
            QPushButton#ReaderModeButton:disabled {
                background: #f1f5f9;
                color: #b5bdc8;
                border-color: #e2e8f0;
            }
            QWidget#TextAreaField {
                background: transparent;
            }
            QWidget#InlineField {
                background: transparent;
            }
            QPushButton {
                min-height: 32px;
                border-radius: 10px;
                padding: 7px 12px;
                font-weight: 700;
            }
            QPushButton#PrimaryButton {
                background: #14b8a6;
                color: #ffffff;
                border: 1px solid #0f9f91;
            }
            QPushButton#PrimaryButton:hover {
                background: #0f766e;
            }
            QPushButton#PrimaryButton:disabled {
                background: #e2e8f0;
                color: #94a3b8;
                border: 1px solid #dde5ee;
            }
            QPushButton#SoftButton {
                background: #fff1f2;
                color: #be123c;
                border: 1px solid #fecdd3;
            }
            QPushButton#SoftButton:hover {
                background: #ffe4e6;
            }
            QPushButton#TinyButton {
                background: #fef3c7;
                color: #92400e;
                border: 1px solid #fde68a;
                padding: 6px 10px;
                min-height: 28px;
            }
            QPushButton#TinyButton:hover {
                background: #fde68a;
            }
            QPushButton#TinyButton:checked {
                background: #0f766e;
                color: #ffffff;
                border-color: #0b645e;
            }
            QScrollArea#PaperChatHistory, QWidget#PaperChatMessages {
                background: #f1f5f9;
                border: none;
            }
            QLabel#UserChatBubble {
                background: #d7f7f0; color: #123b36;
                border: 1px solid #a7e6da; border-radius: 12px;
                padding: 10px 13px;
            }
            QLabel#AIChatBubble {
                background: #ffffff; color: #243447;
                border: 1px solid #dbe4ec; border-radius: 12px;
                padding: 10px 13px;
            }
            QLabel#SystemChatBubble {
                background: #fff7ed; color: #9a3412;
                border: 1px solid #fed7aa; border-radius: 12px;
                padding: 10px 13px;
            }
            QPushButton#GhostButton {
                background: transparent;
                color: #0f766e;
                border: 1px solid #cde7e2;
                padding: 5px 9px;
                min-height: 26px;
                font-weight: 700;
            }
            QPushButton#GhostButton:hover {
                background: #ecfeff;
                border: 1px solid #8bdad2;
            }
            QPushButton#DangerButton {
                background: #ffffff;
                color: #dc2626;
                border: 1px solid #fecaca;
                padding: 5px 9px;
                min-height: 26px;
                font-weight: 800;
            }
            QPushButton#DangerButton:hover {
                background: #fef2f2;
            }
            QLineEdit, QTextEdit, QSpinBox, QComboBox {
                background: #fbfcff;
                border: 1px solid #dde6ee;
                border-radius: 8px;
                padding: 8px 10px;
                color: #1f2937;
                selection-background-color: #14b8a6;
            }
            QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {
                border: 1px solid #8bdad2;
                background: #ffffff;
            }
            QLineEdit:hover, QTextEdit:hover, QSpinBox:hover, QComboBox:hover {
                border: 1px solid #bdd6e8;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                background: transparent;
                border: none;
                width: 18px;
                margin-right: 3px;
            }
            QSpinBox::up-button {
                subcontrol-origin: border;
                subcontrol-position: top right;
                border-top-right-radius: 7px;
            }
            QSpinBox::down-button {
                subcontrol-origin: border;
                subcontrol-position: bottom right;
                border-bottom-right-radius: 7px;
            }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {
                background: #d9f2ee;
            }
            QSpinBox::up-button:pressed, QSpinBox::down-button:pressed {
                background: #b8e6df;
            }
            QSpinBox::up-arrow, QSpinBox::down-arrow {
                width: 10px;
                height: 7px;
            }
            QSpinBox::up-arrow {
                image: url(%SPIN_UP%);
            }
            QSpinBox::up-arrow:hover, QSpinBox::up-arrow:pressed {
                image: url(%SPIN_UP_HOVER%);
            }
            QSpinBox::down-arrow {
                image: url(%SPIN_DOWN%);
            }
            QSpinBox::down-arrow:hover, QSpinBox::down-arrow:pressed {
                image: url(%SPIN_DOWN_HOVER%);
            }
            QLineEdit#SearchBox {
                min-height: 34px;
                padding-left: 14px;
                background: #f8fafc;
                border: 1px solid #cde7e2;
            }
            QTextEdit {
                line-height: 1.45;
                background: #fbfcff;
            }
            QTextEdit#CompactTextEdit {
                line-height: 1.35;
                padding: 7px 10px;
            }
            QTextEdit#FormulaText {
                font-family: "Cambria Math", "Consolas", "Microsoft YaHei UI", "SimSun";
                font-size: 10.5pt;
                line-height: 1.5;
            }
            QCheckBox#SearchScope {
                color: #526070;
                spacing: 4px;
                padding: 2px 4px;
            }
            QTreeWidget#CategoryTree {
                background: #ffffff;
                border: 1px solid #e4edf4;
                border-radius: 10px;
                padding: 6px;
                outline: 0;
            }
            QTreeWidget#CategoryTree::item {
                min-height: 28px;
                border-radius: 7px;
                padding: 3px 8px;
            }
            QTreeWidget#CategoryTree::item:hover {
                background: #f0fdfa;
            }
            QTreeWidget#CategoryTree::item:selected {
                background: #ccfbf1;
                color: #134e4a;
                font-weight: 800;
            }
            QListWidget#PaperList {
                background: transparent;
                border: none;
                outline: 0;
            }
            QListWidget#PaperList::item {
                border: none;
                background: transparent;
            }
            QFrame#CategorySuggestionBar {
                background: #eefaf8;
                border: 1px solid #bfe4df;
                border-radius: 8px;
            }
            QFrame#PaperListEmptyState {
                background: #f6f8fb;
                border: 1px solid #e3e9f0;
                border-radius: 12px;
            }
            QFrame#PaperListEmptyState QLabel {
                background: transparent;
                border: none;
            }
            QLabel#EmptyStateTitle {
                font-size: 15px;
                font-weight: 600;
                color: #475569;
            }
            QLabel#EmptyStateHint {
                font-size: 12px;
                color: #94a3b8;
            }
            QPushButton#EmptyStateClearButton {
                background: #e8f5f3;
                color: #0f766e;
                border: 1px solid #b8dfda;
                border-radius: 8px;
                padding: 6px 16px;
                font-weight: 600;
            }
            QPushButton#EmptyStateClearButton:hover {
                background: #d6efec;
            }
            QListWidget#PaperList::item:selected {
                background: transparent;
            }
            QListWidget#DatasetList {
                background: transparent;
                border: none;
                outline: 0;
            }
            QListWidget#DatasetList::item {
                background: #f8fafc;
                color: #294053;
                border: 1px solid #e4edf4;
                border-radius: 10px;
                min-height: 34px;
                padding: 6px 10px;
            }
            QListWidget#DatasetList::item:hover {
                background: #eefaf8;
                border: 1px solid #b8e4dd;
            }
            QListWidget#DatasetList::item:selected {
                background: #ccfbf1;
                color: #134e4a;
                border: 1px solid #5eead4;
                font-weight: 800;
            }
            QListWidget#CategoryTagList {
                background: #f8fafc;
                border: 1px solid #dde6ee;
                border-radius: 10px;
                padding: 6px;
                outline: 0;
            }
            QListWidget#CategoryTagList::item {
                background: #dcfce7;
                color: #166534;
                border: 1px solid #bbf7d0;
                border-radius: 9px;
                min-height: 28px;
                padding: 6px 10px;
                margin: 3px 2px;
                font-weight: 700;
            }
            QListWidget#CategoryTagList::item:selected {
                background: #ccfbf1;
                color: #134e4a;
                border: 1px solid #5eead4;
            }
            QFrame#PaperCard {
                background: #ffffff;
                border: 1px solid #e7edf3;
                border-radius: 14px;
            }
            QFrame#PaperCard[selected="true"] {
                background: #f0fdfa;
                border: 2px solid #5eead4;
            }
            QFrame#DiscoveryShell {
                background: #fffdf8;
                border: 1px solid #f1d9ca;
                border-radius: 14px;
            }
            QFrame#DiscoveryToolbar {
                background: #fffdf8;
                border-bottom: 1px solid #eadfd7;
                border-top-left-radius: 14px;
                border-top-right-radius: 14px;
            }
            QLabel#DiscoveryTitle {
                color: #172033;
                font-size: 18px;
                font-weight: 900;
            }
            QFrame#DiscoveryRail {
                background: #fbfaf8;
                border-right: 1px solid #eadfd7;
            }
            QLabel#RailHeading {
                color: #64748b;
                font-size: 12px;
                font-weight: 800;
            }
            QLabel#RailHint {
                color: #64748b;
                font-size: 12px;
                line-height: 1.35;
            }
            QPushButton#RailButton {
                background: transparent;
                color: #334155;
                border: 1px solid transparent;
                border-radius: 8px;
                text-align: left;
                padding: 8px 10px;
                font-weight: 750;
            }
            QPushButton#RailButton:hover {
                background: #ecfeff;
                border: 1px solid #b6ece6;
                color: #0f766e;
            }
            QFrame#KeywordPanel {
                background: #ffffff;
                border: 1px solid #e5edf3;
                border-radius: 10px;
            }
            QLineEdit#KeywordInput {
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 7px;
                color: #334155;
                font-size: 12px;
                padding: 6px 8px;
            }
            QPushButton#KeywordAddButton {
                background: #0f9f91;
                color: #ffffff;
                border: 1px solid #0f9f91;
                border-radius: 7px;
                font-size: 14px;
                font-weight: 900;
            }
            QPushButton#KeywordAddButton:hover {
                background: #0f766e;
            }
            QPushButton#KeywordChip {
                background: #ecfeff;
                color: #0f766e;
                border: 1px solid #99f6e4;
                border-radius: 8px;
                padding: 5px 8px;
                text-align: left;
                font-size: 11px;
                font-weight: 800;
            }
            QPushButton#KeywordChip:hover {
                background: #ccfbf1;
                border: 1px solid #5eead4;
            }
            QPushButton#KeywordPreset {
                background: #ffffff;
                color: #475569;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 6px 8px;
                text-align: left;
                font-size: 11px;
                font-weight: 750;
            }
            QPushButton#KeywordPreset:hover {
                background: #f0fdfa;
                color: #0f766e;
                border: 1px solid #99f6e4;
            }
            QFrame#DiscoveryFeedPanel {
                background: #ffffff;
                border: none;
            }
            QListWidget#DiscoveryFeedList {
                background: transparent;
                border: none;
                outline: 0;
            }
            QListWidget#DiscoveryFeedList::item {
                border: none;
                background: transparent;
            }
            QListWidget#DiscoveryFeedList::item:selected {
                background: transparent;
            }
            QFrame#DiscoveryCard {
                background: #ffffff;
                border: 1px solid #dfe7ef;
                border-radius: 10px;
            }
            QFrame#DiscoveryCard:hover {
                border: 1px solid #99d8d2;
                background: #fbfffe;
            }
            QFrame#DiscoveryCard[selected="true"] {
                border: 2px solid #0f9f91;
                background: #f2fffd;
            }
            QLabel#DiscoveryCardTitle {
                color: #111827;
                font-size: 16px;
                font-weight: 900;
            }
            QLabel#DiscoveryMeta {
                color: #667085;
                font-size: 12px;
            }
            QLabel#DiscoverySummary {
                color: #374151;
                font-size: 12px;
                line-height: 1.38;
            }
            QLabel#DiscoveryChip {
                background: #eff6ff;
                color: #1d4ed8;
                border: 1px solid #bfdbfe;
                border-radius: 7px;
                padding: 3px 8px;
                font-size: 11px;
                font-weight: 800;
            }
            QLabel#SavedChip {
                background: #dcfce7;
                color: #166534;
                border: 1px solid #bbf7d0;
                border-radius: 7px;
                padding: 3px 8px;
                font-size: 11px;
                font-weight: 800;
            }
            QFrame#StrongScore, QFrame#WarmScore, QFrame#SoftScore {
                border-radius: 8px;
                padding: 2px;
            }
            QFrame#StrongScore {
                background: #ecfeff;
                border: 1px solid #99f6e4;
            }
            QFrame#WarmScore {
                background: #fffbeb;
                border: 1px solid #fde68a;
            }
            QFrame#SoftScore {
                background: #f8fafc;
                border: 1px solid #e2e8f0;
            }
            QLabel#ScoreLabel, QLabel#ReasonTitle {
                color: #0f766e;
                font-size: 11px;
                font-weight: 800;
            }
            QLabel#ScoreNumber {
                color: #0f766e;
                font-size: 28px;
                font-weight: 900;
            }
            QLabel#ReasonText {
                color: #405368;
                font-size: 11px;
                line-height: 1.3;
            }
            QFrame#DiscoveryInspector {
                background: #ffffff;
                border-left: 1px solid #eadfd7;
                border-top-right-radius: 14px;
                border-bottom-right-radius: 14px;
            }
            QLabel#InspectorTitle {
                color: #111827;
                font-size: 20px;
                font-weight: 900;
            }
            QLabel#SectionLabel {
                color: #172033;
                font-size: 13px;
                font-weight: 900;
            }
            QTextEdit#InspectorText {
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 10px;
                color: #263244;
                font-size: 12px;
                line-height: 1.45;
            }
            QLabel#CardTitle {
                color: #172033;
                font-size: 14px;
                font-weight: 800;
            }
            QLabel#CardMeta {
                color: #7a8494;
                font-size: 11px;
            }
            QLabel#CardSummary {
                color: #435064;
                font-size: 12px;
                line-height: 1.35;
            }
            QLabel#StatusChip {
                background: #e0f2fe;
                color: #0369a1;
                border: 1px solid #bae6fd;
                border-radius: 9px;
                padding: 3px 8px;
                font-size: 11px;
                font-weight: 750;
            }
            QLabel#WarmChip {
                background: #fef3c7;
                color: #92400e;
                border: 1px solid #fde68a;
                border-radius: 9px;
                padding: 3px 8px;
                font-size: 11px;
                font-weight: 750;
            }
            QLabel#CoolChip {
                background: #dcfce7;
                color: #166534;
                border: 1px solid #bbf7d0;
                border-radius: 9px;
                padding: 3px 8px;
                font-size: 11px;
                font-weight: 750;
            }
            QLineEdit#TagInput {
                background: #ffffff;
                border: 1px solid #dde6ee;
                border-radius: 8px;
                padding: 5px 10px;
            }
            QPushButton#TagChip {
                background: #e0e7ff;
                color: #3730a3;
                border: 1px solid #c7d2fe;
                border-radius: 9px;
                padding: 3px 8px;
                font-size: 11px;
                font-weight: 750;
            }
            QPushButton#TagChip:hover {
                background: #c7d2fe;
            }
            QTabWidget#DetailTabs::pane {
                background: #ffffff;
                border: 1px solid #f1d9ca;
                border-radius: 12px;
                top: -1px;
            }
            QTabBar::tab {
                background: #fffaf4;
                color: #7c2d12;
                border: 1px solid #f1d9ca;
                border-bottom: none;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
                padding: 9px 18px;
                margin-right: 4px;
                font-weight: 700;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                color: #0f766e;
            }
            QTableWidget#DatasetTable, QTableWidget#MetricEditTable {
                background: #ffffff;
                alternate-background-color: #f8fafc;
                border: 1px solid #e4edf4;
                border-radius: 12px;
                gridline-color: #edf2f7;
                color: #263244;
                outline: 0;
            }
            QTableWidget#DatasetTable::item, QTableWidget#MetricEditTable::item {
                padding: 8px;
            }
            QTableWidget#DatasetTable::item:selected, QTableWidget#MetricEditTable::item:selected {
                background: #ccfbf1;
                color: #134e4a;
            }
            QHeaderView::section {
                background: #f1f8f7;
                color: #31505a;
                border: none;
                border-right: 1px solid #dce8ef;
                border-bottom: 1px solid #dce8ef;
                padding: 9px;
                font-weight: 800;
            }
            QSplitter::handle {
                background: transparent;
                width: 10px;
            }
            QSplitter::handle:hover {
                background: #cfe3ec;
                border-radius: 2px;
            }
            QSplitter::handle:pressed {
                background: #a9c9d8;
                border-radius: 2px;
            }
            QSplitter::handle:horizontal {
                height: 10px;
            }
            QStatusBar {
                background: #fff7ed;
                color: #7c8490;
            }

            /* Research workbench redesign: one navigation layer, restrained surfaces, semantic color. */
            QMainWindow, QWidget#Root {
                background: #f3f6f9;
            }
            * {
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "SimSun";
                letter-spacing: 0;
            }
            QFrame#AppHeader {
                background: #ffffff;
                border: 1px solid #dce5ee;
                border-radius: 8px;
            }
            QLabel#HeroTitle {
                color: #18212f;
                font-size: 22px;
                font-weight: 800;
            }
            QLabel#HeroSubtitle, QLabel#MutedText, QLabel#CardMeta, QLabel#DiscoveryMeta {
                color: #66758a;
            }
            QLabel#HeaderMeta {
                color: #42526a;
                background: #edf3f8;
                border: 1px solid #d7e3ee;
                border-radius: 6px;
                padding: 6px 9px;
                font-weight: 700;
            }
            QPushButton {
                min-height: 30px;
                border-radius: 6px;
                padding: 6px 10px;
                font-weight: 700;
            }
            QPushButton#NavButton {
                background: transparent;
                color: #526277;
                border: 1px solid transparent;
                padding: 6px 9px;
            }
            QPushButton#NavButton:hover {
                background: #eef4f9;
                color: #1d4ed8;
                border: 1px solid #d9e5f1;
            }
            QPushButton#NavButton[active="true"] {
                background: #e8f0ff;
                color: #1d4ed8;
                border: 1px solid #bfd3ff;
            }
            QPushButton#IconButton {
                min-width: 32px;
                max-width: 32px;
                min-height: 32px;
                max-height: 32px;
                padding: 0;
                background: #ffffff;
                color: #526277;
                border: 1px solid #d7e1eb;
            }
            QPushButton#IconButton:hover {
                background: #eef4f9;
                border: 1px solid #b8c9db;
            }
            QPushButton#PrimaryButton {
                background: #2563eb;
                color: #ffffff;
                border: 1px solid #1d4ed8;
            }
            QPushButton#PrimaryButton:hover {
                background: #1d4ed8;
            }
            QPushButton#PrimaryButton:disabled {
                background: #e2e8f0;
                color: #94a3b8;
                border: 1px solid #dde5ee;
            }
            QPushButton#SoftButton, QPushButton#TinyButton {
                background: #ffffff;
                color: #40536b;
                border: 1px solid #d7e1eb;
            }
            QPushButton#SoftButton:hover, QPushButton#TinyButton:hover {
                background: #eef4f9;
                color: #1d4ed8;
                border: 1px solid #b8c9db;
            }
            QPushButton#GhostButton {
                background: #f0f8f7;
                color: #0f766e;
                border: 1px solid #b9dfda;
            }
            QPushButton#GhostButton:hover {
                background: #ddf1ee;
                border: 1px solid #83c7bd;
            }
            QPushButton#DangerButton {
                background: #ffffff;
                color: #c2413b;
                border: 1px solid #efc5c1;
            }
            QPushButton#DangerButton:hover {
                background: #fff1f0;
            }
            QFrame#Panel, QFrame#DiscoveryShell {
                background: #ffffff;
                border: 1px solid #dce5ee;
                border-radius: 8px;
            }
            QFrame#SubPanel, QFrame#DiscoveryFeedPanel, QFrame#DiscoveryInspector {
                background: transparent;
                border: none;
                border-radius: 0;
            }
            QLabel#PanelTitle, QLabel#DiscoveryTitle {
                color: #18212f;
                font-size: 17px;
                font-weight: 800;
            }
            QLabel#FieldTitle, QLabel#DialogTitle, QLabel#SectionLabel {
                color: #26364b;
                font-weight: 800;
            }
            QLineEdit, QTextEdit, QSpinBox, QComboBox {
                background: #ffffff;
                border: 1px solid #d5e0ea;
                border-radius: 6px;
                color: #1d2939;
                selection-background-color: #bcd7ff;
            }
            QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {
                background: #ffffff;
                border: 1px solid #5b8def;
            }
            QLineEdit#SearchBox {
                min-height: 32px;
                background: #f7f9fc;
                border: 1px solid #cddae6;
                padding-left: 12px;
            }
            QCheckBox#SearchScope {
                color: #526277;
                padding: 2px 3px;
            }
            QTreeWidget#CategoryTree, QListWidget#CategoryTagList {
                background: #f8fafc;
                border: 1px solid #dce5ee;
                border-radius: 6px;
            }
            QTreeWidget#CategoryTree::item {
                min-height: 26px;
                border-radius: 4px;
                padding: 3px 7px;
            }
            QTreeWidget#CategoryTree::item:hover {
                background: #edf4fb;
            }
            QTreeWidget#CategoryTree::item:selected {
                background: #dceafe;
                color: #1e40af;
                font-weight: 800;
            }
            QListWidget#DatasetList::item {
                background: #f8fafc;
                color: #40536b;
                border: 1px solid #dce5ee;
                border-radius: 6px;
                min-height: 32px;
            }
            QListWidget#DatasetList::item:hover {
                background: #edf4fb;
                border: 1px solid #c1d5e8;
            }
            QListWidget#DatasetList::item:selected {
                background: #dceafe;
                color: #1e40af;
                border: 1px solid #9bbcf5;
            }
            QListWidget#CategoryTagList::item {
                background: #edf8f5;
                color: #16645b;
                border: 1px solid #c5e5de;
                border-radius: 5px;
            }
            QFrame#PaperCard, QFrame#DiscoveryCard {
                background: #ffffff;
                border: 1px solid #dce5ee;
                border-radius: 8px;
            }
            QFrame#PaperCard:hover, QFrame#DiscoveryCard:hover {
                border: 1px solid #aac5df;
                background: #fbfdff;
            }
            QFrame#PaperCard[selected="true"], QFrame#DiscoveryCard[selected="true"] {
                background: #f1f6ff;
                border: 2px solid #4f86ee;
            }
            QLabel#CardTitle, QLabel#DiscoveryCardTitle, QLabel#InspectorTitle {
                color: #18212f;
                font-weight: 800;
            }
            QLabel#StatusChip {
                background: #e8f0ff;
                color: #1d4ed8;
                border: 1px solid #c4d7ff;
                border-radius: 5px;
            }
            QLabel#WarmChip {
                background: #fff4dd;
                color: #9a5b12;
                border: 1px solid #f3d3a2;
                border-radius: 5px;
            }
            QLabel#CoolChip, QLabel#SavedChip {
                background: #edf8f5;
                color: #16645b;
                border: 1px solid #c5e5de;
                border-radius: 5px;
            }
            QFrame#DiscoveryToolbar {
                background: #ffffff;
                border-bottom: 1px solid #dce5ee;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }
            QFrame#DiscoveryRail {
                background: #f8fafc;
                border-right: 1px solid #dce5ee;
            }
            QPushButton#RailButton {
                color: #40536b;
                border-radius: 6px;
            }
            QPushButton#RailButton:hover {
                background: #e8f0ff;
                border: 1px solid #c4d7ff;
                color: #1d4ed8;
            }
            QFrame#KeywordPanel {
                background: #ffffff;
                border: 1px solid #dce5ee;
                border-radius: 6px;
            }
            QPushButton#KeywordAddButton {
                background: #2563eb;
                border: 1px solid #1d4ed8;
                border-radius: 6px;
            }
            QPushButton#KeywordChip {
                background: #edf8f5;
                color: #16645b;
                border: 1px solid #c5e5de;
                border-radius: 5px;
            }
            QPushButton#KeywordPreset {
                background: #ffffff;
                color: #526277;
                border: 1px solid #dce5ee;
                border-radius: 5px;
            }
            QLabel#DiscoveryChip {
                background: #e8f0ff;
                color: #1d4ed8;
                border: 1px solid #c4d7ff;
                border-radius: 5px;
            }
            QFrame#StrongScore {
                background: #edf8f5;
                border: 1px solid #c5e5de;
                border-radius: 6px;
            }
            QFrame#WarmScore {
                background: #fff7e8;
                border: 1px solid #f3d3a2;
                border-radius: 6px;
            }
            QFrame#SoftScore {
                background: #f5f8fb;
                border: 1px solid #dce5ee;
                border-radius: 6px;
            }
            QLabel#ScoreLabel, QLabel#ReasonTitle, QLabel#ScoreNumber {
                color: #16645b;
            }
            QTabWidget#DetailTabs::pane {
                background: #ffffff;
                border: 1px solid #dce5ee;
                border-radius: 6px;
            }
            QTabBar::tab {
                background: #f3f6f9;
                color: #66758a;
                border: 1px solid #dce5ee;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 8px 14px;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                color: #1d4ed8;
                border-top: 2px solid #2563eb;
            }
            QTableWidget#DatasetTable, QTableWidget#MetricEditTable {
                background: #ffffff;
                alternate-background-color: #f7f9fc;
                border: 1px solid #dce5ee;
                border-radius: 6px;
                gridline-color: #e8eef4;
                color: #26364b;
            }
            QTableWidget#DatasetTable::item:selected, QTableWidget#MetricEditTable::item:selected {
                background: #dceafe;
                color: #1e40af;
            }
            QHeaderView::section {
                background: #f3f6f9;
                color: #526277;
                border-right: 1px solid #dce5ee;
                border-bottom: 1px solid #dce5ee;
                padding: 8px;
            }
            QStatusBar {
                background: #f3f6f9;
                color: #66758a;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 16px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #b6c3d2;
                min-height: 36px;
                border-radius: 5px;
                margin: 1px 1px;
            }
            QScrollBar::handle:vertical:hover {
                background: #8fa5bb;
            }
            QScrollBar::handle:vertical:pressed {
                background: #6c88a4;
            }
            QScrollBar:horizontal {
                background: transparent;
                height: 16px;
                margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: #b6c3d2;
                min-width: 36px;
                border-radius: 5px;
                margin: 1px 1px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #8fa5bb;
            }
            QScrollBar::handle:horizontal:pressed {
                background: #6c88a4;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: transparent;
            }
            QScrollArea#DiscoveryRailScroll {
                background: #f8fafc;
                border-right: 1px solid #dce5ee;
            }
            QScrollArea#DiscoveryRailScroll > QWidget > QWidget {
                background: #f8fafc;
            }
            QFrame#DiscoveryInspector {
                background: #ffffff;
                border-left: 1px solid #dce5ee;
            }
            QScrollArea#InspectorScroll, QScrollArea#InspectorScroll > QWidget > QWidget {
                background: #ffffff;
            }

            /* Selected direction: search-first library with a dedicated reading workspace. */
            QMainWindow, QWidget#Root {
                background: #ffffff;
            }
            QFrame#AppHeader {
                background: #ffffff;
                border: none;
                border-bottom: 1px solid #e2e8f0;
                border-radius: 0;
            }
            QLabel#HeroTitle {
                color: #172033;
                font-size: 18px;
                font-weight: 800;
                padding-right: 4px;
            }
            QLabel#BrandSubtitle {
                color: #0f766e;
                font-size: 9px;
                font-weight: 700;
            }
            QLabel#HeaderMeta {
                color: #64748b;
                background: transparent;
                border: none;
                padding: 4px 6px;
                font-weight: 600;
            }
            QPushButton#PrimaryButton {
                background: #0f9f91;
                color: #ffffff;
                border: 1px solid #0f8f83;
            }
            QPushButton#PrimaryButton:hover {
                background: #0f766e;
            }
            QPushButton#PrimaryButton:disabled {
                background: #e2e8f0;
                color: #94a3b8;
                border: 1px solid #dde5ee;
            }
            QPushButton#GhostButton {
                background: transparent;
                color: #0f766e;
                border: 1px solid #c7e3df;
            }
            QPushButton#GhostButton:hover {
                background: #effaf8;
                border: 1px solid #8fd0c7;
            }
            QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {
                border: 1px solid #0f9f91;
            }
            QLineEdit#SearchBox {
                min-height: 34px;
                background: #f8fafc;
                border: 1px solid #cbd5e1;
                padding-left: 12px;
            }
            QFrame#NavigationRail {
                background: #f8fafc;
                border: none;
                border-right: 1px solid #e2e8f0;
            }
            QScrollArea#NavigationRailScroll {
                background: transparent;
                border: none;
            }
            QScrollArea#NavigationRailScroll > QWidget > QWidget {
                background: transparent;
            }
            QPushButton#RailNavButton {
                background: transparent;
                color: #334155;
                border: 1px solid transparent;
                text-align: left;
                padding: 8px 10px;
            }
            QPushButton#RailNavButton:hover {
                background: #ecfdf9;
                color: #0f766e;
            }
            QPushButton#RailNavButton[active="true"] {
                background: #dff7f2;
                color: #0f766e;
                border: 1px solid #b8e7df;
            }
            QPushButton#QuickViewButton {
                background: transparent;
                color: #526174;
                border: none;
                text-align: left;
                padding: 5px 9px;
                min-height: 25px;
                font-weight: 600;
            }
            QPushButton#QuickViewButton:hover {
                background: #eef7f5;
                color: #0f766e;
            }
            QLabel#RailHeading {
                color: #64748b;
                font-size: 12px;
                font-weight: 800;
                padding-top: 8px;
            }
            QTreeWidget#CategoryTree {
                background: transparent;
                border: none;
                border-radius: 0;
            }
            QTreeWidget#CategoryTree::item:selected {
                background: #dff7f2;
                color: #0f766e;
            }
            QFrame#LibraryTablePanel {
                background: #ffffff;
                border: none;
            }
            QFrame#PaperListHeader {
                background: #f8fafc;
                border-top: 1px solid #e2e8f0;
                border-bottom: 1px solid #e2e8f0;
            }
            QFrame#PaperCard {
                background: #ffffff;
                border: none;
                border-bottom: 1px solid #e7edf3;
                border-radius: 0;
            }
            QFrame#PaperCard:hover {
                background: #f8fbfc;
                border: none;
                border-bottom: 1px solid #d8e6e4;
            }
            QFrame#PaperCard[selected="true"] {
                background: #edf9f7;
                border: none;
                border-left: 3px solid #0f9f91;
                border-bottom: 1px solid #cce8e4;
            }
            QLabel#CardTitle {
                color: #172033;
                font-weight: 800;
            }
            QLabel#CardColumn, QLabel#CardStatus {
                color: #475569;
                font-size: 12px;
            }
            QLabel#CardImportance {
                color: #a16207;
                font-size: 12px;
            }
            QLabel#CoolChip {
                background: #e8f7f3;
                color: #0f766e;
                border: 1px solid #c3e8e1;
            }
            QFrame#InspectorPanel {
                background: #ffffff;
                border: none;
                border-left: 1px solid #e2e8f0;
            }
            QLabel#InspectorTitle, QLabel#ReaderTitle {
                color: #172033;
                font-size: 17px;
                font-weight: 800;
            }
            QLabel#InspectorExcerpt, QLabel#ReaderExcerpt, QTextEdit#ReaderExcerpt {
                background: #f8fafc;
                color: #334155;
                border-left: 2px solid #0f9f91;
                padding: 10px;
            }
            QLabel#InspectorTags {
                color: #0f766e;
                background: #edf9f7;
                border: 1px solid #cce8e4;
                padding: 8px;
            }
            QFrame#ReaderShell, QFrame#ReaderCanvas {
                background: #f1f5f9;
                border: none;
            }
            QFrame#ReaderHeader {
                background: #ffffff;
                border: none;
                border-bottom: 1px solid #e2e8f0;
            }
            QFrame#ReaderQueue {
                background: #f8fafc;
                border: none;
                border-right: 1px solid #e2e8f0;
            }
            QListWidget#ReaderPaperList {
                background: transparent;
                border: none;
                outline: 0;
            }
            QListWidget#ReaderPaperList::item {
                color: #334155;
                border-bottom: 1px solid #e2e8f0;
                padding: 10px 8px;
                min-height: 48px;
            }
            QListWidget#ReaderPaperList::item:hover {
                background: #eef7f5;
            }
            QListWidget#ReaderPaperList::item:selected {
                background: #dff7f2;
                color: #0f766e;
                border-left: 3px solid #0f9f91;
            }
            QFrame#ReaderNotes {
                background: #ffffff;
                border: none;
                border-left: 1px solid #e2e8f0;
            }
            QTabWidget#ReaderTabs::pane {
                background: #ffffff;
                border: none;
                border-top: 1px solid #e2e8f0;
            }
            QTabWidget#ReaderTabs QTabBar::tab {
                background: transparent;
                color: #64748b;
                border: none;
                border-bottom: 2px solid transparent;
                padding: 8px 12px;
            }
            QTabWidget#ReaderTabs QTabBar::tab:selected {
                color: #0f766e;
                border-bottom: 2px solid #0f9f91;
            }
            QTextEdit#ReaderNotesEdit {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                padding: 10px;
            }
            QTextEdit#ReaderSummary {
                background: #f8fafc;
                color: #334155;
                border: none;
                border-left: 2px solid #0f9f91;
                padding: 10px;
            }
            QSplitter#MainSplitter::handle,
            QSplitter#ReaderSplitter::handle {
                background: #e2e8f0;
                width: 1px;
            }
            QFrame#EditorWorkspace {
                background: #f3f6f8;
                border: none;
            }
            QFrame#EditorWorkspaceHeader {
                background: #ffffff;
                border: none;
                border-bottom: 1px solid #dfe6ec;
                min-height: 56px;
                max-height: 56px;
            }
            QFrame#EditorHeaderBackZone {
                background: #fbfcfd;
                border: none;
                border-right: 1px solid #dfe6ec;
            }
            QFrame#EditorHeaderContext {
                background: #ffffff;
                border: none;
            }
            QPushButton#EditorBackButton {
                background: transparent;
                color: #334155;
                border: 1px solid #d8e0e7;
                border-radius: 5px;
                padding: 7px 10px;
                font-weight: 650;
            }
            QPushButton#EditorBackButton:hover {
                color: #0f766e;
                background: #f0fdfa;
                border-color: #99d8cf;
            }
            QLabel#EditorWorkspaceTitle {
                color: #172033;
                font-size: 15px;
                font-weight: 760;
            }
            QLabel#EditorSaveState {
                color: #4f8f7c;
                background: transparent;
                border: none;
                padding: 3px 5px;
                font-size: 12px;
                font-weight: 650;
            }
            QLabel#EditorSaveState[dirty="true"] {
                color: #a16207;
            }
            QPushButton#EditorToolbarButton {
                background: transparent;
                color: #334155;
                border: 1px solid transparent;
                border-radius: 5px;
                padding: 7px 8px;
                font-weight: 650;
            }
            QPushButton#EditorToolbarButton:hover,
            QPushButton#EditorToolbarButton:pressed {
                background: #f0fdfa;
                color: #0f766e;
                border-color: #cfe8e3;
            }
            QFrame#EditorWorkspaceBody {
                background: #f3f6f8;
                border: none;
            }
            QFrame#EditorOutline {
                background: #fbfcfd;
                border: none;
                border-right: 1px solid #dfe6ec;
            }
            QLabel#EditorOutlineTitle {
                color: #172033;
                font-size: 13px;
                font-weight: 760;
                padding: 4px 2px;
            }
            QLabel#EditorOutlineFooter {
                color: #94a3b8;
                font-size: 11px;
                padding: 6px 8px;
            }
            QListWidget#EditorOutlineList {
                background: transparent;
                border: none;
                outline: 0;
            }
            QListWidget#EditorOutlineList::item {
                color: #526173;
                border: none;
                border-left: 3px solid transparent;
                padding: 6px 10px;
                margin: 1px 0;
            }
            QListWidget#EditorOutlineList::item:hover {
                background: #edf7f5;
                color: #0f766e;
            }
            QListWidget#EditorOutlineList::item:selected {
                background: #e4f4f1;
                color: #0b756c;
                border-left: 3px solid #0f9f91;
                font-weight: 760;
            }
            QScrollArea#EditorDocumentScroll,
            QWidget#EditorDocumentShell {
                background: #ffffff;
                border: none;
            }
            QFrame#EditorDocument {
                background: #ffffff;
                border: none;
                border-radius: 0;
            }
            QFrame#EditorIdentity {
                background: #ffffff;
                border: none;
                border-bottom: 1px solid #e7edf1;
            }
            QTextEdit#EditorPaperTitleEdit {
                background: #ffffff;
                color: #111827;
                border: none;
                border-radius: 0;
                padding: 0;
                font-size: 26px;
                font-weight: 800;
                selection-background-color: #bcebe4;
            }
            QTextEdit#EditorPaperTitleEdit:focus {
                background: #fbfefd;
                border: none;
                border-bottom: 1px solid #0f9f91;
            }
            QTextEdit#EditorPaperAuthorsEdit {
                background: #ffffff;
                color: #0f8f83;
                border: none;
                border-radius: 0;
                padding: 0;
                font-size: 13px;
                font-weight: 620;
                selection-background-color: #bcebe4;
            }
            QTextEdit#EditorPaperAuthorsEdit:focus {
                background: #fbfefd;
                border: none;
                border-bottom: 1px solid #75c9bd;
            }
            QLabel#DocumentSectionTitle {
                color: #172033;
                font-size: 15px;
                font-weight: 780;
                padding-top: 12px;
                padding-bottom: 4px;
            }
            QFrame#EditorMetadataCard {
                background: #ffffff;
                border: none;
                border-bottom: 1px solid #e7edf1;
            }
            QLabel#MetadataLabel {
                color: #64748b;
                font-size: 12px;
                min-width: 64px;
            }
            QFrame#EditorMetadataCard QLineEdit,
            QFrame#EditorMetadataCard QTextEdit,
            QFrame#EditorMetadataCard QSpinBox,
            QFrame#EditorMetadataCard QComboBox {
                background: #f9fbfc;
                color: #263444;
                border: 1px solid #e1e7ec;
                border-radius: 3px;
                padding: 5px 7px;
                min-height: 26px;
            }
            QFrame#EditorMetadataCard QLineEdit:focus,
            QFrame#EditorMetadataCard QTextEdit:focus,
            QFrame#EditorMetadataCard QSpinBox:focus,
            QFrame#EditorMetadataCard QComboBox:focus {
                background: #ffffff;
                border: 1px solid #54b8aa;
            }
            QFrame#EditorDocument QWidget#TextAreaField {
                background: #ffffff;
                border: none;
                border-bottom: 1px solid #e7edf1;
            }
            QFrame#EditorDocument QWidget#TextAreaField QLabel#FieldTitle {
                color: #1f2b3d;
                font-size: 15px;
                font-weight: 760;
            }
            QFrame#EditorDocument QTextEdit#FormulaText {
                background: #ffffff;
                color: #273445;
                border: 1px solid transparent;
                border-radius: 3px;
                padding: 7px 8px;
                line-height: 1.45;
            }
            QFrame#EditorDocument QTextEdit#FormulaText:hover {
                background: #fafcfd;
                border-color: #e4eaef;
            }
            QFrame#EditorDocument QTextEdit#FormulaText:focus {
                background: #ffffff;
                border: 1px solid #54b8aa;
            }
            QFrame#EditorDocument QListWidget#CategoryTagList {
                background: #f4fbf9;
                border: 1px solid #d9eee9;
                border-radius: 3px;
                padding: 4px;
            }
            QFrame#EditorPdfSection {
                background: #ffffff;
                border: none;
            }
            QFrame#EditorActionBar {
                background: #ffffff;
                border: none;
                border-top: 1px solid #dfe6ec;
                min-height: 52px;
                max-height: 52px;
            }
            QLabel#EditorBottomState {
                color: #64748b;
                padding-right: 8px;
            }
            QLabel#EditorBottomState[dirty="true"] {
                color: #a16207;
                font-weight: 700;
            }
            QStatusBar {
                background: #f8fafc;
                color: #64748b;
                border-top: 1px solid #e2e8f0;
            }
            """
        for key, path in spin_icon.items():
            style = style.replace(key, path.resolve().as_posix())
        style = theme.apply_to_qss(style)
        style = style.replace("%BASE_FONT_SIZE%", f"{theme.base_font_pt():g}pt")
        self.setStyleSheet(style)
        self._apply_app_palette()

    def _apply_app_palette(self) -> None:
        """深色主题同步 QPalette,让菜单/提示/原生部件跟随;浅色恢复标准调色板。"""
        if theme.is_dark():
            tokens = theme.DARK_PALETTE_TOKENS
            palette = QPalette()
            palette.setColor(QPalette.ColorRole.Window, QColor(tokens["window"]))
            palette.setColor(QPalette.ColorRole.WindowText, QColor(tokens["window_text"]))
            palette.setColor(QPalette.ColorRole.Base, QColor(tokens["base"]))
            palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#232b35"))
            palette.setColor(QPalette.ColorRole.Text, QColor(tokens["text"]))
            palette.setColor(QPalette.ColorRole.Button, QColor(tokens["button"]))
            palette.setColor(QPalette.ColorRole.ButtonText, QColor(tokens["button_text"]))
            palette.setColor(QPalette.ColorRole.Highlight, QColor(tokens["highlight"]))
            palette.setColor(QPalette.ColorRole.HighlightedText, QColor(tokens["highlighted_text"]))
            palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(tokens["tooltip_base"]))
            palette.setColor(QPalette.ColorRole.ToolTipText, QColor(tokens["tooltip_text"]))
            palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(tokens["placeholder"]))
            palette.setColor(QPalette.ColorRole.Link, QColor(tokens["link"]))
            QApplication.setPalette(palette)
        else:
            QApplication.setPalette(QApplication.style().standardPalette())

    def refresh_categories(self) -> None:
        # F20:重建期间屏蔽 selectionChanged,防止「恢复默认选中」触发
        # on_category_selected 把当前筛选(尤其智能文件夹)清掉
        self.category_tree.blockSignals(True)
        try:
            self._rebuild_category_tree()
        finally:
            self.category_tree.blockSignals(False)
        self._restore_tree_selection()
        if hasattr(self, "category_filter_edit"):
            self.filter_category_tree(self.category_filter_edit.text())
        self.refresh_category_combo()

    def _restore_tree_selection(self) -> None:
        """按当前筛选状态恢复树高亮(全部文献/回收站/分类/智能分组/智能文件夹)。"""
        target_data = None
        if self._viewing_trash:
            target_data = TRASH_CATEGORY_ID
        elif self.current_saved_search_id is not None:
            target_data = ("saved", self.current_saved_search_id)
        elif self.smart_filter is not None:
            target_data = self.smart_filter
        elif self.current_category_id is not None:
            target_data = self.current_category_id
        matched = None
        for index in range(self.category_tree.topLevelItemCount()):
            item = self.category_tree.topLevelItem(index)
            stack = [item]
            while stack:
                current = stack.pop()
                if current.data(0, Qt.ItemDataRole.UserRole) == target_data:
                    matched = current
                    break
                stack.extend(current.child(index) for index in range(current.childCount()))
            if matched is not None:
                break
        if matched is not None:
            self.category_tree.setCurrentItem(matched)
        elif self.category_tree.topLevelItemCount():
            self.category_tree.setCurrentItem(self.category_tree.topLevelItem(0))

    def _rebuild_category_tree(self) -> None:
        categories = self.repository.list_categories()
        counts = self.repository.category_paper_counts()
        parent_ids = {category.parent_id for category in categories if category.parent_id is not None}
        self.category_lookup = {category.id: category for category in categories}
        self.category_tree.clear()

        total_count = len(self.repository.list_papers())
        root = QTreeWidgetItem([f"全部文献  {total_count}"])
        root.setData(0, Qt.ItemDataRole.UserRole, None)
        root.setExpanded(True)
        self.category_tree.addTopLevelItem(root)

        trash_count = len(self.repository.list_trashed_papers())
        trash_label = f"回收站  {trash_count}" if trash_count else "回收站"
        trash_item = QTreeWidgetItem([trash_label])
        trash_item.setData(0, Qt.ItemDataRole.UserRole, TRASH_CATEGORY_ID)
        if not trash_count:
            trash_item.setForeground(0, Qt.GlobalColor.gray)
        self.category_tree.addTopLevelItem(trash_item)

        self._add_smart_group_nodes()

        item_lookup: dict[int, QTreeWidgetItem] = {}
        for category in categories:
            count = counts.get(category.id, 0)
            text = category.name if category.id in parent_ids else f"{category.name}  {count}"
            item = QTreeWidgetItem([text])
            item.setData(0, Qt.ItemDataRole.UserRole, category.id)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, category.name)
            if category.preset_keywords:
                item.setToolTip(0, f"预设关键词：{category.preset_keywords}")
            if count == 0 and category.id not in parent_ids:
                item.setForeground(0, Qt.GlobalColor.gray)
            item_lookup[category.id] = item

        for category in categories:
            item = item_lookup[category.id]
            if category.parent_id and category.parent_id in item_lookup:
                item_lookup[category.parent_id].addChild(item)
            else:
                root.addChild(item)

        self.category_tree.expandAll()
        self.category_tree.setCurrentItem(root)

    def _add_smart_group_nodes(self) -> None:
        """F3 智能分组:按阅读状态/年份/重要度自动分组,点击子节点筛选。"""
        alive = self.repository.list_papers()
        groups: list[tuple[str, list[tuple[str, object, int]]]] = []

        status_counts: dict[str, int] = {}
        for paper in alive:
            status = paper.reading_status or "待读"
            status_counts[status] = status_counts.get(status, 0) + 1
        groups.append((
            "阅读状态",
            [
                (status, status, count)
                for status, count in sorted(
                    status_counts.items(),
                    key=lambda pair: (-pair[1], READING_STATUSES.index(pair[0]) if pair[0] in READING_STATUSES else 99),
                )
            ],
        ))

        year_counts: dict[int, int] = {}
        for paper in alive:
            if paper.year:
                year_counts[paper.year] = year_counts.get(paper.year, 0) + 1
        groups.append((
            "年份",
            [(str(year), year, count) for year, count in sorted(year_counts.items(), reverse=True)],
        ))

        importance_labels = [(5, "重要度 5"), (4, "重要度 4"), (3, "重要度 3"), (2, "重要度 2"), (1, "重要度 1")]
        groups.append((
            "重要度",
            [
                (label, value, sum(1 for paper in alive if paper.importance == value))
                for value, label in importance_labels
            ],
        ))

        for group_name, entries in groups:
            visible = [(label, value, count) for label, value, count in entries if count]
            if not visible:
                continue
            parent = QTreeWidgetItem([group_name])
            parent.setData(0, Qt.ItemDataRole.UserRole, SMART_GROUP_NONE)
            for label, value, count in visible:
                child = QTreeWidgetItem([f"{label}  {count}"])
                child.setData(0, Qt.ItemDataRole.UserRole, ("smart", str(value)))
                parent.addChild(child)
            self.category_tree.addTopLevelItem(parent)

        # F20 智能文件夹:每个节点=一条保存的搜索,节点文字含实时匹配数
        saved_entries = self.repository.list_saved_searches()
        if saved_entries:
            saved_parent = QTreeWidgetItem(["智能文件夹"])
            saved_parent.setData(0, Qt.ItemDataRole.UserRole, SAVED_SEARCH_NONE)
            for entry in saved_entries:
                count = len(self.repository.list_papers(**entry.rule.to_kwargs()))
                child = QTreeWidgetItem([f"{entry.name}  {count}"])
                child.setData(0, Qt.ItemDataRole.UserRole, ("saved", entry.saved_search_id))
                child.setToolTip(0, "点击应用筛选;右键编辑/删除")
                saved_parent.addChild(child)
            self.category_tree.addTopLevelItem(saved_parent)
            saved_parent.setExpanded(True)

    def filter_category_tree(self, text: str) -> None:
        query = text.strip().casefold()
        if self.category_tree.topLevelItemCount() == 0:
            return
        root = self.category_tree.topLevelItem(0)

        def update_visibility(item: QTreeWidgetItem) -> bool:
            own_name = str(item.data(0, Qt.ItemDataRole.UserRole + 1) or item.text(0))
            own_match = not query or query in own_name.casefold()
            child_match = False
            for child_index in range(item.childCount()):
                child_match = update_visibility(item.child(child_index)) or child_match
            visible = own_match or child_match
            item.setHidden(not visible)
            if query and child_match:
                item.setExpanded(True)
            return visible

        root.setHidden(False)
        for child_index in range(root.childCount()):
            update_visibility(root.child(child_index))
        if query:
            root.setExpanded(True)

    def refresh_category_combo(self) -> None:
        current_id = self.category_combo.currentData()
        self._suppress_category_signal = True
        try:
            self._refresh_category_combo_items(current_id)
        finally:
            self._suppress_category_signal = False

    def _refresh_category_combo_items(self, current_id: int | None) -> None:
        self.category_combo.clear()
        self.category_combo.addItem("不指定", None)
        categories = sorted(
            self.category_lookup.values(),
            key=lambda category: (category.parent_id or 0, category.sort_order, category.name),
        )
        for category in categories:
            self.category_combo.addItem(self._category_breadcrumb(category), category.id)
        if current_id is not None:
            index = self.category_combo.findData(current_id)
            if index >= 0:
                self.category_combo.setCurrentIndex(index)

    def _category_breadcrumb(self, category: Category) -> str:
        names = [category.name]
        parent_id = category.parent_id
        while parent_id is not None and parent_id in self.category_lookup:
            parent = self.category_lookup[parent_id]
            names.append(parent.name)
            parent_id = parent.parent_id
        return " / ".join(reversed(names))

    def _on_primary_category_changed(self, index: int) -> None:
        """F19:下拉选择即主分类;重建选项时的信号被屏蔽,只在真实切换时标脏。"""
        if self._suppress_category_signal:
            return
        data = self.category_combo.itemData(index)
        if data is None:
            return
        if self.current_paper_id is not None and self.selected_category_ids != [int(data)]:
            self.selected_category_ids = [int(data)]
            self._mark_dirty()

    def refresh_papers(self) -> None:
        saved_kwargs: dict = {}
        if self.current_saved_search_id is not None:
            entry = next(
                (
                    saved
                    for saved in self.repository.list_saved_searches()
                    if saved.saved_search_id == self.current_saved_search_id
                ),
                None,
            )
            if entry is None:
                self.current_saved_search_id = None
            else:
                saved_kwargs = entry.rule.to_kwargs()
        smart_kwargs: dict = {}
        if self.smart_filter is not None:
            kind, value = self.smart_filter
            if kind == "smart":
                if value in READING_STATUSES:
                    smart_kwargs["reading_status"] = value
                elif value.isdigit():
                    smart_kwargs["year"] = int(value)
                else:
                    smart_kwargs["min_importance"] = int(value.split()[-1])
        merge_kwargs = dict(saved_kwargs)
        merge_kwargs.update(
            {key: value for key, value in smart_kwargs.items() if key not in merge_kwargs}
        )
        search_text = merge_kwargs.pop("search", None)
        if search_text is None:
            search_text = self.search_edit.text()
        merge_kwargs = {key: value for key, value in merge_kwargs.items() if value is not None}
        papers = self.repository.list_papers(
            search=search_text if search_text is not None else self.search_edit.text(),
            category_id=self.current_category_id,
            search_fields=self.selected_search_fields(),
            sort_by=str(self.paper_sort_combo.currentData() or "updated"),
            trashed=self._viewing_trash,
            tag_id=self.current_tag_id,
            **merge_kwargs,
        )
        selected_id = self.current_paper_id
        search_query = self.search_edit.text().strip()
        search_fields = self.selected_search_fields()
        initial_paper_id: int | None = None
        self._updating_paper_list = True
        try:
            self._reconcile_paper_list(papers, search_query, search_fields)
            if selected_id is not None and any(paper.id == selected_id for paper in papers):
                for row in range(self.paper_list.count()):
                    if self.paper_list.item(row).data(Qt.ItemDataRole.UserRole) == selected_id:
                        self.paper_list.setCurrentItem(self.paper_list.item(row))
                        break
            elif papers and self.current_paper_id is None:
                self.paper_list.setCurrentRow(0)
                initial_paper_id = papers[0].id
        finally:
            self._updating_paper_list = False

        count_text = f"{len(papers)} 条"
        self.paper_count.setText(count_text)
        self.header_count.setText(f"{len(papers)} 篇文献")
        self.populate_reader_queue(papers)
        self.statusBar().showMessage(f"当前视图共 {len(papers)} 篇文献")
        if not papers:
            # 搜索/筛选无结果:给出引导;有未保存编辑时保留详情现场,避免静默丢失
            if not self._dirty:
                self.current_paper_id = None
                self.clear_detail()
            self._show_paper_list_empty_state()
        else:
            self._hide_paper_list_empty_state()
            if initial_paper_id is not None:
                self.load_paper(initial_paper_id)
        self.sync_card_selection()

    def _reconcile_paper_list(
        self,
        papers: list[Paper],
        search_query: str,
        search_fields: set[str],
    ) -> None:
        """差量刷新:复用仍存在的行与卡片,只移除失效行、插入新行、原地更新内容。

        注意:离开列表的卡片会被 Qt 异步销毁,不做控件级缓存(曾因此产生悬空对象);
        保留"在场行原地更新"即可覆盖连续搜索输入的主场景。
        """
        list_widget = self.paper_list
        desired_ids = {paper.id for paper in papers}
        scrollbar = list_widget.verticalScrollBar()
        scroll_value = scrollbar.value()
        was_at_bottom = scroll_value >= scrollbar.maximum() > scrollbar.minimum()

        seen_ids: set[int] = set()
        for row in range(list_widget.count() - 1, -1, -1):
            item = list_widget.item(row)
            paper_id = item.data(Qt.ItemDataRole.UserRole)
            stale = paper_id not in desired_ids or paper_id in seen_ids
            seen_ids.add(paper_id)
            if stale:
                list_widget.takeItem(row)

        row_by_id = {
            list_widget.item(row).data(Qt.ItemDataRole.UserRole): row
            for row in range(list_widget.count())
        }
        for target, paper in enumerate(papers):
            item = list_widget.item(target)
            current_id = item.data(Qt.ItemDataRole.UserRole) if item else None
            if current_id != paper.id:
                source_row = row_by_id.get(paper.id)
                if source_row is None:
                    item = QListWidgetItem()
                    item.setData(Qt.ItemDataRole.UserRole, paper.id)
                    item.setSizeHint(QSize(0, theme.card_row_height()))
                    list_widget.insertItem(target, item)
                    for paper_id_key, row_index in row_by_id.items():
                        if row_index >= target:
                            row_by_id[paper_id_key] = row_index + 1
                else:
                    moved = list_widget.takeItem(source_row)
                    list_widget.insertItem(target, moved)
                    row_by_id = {
                        list_widget.item(row).data(Qt.ItemDataRole.UserRole): row
                        for row in range(list_widget.count())
                    }
                item = list_widget.item(target)
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                (
                    f"{paper.title or '未命名文献'}，{paper.authors or '未知作者'}，"
                    f"{paper.year or '年份未知'}，状态 {paper.reading_status or '待读'}"
                ),
            )
            card = list_widget.itemWidget(item)
            if not isinstance(card, PaperCard):
                card = PaperCard(
                    paper,
                    search_query=search_query,
                    match_fields=self.paper_match_fields(
                        paper, search_query, search_fields
                    ),
                )
                list_widget.setItemWidget(item, card)
                card.doubleClicked.connect(self.open_paper_id_in_reader)
                card.favoriteToggled.connect(self._on_card_favorite_toggled)
                card.listToggleRequested.connect(self._on_card_list_toggle)
            card.update_content(
                paper,
                search_query=search_query,
                match_fields=self.paper_match_fields(
                    paper, search_query, search_fields
                ),
            )
        if list_widget.count() != len(papers) or (
            list_widget.count() > 0
            and list_widget.item(list_widget.count() - 1).data(Qt.ItemDataRole.UserRole)
            != papers[-1].id
        ):
            # 自愈:极端时序导致行状态损坏时,清空后整体重建一次
            for row in range(list_widget.count() - 1, -1, -1):
                list_widget.takeItem(row)
            for target, paper in enumerate(papers):
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, paper.id)
                item.setSizeHint(QSize(0, theme.card_row_height()))
                list_widget.insertItem(target, item)
                card = PaperCard(
                    paper,
                    search_query=search_query,
                    match_fields=self.paper_match_fields(
                        paper, search_query, search_fields
                    ),
                )
                list_widget.setItemWidget(item, card)
                card.doubleClicked.connect(self.open_paper_id_in_reader)
                card.favoriteToggled.connect(self._on_card_favorite_toggled)
                card.listToggleRequested.connect(self._on_card_list_toggle)
                card.update_content(
                    paper,
                    search_query=search_query,
                    match_fields=self.paper_match_fields(
                        paper, search_query, search_fields
                    ),
                )
        self._restore_paper_list_scroll(scroll_value, was_at_bottom)

    def _restore_paper_list_scroll(self, scroll_value: int, was_at_bottom: bool) -> None:
        """U12:刷新后保持阅读位置——原位继续,原本在底部则跟随底部。"""
        scrollbar = self.paper_list.verticalScrollBar()
        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())
            return
        scrollbar.setValue(min(max(scroll_value, scrollbar.minimum()), scrollbar.maximum()))

    def _show_paper_list_empty_state(self) -> None:
        if self._viewing_trash:
            self.paper_list_empty_title.setText("回收站是空的")
            self.paper_list_empty_hint.setText("删除的文献会先进入回收站,可随时恢复。")
            self.clear_filters_button.setVisible(False)
        else:
            filtering = bool(self.search_edit.text().strip()) or self.current_category_id is not None
            if filtering:
                self.paper_list_empty_title.setText("未找到相关文献")
                self.paper_list_empty_hint.setText("换个关键词，或清除筛选条件再试。")
                self.clear_filters_button.setVisible(True)
            else:
                self.paper_list_empty_title.setText("文献库还是空的")
                self.paper_list_empty_hint.setText(
                    "点击顶部「导入或新建文献」添加 PDF，或到「发现」页看看最新论文。"
                )
                self.clear_filters_button.setVisible(False)
        overlay = self.paper_list_empty_state
        overlay.setGeometry(
            0, 0, self.paper_list.viewport().width(), self.paper_list.viewport().height()
        )
        overlay.raise_()
        overlay.show()

    def _hide_paper_list_empty_state(self) -> None:
        self.paper_list_empty_state.hide()

    def clear_library_filters(self) -> None:
        self.search_edit.clear()
        root = self.category_tree.topLevelItem(0)
        if root is not None and self.category_tree.currentItem() is not root:
            self.category_tree.setCurrentItem(root)
        elif self.search_timer.isActive():
            self.search_timer.stop()
            self.refresh_papers()

    def paper_match_fields(
        self,
        paper: Paper,
        query: str,
        selected_fields: set[str],
    ) -> list[str]:
        if not query:
            return []
        needle = query.casefold()
        detail = paper.detail
        candidates = [
            ("title", "标题", paper.title),
            ("authors", "作者", paper.authors),
            ("abstract", "摘要", paper.abstract),
            (
                "memory_anchor",
                "记忆锚点",
                detail.memory_anchor if detail is not None else "",
            ),
            (
                "summary",
                "总结/方法",
                "\n".join(
                    [
                        detail.summary,
                        detail.datasets,
                        detail.dataset_metrics,
                        detail.methods,
                        detail.contributions,
                    ]
                )
                if detail is not None
                else "",
            ),
            (
                "notes",
                "批注",
                "\n".join(
                    [
                        detail.personal_notes,
                        detail.experiment_notes,
                        detail.reproducibility_notes,
                    ]
                )
                if detail is not None
                else "",
            ),
            (
                "metadata",
                "元数据",
                "\n".join(
                    [
                        paper.venue,
                        paper.reading_status,
                        paper.doi,
                        paper.arxiv_id,
                    ]
                ),
            ),
            (
                "full_text",
                "全文",
                query if bool(getattr(paper, "full_text_match", False)) else "",
            ),
        ]
        return [
            label
            for field, label, value in candidates
            if field in selected_fields and needle in (value or "").casefold()
        ][:3]

    def _open_saved_search_menu(self, position, saved_search_id: int | None) -> None:
        """F20 智能文件夹右键菜单:父节点=新建,子节点=编辑/重命名/删除。"""
        menu = QMenu(self)
        if saved_search_id is None:
            save_current_action = menu.addAction("把当前筛选存为智能文件夹…")
            new_action = menu.addAction("新建智能文件夹…")
            action = menu.exec(self.category_tree.viewport().mapToGlobal(position))
            if action == save_current_action:
                self.create_saved_search_from_current()
            elif action == new_action:
                self.create_saved_search_blank()
            return
        edit_action = menu.addAction("编辑规则…")
        rename_action = menu.addAction("重命名…")
        menu.addSeparator()
        delete_action = menu.addAction("删除")
        action = menu.exec(self.category_tree.viewport().mapToGlobal(position))
        if action == edit_action:
            self.edit_saved_search(saved_search_id)
        elif action == rename_action:
            self.rename_saved_search(saved_search_id)
        elif action == delete_action:
            self.delete_saved_search(saved_search_id)

    def _rule_from_current_filters(self) -> SavedSearchRule:
        """把当前列表页的筛选状态打包成规则(所见即所得)。"""
        checked = {
            field
            for field, checkbox in self.search_checkboxes.items()
            if checkbox.isChecked()
        }
        all_fields = set(self.search_checkboxes)
        fields = tuple(checked) if checked != all_fields else None
        reading_status = None
        year = None
        min_importance = None
        if self.smart_filter is not None:
            _kind, value = self.smart_filter
            if value in READING_STATUSES:
                reading_status = value
            elif value.isdigit():
                year = int(value)
            else:
                min_importance = int(value.split()[-1])
        return SavedSearchRule(
            search=self.search_edit.text().strip(),
            search_fields=fields,
            tag_id=self.current_tag_id,
            category_id=self.current_category_id,
            reading_status=reading_status,
            year=year,
            min_importance=min_importance,
        )

    def create_saved_search_blank(self) -> None:
        self._open_saved_search_dialog(name="")

    def create_saved_search_from_current(self) -> None:
        rule = self._rule_from_current_filters()
        self._open_saved_search_dialog(name="", rule=rule)

    def edit_saved_search(self, saved_search_id: int) -> None:
        entry = next(
            (
                saved
                for saved in self.repository.list_saved_searches()
                if saved.saved_search_id == saved_search_id
            ),
            None,
        )
        if entry is None:
            return
        self._open_saved_search_dialog(
            name=entry.name, rule=entry.rule, saved_search_id=saved_search_id
        )

    def rename_saved_search(self, saved_search_id: int) -> None:
        entry = next(
            (
                saved
                for saved in self.repository.list_saved_searches()
                if saved.saved_search_id == saved_search_id
            ),
            None,
        )
        if entry is None:
            return
        name, accepted = QInputDialog.getText(
            self, "重命名智能文件夹", "新名称:", QLineEdit.EchoMode.Normal, entry.name
        )
        if not accepted or not name.strip():
            return
        self.repository.update_saved_search(saved_search_id, name, entry.rule)
        self.refresh_categories()

    def delete_saved_search(self, saved_search_id: int) -> None:
        entry = next(
            (
                saved
                for saved in self.repository.list_saved_searches()
                if saved.saved_search_id == saved_search_id
            ),
            None,
        )
        if entry is None:
            return
        if not confirm_action(
            self,
            title=f"删除智能文件夹「{entry.name}」?",
            message="只删除这个筛选规则,文献与分类/标签不受影响。",
            confirm_label="删除",
            destructive=True,
        ):
            return
        self.repository.delete_saved_search(saved_search_id)
        if self.current_saved_search_id == saved_search_id:
            self.current_saved_search_id = None
        self.refresh_categories()
        self.refresh_papers()

    def _open_saved_search_dialog(
        self,
        *,
        name: str,
        rule: SavedSearchRule | None = None,
        saved_search_id: int | None = None,
    ) -> None:
        dialog = SavedSearchDialog(self.repository, name=name, rule=rule, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_name = dialog.name_edit.text().strip()
        new_rule = dialog.build_rule()
        if saved_search_id is None:
            entry = self.repository.create_saved_search(new_name, new_rule)
            # 建完即应用,所见即所得
            self.current_category_id = None
            self.current_tag_id = None
            self.smart_filter = None
            self.current_saved_search_id = entry.saved_search_id
        else:
            self.repository.update_saved_search(saved_search_id, new_name, new_rule)
        self.refresh_categories()
        self.refresh_papers()

    def open_category_menu(self, position) -> None:
        item = self.category_tree.itemAt(position)
        if item is not None:
            self.category_tree.setCurrentItem(item)
        data = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if data == SAVED_SEARCH_NONE:
            self._open_saved_search_menu(position, saved_search_id=None)
            return
        if isinstance(data, tuple) and len(data) == 2 and data[0] == "saved":
            self._open_saved_search_menu(position, saved_search_id=int(data[1]))
            return
        # 树空白处/任意节点都提供「把当前筛选存为智能文件夹」入口
        if item is None:
            menu = QMenu(self)
            save_current_action = menu.addAction("把当前筛选存为智能文件夹…")
            new_saved_action = menu.addAction("新建智能文件夹…")
            action = menu.exec(self.category_tree.viewport().mapToGlobal(position))
            if action == save_current_action:
                self.create_saved_search_from_current()
            elif action == new_saved_action:
                self.create_saved_search_blank()
            return
        category_id = data
        menu = QMenu(self)
        add_action = menu.addAction("添加子分类")
        add_sibling_action = menu.addAction("添加同级分类")
        rename_action = menu.addAction("重命名")
        menu.addSeparator()
        move_up_action = menu.addAction("上移")
        move_down_action = menu.addAction("下移")
        menu.addSeparator()
        delete_action = menu.addAction("删除")

        is_root = category_id is None
        rename_action.setEnabled(not is_root)
        move_up_action.setEnabled(not is_root)
        move_down_action.setEnabled(not is_root)
        delete_action.setEnabled(not is_root)
        add_sibling_action.setEnabled(item is not None)

        action = menu.exec(self.category_tree.viewport().mapToGlobal(position))
        if action == add_action:
            self.add_category(parent_id=category_id)
        elif action == add_sibling_action:
            parent_id = self.category_lookup.get(category_id).parent_id if category_id in self.category_lookup else None
            self.add_category(parent_id=parent_id)
        elif action == rename_action and category_id is not None:
            self.rename_category(int(category_id))
        elif action == move_up_action and category_id is not None:
            self.move_category(int(category_id), -1)
        elif action == move_down_action and category_id is not None:
            self.move_category(int(category_id), 1)
        elif action == delete_action and category_id is not None:
            self.delete_category(int(category_id))

    def open_paper_menu(self, position) -> None:
        item = self.paper_list.itemAt(position)
        if item is not None:
            self.paper_list.setCurrentItem(item)
        menu = QMenu(self)
        open_action = menu.addAction("打开 PDF")
        ai_action = menu.addAction("AI 解析")
        save_action = menu.addAction("保存当前编辑")
        menu.addSeparator()
        copy_title_action = menu.addAction("复制标题")
        copy_bibtex_action = menu.addAction("复制 BibTeX")
        export_action = menu.addAction("导出引用(BibTeX/RIS)")
        menu.addSeparator()
        new_action = menu.addAction("新建文献")
        delete_action = menu.addAction("移入回收站")
        has_paper = self.current_paper_id is not None
        open_action.setEnabled(has_paper)
        ai_action.setEnabled(has_paper)
        save_action.setEnabled(has_paper)
        copy_title_action.setEnabled(has_paper)
        copy_bibtex_action.setEnabled(has_paper)
        export_action.setEnabled(has_paper)
        delete_action.setEnabled(has_paper)

        action = menu.exec(self.paper_list.viewport().mapToGlobal(position))
        if action == open_action:
            self.open_current_pdf()
        elif action == ai_action:
            self.ai_analyze_current()
        elif action == save_action:
            self.save_current_paper()
        elif action == copy_title_action:
            self.copy_current_paper_title()
        elif action == copy_bibtex_action:
            self.copy_current_bibtex()
        elif action == export_action:
            self.batch_export_citations()
        elif action == new_action:
            self.create_blank_paper()
        elif action == delete_action:
            self.delete_current_paper()

    def copy_current_paper_title(self) -> None:
        """U16:右键复制当前文献标题。"""
        paper = (
            self.repository.get_paper(self.current_paper_id)
            if self.current_paper_id is not None
            else None
        )
        if paper is None:
            return
        QApplication.clipboard().setText(paper.title or "")
        self.statusBar().showMessage("标题已复制到剪贴板", 3000)

    def selected_paper_ids(self) -> list[int]:
        ids = [
            int(item.data(Qt.ItemDataRole.UserRole))
            for item in self.paper_list.selectedItems()
            if item.data(Qt.ItemDataRole.UserRole) is not None
        ]
        if not ids and self.current_paper_id is not None:
            ids.append(self.current_paper_id)
        return list(dict.fromkeys(ids))

    def batch_set_reading_status(self, reading_status: str) -> None:
        paper_ids = self.selected_paper_ids()
        if not paper_ids or not self._confirm_pending_changes():
            return
        updated = self.repository.set_papers_reading_status(paper_ids, reading_status)
        current_id = self.current_paper_id
        self.refresh_papers()
        if current_id is not None and current_id in paper_ids:
            self.load_paper(current_id)
        self.statusBar().showMessage(
            f"已将 {updated} 篇文献设置为“{reading_status}”",
            4000,
        )

    def batch_add_category(self) -> None:
        paper_ids = self.selected_paper_ids()
        if not paper_ids or not self._confirm_pending_changes():
            return
        categories = sorted(
            self.category_lookup.values(),
            key=lambda category: self._category_breadcrumb(category).casefold(),
        )
        if not categories:
            QMessageBox.information(self, "没有分类", "请先创建一个分类。")
            return
        labels = [self._category_breadcrumb(category) for category in categories]
        selected_label, accepted = QInputDialog.getItem(
            self,
            "批量添加分类",
            f"给所选 {len(paper_ids)} 篇文献添加分类：",
            labels,
            0,
            False,
        )
        if not accepted:
            return
        category = categories[labels.index(selected_label)]
        updated = self.repository.add_category_to_papers(paper_ids, category.id)
        current_id = self.current_paper_id
        self.refresh_categories()
        self.refresh_papers()
        if current_id is not None and current_id in paper_ids:
            self.load_paper(current_id)
        self.statusBar().showMessage(
            f"已为 {updated} 篇文献添加分类“{category.name}”",
            4000,
        )

    def batch_delete_papers(self) -> None:
        paper_ids = self.selected_paper_ids()
        if not paper_ids or not self._confirm_pending_changes():
            return
        if not confirm_action(
            self,
            title=f"将 {len(paper_ids)} 篇文献移入回收站？",
            message="所选文献会移入回收站,可随时恢复。",
            detail="文献从列表隐藏;PDF 文件保留。可在左侧「回收站」恢复或彻底删除。",
            confirm_label="移入回收站",
            destructive=True,
        ):
            return
        deleted = self.repository.delete_papers(paper_ids)
        if self.current_paper_id in paper_ids:
            self.current_paper_id = None
            self._set_dirty(False)
        self.refresh_categories()
        self.refresh_papers()
        self.refresh_dataset_metrics()
        self.statusBar().showMessage(f"已将 {deleted} 篇文献移入回收站,可从左侧恢复", 4000)
        self._offer_undo_trash(paper_ids)

    def _offer_undo_trash(self, paper_ids: list[int]) -> None:
        """移入回收站后短暂提供一键撤销(U11)。"""
        bar = QMessageBox(self)
        bar.setIcon(QMessageBox.Icon.NoIcon)
        bar.setWindowTitle("已移入回收站")
        bar.setText(f"已将 {len(paper_ids)} 篇文献移入回收站。")
        undo_button = bar.addButton("撤销", QMessageBox.ButtonRole.ActionRole)
        bar.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
        bar.setModal(False)
        bar.show()
        QTimer.singleShot(6000, bar.close)

        def undo():
            self.repository.restore_papers(paper_ids)
            bar.close()
            self.refresh_categories()
            self.refresh_papers()
            self.refresh_dataset_metrics()
            self.statusBar().showMessage("已撤销,文献已恢复", 3000)

        undo_button.clicked.connect(undo)

    def batch_restore_papers(self) -> None:
        paper_ids = self.selected_paper_ids()
        if not paper_ids:
            return
        restored = self.repository.restore_papers(paper_ids)
        if self.current_paper_id in paper_ids:
            self.current_paper_id = None
            self._set_dirty(False)
        # 恢复后显式回到文献库视图(旧实现依赖树重建的选中副作用,已移除)
        self._viewing_trash = False
        self.refresh_categories()
        self.refresh_papers()
        self.refresh_dataset_metrics()
        self.statusBar().showMessage(f"已从回收站恢复 {restored} 篇文献", 4000)

    def batch_purge_papers(self) -> None:
        paper_ids = self.selected_paper_ids()
        if not paper_ids or not self._confirm_pending_changes():
            print("DBG purge aborted")
            return
        dialog = ConfirmationDialog(
            window_title="彻底删除",
            title=f"彻底删除 {len(paper_ids)} 篇文献？",
            message="此操作不可恢复,文献与关联数据将被清除。",
            detail="同时会删除受管 PDF 原文件与已生成的译文目录,释放磁盘空间。",
            eyebrow="危险操作",
            tone="warning",
            actions=(
                DialogAction("cancel", "取消"),
                DialogAction("purge", "彻底删除", "danger", default=True),
            ),
            parent=self,
        )
        if dialog.exec_action() != "purge":
            return
        # 阅读器组件会缓存已打开的 PDF 句柄,先释放才能在 Windows 上删除文件
        self.reader_pdf_preview.close_documents()
        file_paths: list[Path] = []
        translation_dirs: list[Path] = []
        for paper_id in paper_ids:
            paper = self.repository.get_paper(paper_id, include_text_content=False)
            if paper is not None and paper.pdf_path:
                resolved = self.repository.resolve_pdf_path(paper.pdf_path)
                if resolved is not None and resolved.exists():
                    file_paths.append(resolved)
            translation_dirs.append(self.translation_output_dir(paper_id))
        purged = self.repository.purge_papers(paper_ids)
        removed_files = 0
        for path in file_paths:
            try:
                path.unlink(missing_ok=True)
                removed_files += 1
            except OSError:
                continue
        for directory in translation_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        if self.current_paper_id in paper_ids:
            self.current_paper_id = None
            self._set_dirty(False)
        self.refresh_categories()
        self.refresh_papers()
        self.refresh_dataset_metrics()
        self.statusBar().showMessage(
            f"已彻底删除 {purged} 篇文献,清理 {removed_files} 个 PDF 与译文文件", 5000
        )

    def _rebuild_batch_menu(self) -> None:
        menu = self._batch_menu
        menu.clear()
        if self._viewing_trash:
            menu.addAction("恢复所选文献").triggered.connect(self.batch_restore_papers)
            menu.addSeparator()
            purge_action = menu.addAction("彻底删除(含 PDF 与译文)")
            purge_action.triggered.connect(self.batch_purge_papers)
            return
        status_menu = menu.addMenu("设置阅读状态")
        for reading_status in READING_STATUSES:
            action = status_menu.addAction(reading_status)
            action.triggered.connect(
                lambda _checked=False, value=reading_status: self.batch_set_reading_status(value)
            )
        menu.addAction("添加分类").triggered.connect(self.batch_add_category)
        menu.addSeparator()
        menu.addAction("翻译所选文献").triggered.connect(self.batch_translate_papers)
        menu.addAction("导出引用(BibTeX/RIS)").triggered.connect(self.batch_export_citations)
        menu.addAction("移入回收站").triggered.connect(self.batch_delete_papers)

    def batch_translate_papers(self) -> None:
        """F22:多选批量翻译——一次确认,逐篇进入现有任务队列串行执行。"""
        paper_ids = self.selected_paper_ids()
        candidates: list[tuple[int, Path]] = []
        missing = 0
        for paper_id in paper_ids:
            paper = self.repository.get_paper(paper_id)
            if paper is None or not paper.pdf_path:
                missing += 1
                continue
            pdf_path = self.repository.resolve_pdf_path(paper.pdf_path)
            if pdf_path is None or not pdf_path.exists():
                missing += 1
                continue
            candidates.append((paper_id, pdf_path))
        if not candidates:
            QMessageBox.information(
                self, "没有可翻译文献", "所选文献没有本地 PDF,无法翻译。"
            )
            return
        total_mb = sum(path.stat().st_size for _, path in candidates) / (1024 * 1024)
        if not confirm_external_request(
            self.settings_path,
            operation="批量翻译所选 PDF",
            content_summary=(
                f"{len(candidates)} 篇 PDF 全文，共约 {total_mb:.1f} MB"
                + (f"（另有 {missing} 篇无本地 PDF，将跳过）" if missing else "")
            ),
            output_summary="译文将保存到各文献的翻译目录",
            parent=self,
        ):
            self.statusBar().showMessage("已取消批量翻译", 3000)
            return
        for paper_id, pdf_path in candidates:
            if self.translate_thread is not None or self.ai_thread is not None:
                self._enqueue_task(
                    "PDF 翻译",
                    pdf_path.name,
                    lambda p=paper_id, path=pdf_path: self.start_pdf_translation(
                        p, path, confirm=False
                    ),
                )
            else:
                self.start_pdf_translation(paper_id, pdf_path, confirm=False)
        self.statusBar().showMessage(
            f"已加入翻译队列 {len(candidates)} 篇"
            + (f"（跳过 {missing} 篇无本地 PDF）" if missing else ""),
            5000,
        )

    def batch_export_citations(self) -> None:
        from synthrecall.citation_export import papers_to_bibtex, papers_to_ris, write_export

        paper_ids = self.selected_paper_ids()
        papers = (
            [
                self.repository.get_paper(paper_id, include_text_content=False)
                for paper_id in paper_ids
            ]
            if paper_ids
            else self.repository.list_papers()
        )
        if not papers:
            QMessageBox.information(self, "没有文献", "当前没有可导出的文献。")
            return
        file_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出引用",
            str(self.data_dir / "exports" / "references.bib"),
            "BibTeX (*.bib);;RIS (*.ris)",
        )
        if not file_path:
            return
        export_format = "ris" if "ris" in selected_filter.casefold() or file_path.casefold().endswith(".ris") else "bibtex"
        content = (
            papers_to_ris(papers) if export_format == "ris" else papers_to_bibtex(papers)
        )
        try:
            write_export(Path(file_path), content)
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：{exc}")
            return
        self.statusBar().showMessage(
            f"已导出 {len(papers)} 篇文献的引用({export_format.upper()}):{file_path}", 5000
        )
    def apply_library_quick_view(self, view_name: str) -> None:
        self.current_category_id = None
        self.current_tag_id = None
        self.search_edit.clear()
        sort_by = {
            "all": "updated",
            "updated": "updated",
            "recent": "last_opened",
            "last_opened": "last_opened",
            "important": "importance",
        }.get(view_name, "updated")
        index = self.paper_sort_combo.findData(sort_by)
        if index >= 0:
            self.paper_sort_combo.setCurrentIndex(index)
        self.refresh_categories()
        self.refresh_papers()

    def show_detail_editor(self) -> None:
        if self.current_paper_id is None:
            QMessageBox.information(self, "没有文献", "请先选择一篇文献。")
            return
        self._push_nav_history("editor")
        self.update_editor_workspace_identity()
        self.app_header.hide()
        self.main_stack.setCurrentWidget(self.editor_page)
        self.editor_outline.setCurrentRow(0)
        self._navigate_editor_outline(0)
        self._set_active_workspace("library")
        self.statusBar().showMessage("编辑资料：按 Ctrl+S 保存，返回文献库前会检查未保存修改")

    def return_from_editor(self) -> None:
        if not self._confirm_pending_changes():
            return
        # F28:编辑器里可能改了主分类(含从未分类改为正式分类),
        # 无论保存还是放弃都刷新树计数与列表,避免"仍在未分类"的假象
        self.refresh_categories()
        self.refresh_papers()
        self.show_detail_summary()
        self.statusBar().showMessage("已返回文献库", 3000)

    def show_detail_summary(self) -> None:
        self.detail_stack.setCurrentIndex(0)
        self.inspector_panel.setMaximumWidth(560)
        if self.main_stack.currentWidget() is self.editor_page:
            self.app_header.show()
            self.main_stack.setCurrentWidget(self.library_page)

    def load_reader_documents(self, paper: Paper) -> None:
        resolved = self.repository.resolve_pdf_path(paper.pdf_path) if paper.pdf_path else None
        original = resolved if resolved is not None and resolved.exists() else None
        mono = None
        dual = None
        if original is not None:
            result = find_translation_result(
                original,
                self.translation_output_dir(paper.id),
            )
            mono = result.mono_pdf
            dual = result.dual_pdf
        self.reader_pdf_preview.set_documents(
            original=original,
            mono=mono,
            dual=dual,
        )

    def enter_reader(self) -> None:
        if self.current_paper_id is None:
            QMessageBox.information(self, "没有文献", "请先选择一篇文献。")
            return
        self._push_nav_history("reader")
        # F22:换一篇文献阅读时,先把上一篇的未落库时长落账再重置
        if self._reading_paper_id != self.current_paper_id:
            self._flush_reading_seconds()
            self._reading_paper_id = self.current_paper_id
            self._reading_seconds = 0
            self._reading_promoted = set()
        self.populate_reader_queue()
        self.app_header.hide()
        self.main_stack.setCurrentWidget(self.reader_page)
        self._set_active_workspace("library")
        QTimer.singleShot(0, self.reader_pdf_preview.fit_width)
        self.statusBar().showMessage("沉浸阅读：PDF、要点与批注保持在同一上下文中")

    def open_paper_in_reader(self, item: QListWidgetItem) -> None:
        paper_id = item.data(Qt.ItemDataRole.UserRole)
        if paper_id is not None:
            self.open_paper_id_in_reader(int(paper_id))

    def open_paper_id_in_reader(self, paper_id: int) -> None:
        if self.current_paper_id != paper_id:
            self.select_paper(paper_id)
        if self.current_paper_id == paper_id:
            self.enter_reader()

    def leave_reader(self) -> None:
        if not self._confirm_pending_changes():
            return
        self._flush_reading_seconds()
        self.app_header.show()
        self.main_stack.setCurrentWidget(self.library_page)
        self.show_detail_summary()
        self._set_active_workspace("library")
        self.statusBar().showMessage("已返回文献库", 3000)

    def populate_reader_queue(self, papers: list[Paper] | None = None) -> None:
        if papers is None:
            papers = self.repository.list_papers(
                search=self.search_edit.text(),
                category_id=self.current_category_id,
                search_fields=self.selected_search_fields(),
                sort_by=str(self.paper_sort_combo.currentData() or "updated"),
            )
        self._updating_reader_queue = True
        try:
            self.reader_paper_list.clear()
            for paper in papers:
                title = self._brief_text(paper.title or "未命名文献", 44)
                item = QListWidgetItem(
                    f"{title}\n{paper.year or '年份未知'} · {paper.reading_status or '待读'}"
                )
                item.setData(Qt.ItemDataRole.UserRole, paper.id)
                item.setToolTip(paper.title or "未命名文献")
                item.setSizeHint(QSize(0, 64))
                self.reader_paper_list.addItem(item)
                if paper.id == self.current_paper_id:
                    self.reader_paper_list.setCurrentItem(item)
            self.reader_queue_count.setText(f"{len(papers)} 篇")
        finally:
            self._updating_reader_queue = False

    def on_reader_paper_selected(self) -> None:
        if self._updating_reader_queue:
            return
        item = self.reader_paper_list.currentItem()
        if item is None:
            return
        paper_id = item.data(Qt.ItemDataRole.UserRole)
        if paper_id is None or int(paper_id) == self.current_paper_id:
            return
        if not self._confirm_pending_changes():
            self.populate_reader_queue()
            return
        self.load_paper(int(paper_id))
        self.select_paper(int(paper_id))
        QTimer.singleShot(0, self.reader_pdf_preview.fit_width)

    def sync_reader_notes_to_detail(self) -> None:
        if self._suppress_dirty_tracking or not hasattr(self, "personal_notes_edit"):
            return
        text = self.reader_notes_edit.toPlainText()
        if self.personal_notes_edit.toPlainText() != text:
            self.personal_notes_edit.setPlainText(text)

    def save_reader_notes(self) -> None:
        if self.current_paper_id is None:
            return
        self.personal_notes_edit.setPlainText(self.reader_notes_edit.toPlainText())
        if self.save_current_paper(refresh=False):
            self.statusBar().showMessage("阅读批注已保存", 3000)

    def refresh_pdf_annotations(self) -> None:
        """刷新右侧「批注」页的 PDF 批注列表(阅读器文档变化/保存后自动调用)。"""
        list_widget = getattr(self, "pdf_annotations_list", None)
        if list_widget is None:
            return
        summary = self.reader_pdf_preview.annotations_summary()
        list_widget.clear()
        kind_labels = {"Highlight": "高亮", "FreeText": "文字批注", "Ink": "手绘"}
        for entry in summary:
            kind = kind_labels.get(entry["kind"], entry["kind"])
            text = entry["text"]
            clipped = text if len(text) <= 120 else text[:119] + "…"
            item = QListWidgetItem(f"第 {entry['page']} 页 · {kind}\n{clipped}")
            item.setData(Qt.ItemDataRole.UserRole, entry["page"])
            item.setToolTip(text)
            list_widget.addItem(item)
        count_text = f"共 {len(summary)} 条" if summary else "还没有 PDF 批注"
        self.pdf_annotations_count.setText(count_text)

    def _jump_to_pdf_annotation(self, item: QListWidgetItem) -> None:
        page = item.data(Qt.ItemDataRole.UserRole)
        if page is None:
            return
        self.reader_pdf_preview.jump_to_page(int(page) - 1)
        self.statusBar().showMessage(f"已跳转到第 {page} 页", 2500)

    def export_reader_notes_markdown(self) -> None:
        if self.current_paper_id is None:
            QMessageBox.information(self, "未选择文献", "请先选择要导出批注的文献。")
            return
        paper = self.repository.get_paper(self.current_paper_id)
        if paper is None:
            return
        summary = self.reader_pdf_preview.annotations_summary()
        notes = self.reader_notes_edit.toPlainText().strip()
        if not summary and not notes:
            QMessageBox.information(
                self, "暂无内容", "当前文献还没有 PDF 批注或笔记,先去添加一些吧。"
            )
            return
        export_dir = self.data_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        safe_title = "".join(
            ch for ch in (paper.title or "未命名文献")[:60] if ch not in r'\/:*?"<>|'
        ).strip() or "未命名文献"
        default_path = export_dir / f"{safe_title}-批注.md"
        file_path, _selected = QFileDialog.getSaveFileName(
            self,
            "导出批注为 Markdown",
            str(default_path),
            "Markdown 文件 (*.md);;所有文件 (*.*)",
        )
        if not file_path:
            return
        kind_labels = {"Highlight": "高亮", "FreeText": "文字批注", "Ink": "手绘"}
        lines = [f"# {paper.title or '未命名文献'}", ""]
        if paper.authors or paper.year:
            meta = " · ".join(part for part in (paper.authors, str(paper.year or "")) if part)
            lines += [meta, ""]
        if notes:
            lines += ["## 我的笔记", "", notes, ""]
        if summary:
            lines += ["## PDF 批注", ""]
            last_page = None
            for entry in summary:
                if entry["page"] != last_page:
                    lines.append(f"### 第 {entry['page']} 页")
                    lines.append("")
                    last_page = entry["page"]
                kind = kind_labels.get(entry["kind"], entry["kind"])
                lines.append(f"- **{kind}** {entry['text']}")
            lines.append("")
        try:
            Path(file_path).write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：{exc}")
            return
        self.statusBar().showMessage(f"批注已导出：{file_path}", 5000)

    # ------------------------------------------------------------------
    # 浏览历史返回:记录页面切换顺序,Back = 回到上一页(非上一层)
    # ------------------------------------------------------------------

    def _current_nav_entry(self) -> tuple[str, int | None] | None:
        widget = self.main_stack.currentWidget()
        paper_id = self.current_paper_id
        if widget is self.library_page:
            return ("library", None)
        if widget is self.dataset_page:
            return ("dataset", None)
        if widget is self.discovery_page:
            return ("discovery", None)
        if widget is self.reader_page:
            return ("reader", paper_id)
        if widget is self.editor_page:
            return ("editor", paper_id)
        return None

    def _push_nav_history(self, target_key: str) -> None:
        """切页前记录当前页面;回退中的导航与同页切换不记录。"""
        if self._nav_back_in_progress:
            return
        current = self._current_nav_entry()
        if current is None or current[0] == target_key:
            return
        if self._nav_history and self._nav_history[-1] == current:
            return
        self._nav_history.append(current)
        if len(self._nav_history) > 50:  # 防长会话无限膨胀
            del self._nav_history[: len(self._nav_history) - 50]

    def go_back_previous_view(self) -> None:
        if not self._nav_history:
            self.statusBar().showMessage("没有上一页了", 2000)
            return
        key, paper_id = self._nav_history.pop()
        self._nav_back_in_progress = True
        try:
            if key == "library":
                self.show_library_page()
            elif key == "dataset":
                self.show_dataset_page()
            elif key == "discovery":
                self.show_discovery_page()
            elif key == "reader" and paper_id is not None:
                self.open_paper_id_in_reader(paper_id)
            elif key == "editor" and paper_id is not None:
                if self.current_paper_id != paper_id:
                    self.select_paper(paper_id)
                self.show_detail_editor()
        finally:
            self._nav_back_in_progress = False

    def eventFilter(self, obj, event) -> bool:
        # 鼠标侧键后退 = 返回上一页(XButton1);前进键不做绑定
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.XButton1
        ):
            self.go_back_previous_view()
            return True
        return super().eventFilter(obj, event)

    def show_library_page(self) -> None:
        guarded_pages = tuple(
            page
            for page in (
                getattr(self, "reader_page", None),
                getattr(self, "editor_page", None),
            )
            if page is not None
        )
        if (
            self.main_stack.currentWidget() in guarded_pages
            and not self._confirm_pending_changes()
        ):
            return
        self._push_nav_history("library")
        self.app_header.show()
        self.main_stack.setCurrentWidget(self.library_page)
        self.show_detail_summary()
        self._set_active_workspace("library")

    def show_dataset_page(self) -> None:
        if not self._confirm_pending_changes():
            return
        self._push_nav_history("dataset")
        self.app_header.show()
        self.refresh_dataset_metrics()
        self.main_stack.setCurrentIndex(1)
        self._set_active_workspace("dataset")

    def show_discovery_page(self) -> None:
        if not self._confirm_pending_changes():
            return
        self._push_nav_history("discovery")
        self.app_header.show()
        self.main_stack.setCurrentWidget(self.discovery_page)
        self._set_active_workspace("discovery")

    def import_discovered_paper(self, arxiv_paper: ArxivPaper) -> int | None:
        if not self._confirm_pending_changes():
            return None
        paper = self.repository.create_blank_paper()
        score = arxiv_paper.score
        summary_parts = []
        if score is not None:
            summary_parts.extend(score.summary_points)
            summary_parts.append(f"AI value score: {score.overall}/100")
            summary_parts.append(score.reason)
        detail_summary = "\n".join(f"- {part}" for part in summary_parts if part)
        update = PaperUpdate(
            title=arxiv_paper.title,
            authors=arxiv_paper.authors_text,
            year=arxiv_paper.year,
            venue=f"arXiv {arxiv_paper.primary_category}".strip(),
            doi=arxiv_paper.doi,
            arxiv_id=arxiv_paper.arxiv_id,
            url=arxiv_paper.entry_url,
            abstract=arxiv_paper.summary,
            pdf_path=arxiv_paper.local_pdf_path,
            reading_status=READING_STATUSES[0],
            importance=5 if score and score.overall >= 88 else 4 if score and score.overall >= 78 else 3,
            summary=detail_summary,
            datasets="",
            dataset_metrics="",
            methods="; ".join(score.matched_terms) if score is not None else "",
            contributions=score.reason if score is not None else "",
            limitations="",
            experiment_notes="",
            reproducibility_notes=(
                f"Discovery reproducibility score: {score.reproducibility}/10"
                if score is not None
                else ""
            ),
            memory_anchor="; ".join(score.matched_terms[:5]) if score is not None else "",
            personal_notes="Imported from arXiv Discovery.",
        )
        try:
            self.repository.update_paper(paper.id, update, category_ids=[])
        except Exception as exc:
            QMessageBox.warning(self, "入库失败", f"无法把 arXiv 文献写入本地文献库：{exc}")
            return None
        self.discovery_page.mark_current_saved(arxiv_paper)
        self.current_paper_id = paper.id
        self.refresh_categories()
        self.refresh_papers()
        self.select_paper(paper.id)
        self.load_paper(paper.id)
        self.statusBar().showMessage(f"已入库：{arxiv_paper.title}", 5000)
        return paper.id

    def deep_read_discovered_paper(self, arxiv_paper: ArxivPaper) -> None:
        if not self.ensure_api_ready():
            return
        paper_id = self.import_discovered_paper(arxiv_paper)
        if paper_id is None:
            return
        self.show_library_page()
        self.start_ai_analysis(paper_id)

    def open_metric_paper(self, item: QTableWidgetItem | None = None) -> None:
        row = item.row() if item is not None else self.dataset_table.currentRow()
        if row < 0:
            return
        source_item = self.dataset_table.item(row, 0)
        if source_item is None:
            return
        paper_id = source_item.data(Qt.ItemDataRole.UserRole + 1)
        if paper_id is None:
            # 未关联文献不再弹模态框打断(B28 同思路):直接打开该条目的
            # 编辑对话框,当场把来源文献关联上
            self.dataset_table.setCurrentCell(row, 0)
            self.edit_ranking_entry()
            return
        if not self._confirm_pending_changes():
            return
        self.current_category_id = None
        if self.category_tree.topLevelItemCount():
            self.category_tree.setCurrentItem(self.category_tree.topLevelItem(0))
        self.show_library_page()
        self.refresh_papers()
        self.select_paper(int(paper_id))
        self.load_paper(int(paper_id))
        self.tabs.setCurrentIndex(1)

    def refresh_dataset_metrics(self) -> None:
        if not hasattr(self, "dataset_list"):
            return
        current_dataset = self.current_dataset_name()
        query = self.dataset_filter_edit.text().strip().lower()
        self.dataset_metric_rows = self.repository.list_dataset_metrics()
        dataset_names = sorted({row.dataset for row in self.dataset_metric_rows if row.dataset})
        if query:
            dataset_names = [name for name in dataset_names if query in name.lower()]

        self.dataset_list.blockSignals(True)
        self.dataset_list.clear()
        for name in dataset_names:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.dataset_list.addItem(item)
            if name == current_dataset:
                self.dataset_list.setCurrentItem(item)
        if self.dataset_list.currentItem() is None and self.dataset_list.count():
            self.dataset_list.setCurrentRow(0)
        self.dataset_list.blockSignals(False)
        self.update_metric_options()
        self.populate_dataset_ranking()
        self.statusBar().showMessage(f"共 {len(dataset_names)} 个数据集")
        # U6:库中还没有任何数据集时,显示页面内空态引导(代替弹窗/散落按钮)
        empty = not self.dataset_metric_rows
        if hasattr(self, "dataset_empty_state"):
            self.dataset_empty_state.setVisible(empty)
            self.dataset_splitter.setVisible(not empty)

    def _go_add_dataset_metrics(self) -> None:
        self.show_library_page()
        self.show_detail_editor()
        self.statusBar().showMessage("在「研究总结」页填写数据集指标,保存后会出现在排行页", 5000)

    def current_dataset_name(self) -> str:
        item = self.dataset_list.currentItem() if hasattr(self, "dataset_list") else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""

    def on_dataset_selected(self) -> None:
        self.update_metric_options()
        self.populate_dataset_ranking()

    def update_metric_options(self) -> None:
        if not hasattr(self, "metric_combo"):
            return
        dataset_name = self.current_dataset_name()
        current_metric = self.metric_combo.currentData()
        rows = [
            row
            for row in getattr(self, "dataset_metric_rows", [])
            if row.dataset == dataset_name
        ]
        metrics = sorted({row.metric for row in rows if row.metric})
        self.metric_combo.blockSignals(True)
        self.metric_combo.clear()
        for metric in metrics:
            self.metric_combo.addItem(metric, metric)
        if current_metric:
            index = self.metric_combo.findData(current_metric)
            if index >= 0:
                self.metric_combo.setCurrentIndex(index)
        self.metric_combo.blockSignals(False)

    def populate_dataset_ranking(self) -> None:
        if not hasattr(self, "dataset_table"):
            return
        dataset_name = self.current_dataset_name()
        metric_name = self.metric_combo.currentData() if hasattr(self, "metric_combo") else ""
        self.dataset_ranking_title.setText(dataset_name or "选择一个数据集")
        rows = [
            row
            for row in getattr(self, "dataset_metric_rows", [])
            if row.dataset == dataset_name and (not metric_name or row.metric == metric_name)
        ]
        sort_mode = self.sort_combo.currentData() if hasattr(self, "sort_combo") else "auto"
        if sort_mode == "auto":
            reverse = not rows or rows[0].direction != "lower"
        else:
            reverse = sort_mode != "asc"
        numeric_rows = sorted(
            [row for row in rows if row.numeric_value is not None],
            key=lambda row: row.numeric_value,
            reverse=reverse,
        )
        rows = numeric_rows + [row for row in rows if row.numeric_value is None]

        self.dataset_table.setRowCount(0)
        for row_index, metric in enumerate(rows):
            self.dataset_table.insertRow(row_index)
            values = [
                str(row_index + 1),
                metric.model,
                (
                    f"{metric.value} {metric.unit}".strip()
                    if metric.unit and metric.unit not in metric.value
                    else metric.value
                ),
                metric.task,
                metric.paper_title,
                metric.notes,
            ]
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setData(Qt.ItemDataRole.UserRole, metric.entry_id)
                table_item.setData(Qt.ItemDataRole.UserRole + 1, metric.paper_id)
                if column in {1, 4}:
                    table_item.setToolTip(
                        "双击打开关联文献;未关联时双击进入编辑以关联"
                    )
                if column == 4 and metric.paper_id is None:
                    table_item.setText("未关联")
                    table_item.setForeground(
                        QBrush(QColor(theme.color("chart_muted")))
                    )
                self.dataset_table.setItem(row_index, column, table_item)
        self.dataset_table.resizeRowsToContents()
        self.statusBar().showMessage(f"{dataset_name} / {metric_name or '全部指标'}：{len(rows)} 条结果")

    def on_dataset_cell_clicked(self, row: int, column: int) -> None:
        # 用户反馈:跳转/编辑统一走双击,单击只提示不打断(误选行也会触发单击)
        if column not in {1, 4}:
            return
        item = self.dataset_table.item(row, 0)
        linked = item is not None and item.data(Qt.ItemDataRole.UserRole + 1) is not None
        self.statusBar().showMessage(
            "双击打开关联文献" if linked else "双击编辑此条目(可关联来源文献)", 3000
        )

    def add_ranking_entry(self) -> None:
        initial = {"dataset": self.current_dataset_name()}
        dialog = DatasetMetricDialog(self.repository.list_papers(), self, initial=initial)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.repository.add_dataset_metric_entry(dialog.update())
        except Exception as exc:
            QMessageBox.warning(self, "添加失败", f"无法添加排行榜条目：{exc}")
            return
        self.refresh_dataset_metrics()

    def edit_ranking_entry(self) -> None:
        row = self.dataset_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择条目", "请先选择一条排行榜记录。")
            return
        first_item = self.dataset_table.item(row, 0)
        if first_item is None:
            return
        entry_id = first_item.data(Qt.ItemDataRole.UserRole)
        if entry_id is None:
            QMessageBox.information(self, "无法编辑", "这个条目没有可编辑的记录 ID。")
            return
        initial = {
            "dataset": self.current_dataset_name(),
            "model": self._table_text(row, 1),
            "value": self._table_text(row, 2),
            "task": self._table_text(row, 3),
            "paper_id": first_item.data(Qt.ItemDataRole.UserRole + 1),
            "notes": self._table_text(row, 5),
        }
        source_metric = next(
            (
                metric
                for metric in getattr(self, "dataset_metric_rows", [])
                if metric.entry_id == int(entry_id)
            ),
            None,
        )
        if source_metric is not None:
            initial["value"] = source_metric.value
            initial["unit"] = source_metric.unit
            initial["direction"] = source_metric.direction
        metric = self.metric_combo.currentData() if hasattr(self, "metric_combo") else ""
        initial["metric"] = metric or ""
        dialog = DatasetMetricDialog(self.repository.list_papers(), self, initial=initial)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.repository.update_dataset_metric_entry(int(entry_id), dialog.update())
        except Exception as exc:
            QMessageBox.warning(self, "编辑失败", f"无法更新排行榜条目：{exc}")
            return
        self.refresh_dataset_metrics()

    def delete_ranking_entry(self) -> None:
        row = self.dataset_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择条目", "请先选择一条排行榜记录。")
            return
        first_item = self.dataset_table.item(row, 0)
        if first_item is None:
            return
        entry_id = first_item.data(Qt.ItemDataRole.UserRole)
        if entry_id is None:
            return
        if not confirm_action(
            self,
            title="删除排行榜条目？",
            message="当前数据集指标记录会从排行榜中移除。",
            detail="此操作不会删除关联的文献。",
            confirm_label="删除条目",
            destructive=True,
        ):
            return
        self.repository.delete_dataset_metric_entry(int(entry_id))
        self.refresh_dataset_metrics()

    def _table_text(self, row: int, column: int) -> str:
        item = self.dataset_table.item(row, column)
        return item.text() if item is not None else ""

    def selected_search_fields(self) -> set[str]:
        fields = {
            field
            for field, checkbox in self.search_checkboxes.items()
            if checkbox.isChecked()
        }
        if not fields:
            # B15:全部字段被取消时按"全字段"搜索,避免静默返回 0 结果
            fields = {
                "title",
                "authors",
                "abstract",
                "memory_anchor",
                "summary",
                "notes",
                "full_text",
                "metadata",
            }
        return fields

    def _hint_when_no_search_fields(self) -> None:
        if not self.selected_search_fields_or_none():
            self.statusBar().showMessage(
                "已取消全部搜索范围:当前按全字段搜索。可重新勾选以缩小范围。", 5000
            )

    def selected_search_fields_or_none(self) -> set[str]:
        return {
            field
            for field, checkbox in self.search_checkboxes.items()
            if checkbox.isChecked()
        }

    def schedule_search(self) -> None:
        self.search_timer.start()

    def on_category_selected(self) -> None:
        items = self.category_tree.selectedItems()
        if not items:
            return
        data = items[0].data(0, Qt.ItemDataRole.UserRole)
        if data == TRASH_CATEGORY_ID:
            self._viewing_trash = True
            self.current_category_id = None
            self.smart_filter = None
            self.current_saved_search_id = None
        elif isinstance(data, tuple) and len(data) == 2 and data[0] == "saved":
            self._viewing_trash = False
            self.current_category_id = None
            self.current_tag_id = None
            self.smart_filter = None
            self.current_saved_search_id = int(data[1])
        elif isinstance(data, tuple) and len(data) == 2 and data[0] == "smart":
            self._viewing_trash = False
            self.current_category_id = None
            self.current_tag_id = None
            self.current_saved_search_id = None
            self.smart_filter = data
        elif data == SMART_GROUP_NONE or data == SAVED_SEARCH_NONE:
            # 点父节点=清除对应筛选
            self._viewing_trash = False
            self.smart_filter = None
            self.current_saved_search_id = None
        else:
            self._viewing_trash = False
            self.smart_filter = None
            self.current_saved_search_id = None
            self.current_category_id = data
        self.refresh_papers()

    def on_paper_selected(self) -> None:
        if self._updating_paper_list or self._restoring_paper_selection:
            return
        item = self.paper_list.currentItem()
        if item is None:
            return
        # F28:多选(Ctrl/Shift)时提示可批量操作;详情仍跟随当前项
        # (最后点击的文献),守卫与加载链保持原有行为
        selected_count = len(self.paper_list.selectedItems())
        if selected_count > 1:
            self.statusBar().showMessage(
                f"已选 {selected_count} 篇文献——可使用「批量」菜单批量操作。", 5000
            )
        paper_id = item.data(Qt.ItemDataRole.UserRole)
        if paper_id is None:
            return
        paper_id = int(paper_id)
        if paper_id == self.current_paper_id:
            self.sync_card_selection()
            return
        previous_paper_id = self.current_paper_id
        if not self._confirm_pending_changes():
            self._restore_paper_selection(previous_paper_id)
            return
        self.sync_card_selection()
        self.load_paper(paper_id)

    def _restore_paper_selection(self, paper_id: int | None) -> None:
        self._restoring_paper_selection = True
        try:
            if paper_id is None:
                self.paper_list.clearSelection()
                return
            for row in range(self.paper_list.count()):
                item = self.paper_list.item(row)
                if item and item.data(Qt.ItemDataRole.UserRole) == paper_id:
                    self.paper_list.setCurrentItem(item)
                    return
            self.paper_list.clearSelection()
        finally:
            self._restoring_paper_selection = False
            self.sync_card_selection()

    def load_paper(self, paper_id: int) -> None:
        paper = self.repository.get_paper(paper_id, include_text_content=False)
        # F19:建议条只对当前文献有效——切走后残留的旧建议点「应用」会
        # 把分类写到之前那篇文献上(用户看到的就是"没分进去")
        if self._pending_suggestion is not None and self._pending_suggestion[0] != paper_id:
            self._clear_suggestion()
        with self._suspend_dirty_tracking():
            self.current_paper_id = paper.id
            self.detail_hint.setText(paper.reading_status or "待读")

            self._set_field_text(self.title_edit, paper.title)
            self._set_field_text(self.authors_edit, paper.authors)
            self.year_edit.setValue(paper.year or 0)
            self._set_field_text(self.venue_edit, paper.venue)
            self.doi_edit.setText(paper.doi)
            self.arxiv_edit.setText(paper.arxiv_id)
            self._set_field_text(self.url_edit, paper.url)
            self._set_field_text(self.pdf_path_edit, paper.pdf_path)
            # F19:主分类 = 第一个正式分类(多分类遗留数据由整理向导收敛)
            formal = [
                category
                for category in paper.categories
                if category.name != UNCATEGORIZED_CATEGORY_NAME
            ]
            self.selected_category_ids = [formal[0].id] if formal else []
            category_id = self.selected_category_ids[0] if self.selected_category_ids else None
            self._suppress_category_signal = True
            category_index = self.category_combo.findData(category_id)
            self.category_combo.setCurrentIndex(max(0, category_index))
            self._suppress_category_signal = False
            self.status_combo.setCurrentText(paper.reading_status)
            # F21:阅读页状态下拉与当前文献同步(不发信号)
            self.reader_status_combo.blockSignals(True)
            self.reader_status_combo.setCurrentText(paper.reading_status or "待读")
            self.reader_status_combo.blockSignals(False)
            self.importance_edit.setValue(paper.importance)
            self.abstract_language = "en"
            self.abstract_source_text = paper.abstract
            self.abstract_edit.setReadOnly(False)
            self.abstract_lang_button.setText("中/英")
            self.abstract_edit.setPlainText(paper.abstract)
            self.pdf_preview.set_pdf_path(
                str(self.repository.resolve_pdf_path(paper.pdf_path))
                if paper.pdf_path
                else ""
            )
            self.load_reader_documents(paper)

            detail = paper.detail
            self.summary_edit.setPlainText(detail.summary if detail else "")
            self.datasets_edit.setPlainText(detail.datasets if detail else "")
            self.dataset_metrics_edit.setPlainText(detail.dataset_metrics if detail else "")
            self.set_dataset_metrics_table_text(detail.dataset_metrics if detail else "")
            self.methods_edit.setPlainText(detail.methods if detail else "")
            self.contributions_edit.setPlainText(detail.contributions if detail else "")
            self.limitations_edit.setPlainText(detail.limitations if detail else "")
            self.memory_anchor_edit.setPlainText(detail.memory_anchor if detail else "")
            self.experiment_notes_edit.setPlainText(detail.experiment_notes if detail else "")
            self.reproducibility_notes_edit.setPlainText(
                detail.reproducibility_notes if detail else ""
            )
            self.personal_notes_edit.setPlainText(detail.personal_notes if detail else "")
            self.reader_notes_edit.setPlainText(detail.personal_notes if detail else "")
            self.inspector_title_label.setText(paper.title or "未命名文献")
            meta_parts = [
                paper.authors or "未知作者",
                str(paper.year) if paper.year else "年份未知",
                paper.venue or "来源未填写",
            ]
            meta_text = " · ".join(meta_parts)
            self.inspector_meta_label.setText(meta_text)
            self.inspector_memory_label.setText(
                format_memory_anchor(detail.memory_anchor if detail else "")
                or "尚未填写记忆锚点。可进入编辑页手动记录，或使用 AI 解析。"
            )
            self.inspector_abstract_label.setText(
                self._brief_text(paper.abstract, 460) or "尚未填写摘要。"
            )
            self.inspector_categories_label.setText(
                "  ·  ".join(category.name for category in paper.categories)
                or "未分类"
            )
            self.reader_title_label.setText(paper.title or "未命名文献")
            self.reader_meta_label.setText(
                f"{paper.year or '年份未知'} · {paper.reading_status or '待读'}"
            )
            self.reader_memory_label.setPlainText(
                format_memory_anchor(detail.memory_anchor if detail else "")
                or "尚未填写记忆锚点。"
            )
            self.reader_summary_label.setPlainText(
                detail.summary if detail and detail.summary else "尚未生成全文总结。"
            )
            self.update_pdf_action_state()
            self.update_detail_section_visibility()
        self._set_dirty(False)
        self.refresh_tag_chips()
        # F13:相关文献推荐随当前文献刷新(排除保存/删除造成的过期推荐)
        self._refresh_related_papers(paper_id)

    def clear_detail(self) -> None:
        with self._suspend_dirty_tracking():
            self.detail_hint.setText("选择一篇文献开始编辑")
            self.pdf_preview.set_pdf_path("")
            self.reader_pdf_preview.set_pdf_path("")
            self.inspector_title_label.setText("尚未选择文献")
            self.inspector_meta_label.clear()
            self.inspector_memory_label.setText("这里会显示最值得记住的一句话。")
            self.inspector_abstract_label.setText("选择文献后显示摘要。")
            self.inspector_categories_label.clear()
            self.related_papers_list.clear()
            self.related_papers_list.hide()
            self.reader_title_label.setText("沉浸阅读")
            self.reader_meta_label.clear()
            self.reader_memory_label.setPlainText("选择文献后显示。")
            self.reader_summary_label.setText("尚未生成总结。")
            self.reader_notes_edit.clear()
            for widget in [
                self.title_edit,
                self.authors_edit,
                self.venue_edit,
                self.doi_edit,
                self.arxiv_edit,
                self.url_edit,
                self.pdf_path_edit,
            ]:
                widget.clear()
            self.year_edit.setValue(0)
            self.selected_category_ids = []
            self.category_combo.setCurrentIndex(0)
            self.status_combo.setCurrentIndex(0)
            self.importance_edit.setValue(3)
            for editor in [
                self.abstract_edit,
                self.summary_edit,
                self.datasets_edit,
                self.dataset_metrics_edit,
                self.methods_edit,
                self.contributions_edit,
                self.limitations_edit,
                self.memory_anchor_edit,
                self.experiment_notes_edit,
                self.reproducibility_notes_edit,
                self.personal_notes_edit,
            ]:
                editor.clear()
            self.dataset_metrics_table.setRowCount(0)
            self.abstract_language = "en"
            self.abstract_source_text = ""
            self.abstract_edit.setReadOnly(False)
            if hasattr(self, "abstract_lang_button"):
                self.abstract_lang_button.setText("中/英")
            self.update_pdf_action_state()
            self.update_detail_section_visibility()
        self._set_dirty(False)

    def current_abstract_for_save(self) -> str:
        if self.abstract_language == "zh":
            return self.abstract_source_text
        return self.abstract_edit.toPlainText()

    def toggle_abstract_language(self) -> None:
        if self.abstract_language == "zh":
            self.abstract_language = "en"
            self.abstract_edit.setReadOnly(False)
            with self._suspend_dirty_tracking():
                self.abstract_edit.setPlainText(self.abstract_source_text)
            self.abstract_lang_button.setText("中/英")
            return
        source = self.abstract_edit.toPlainText().strip()
        if not source:
            QMessageBox.information(self, "没有摘要", "当前文献还没有可翻译的摘要。")
            return
        self.abstract_source_text = source
        cached = self.repository.get_abstract_translation(self.current_paper_id) if self.current_paper_id else ""
        if cached:
            self.show_abstract_translation(cached)
            return
        if not self.ensure_api_ready():
            return
        self.start_abstract_translation(source)

    def show_abstract_translation(self, translation: str) -> None:
        self.abstract_language = "zh"
        with self._suspend_dirty_tracking():
            self.abstract_edit.setPlainText(translation)
        self.abstract_edit.setReadOnly(True)
        self.abstract_lang_button.setText("原文")

    def start_abstract_translation(
        self,
        abstract: str,
        *,
        paper_id: int | None = None,
        confirm: bool = True,
    ) -> None:
        if paper_id is None:
            paper_id = self.current_paper_id
        if paper_id is None:
            return
        if self.abstract_translate_thread is not None:
            # F16:摘要翻译运行中,新任务排队顺序执行
            self._enqueue_task(
                "摘要翻译",
                "",
                lambda: self.start_abstract_translation(
                    abstract, paper_id=paper_id, confirm=False
                ),
            )
            return
        if confirm and not confirm_external_request(
            self.settings_path,
            operation="翻译论文摘要",
            content_summary=f"论文标题和摘要，共约 {len(abstract):,} 个字符",
            output_summary="中文摘要将保存在本地文献中",
            parent=self,
        ):
            return
        self.abstract_lang_button.setText("翻译中")
        self.abstract_lang_button.setEnabled(False)
        self.statusBar().showMessage("正在翻译摘要...")
        # B35:逐任务绑定身份,线程清理只清仍指向本任务的槽位
        thread = QThread(self)
        worker = AbstractTranslateWorker(
            settings_path=self.settings_path,
            paper_id=paper_id,
            title=self._field_text(self.title_edit),
            abstract=abstract,
            usage_recorder=lambda usage, pid=paper_id: self.repository.record_token_usage(
                feature="摘要翻译", paper_id=pid, source="app", **usage
            ),
        )
        task_id = self.task_manager.add(
            "摘要翻译",
            detail=self._brief_text(self._field_text(self.title_edit), 60),
            cancel=lambda w=worker: self._cancel_abstract_translation_worker(w),
            retry=lambda p=paper_id, text=abstract: self.retry_abstract_translation(p, text),
        )
        worker.task_id = task_id
        self.abstract_translate_thread = thread
        self.abstract_translate_worker = worker
        self.abstract_translate_task_id = task_id
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # B23:流式进度直接反映在按钮文字上(此任务无加载对话框)
        worker.progress.connect(
            lambda text: (
                self.abstract_lang_button.setText(f"翻译中·{text.removeprefix('已接收 ')}")
                if self.abstract_lang_button is not None
                else None
            )
        )
        worker.finished.connect(self.on_abstract_translation_finished)
        worker.failed.connect(self.on_abstract_translation_failed)
        worker.cancelled.connect(self.on_abstract_translation_cancelled)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self.clear_abstract_translate_thread)
        thread.start()

    def on_abstract_translation_finished(self, paper_id: int, translation: str) -> None:
        self.task_manager.succeed(self._sender_task_id(), "中文摘要已保存")
        self.abstract_lang_button.setEnabled(True)
        if translation.strip():
            self.repository.set_abstract_translation(paper_id, translation)
        if self.current_paper_id == paper_id:
            self.show_abstract_translation(translation)
        self.statusBar().showMessage("摘要翻译完成。", 4000)

    def on_abstract_translation_failed(self, message: str) -> None:
        self.task_manager.fail(self._sender_task_id(), message)
        self.abstract_lang_button.setEnabled(True)
        self.abstract_lang_button.setText("中/英")
        self.statusBar().showMessage("摘要翻译失败", 5000)
        if self.task_manager.pending_count() == 0:
            QMessageBox.warning(self, "摘要翻译失败", message)

    def on_abstract_translation_cancelled(self) -> None:
        self.task_manager.cancelled(self._sender_task_id())
        self.abstract_lang_button.setEnabled(True)
        self.abstract_lang_button.setText("中/英")
        self.statusBar().showMessage("摘要翻译已取消", 4000)

    def _cancel_abstract_translation_worker(self, worker: AbstractTranslateWorker) -> None:
        worker.cancel()

    def retry_abstract_translation(self, paper_id: int, abstract: str) -> None:
        self.focus_paper(paper_id)
        self.start_abstract_translation(abstract, paper_id=paper_id, confirm=False)

    def clear_abstract_translate_thread(self) -> None:
        # B35:作用域清理,同 clear_translate_thread
        if self.abstract_translate_thread is self.sender():
            self.abstract_translate_thread = None
            self.abstract_translate_worker = None
            self.abstract_translate_task_id = None

    def update_pdf_action_state(self) -> None:
        if not hasattr(self, "open_pdf_button"):
            return
        pdf_path = self.current_pdf_path(show_message=False)
        has_local_pdf = pdf_path is not None
        if hasattr(self, "home_translate_button"):
            self.home_translate_button.setEnabled(has_local_pdf)
        if has_local_pdf:
            self.open_pdf_button.setText("打开原文")
            self.open_pdf_button.setToolTip("用系统默认 PDF 阅读器打开当前文献的 PDF 原件。")
            self.open_pdf_button.setEnabled(True)
            return
        if self.current_paper_id is not None and self.current_arxiv_pdf_url():
            self.open_pdf_button.setText("获取 PDF")
            self.open_pdf_button.setToolTip("当前文献还没有本地 PDF，点击后从 arXiv 下载并按论文标题归档。")
            self.open_pdf_button.setEnabled(True)
            return
        self.open_pdf_button.setText("打开原文")
        self.open_pdf_button.setToolTip("当前文献没有可打开或可下载的 PDF。")
        self.open_pdf_button.setEnabled(self.current_paper_id is not None)

    def current_arxiv_pdf_url(self) -> str:
        text = " ".join(
            [
                self.arxiv_edit.text().strip(),
                self._field_text(self.url_edit),
                self._field_text(self.pdf_path_edit),
            ]
        )
        match = re.search(
            r"arxiv\.org/(?:abs|pdf)/([^/?#\s]+)",
            text,
            flags=re.IGNORECASE,
        )
        arxiv_id = match.group(1) if match else self.arxiv_edit.text().strip()
        if not arxiv_id:
            return ""
        arxiv_id = arxiv_id.removeprefix("arXiv:").removeprefix("arxiv:").strip().removesuffix(".pdf")
        if not re.fullmatch(r"[0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-zA-Z.-]+/[0-9]{7}(?:v\d+)?", arxiv_id):
            return ""
        return f"https://arxiv.org/pdf/{arxiv_id}"

    def start_arxiv_pdf_download(self) -> None:
        if self.current_paper_id is None:
            QMessageBox.information(self, "没有文献", "请先选择一篇文献。")
            return
        paper_id = self.current_paper_id
        pdf_url = self.current_arxiv_pdf_url()
        if not pdf_url:
            QMessageBox.information(self, "无法下载 PDF", "当前文献没有有效的 arXiv ID 或 arXiv 链接。")
            return
        if self.arxiv_pdf_thread is not None:
            # F16:下载运行中,新下载任务排队顺序执行
            self._enqueue_task(
                "下载 arXiv PDF",
                self._brief_text(self._field_text(self.title_edit), 60),
                lambda: self._start_arxiv_download_for(paper_id, pdf_url),
            )
            return
        self._start_arxiv_download_for(paper_id, pdf_url)

    def _start_arxiv_download_for(self, paper_id: int, pdf_url: str) -> None:
        title = self._field_text(self.title_edit).strip() or self.arxiv_edit.text().strip() or "arxiv-paper"
        self.statusBar().showMessage("正在从 arXiv 下载 PDF...")
        self.open_pdf_button.setText("下载中...")
        self.open_pdf_button.setEnabled(False)
        # B35:逐任务绑定身份,线程清理只清仍指向本任务的槽位
        thread = QThread(self)
        worker = ArxivPDFDownloadWorker(
            paper_id=paper_id,
            pdf_url=pdf_url,
            title=title,
            arxiv_id=self.arxiv_edit.text().strip(),
            output_dir=self.data_dir / "papers" / "arxiv",
        )
        task_id = self.task_manager.add(
            "下载 arXiv PDF",
            detail=title,
            cancel=lambda w=worker: self._cancel_arxiv_pdf_download_worker(w),
        )
        worker.task_id = task_id
        self.arxiv_pdf_thread = thread
        self.arxiv_pdf_worker = worker
        self.arxiv_pdf_task_id = task_id
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self.on_arxiv_pdf_download_finished)
        worker.failed.connect(self.on_arxiv_pdf_download_failed)
        worker.cancelled.connect(self.on_arxiv_pdf_download_cancelled)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self.clear_arxiv_pdf_thread)
        thread.start()

    def on_arxiv_pdf_download_finished(self, paper_id: int, path: Path) -> None:
        try:
            self.repository.update_paper_pdf_path(paper_id, str(path))
        except Exception as exc:
            self.task_manager.fail(
                self._sender_task_id(),
                f"PDF 已下载，但无法写回文献：{exc}",
            )
            QMessageBox.warning(self, "保存 PDF 路径失败", f"PDF 已下载，但无法写回文献：{exc}")
            self.update_pdf_action_state()
            return
        self.task_manager.succeed(self._sender_task_id(), f"PDF 已保存：{path}")
        if self.current_paper_id == paper_id:
            with self._suspend_dirty_tracking():
                self._set_field_text(self.pdf_path_edit, str(path))
            self.pdf_preview.set_pdf_path(str(path))
            self.update_pdf_action_state()
            self._set_dirty(False)
        self.refresh_papers()
        self.select_paper(paper_id)
        self.statusBar().showMessage(f"PDF 已下载并归档：{path.name}", 5000)

    def on_arxiv_pdf_download_failed(self, message: str) -> None:
        self.task_manager.fail(self._sender_task_id(), message)
        self.statusBar().showMessage("arXiv PDF 下载失败", 5000)
        self.update_pdf_action_state()
        if self.task_manager.pending_count() == 0:
            QMessageBox.warning(self, "PDF 下载失败", message)

    def on_arxiv_pdf_download_cancelled(self) -> None:
        self.task_manager.cancelled(self._sender_task_id())
        self.statusBar().showMessage("arXiv PDF 下载已取消", 4000)
        self.update_pdf_action_state()

    def _cancel_arxiv_pdf_download_worker(self, worker: ArxivPDFDownloadWorker) -> None:
        worker.cancel()

    def clear_arxiv_pdf_thread(self) -> None:
        # B35:作用域清理,同 clear_translate_thread
        if self.arxiv_pdf_thread is self.sender():
            self.arxiv_pdf_thread = None
            self.arxiv_pdf_worker = None
            self.arxiv_pdf_task_id = None

    def selected_category_for_new_paper(self) -> int | None:
        return self.current_category_id

    def selected_category_for_save(self) -> int | None:
        if self.selected_category_ids:
            return self.selected_category_ids[0]
        data = self.category_combo.currentData()
        return int(data) if data is not None else None

    def selected_categories_for_save(self) -> list[int]:
        return list(self.selected_category_ids)


    def add_category(self, parent_id: int | None = None) -> None:
        if parent_id is None:
            parent_id = self.current_category_id
        name, accepted = QInputDialog.getText(self, "添加分类", "分类名称")
        if not accepted or not name.strip():
            return
        try:
            self.repository.add_category(name=name, parent_id=parent_id)
        except Exception as exc:
            QMessageBox.warning(self, "添加失败", f"无法添加分类：{exc}")
            return
        self.refresh_categories()

    def rename_category(self, category_id: int) -> None:
        category = self.category_lookup.get(category_id)
        if category is None:
            return
        name, accepted = QInputDialog.getText(self, "重命名分类", "分类名称", text=category.name)
        if not accepted or not name.strip():
            return
        try:
            self.repository.rename_category(category_id, name)
        except Exception as exc:
            QMessageBox.warning(self, "重命名失败", f"无法重命名分类：{exc}")
            return
        self.refresh_categories()

    def delete_category(self, category_id: int) -> None:
        category = self.category_lookup.get(category_id)
        if category is None:
            return
        if not confirm_action(
            self,
            title=f"删除“{category.name}”？",
            message="这个分类及其子分类会从分类树中移除。",
            detail="文献本身不会被删除，但会失去这些分类关联。",
            confirm_label="删除分类",
            destructive=True,
        ):
            return
        try:
            self.repository.delete_category(category_id)
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", f"无法删除分类：{exc}")
            return
        self.current_category_id = None
        self.refresh_categories()
        self.refresh_papers()

    def move_category(self, category_id: int, direction: int) -> None:
        self.repository.move_category(category_id, direction)
        self.refresh_categories()

    def add_pdf(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择论文 PDF",
            str(self.data_dir),
            "PDF 文件 (*.pdf)",
        )
        if not file_path:
            return
        if not self._confirm_pending_changes():
            return
        self.preview_and_import_pdf(Path(file_path), analyze_after_import=False)

    def open_api_settings(self) -> bool:
        # U10:恢复上次所在步骤;首次打开(无记忆)默认第 1 步「读取论文」,
        # 新用户仍直达 API 服务/Key/模型配置页(U1 语义保留)
        dialog = LLMSettingsDialog(self.settings_path, self)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def ensure_api_ready(self) -> bool:
        settings = LLMSettings.load(self.settings_path)
        if (
            settings.base_url
            and settings.model
            and get_llm_api_key(settings.base_url)
        ):
            return True
        QMessageBox.information(self, "需要 API 设置", "请先填写 Base URL、模型名和 API Key。")
        return self.open_api_settings()

    def ai_import_pdf(self) -> None:
        if not self.ensure_api_ready():
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择要 AI 解析的论文 PDF",
            str(self.data_dir.parent),
            "PDF 文件 (*.pdf)",
        )
        if not file_path:
            return
        if not self._confirm_pending_changes():
            return
        self.preview_and_import_pdf(Path(file_path), analyze_after_import=True)

    def ai_import_url(self) -> None:
        if not self.ensure_api_ready():
            return
        source_url, accepted = QInputDialog.getText(
            self,
            "AI 链接导入",
            "输入论文页面链接或在线 PDF 链接：",
            QLineEdit.EchoMode.Normal,
        )
        if not accepted or not source_url.strip():
            return
        if not self._confirm_pending_changes():
            return
        self.start_url_import(source_url.strip())

    def start_url_import(self, source_url: str) -> None:
        if self.url_import_thread is not None:
            QMessageBox.information(self, "正在导入", "已有一个论文链接导入任务正在运行。")
            return
        self.statusBar().showMessage("正在识别链接并查找论文 PDF...")
        # B35:逐任务绑定身份,线程清理只清仍指向本任务的槽位
        thread = QThread(self)
        worker = URLPDFImportWorker(
            source_url,
            self.data_dir / ".url-imports",
        )
        task_id = self.task_manager.add(
            "AI 链接导入",
            detail=source_url,
            cancel=lambda w=worker: self._cancel_url_import_worker(w),
            retry=lambda url=source_url: self.start_url_import(url),
        )
        worker.task_id = task_id
        self.url_import_thread = thread
        self.url_import_worker = worker
        self.url_import_task_id = task_id
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self.on_url_import_finished)
        worker.failed.connect(self.on_url_import_failed)
        worker.cancelled.connect(self.on_url_import_cancelled)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self.clear_url_import_thread)
        thread.start()

    def on_url_import_finished(self, result: ResolvedPaperPDF) -> None:
        self.task_manager.succeed(
            self._sender_task_id(),
            f"已找到 PDF：{result.pdf_url}",
        )
        self.statusBar().showMessage("已找到 PDF，请确认论文信息", 4000)
        try:
            self.preview_and_import_pdf(result.path, analyze_after_import=True)
        finally:
            result.path.unlink(missing_ok=True)
            try:
                result.path.parent.rmdir()
            except OSError:
                pass

    def on_url_import_failed(self, message: str) -> None:
        self.task_manager.fail(self._sender_task_id(), message)
        self.statusBar().showMessage("论文链接导入失败", 5000)
        if self.task_manager.pending_count() == 0:
            QMessageBox.warning(self, "链接导入失败", message)

    def on_url_import_cancelled(self) -> None:
        self.task_manager.cancelled(self._sender_task_id())
        self.statusBar().showMessage("论文链接导入已取消", 4000)

    def _cancel_url_import_worker(self, worker: URLPDFImportWorker) -> None:
        worker.cancel()

    def clear_url_import_thread(self) -> None:
        # B35:作用域清理,同 clear_translate_thread
        if self.url_import_thread is self.sender():
            self.url_import_thread = None
            self.url_import_worker = None
            self.url_import_task_id = None

    def preview_and_import_pdf(
        self,
        pdf_path: Path,
        *,
        analyze_after_import: bool,
    ) -> int | None:
        try:
            preview = self.repository.preview_pdf_import(
                pdf_path,
                category_id=None,
                library_paper_dir=self.data_dir / "papers",
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取失败", f"无法读取 PDF 导入信息：{exc}")
            return None

        dialog = PDFImportPreviewDialog(
            preview,
            self.repository.list_categories(),
            selected_category_id=self.selected_category_for_new_paper(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        if dialog.action == PDFImportPreviewDialog.ACTION_OPEN_EXISTING:
            if preview.duplicate_paper_id is not None:
                self.focus_paper(preview.duplicate_paper_id)
                self.statusBar().showMessage("已打开库内已有文献", 4000)
                return preview.duplicate_paper_id
            return None

        category_id = dialog.selected_category_id()
        metadata = dialog.metadata()
        try:
            if dialog.action == PDFImportPreviewDialog.ACTION_MERGE:
                if preview.duplicate_paper_id is None:
                    return None
                paper = self.repository.merge_pdf_import(
                    pdf_path,
                    preview.duplicate_paper_id,
                    metadata,
                    category_id=category_id,
                    library_paper_dir=self.data_dir / "papers",
                )
                self.focus_paper(paper.id)
                self.statusBar().showMessage("已合并缺失信息，原有笔记和 PDF 未被覆盖", 5000)
                return paper.id

            result = self.repository.import_pdf(
                pdf_path,
                category_id=category_id,
                library_paper_dir=self.data_dir / "papers",
                metadata=metadata,
            )
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", f"无法导入 PDF：{exc}")
            return None

        paper = result.paper
        self.focus_paper(paper.id)
        if not result.created:
            QMessageBox.information(
                self,
                "检测到重复文献",
                f"{result.duplicate_reason}，未重复导入。\n\n"
                f"库内已有：《{paper.title}》",
            )
            return paper.id
        self.statusBar().showMessage(f"已导入：{paper.title}", 5000)
        if analyze_after_import:
            self.start_ai_analysis(paper.id)
        return paper.id

    def enrich_metadata_from_public_api(self) -> None:
        """F6:按 DOI/arXiv 联网补全元数据(只填空缺字段)。"""
        from synthrecall.metadata_enrichment import fetch_metadata

        if self.current_paper_id is None:
            QMessageBox.information(self, "未选择文献", "请先选择一篇文献。")
            return
        doi = self.doi_edit.text().strip()
        arxiv_id = self.arxiv_edit.text().strip()
        if not doi and not arxiv_id:
            QMessageBox.information(
                self,
                "缺少标识符",
                "请先填写 DOI 或 arXiv ID(任一即可),再点击补全。",
            )
            return
        self.enrich_metadata_button.setEnabled(False)
        self.enrich_metadata_button.setText("查询中…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        def run():
            try:
                metadata = fetch_metadata(doi=doi, arxiv_id=arxiv_id)
            except (RuntimeError, ValueError) as exc:
                message = str(exc)

                def on_error():
                    self.enrich_metadata_button.setEnabled(True)
                    self.enrich_metadata_button.setText("按 DOI / arXiv 补全")
                    QApplication.restoreOverrideCursor()
                    QMessageBox.warning(self, "补全失败", message)

                run_on_ui_thread(on_error)
                return

            def on_done():
                self.enrich_metadata_button.setEnabled(True)
                self.enrich_metadata_button.setText("按 DOI / arXiv 补全")
                QApplication.restoreOverrideCursor()
                self._apply_enriched_metadata(metadata)

            run_on_ui_thread(on_done)

        import threading

        threading.Thread(target=run, daemon=True).start()

    def _apply_enriched_metadata(self, metadata) -> None:

        updates: list[str] = []
        if not self.title_edit.toPlainText().strip() and metadata.title:
            self._set_field_text(self.title_edit, metadata.title)
            updates.append("标题")
        if not self.authors_edit.toPlainText().strip() and metadata.authors:
            self._set_field_text(self.authors_edit, metadata.authors)
            updates.append("作者")
        if self.year_edit.value() == 0 and metadata.year:
            self.year_edit.setValue(metadata.year)
            updates.append("年份")
        if not self.venue_edit.toPlainText().strip() and metadata.venue:
            self._set_field_text(self.venue_edit, metadata.venue)
            updates.append("来源")
        if not self.abstract_edit.toPlainText().strip() and metadata.abstract:
            self._set_field_text(self.abstract_edit, metadata.abstract)
            updates.append("摘要")
        if not self.doi_edit.text().strip() and metadata.doi:
            self.doi_edit.setText(metadata.doi)
            updates.append("DOI")
        if not self.arxiv_edit.text().strip() and metadata.arxiv_id:
            self.arxiv_edit.setText(metadata.arxiv_id)
            updates.append("arXiv ID")
        if not self.url_edit.toPlainText().strip() and metadata.url:
            self._set_field_text(self.url_edit, metadata.url)
            updates.append("链接")
        if updates:
            self.statusBar().showMessage(f"已补全:{'、'.join(updates)}(未保存,确认后 Ctrl+S)", 6000)
        else:
            self.statusBar().showMessage("官方元数据没有可补充的空缺字段", 4000)

    def focus_paper(self, paper_id: int) -> None:
        self.current_category_id = None
        self.current_paper_id = paper_id
        self.refresh_categories()
        self.refresh_papers()
        self.select_paper(paper_id)
        self.load_paper(paper_id)
        self.show_library_page()

    def open_paper_chat(self) -> None:
        if self.current_paper_id is None:
            QMessageBox.information(self, "没有文献", "请先选择一篇文献。")
            return
        if not self.ensure_api_ready():
            return
        if not self.save_current_paper(refresh=False):
            return
        if self.paper_chat_dialog is not None and self.paper_chat_dialog.isVisible():
            self.paper_chat_dialog.raise_()
            self.paper_chat_dialog.activateWindow()
            return
        self.paper_chat_dialog = PaperChatDialog(
            self.repository,
            self.settings_path,
            self.current_paper_id,
            self,
        )
        self.paper_chat_dialog.finished.connect(
            lambda _result: setattr(self, "paper_chat_dialog", None)
        )
        self.paper_chat_dialog.show()

    def ai_analyze_current(self) -> None:
        if self.current_paper_id is None:
            QMessageBox.information(self, "没有文献", "请先选择或导入一篇文献。")
            return
        if not self.ensure_api_ready():
            return
        if not self.save_current_paper():
            return
        if self.current_paper_id is not None:
            self.start_ai_analysis(self.current_paper_id)

    def start_ai_analysis(self, paper_id: int, *, confirm: bool = True) -> None:
        if self.ai_thread is not None:
            paper = self.repository.get_paper(paper_id, include_text_content=False)
            # F16:解析运行中,新解析任务排队顺序执行
            self._enqueue_task(
                "AI 论文解析",
                self._brief_text(paper.title if paper else "", 60),
                lambda: self.start_ai_analysis(paper_id, confirm=False),
            )
            return
        settings = LLMSettings.load(self.settings_path)
        source = self.repository.get_ai_source(paper_id, max_chars=settings.max_chars)
        sent_chars = sum(len(value or "") for value in source.values())
        if not confirm_external_request(
            self.settings_path,
            operation="AI 解析整篇论文",
            content_summary=f"标题、作者、摘要、笔记和正文，最多约 {sent_chars:,} 个字符",
            output_summary="解析结果将写入当前文献的总结、方法、贡献和批注字段",
            parent=self,
        ):
            self.statusBar().showMessage("已取消 AI 解析", 3000)
            return
        self.statusBar().showMessage("AI 正在解析论文，请稍候...")
        self.loading_dialog = LoadingDialog(self)
        self.loading_dialog.cancelRequested.connect(self.request_cancel_ai_analysis)
        self.loading_dialog.show()
        # B35:逐任务绑定身份,线程清理只清仍指向本任务的槽位
        thread = QThread(self)
        worker = AIAnalyzeWorker(
            self.repository,
            self.settings_path,
            paper_id,
            usage_recorder=lambda usage, pid=paper_id: self.repository.record_token_usage(
                feature="AI 解析", paper_id=pid, source="app", **usage
            ),
        )
        task_id = self.task_manager.add(
            "AI 论文解析",
            detail=self._brief_text(source.get("title", ""), 60),
            cancel=lambda w=worker: self._cancel_ai_analysis_worker(w),
            retry=lambda p=paper_id: self.start_ai_analysis(p),
        )
        worker.task_id = task_id
        self.ai_thread = thread
        self.ai_worker = worker
        self.ai_task_id = task_id
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(
            lambda text: self.loading_dialog.set_progress(text)
            if self.loading_dialog is not None
            else None
        )
        worker.finished.connect(self.on_ai_analysis_finished)
        worker.finished.connect(
            lambda _pid, _analysis: (
                self.loading_dialog.set_progress("")
                if self.loading_dialog is not None
                else None
            )
        )
        worker.failed.connect(self.on_ai_analysis_failed)
        worker.cancelled.connect(self.on_ai_analysis_cancelled)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self.clear_ai_thread)
        thread.start()

    def on_ai_analysis_finished(self, paper_id: int, analysis) -> None:
        self.task_manager.succeed(self._sender_task_id(), "解析结果已写入文献")
        self.close_loading_dialog()
        self.refresh_papers()
        self.select_paper(paper_id)
        self.load_paper(paper_id)
        self.refresh_dataset_metrics()
        self.auto_classify_library(show_message=False)
        # F22:AI 解析顺手把结构化实验数据落进数据集排行榜(先原文核对再入库)
        metric_note = ""
        rows = analysis.experiment_metrics
        if rows:
            source = self.repository.get_ai_source(paper_id, max_chars=2_000_000)
            added, verified = self.repository.apply_ai_experiment_metrics(
                paper_id, rows, source.get("text") or ""
            )
            if added:
                metric_note = f"，新增实验数据 {added} 条（已核对 {verified} 条）"
                self.refresh_dataset_metrics()
        # F19:解析产物在手,顺手入队 AI 主分类建议(任务队列顺序执行)
        self._enqueue_task(
            "AI 分类建议",
            detail="",
            starter=lambda pid=paper_id: self.start_category_suggest(pid),
        )
        if self.task_manager.pending_count() > 0:
            # B35:批量解析时不再逐个弹完成模态框
            self.statusBar().showMessage(
                f"AI 解析完成{metric_note}（队列中还有 "
                f"{self.task_manager.pending_count()} 个任务）",
                5000,
            )
        else:
            self.statusBar().showMessage(
                f"AI 解析完成，已生成文献信息。{metric_note}", 5000
            )
            QMessageBox.information(self, "AI 解析完成", "已将大模型解析结果写入当前文献。")

    def on_ai_analysis_failed(self, message: str) -> None:
        self.task_manager.fail(self._sender_task_id(), message)
        self.close_loading_dialog()
        if self.task_manager.pending_count() > 0:
            self.statusBar().showMessage(
                f"AI 解析失败（队列中还有 {self.task_manager.pending_count()} 个任务继续）",
                5000,
            )
        else:
            self.statusBar().showMessage("AI 解析失败", 5000)
            QMessageBox.warning(self, "AI 解析失败", message)

    def request_cancel_ai_analysis(self) -> None:
        if self.task_manager.request_cancel(self.ai_task_id) and self.loading_dialog is not None:
            self.loading_dialog.set_cancelling()

    def _cancel_ai_analysis_worker(self, worker: AIAnalyzeWorker) -> None:
        worker.cancel()

    def on_ai_analysis_cancelled(self) -> None:
        self.task_manager.cancelled(self._sender_task_id())
        self.close_loading_dialog()
        self.statusBar().showMessage("AI 解析已取消", 4000)

    def clear_ai_thread(self) -> None:
        # B35:作用域清理,同 clear_translate_thread
        if self.ai_thread is self.sender():
            self.ai_thread = None
            self.ai_worker = None
            self.ai_task_id = None

    def translate_current_pdf(self) -> None:
        if self.current_paper_id is None:
            QMessageBox.information(self, "没有文献", "请先选择一篇带 PDF 的文献。")
            return
        if not self.ensure_api_ready():
            return
        pdf_path = self.current_pdf_path(show_message=False)
        if pdf_path is None:
            if self.current_arxiv_pdf_url():
                self.start_arxiv_pdf_download()
                return
            self.current_pdf_path(show_message=True)
            return
        if not self.save_current_paper():
            return
        paper_id = self.current_paper_id
        if self.current_paper_id is None:
            return
        if self.translate_thread is not None or self.ai_thread is not None:
            # F16:已有翻译或解析在跑,入队顺序执行而不是拒绝
            self._enqueue_task(
                "PDF 翻译",
                pdf_path.name,
                lambda: self.start_pdf_translation(paper_id, pdf_path, confirm=False),
            )
            return
        self.start_pdf_translation(paper_id, pdf_path)

    def start_pdf_translation(
        self, paper_id: int, pdf_path: Path, *, confirm: bool = True
    ) -> None:
        try:
            with fitz.open(pdf_path) as document:
                page_count = document.page_count
        except Exception:
            page_count = 0
        size_mb = pdf_path.stat().st_size / (1024 * 1024) if pdf_path.exists() else 0
        output_dir = self.translation_output_dir(paper_id)
        if confirm and not confirm_external_request(
            self.settings_path,
            operation="使用 BabelDOC 翻译 PDF",
            content_summary=(
                f"PDF 全文，{page_count or '未知'} 页，文件大小约 {size_mb:.1f} MB"
            ),
            output_summary=str(output_dir),
            parent=self,
        ):
            self.statusBar().showMessage("已取消 PDF 翻译", 3000)
            return
        self.statusBar().showMessage("BabelDOC 正在翻译 PDF，请稍候...")
        self.loading_dialog = LoadingDialog(
            self,
            title="PDF 翻译中",
            message="正在翻译 PDF，请稍候...",
            hint="BabelDOC 正在保留排版并调用大模型翻译。",
        )
        self.loading_dialog.cancelRequested.connect(self.request_cancel_pdf_translation)
        self.loading_dialog.show()
        # B35:线程/worker/任务 id 逐任务绑定——task_id 挂在 worker 身上,完成/
        # 失败/取消处理器经 self.sender() 取回自己的任务;线程清理按身份守卫,
        # 只清仍指向本任务的槽位。此前共享槽位 + 无条件清理会让上一任务的
        # 迟到清理抹掉下一任务的引用,队列整体卡死(永远"运行中",取消/重试
        # 全部失效)。注意:处理器必须用 bound method 连接(排队回主线程),
        # lambda 连接会跟随发送者线程,工作线程退出时回调直接丢失。
        thread = QThread(self)
        worker = PDFTranslateWorker(
            settings_path=self.settings_path,
            paper_id=paper_id,
            input_pdf=pdf_path,
            output_dir=output_dir,
            # B14:显式给定翻译组件的 HOME 与应用根目录,不再依赖目录层级假设
            babeldoc_home=self.data_dir / "babeldoc_home",
            app_root=(
                Path(sys.executable).resolve().parent
                if getattr(sys, "frozen", False)
                else Path.cwd()
            ),
        )
        task_id = self.task_manager.add(
            "PDF 翻译",
            detail=pdf_path.name,
            cancel=lambda w=worker: self._cancel_pdf_translation_worker(w),
            retry=lambda p=paper_id, path=pdf_path: self.start_pdf_translation(p, path),
        )
        worker.task_id = task_id
        self.translate_thread = thread
        self.translate_worker = worker
        self.translate_task_id = task_id
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self.on_pdf_translation_finished)
        worker.failed.connect(self.on_pdf_translation_failed)
        worker.cancelled.connect(self.on_pdf_translation_cancelled)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self.clear_translate_thread)
        thread.start()

    def _sender_task_id(self) -> int | None:
        """B35:从发出信号的 worker 上取回本任务的 id(逐任务绑定身份)。"""
        sender = self.sender()
        return getattr(sender, "task_id", None) if sender is not None else None

    def on_pdf_translation_finished(self, paper_id: int, result: PDFTranslationResult) -> None:
        usage_note = ""
        if result.total_tokens > 0:
            # F20-4:BabelDOC 翻译按次记录真实 token 用量(日志解析,缺则估算)
            suffix = "（估算）" if result.tokens_estimated else ""
            usage_note = f" · Token {result.total_tokens:,}{suffix}"
            try:
                self.repository.record_token_usage(
                    feature="PDF 翻译",
                    model=LLMSettings.load(self.settings_path).model,
                    paper_id=paper_id,
                    source="babeldoc",
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    total_tokens=result.total_tokens,
                    estimated=result.tokens_estimated,
                )
            except Exception:  # noqa: S110 — 统计旁路失败不影响主流程
                pass
        self.task_manager.succeed(
            self._sender_task_id(), f"输出目录：{result.output_dir}{usage_note}"
        )
        self.close_loading_dialog()
        if self.task_manager.pending_count() > 0:
            # B35:批量翻译时不再逐个弹完成模态框,状态栏提示即可
            self.statusBar().showMessage(
                f"PDF 翻译完成：{result.output_dir}（队列中还有 "
                f"{self.task_manager.pending_count()} 个任务）",
                5000,
            )
        else:
            self.statusBar().showMessage("PDF 翻译完成。", 5000)
            paths = []
            if result.mono_pdf is not None:
                paths.append(f"译文：{result.mono_pdf}")
            if result.dual_pdf is not None:
                paths.append(f"双语对照：{result.dual_pdf}")
            message = "已生成翻译 PDF。"
            if paths:
                message += "\n\n" + "\n".join(paths)
            QMessageBox.information(self, "PDF 翻译完成", message)
        if self.current_paper_id == paper_id:
            current_page = self.reader_pdf_preview.current_visible_page()
            paper = self.repository.get_paper(paper_id)
            if paper is not None:
                self.load_reader_documents(paper)
                QTimer.singleShot(
                    0,
                    lambda page=current_page: self.reader_pdf_preview.jump_to_page(page),
                )
            self.select_paper(paper_id)

    def on_pdf_translation_failed(self, message: str) -> None:
        self.task_manager.fail(self._sender_task_id(), message)
        self.close_loading_dialog()
        if self.task_manager.pending_count() > 0:
            # B35:批量翻译时失败不弹模态框打断后续任务,任务中心可查详情
            self.statusBar().showMessage(
                f"PDF 翻译失败（队列中还有 {self.task_manager.pending_count()} 个任务继续）",
                5000,
            )
        else:
            self.statusBar().showMessage("PDF 翻译失败", 5000)
            QMessageBox.warning(self, "PDF 翻译失败", message)

    def request_cancel_pdf_translation(self) -> None:
        if (
            self.task_manager.request_cancel(self.translate_task_id)
            and self.loading_dialog is not None
        ):
            self.loading_dialog.set_cancelling()

    def _cancel_pdf_translation_worker(self, worker: PDFTranslateWorker) -> None:
        worker.cancel()

    def on_pdf_translation_cancelled(self) -> None:
        self.task_manager.cancelled(self._sender_task_id())
        self.close_loading_dialog()
        self.statusBar().showMessage("PDF 翻译已取消", 4000)

    def clear_translate_thread(self) -> None:
        # B35:作用域清理——只清空仍指向本线程的槽位,避免迟到的前一任务
        # 清理把当前任务的引用抹掉(共享槽位下的竞态)。
        if self.translate_thread is self.sender():
            self.translate_thread = None
            self.translate_worker = None
            self.translate_task_id = None

    def close_loading_dialog(self) -> None:
        if self.loading_dialog is not None:
            self.loading_dialog.close()
            self.loading_dialog.deleteLater()
            self.loading_dialog = None

    def auto_classify_library(self, show_message: bool = True) -> None:
        if not self._confirm_pending_changes():
            return
        result = self.repository.auto_classify_all()
        self.refresh_categories()
        self.refresh_papers()
        self.refresh_dataset_metrics()
        if show_message:
            QMessageBox.information(
                self,
                "自动整理完成",
                (
                    f"共检查 {result.total} 篇文献。\n"
                    f"更新分类 {result.updated} 篇，保持不变 {result.unchanged} 篇。\n"
                    f"新增分类 {result.created_categories} 个。"
                ),
            )

    def _brief_text(self, text: str, limit: int) -> str:
        compact = " ".join((text or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 1] + "..."

    def create_blank_paper(self) -> None:
        if not self._confirm_pending_changes():
            return
        paper = self.repository.create_blank_paper()
        self.current_paper_id = paper.id
        self.refresh_papers()
        self.select_paper(paper.id)
        self.load_paper(paper.id)

    def delete_current_paper(self) -> None:
        if self.current_paper_id is None:
            return
        title = self._field_text(self.title_edit).strip() or "当前文献"
        if not confirm_action(
            self,
            title="将这篇文献移入回收站？",
            message=f"“{title}”会移入回收站,可随时恢复。",
            detail="文献从列表隐藏;PDF 文件保留。可在左侧「回收站」恢复或彻底删除。",
            confirm_label="移入回收站",
            destructive=True,
        ):
            return
        paper_id = self.current_paper_id
        try:
            self.repository.delete_paper(paper_id)
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", f"无法删除文献：{exc}")
            return
        self._set_dirty(False)
        self.current_paper_id = None
        self.refresh_categories()
        self.refresh_papers()
        self.refresh_dataset_metrics()

    def select_paper(self, paper_id: int) -> None:
        for row in range(self.paper_list.count()):
            item = self.paper_list.item(row)
            if item and item.data(Qt.ItemDataRole.UserRole) == paper_id:
                self.paper_list.setCurrentItem(item)
                self.sync_card_selection()
                cur_item = self.paper_list.currentItem()
                if cur_item is item:
                    # current 已落在目标行:确保选中视觉;详情缺失时补加载
                    if not item.isSelected():
                        item.setSelected(True)
                    if paper_id != self.current_paper_id:
                        self.load_paper(paper_id)
                else:
                    # setCurrentItem 被未保存修改守卫恢复(取消切换):
                    # 保持原文献选中,不加载
                    self.sync_card_selection()
                return

    def sync_card_selection(self) -> None:
        current = self.paper_list.currentItem()
        for row in range(self.paper_list.count()):
            item = self.paper_list.item(row)
            card = self.paper_list.itemWidget(item)
            if card is None:
                continue
            # F28:多选(ExtendedSelection)下每张选中卡片都要有视觉反馈,
            # 只看 currentItem 会让 Ctrl 多选看起来"没生效"
            card.setProperty("selected", item.isSelected() or item is current)
            card.style().unpolish(card)
            card.style().polish(card)

    def _on_card_list_toggle(self, paper_id: int, modifiers) -> None:
        """F28:卡片 Ctrl/Shift 点击 → 按 Qt 选择语义切换列表选中。

        卡片(及其子控件)会吞修饰键点击,ExtendedSelection 的原生路径
        不可靠,这里显式实现:Ctrl=切换选中并置为当前项;Shift=从当前项
        到本项范围全选。选中变化会自动触发 on_paper_selected 的多选反馈。
        """
        item = None
        for row in range(self.paper_list.count()):
            candidate = self.paper_list.item(row)
            if candidate.data(Qt.ItemDataRole.UserRole) == paper_id:
                item = candidate
                break
        if item is None:
            return
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            anchor = self.paper_list.currentItem() or item
            anchor_row = self.paper_list.row(anchor)
            target_row = self.paper_list.row(item)
            for row in range(min(anchor_row, target_row), max(anchor_row, target_row) + 1):
                self.paper_list.item(row).setSelected(True)
            self.paper_list.setCurrentItem(item)
            return
        item.setSelected(not item.isSelected())
        self.paper_list.setCurrentItem(item)

    def _on_pdf_annotation_dirty_changed(self, _dirty: bool) -> None:
        self.setWindowTitle(
            f"{APP_DISPLAY_NAME} *"
            if self._dirty or self.reader_pdf_preview.has_unsaved_annotations
            else APP_DISPLAY_NAME
        )

    def save_active_content(
        self,
        _checked: bool = False,
        *,
        refresh: bool = True,
    ) -> bool:
        if (
            hasattr(self, "reader_pdf_preview")
            and self.reader_pdf_preview.has_unsaved_annotations
            and not self.reader_pdf_preview.save_all_annotations()
        ):
            return False
        return self.save_current_paper(refresh=refresh)

    def save_current_paper(self, _checked: bool = False, *, refresh: bool = True) -> bool:
        if self.current_paper_id is None:
            QMessageBox.information(self, "没有文献", "请先新建或选择一篇文献。")
            return False
        update = PaperUpdate(
            title=self._field_text(self.title_edit),
            authors=self._field_text(self.authors_edit),
            year=None if self.year_edit.value() == 0 else self.year_edit.value(),
            venue=self._field_text(self.venue_edit),
            doi=self.doi_edit.text(),
            arxiv_id=self.arxiv_edit.text(),
            url=self._field_text(self.url_edit),
            abstract=self.current_abstract_for_save(),
            pdf_path=self._field_text(self.pdf_path_edit),
            reading_status=self.status_combo.currentText(),
            importance=self.importance_edit.value(),
            summary=self.summary_edit.toPlainText(),
            datasets=self.datasets_edit.toPlainText(),
            dataset_metrics=self.dataset_metrics_table_text(),
            methods=self.methods_edit.toPlainText(),
            contributions=self.contributions_edit.toPlainText(),
            limitations=self.limitations_edit.toPlainText(),
            experiment_notes=self.experiment_notes_edit.toPlainText(),
            reproducibility_notes=self.reproducibility_notes_edit.toPlainText(),
            memory_anchor=self.memory_anchor_edit.toPlainText(),
            personal_notes=self.personal_notes_edit.toPlainText(),
        )
        try:
            self.repository.update_paper(
                self.current_paper_id,
                update,
                category_ids=self.selected_categories_for_save(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", f"无法保存文献：{exc}")
            return False
        paper_id = self.current_paper_id
        self._set_dirty(False)
        if refresh:
            self.refresh_papers()
            self.refresh_categories()
            self.select_paper(paper_id)
            self.refresh_dataset_metrics()
        self.statusBar().showMessage("已保存", 3000)
        return True

    def closeEvent(self, event: QCloseEvent) -> None:
        # F26:任何关闭路径(退出/托盘化)都先把缓冲的阅读秒数落账,
        # 否则直接关应用会丢最近一分钟的阅读时长
        self._flush_reading_seconds()
        self._save_all_splitter_states()
        action = "exit" if self._tray_exit_requested else self._prompt_window_close_action()
        if action == "tray":
            event.ignore()
            QTimer.singleShot(0, self._hide_to_system_tray)
            return
        if action != "exit":
            self._tray_exit_requested = False
            event.ignore()
            return
        if self.task_manager.active_count():
            self._tray_exit_requested = False
            QMessageBox.information(
                self,
                "后台任务仍在运行",
                f"请先在任务中心取消任务并等待其结束，再关闭 {APP_NAME}。",
            )
            self.open_task_center()
            event.ignore()
            return
        if self._confirm_pending_changes():
            if self.tray_icon is not None:
                self.tray_icon.hide()
            event.accept()
            return
        self._tray_exit_requested = False
        event.ignore()

    def _ensure_tray_icon(self) -> QSystemTrayIcon | None:
        if self.tray_icon is not None:
            return self.tray_icon
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None

        icon = self.windowIcon()
        if icon.isNull():
            icon_path = app_icon_path(ico=True)
            if icon_path.exists():
                icon = QIcon(str(icon_path))
        tray_icon = QSystemTrayIcon(icon, self)
        tray_icon.setToolTip(APP_DISPLAY_NAME)
        menu = QMenu(self)
        show_action = QAction("显示主窗口", menu)
        show_action.triggered.connect(self._restore_from_system_tray)
        exit_action = QAction(f"退出 {APP_NAME}", menu)
        exit_action.triggered.connect(self._exit_from_system_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        tray_icon.setContextMenu(menu)
        tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon = tray_icon
        return tray_icon

    def _hide_to_system_tray(self) -> None:
        tray_icon = self._ensure_tray_icon()
        if tray_icon is None:
            self.showMinimized()
            self.statusBar().showMessage("系统托盘不可用，已最小化到任务栏", 5000)
            return
        tray_icon.show()
        self.hide()
        tray_icon.showMessage(
            APP_DISPLAY_NAME,
            "ResearchWeave 正在右下角通知区域运行。双击图标可恢复窗口。",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )

    def _restore_from_system_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _exit_from_system_tray(self) -> None:
        self._tray_exit_requested = True
        self._restore_from_system_tray()
        self.close()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._restore_from_system_tray()

    def _prompt_window_close_action(self) -> str:
        dialog = ConfirmationDialog(
            window_title=f"关闭 {APP_NAME}",
            title="暂时离开，还是退出软件？",
            message="选择最适合你当前工作的方式。",
            detail=(
                "缩小到系统托盘：隐藏到右下角通知区域，保留窗口状态和后台任务。\n"
                f"退出 {APP_NAME}：完全关闭软件；未保存内容会在下一步提醒。"
            ),
            eyebrow=f"关闭 {APP_NAME}",
            tone="question",
            actions=(
                DialogAction("cancel", "继续使用"),
                DialogAction("exit", f"退出 {APP_NAME}", "danger-soft"),
                DialogAction(
                    "tray",
                    "缩小到系统托盘",
                    "primary",
                    description="隐藏到右下角，保留窗口状态和后台任务",
                    default=True,
                ),
            ),
            parent=self,
        )
        return dialog.exec_action()

    def backup_data_now(self) -> None:
        """F5:一键整库备份(数据库+WAL 合并+设置+papers)。"""
        from synthrecall.backup import create_backup

        default_dir = self.data_dir / "backups"
        default_dir.mkdir(parents=True, exist_ok=True)
        default_path = default_dir / (
            f"ResearchWeave-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        )
        file_path, _selected = QFileDialog.getSaveFileName(
            self,
            "备份数据",
            str(default_path),
            "备份文件 (*.zip);;所有文件 (*.*)",
        )
        if not file_path:
            return
        try:
            backup_path = create_backup(self.data_dir, Path(file_path))
        except (OSError, FileNotFoundError) as exc:
            QMessageBox.warning(self, "备份失败", str(exc))
            return
        size_mib = backup_path.stat().st_size / 1024 / 1024
        self.statusBar().showMessage(
            f"备份完成:{backup_path.name}({size_mib:.1f} MiB)", 6000
        )

    def restore_data_from_backup(self) -> None:
        """F5:从备份 ZIP 恢复;恢复前自动做当前状态安全备份,完成后要求重启。"""
        from synthrecall.backup import restore_backup

        backups_dir = self.data_dir / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        file_path, _selected = QFileDialog.getOpenFileName(
            self,
            "选择备份文件",
            str(backups_dir),
            "备份文件 (*.zip);;所有文件 (*.*)",
        )
        if not file_path:
            return
        dialog = ConfirmationDialog(
            window_title="从备份恢复",
            title="用该备份覆盖当前数据？",
            message="当前库会被备份文件的内容替换。",
            detail="恢复前会自动把当前状态另存为安全备份(pre-restore-*.zip);"
            "恢复完成后需要重启软件生效。",
            eyebrow="危险操作",
            tone="warning",
            actions=(
                DialogAction("cancel", "取消"),
                DialogAction("restore", "恢复并提示重启", "danger", default=True),
            ),
            parent=self,
        )
        if dialog.exec_action() != "restore":
            return
        try:
            report = restore_backup(Path(file_path), self.data_dir)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "恢复失败", str(exc))
            return
        safety_note = (
            f"当前状态已另存为 {report.safety_backup.name}。" if report.safety_backup else ""
        )
        QMessageBox.information(
            self,
            "恢复完成",
            f"已恢复 {report.restored_files} 个文件。{safety_note}\n"
            "请关闭软件后重新打开,以加载恢复后的数据。",
        )

    def open_duplicate_cleanup(self) -> None:
        """F12:库内重复文献扫描与合并向导;其余记录移入回收站后刷新全界面。"""
        dialog = DuplicateCleanupDialog(self.repository, parent=self)
        dialog.duplicatesChanged.connect(self._refresh_after_library_change)
        dialog.exec()
        self._refresh_after_library_change()

    def open_statistics_panel(self) -> None:
        """F8:库统计面板(概览 + 分布/趋势图表);只读,打开即刷新。"""
        StatisticsDialog(self.repository, parent=self).exec()

    def open_appearance_settings(self) -> None:
        """F10:界面设置(主题/字号/密度),改动实时应用。"""
        dialog = AppearanceDialog(self)
        dialog.appearance_changed.connect(self.apply_appearance)
        dialog.exec()

    def apply_appearance(self) -> None:
        """F10:主题/字号/密度变化后重刷全局样式与文献列表(卡片高度随密度)。

        setItemWidget 的行控件(相关文献/阅读队列等)在主样式表热切换时不会被
        Qt 重新 polish,必须显式 unpolish/polish 全部控件,否则出现主题反相残留。
        """
        self._apply_style()
        for widget in QApplication.allWidgets():
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()
        self.refresh_papers()

    def _category_theme_names(self) -> list[str]:
        """F19:给 AI 的候选分类名——主题类分组下的子分类 + 顶层分类,排除
        未分类/阅读状态这类非主题节点。"""
        excluded = {"未分类", "阅读状态"}
        names = [
            category.name
            for category in self.category_lookup.values()
            if category.name not in excluded
            and not (category.parent_id and self.category_lookup.get(category.parent_id) and self.category_lookup[category.parent_id].name == "阅读状态")
        ]
        return sorted(dict.fromkeys(names))

    def start_category_suggest(self, paper_id: int, *, confirm: bool = True) -> None:
        """F19:后台跑 AI 主分类建议(线程),完成后在详情页显示建议条。"""
        if getattr(self, "category_suggest_thread", None) is not None:
            return
        from synthrecall.settings import get_llm_api_key

        settings = LLMSettings.load(self.settings_path)
        if not get_llm_api_key(settings.base_url):
            self.statusBar().showMessage("未配置 API Key,跳过 AI 分类建议", 4000)
            return
        source = self.repository.get_ai_source(paper_id, max_chars=2000)
        names = self._category_theme_names()
        # B35:逐任务绑定身份,线程清理只清仍指向本任务的槽位
        thread = QThread(self)
        worker = CategorySuggestWorker(
            self.settings_path,
            paper_id,
            source,
            names,
            usage_recorder=lambda usage, pid=paper_id: self.repository.record_token_usage(
                feature="AI 分类建议", paper_id=pid, source="app", **usage
            ),
        )
        task_id = self.task_manager.add(
            "AI 分类建议",
            detail=self._brief_text(source.get("title", ""), 60),
        )
        worker.task_id = task_id
        self.category_suggest_thread = thread
        self.category_suggest_worker = worker
        self.category_suggest_task_id = task_id
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_category_suggestion)
        worker.failed.connect(self._on_category_suggestion_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_category_suggest_thread)
        thread.start()

    def _clear_category_suggest_thread(self) -> None:
        # B35:作用域清理,同 clear_translate_thread
        if self.category_suggest_thread is self.sender():
            self.category_suggest_thread = None
            self.category_suggest_worker = None
            self.category_suggest_task_id = None

    def _on_category_suggestion(self, paper_id: int, suggestion: str) -> None:
        self.task_manager.succeed(self._sender_task_id(), "已生成分类建议")
        if self.current_paper_id != paper_id:
            return
        label = suggestion.removeprefix("NEW::")
        prefix = "建议新分类:" if suggestion.startswith("NEW::") else "AI 建议主分类:"
        self.category_suggestion_label.setText(f"{prefix} {label}")
        self.category_suggestion_bar.show()
        self._pending_suggestion = (paper_id, suggestion)

    def _on_category_suggestion_failed(self, message: str) -> None:
        self.task_manager.fail(self._sender_task_id(), message)
        self.statusBar().showMessage(f"AI 分类建议失败:{message}", 6000)

    def _apply_suggested_category(self) -> None:
        if self._pending_suggestion is None:
            return
        paper_id, suggestion = self._pending_suggestion
        if suggestion.startswith("NEW::"):
            name = suggestion.removeprefix("NEW::").strip()
            if not name:
                return
            match = next(
                (c for c in self.category_lookup.values() if c.name == name),
                None,
            )
            if match is None:
                match = self.repository.add_category(name)
        else:
            match = next(
                (
                    c
                    for c in self.category_lookup.values()
                    if c.name.strip().casefold() == suggestion.strip().casefold()
                ),
                None,
            )
            if match is None:
                self.statusBar().showMessage(f"分类「{suggestion}」已不存在,建议忽略", 5000)
                return
        self.repository.set_primary_category(paper_id, match.id)
        self._clear_suggestion()
        # 应用后三处视图必须同步刷新,否则看起来"没分进去":
        # 树计数(未分类/新分类)、列表卡片分类 chip、详情页分类与建议条
        self.refresh_categories()
        self.refresh_papers()
        if self.current_paper_id == paper_id and not self._dirty:
            self.load_paper(paper_id)
        self.statusBar().showMessage(f"已将文献归入「{match.name}」", 5000)
        if self.current_paper_id == paper_id:
            self.load_paper(paper_id)
        self.statusBar().showMessage("已应用 AI 建议的主分类", 4000)

    def _clear_suggestion(self) -> None:
        self._pending_suggestion = None
        self.category_suggestion_bar.hide()

    def suggest_categories_for_uncategorized(self) -> None:
        """F19 批量入口:对所有主分类为「未分类」的文献入队 AI 建议(顺序执行)。"""
        uncategorized = self.repository.list_papers()
        targets = [
            paper.id
            for paper in uncategorized
            if all(c.name == "未分类" for c in paper.categories)
        ]
        if not targets:
            QMessageBox.information(self, "没有待建议的文献", "所有文献都已有主分类。")
            return
        for paper_id in targets:
            self._enqueue_task(
                "AI 分类建议",
                detail="",
                starter=lambda pid=paper_id: self.start_category_suggest(pid),
            )
        self.statusBar().showMessage(
            f"已入队 {len(targets)} 篇文献的 AI 分类建议,完成后在文献详情显示。", 5000
        )

    @staticmethod
    def _parse_identifier(raw: str) -> tuple[str, str]:
        """魔棒:把用户输入解析为 (类型, 值)。类型 ∈ {doi, arxiv, url}。"""
        value = (raw or "").strip().strip("`'\"")
        lowered = value.lower()
        if not value:
            return "", ""
        if "doi.org/" in lowered or lowered.startswith("doi:") or re.match(r"^10\.\d{4,9}/\S+$", value):
            cleaned = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
            cleaned = re.sub(r"^doi:", "", cleaned, flags=re.IGNORECASE)
            return "doi", cleaned.strip()
        # arXiv:新式 ID(2501.12345)与旧式 ID(cs/0301012、math/0309136)均支持;
        # 可裸写,也可来自 arxiv.org 的 abs/pdf/html 链接(含 export 子域)
        arxiv_match = re.search(
            r"(?:(?:export\.)?arxiv\.org/(?:abs|pdf|html)/|arxiv:)\s*"
            r"([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z-]+/[0-9]{7}(?:v\d+)?)",
            value,
            flags=re.IGNORECASE,
        )
        if arxiv_match:
            return "arxiv", arxiv_match.group(1)
        if re.match(r"^[0-9]{4}\.[0-9]{4,5}(v\d+)?$", value):
            return "arxiv", value
        if re.match(r"^[a-z-]+/[0-9]{7}(v\d+)?$", value, flags=re.IGNORECASE):
            return "arxiv", value
        if lowered.startswith(("http://", "https://")):
            return "url", value
        return "", ""

    def add_by_identifier(self) -> None:
        """魔棒添加:DOI/arXiv 一键建条目(+arXiv 自动下 PDF);通用链接转 AI 链接导入。"""
        text, accepted = QInputDialog.getText(
            self,
            "按 DOI / arXiv 添加",
            "粘贴 DOI、arXiv ID 或其链接(如 https://arxiv.org/abs/1706.03762),自动创建文献:",
            QLineEdit.EchoMode.Normal,
        )
        if not accepted or not text.strip():
            return
        kind, value = self._parse_identifier(text)
        if kind == "":
            hint_lines = [
                "没有识别出 DOI 或 arXiv ID。",
                "支持:10.xxxx/xxxx、doi.org 链接、arXiv:2603.25739,以及 arxiv.org 的 abs/pdf/html 链接。",
                "普通网页链接请使用「AI 链接导入」。",
            ]
            QMessageBox.information(
                self,
                "无法识别",
                chr(10).join(hint_lines),
            )
            return
        if kind == "url":
            self.ai_import_url(value)
            return
        self._confirm_pending_changes()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage("正在按标识符拉取元数据…")

        def run():
            try:
                if kind == "doi":
                    metadata = fetch_metadata(doi=value)
                else:
                    metadata = fetch_metadata(arxiv_id=value)
            except (RuntimeError, ValueError) as fetch_error:
                error_message = str(fetch_error)
                run_on_ui_thread(
                    lambda: (
                        QApplication.restoreOverrideCursor(),
                        QMessageBox.warning(self, "添加失败", error_message),
                    )
                )
                return
            run_on_ui_thread(lambda: self._finish_identifier_add(metadata))

        import threading

        threading.Thread(target=run, daemon=True).start()

    def _finish_identifier_add(self, metadata) -> None:
        QApplication.restoreOverrideCursor()
        paper, duplicate = self.repository.create_identifier_paper(metadata)
        if duplicate is not None:
            nl = chr(10)
            message = (
                f"库中已有这篇文献:{nl}《{duplicate.title}》{nl}{nl}打开已有文献吗?"
            )
            choice = QMessageBox.question(
                self,
                "文献已存在",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if choice == QMessageBox.StandardButton.Yes:
                self.show_library_page()
                self.select_paper(duplicate.id)
            return
        if paper is None:
            return
        self.refresh_papers()
        self.select_paper(paper.id)
        # F28:select_paper 不再隐式加载(保持旧的无守卫语义),魔棒创建的
        # 全新文献没有未保存状态,这里显式加载详情
        self.load_paper(paper.id)
        self.statusBar().showMessage(f"已创建文献:《{paper.title[:40]}》", 5000)
        # arXiv 文献顺手入队 PDF 下载
        pdf_url = arxiv_pdf_url(paper.arxiv_id or paper.url)
        if pdf_url and not paper.pdf_path:
            self._enqueue_task(
                "arXiv PDF 下载",
                detail=self._brief_text(paper.title, 60),
                starter=lambda pid=paper.id, url=pdf_url: self._start_arxiv_download_for(pid, url),
            )

    def _refresh_related_papers(self, paper_id: int) -> None:
        """F13:刷新详情页相关文献列表;无推荐时隐藏。"""
        self.related_papers_list.clear()
        related = self.repository.related_papers(paper_id, limit=5)
        if not related:
            self.related_papers_list.hide()
            return
        for candidate, score, reasons in related:
            # 两行条目:标题一行、共享依据一行(长标题不再把依据挤没)。
            # 截断宽度用固定值:启动期 select_paper 早于首次布局,隐藏列表的
            # width() 是默认 640px,按它算省略号会在真实 ~330px 视口里退化成
            # 半字硬切(GLM 验收抓到);详情面板最窄 ~300px,取 290 稳妥。
            row = QWidget()
            row.setFixedHeight(56)
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(8, 4, 8, 4)
            row_layout.setSpacing(1)
            title_text = candidate.title or "未命名文献"
            reason_text = "共享:" + "，".join(reasons) if reasons else "相关"
            title_font = QFont(self.font())
            title_font.setPixelSize(20)  # 与 QSS QLabel#InspectorTitle 一致
            title_font.setBold(True)  # QSS 字重 900,metrics 必须同字重
            title_metrics = QFontMetrics(title_font)
            reason_font = QFont(self.font())
            reason_font.setPixelSize(12)  # 与 QSS QLabel#MutedText 一致
            reason_metrics = QFontMetrics(reason_font)
            title_label = QLabel(
                title_metrics.elidedText(title_text, Qt.TextElideMode.ElideRight, 290)
            )
            title_label.setObjectName("InspectorTitle")
            reason_label = QLabel(
                reason_metrics.elidedText(reason_text, Qt.TextElideMode.ElideRight, 620)
            )
            reason_label.setObjectName("MutedText")
            reason_label.setWordWrap(True)
            row_layout.addWidget(title_label)
            row_layout.addWidget(reason_label)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            # 行宽用与截断一致的固定值:启动期 width() 不可信(见上),
            # 行宽超过真实视口会把行尾顶进滚动条区被盖住
            row_hint = row.sizeHint()
            row_hint.setWidth(330)
            item.setSizeHint(row_hint)
            item.setToolTip(f"{title_text}\n{reason_text}\n相关度 {score};点击打开这篇文献")
            self.related_papers_list.addItem(item)
            self.related_papers_list.setItemWidget(item, row)
        self.related_papers_list.show()

    def _open_related_paper(self, item: QListWidgetItem) -> None:
        target_id = item.data(Qt.ItemDataRole.UserRole)
        if target_id is None:
            return
        self.show_library_page()
        self.select_paper(int(target_id))

    def open_category_cleanup(self) -> None:
        """F19:分类整理向导(多分类收敛为主分类+标签);完成后刷新全界面。"""
        from synthrecall.ui.category_cleanup import CategoryCleanupDialog

        dialog = CategoryCleanupDialog(self.repository, parent=self)
        dialog.exec()
        self._refresh_after_library_change()

    def open_workspace_manager(self) -> None:
        """F11:工作区管理;切换由 switch_workspace 处理(更新注册表+重启)。"""
        dialog = WorkspaceManagerDialog(
            current_root=self.data_dir.parent,
            default_root=Path.cwd(),
            parent=self,
        )
        dialog.switchRequested.connect(self.switch_workspace)
        dialog.exec()

    def switch_workspace(self, name: str) -> None:
        """F11:切换当前工作区——确认后写注册表并以新进程重启。"""
        if not confirm_action(
            self,
            title=f"切换到工作区「{name}」?",
            message="软件将自动重启并打开该工作区的文献库。",
            detail="当前库的数据不受影响;未保存的修改需要先处理。",
            confirm_label="切换并重启",
        ):
            return
        if not self._confirm_pending_changes():
            return
        from synthrecall.workspaces import set_current

        set_current(name)
        # 切换重启:跳过"托盘/退出"询问直接退出;环境变量告知新进程
        # 这是切换交接(旧进程即将退出,单实例锁可短暂重试等待)
        self._tray_exit_requested = True
        self.close()
        self._restart_app()

    @staticmethod
    def _restart_app() -> None:
        """以新进程重启应用(开发态 python run_demo.py;打包版直接 exe)。

        置 SYNTHRECALL_SWITCHING=1:新进程的单实例锁 acquire 失败时知道是
        切换交接(旧进程几百毫秒内退出),短暂重试而不是立即退出。
        """
        os.environ["SYNTHRECALL_SWITCHING"] = "1"
        if getattr(sys, "frozen", False):
            QProcess.startDetached(sys.executable, [])
        else:
            QProcess.startDetached(
                sys.executable, [str(Path(sys.argv[0]).resolve())]
            )
        os.environ.pop("SYNTHRECALL_SWITCHING", None)

    def _refresh_after_library_change(self) -> None:
        self.refresh_papers()
        self.refresh_categories()
        self.refresh_tag_rail()

    def _on_card_favorite_toggled(self, paper_id: int, favorite: bool) -> None:
        """F21:卡片星标收藏;卡片自身已即时翻转视觉,这里只落库并提示。"""
        self.repository.set_paper_favorite(paper_id, favorite)
        self.statusBar().showMessage(
            "已加入收藏" if favorite else "已取消收藏", 3000
        )

    def _on_reading_tick(self) -> None:
        if (
            self._reading_paper_id is None
            or self.main_stack.currentWidget() is not self.reader_page
            or not self.isActive()
        ):
            return
        self._reading_seconds += 1
        self._reading_unsaved_seconds += 1
        if self._reading_unsaved_seconds >= 60:
            self._flush_reading_seconds()
        self._apply_reading_promotion(self._reading_seconds)

    def _flush_reading_seconds(self) -> None:
        """F22:把累计的真实阅读秒数按本地日期落库(每 60 秒/离开阅读页时)。"""
        if self._reading_paper_id is not None and self._reading_unsaved_seconds > 0:
            try:
                self.repository.add_reading_seconds(
                    self._reading_paper_id, self._reading_unsaved_seconds
                )
            except Exception:  # noqa: S110 — 统计旁路失败不影响阅读
                pass
        self._reading_unsaved_seconds = 0

    def _on_reader_status_changed(self, index: int) -> None:
        """F21:阅读页手动标记阅读状态。"""
        if self.current_paper_id is None or index < 0:
            return
        status = self.reader_status_combo.itemText(index)
        self.repository.set_papers_reading_status([self.current_paper_id], status)
        self.refresh_papers()
        self.statusBar().showMessage(f"已手动标记为「{status}」", 3000)

    def _apply_reading_promotion(self, seconds: int) -> None:
        """F21:按累计真实阅读秒数自动升级阅读状态(只升不降)。"""
        paper = self.repository.get_paper(self._reading_paper_id)
        if paper is None:
            return
        status = paper.reading_status or "待读"
        target = None
        if seconds >= 30 and status == "待读" and "browse" not in self._reading_promoted:
            target = "浏览"
            self._reading_promoted.add("browse")
        elif (
            seconds >= 300
            and status == "浏览"
            and "read" not in self._reading_promoted
        ):
            target = "已读"
            self._reading_promoted.add("read")
        if target is None:
            return
        self.repository.set_papers_reading_status([self._reading_paper_id], target)
        self.refresh_papers()
        self.statusBar().showMessage(
            f"已根据实际阅读时长自动标记为「{target}」（累计 {seconds} 秒）",
            5000,
        )

    def scan_orphan_files(self) -> None:
        """扫描 data/papers 与 data/translations 下未被数据库引用的文件并按需清理。"""
        # 在册文件 = 正常库 + 回收站(软删除期间文件仍保留,不可当孤儿)
        referenced: set[Path] = set()
        for paper in self.repository.list_papers() + self.repository.list_trashed_papers():
            if paper.pdf_path:
                resolved = self.repository.resolve_pdf_path(paper.pdf_path)
                if resolved is not None:
                    referenced.add(resolved.resolve())
        translations_root = self.data_dir / "translations"
        if translations_root.exists():
            for directory in translations_root.iterdir():
                if directory.is_dir():
                    referenced.add(directory.resolve())

        orphan_pdfs: list[Path] = []
        papers_root = self.data_dir / "papers"
        if papers_root.exists():
            for path in papers_root.rglob("*.pdf"):
                if path.resolve() not in referenced:
                    orphan_pdfs.append(path)
        orphan_translations: list[Path] = []
        if translations_root.exists():
            for directory in translations_root.iterdir():
                if directory.is_dir() and not directory.name.isdigit():
                    orphan_translations.append(directory)
        if not orphan_pdfs and not orphan_translations:
            QMessageBox.information(
                self, "扫描完成", "没有发现孤儿文件,库非常干净。"
            )
            return

        pdf_size = sum(path.stat().st_size for path in orphan_pdfs)
        trans_size = sum(
            sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
            for directory in orphan_translations
        )
        detail_lines = []
        for path in orphan_pdfs[:10]:
            detail_lines.append(f"PDF:{path.relative_to(self.data_dir)}")
        for directory in orphan_translations[:10]:
            detail_lines.append(f"译文:{directory.relative_to(self.data_dir)}")
        if len(orphan_pdfs) + len(orphan_translations) > 20:
            detail_lines.append("……")
        size_mib = (pdf_size + trans_size) / 1024 / 1024
        if not confirm_action(
            self,
            title=f"发现 {len(orphan_pdfs)} 个孤儿 PDF 与 {len(orphan_translations)} 个孤儿译文目录",
            message=f"这些文件不再被任何文献引用,共占 {size_mib:.1f} MiB。要删除吗?",
            detail="\n".join(detail_lines),
            confirm_label="删除这些文件",
            destructive=True,
        ):
            return
        # 阅读器组件会缓存已打开的 PDF 句柄,先释放才能在 Windows 上删除文件
        self.reader_pdf_preview.close_documents()
        removed = 0
        for path in orphan_pdfs:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        for directory in orphan_translations:
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
        self.statusBar().showMessage(f"已清理 {removed} 个孤儿文件/目录", 5000)

    def open_current_pdf(self) -> None:
        if self.current_paper_id is None:
            QMessageBox.information(self, "没有文献", "请先选择一篇文献。")
            return
        pdf_path = self.current_pdf_path(show_message=False)
        if pdf_path is None:
            if self.current_arxiv_pdf_url():
                self.start_arxiv_pdf_download()
                return
            self.current_pdf_path(show_message=True)
            return
        try:
            os.startfile(pdf_path)
            self.repository.mark_opened(self.current_paper_id)
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", f"无法打开 PDF：{exc}")

    def open_current_translation_pdf(self) -> None:
        self.open_current_translation(kind="mono")

    def open_current_dual_translation_pdf(self) -> None:
        self.open_current_translation(kind="dual")

    def open_current_translation(self, kind: str) -> None:
        if self.current_paper_id is None:
            QMessageBox.information(self, "没有文献", "请先选择一篇文献。")
            return
        pdf_path = self.current_pdf_path(show_message=False)
        if pdf_path is None:
            QMessageBox.information(self, "找不到 PDF", "当前文献没有有效的 PDF 路径。")
            return
        result = find_translation_result(pdf_path, self.translation_output_dir(self.current_paper_id))
        target = result.mono_pdf if kind == "mono" else result.dual_pdf
        if target is None:
            label = "单语译文" if kind == "mono" else "双语对照"
            QMessageBox.information(self, "没有译文", f"还没有找到当前文献的{label} PDF，请先点击“翻译PDF”。")
            return
        try:
            os.startfile(target)
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", f"无法打开译文 PDF：{exc}")

    def current_pdf_path(self, show_message: bool = True) -> Path | None:
        pdf_text = self._field_text(self.pdf_path_edit).strip()
        if not pdf_text:
            if show_message:
                QMessageBox.information(self, "找不到 PDF", "当前文献没有 PDF 路径。")
            return None
        pdf_path = self.repository.resolve_pdf_path(pdf_text)
        if not pdf_path.exists():
            if show_message:
                QMessageBox.information(self, "找不到 PDF", "当前文献没有有效的 PDF 路径。")
            return None
        return pdf_path

    def translation_output_dir(self, paper_id: int) -> Path:
        settings = LLMSettings.load(self.settings_path)
        base_dir_text = settings.translation_output_dir.strip()
        base_dir = Path(base_dir_text).expanduser() if base_dir_text else self.data_dir / "translations"
        if not base_dir.is_absolute():
            base_dir = (self.data_dir.parent / base_dir).resolve()
        return base_dir / str(paper_id)
