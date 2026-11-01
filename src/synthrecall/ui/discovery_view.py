from __future__ import annotations

import random
import re
import webbrowser
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from synthrecall.ai import LLMClient
from synthrecall.arxiv_discovery import (
    ArxivClient,
    ArxivPaper,
    DiscoveryCache,
    DiscoveryRecommender,
)
from synthrecall.settings import LLMSettings, get_llm_api_key
from synthrecall.tasks import TaskManager
from synthrecall.ui.external_request import confirm_external_request

DEFAULT_FOCUS_TERMS = [
    "vision transformer",
    "dynamic convolution",
    "low rank",
    "large kernel",
    "segmentation",
    "efficient visual recognition",
]

SUGGESTED_FOCUS_TERMS = [
    "multimodal learning",
    "diffusion model",
    "medical image segmentation",
    "remote sensing",
    "self-supervised learning",
    "3D vision",
    "graph neural network",
    "foundation model",
    "model compression",
    "object detection",
    "semantic segmentation",
    "uncertainty estimation",
]


class ArxivFetchWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, query: str, limit: int, sort_by: str, focus_terms: list[str], start: int = 0) -> None:
        super().__init__()
        self.query = query
        self.limit = limit
        self.sort_by = sort_by
        self.focus_terms = focus_terms
        self.start = start
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def run(self) -> None:
        try:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            papers = ArxivClient().search(
                self.query,
                max_results=self.limit,
                start=self.start,
                sort_by=self.sort_by,
            )
            recommender = DiscoveryRecommender(self.focus_terms)
            for paper in papers:
                paper.score = recommender.score(paper)
        except Exception as exc:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
            return
        if self._cancel_requested:
            self.cancelled.emit()
            return
        self.finished.emit(papers)


class ArxivDownloadWorker(QObject):
    finished = Signal(object, object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, paper: ArxivPaper, output_dir: Path) -> None:
        super().__init__()
        self.paper = paper
        self.output_dir = output_dir
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def run(self) -> None:
        try:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            path = ArxivClient().download_pdf(
                self.paper,
                self.output_dir,
                cancel_requested=lambda: self._cancel_requested,
            )
        except Exception as exc:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
            return
        if self._cancel_requested:
            self.cancelled.emit()
            return
        self.finished.emit(self.paper, path)


class DiscoveryAbstractTranslateWorker(QObject):
    finished = Signal(object, str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        settings_path: Path,
        paper: ArxivPaper,
        *,
        usage_recorder=None,
    ) -> None:
        super().__init__()
        self.settings_path = settings_path
        self.paper = paper
        self.usage_recorder = usage_recorder
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def _report_usage(self, usage: dict) -> None:
        if self.usage_recorder is not None:
            self.usage_recorder(usage)

    def run(self) -> None:
        try:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            settings = LLMSettings.load(self.settings_path)
            translation = LLMClient(
                settings,
                get_llm_api_key(settings.base_url),
                usage_callback=self._report_usage,
            ).translate_abstract_to_zh(
                self.paper.summary,
                title=self.paper.title,
            )
        except Exception as exc:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
            return
        if self._cancel_requested:
            self.cancelled.emit()
            return
        self.finished.emit(self.paper, translation)


class DiscoveryPaperCard(QFrame):
    def __init__(self, paper: ArxivPaper, summary_text: str | None = None) -> None:
        super().__init__()
        self.paper = paper
        self.setObjectName("DiscoveryCard")
        score_text = f"，AI 价值评分 {paper.score.overall}" if paper.score is not None else ""
        self.setAccessibleName(paper.title or "未命名 arXiv 论文")
        self.setAccessibleDescription(
            f"{paper.authors_text or '未知作者'}，arXiv {paper.arxiv_id}{score_text}"
        )
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        score = paper.score
        score_value = score.overall if score is not None else 0
        score_name = "StrongScore" if score_value >= 88 else "WarmScore" if score_value >= 78 else "SoftScore"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(16)

        body = QVBoxLayout()
        body.setSpacing(8)

        title = QLabel(paper.title or "Untitled arXiv paper")
        title.setObjectName("DiscoveryCardTitle")
        title.setWordWrap(True)

        meta = QLabel(f"{paper.authors_text}    arXiv: {paper.arxiv_id}    {paper.published}")
        meta.setObjectName("DiscoveryMeta")

        chip_row = QHBoxLayout()
        chip_row.setSpacing(6)
        for category in paper.categories[:3]:
            chip = QLabel(category)
            chip.setObjectName("DiscoveryChip")
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip_row.addWidget(chip)
        if paper.is_saved:
            saved = QLabel("已入库")
            saved.setObjectName("SavedChip")
            saved.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip_row.addWidget(saved)
        chip_row.addStretch(1)

        summary = QLabel(self._clip(summary_text or paper.summary, 230))
        summary.setObjectName("DiscoverySummary")
        summary.setWordWrap(True)

        body.addWidget(title)
        body.addWidget(meta)
        body.addLayout(chip_row)
        body.addWidget(summary)

        score_box = QFrame()
        score_box.setObjectName(score_name)
        score_box.setFixedWidth(138)
        score_layout = QVBoxLayout(score_box)
        score_layout.setContentsMargins(12, 10, 12, 10)
        score_layout.setSpacing(7)
        score_label = QLabel("AI 价值评分")
        score_label.setObjectName("ScoreLabel")
        score_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        score_number = QLabel(str(score_value or "--"))
        score_number.setObjectName("ScoreNumber")
        score_number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        reason_title = QLabel("为什么值得看")
        reason_title.setObjectName("ReasonTitle")
        reason = QLabel(self._clip(score.reason if score else "等待评估", 72))
        reason.setObjectName("ReasonText")
        reason.setWordWrap(True)
        score_layout.addWidget(score_label)
        score_layout.addWidget(score_number)
        score_layout.addWidget(reason_title)
        score_layout.addWidget(reason, 1)

        layout.addLayout(body, 1)
        layout.addWidget(score_box)

    def _clip(self, text: str, limit: int) -> str:
        compact = " ".join((text or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 1] + "..."


class DiscoveryView(QWidget):
    importRequested = Signal(object)
    deepReadRequested = Signal(object)
    backRequested = Signal()

    def __init__(
        self,
        data_dir: Path,
        parent: QWidget | None = None,
        task_manager: TaskManager | None = None,
        usage_recorder=None,
    ) -> None:
        super().__init__(parent)
        self.data_dir = data_dir
        self.task_manager = task_manager
        self.usage_recorder = usage_recorder
        self.cache = DiscoveryCache(data_dir / "arxiv_discovery_cache.json")
        self.papers: list[ArxivPaper] = []
        self.current_paper: ArxivPaper | None = None
        self.fetch_thread: QThread | None = None
        self.fetch_worker: ArxivFetchWorker | None = None
        self.fetch_task_id: int | None = None
        self.download_thread: QThread | None = None
        self.download_worker: ArxivDownloadWorker | None = None
        self.download_task_id: int | None = None
        self.abstract_translate_thread: QThread | None = None
        self.abstract_translate_worker: DiscoveryAbstractTranslateWorker | None = None
        self.abstract_translate_task_id: int | None = None
        self.abstract_language = "en"
        self.focus_terms = list(DEFAULT_FOCUS_TERMS)
        self.initial_feed_requested = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        shell = QFrame()
        shell.setObjectName("DiscoveryShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        shell_layout.addWidget(self._build_toolbar())
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("DiscoverySplitter")
        self.splitter.addWidget(self._build_left_rail())
        self.splitter.addWidget(self._build_feed_panel())
        self.splitter.addWidget(self._build_inspector_panel())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 4)
        self.splitter.setStretchFactor(2, 2)
        self.splitter.setSizes([220, 680, 420])
        shell_layout.addWidget(self.splitter, 1)

        layout.addWidget(shell, 1)

    def _build_toolbar(self) -> QWidget:
        toolbar = QFrame()
        toolbar.setObjectName("DiscoveryToolbar")
        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        back_button = QPushButton("返回文献库")
        back_button.setObjectName("SoftButton")
        back_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        back_button.setToolTip("返回文献库主页面（Alt+1）")
        back_button.clicked.connect(self.backRequested.emit)
        title = QLabel("arXiv 发现")
        title.setObjectName("DiscoveryTitle")
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("SearchBox")
        self.search_edit.setPlaceholderText("搜索论文标题、作者、关键词或 arXiv ID")
        self.search_edit.returnPressed.connect(lambda: self.refresh_feed(randomize=True))
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("按相关度排序", "relevance")
        self.sort_combo.addItem("最新发布", "submittedDate")
        self.sort_combo.addItem("最近更新", "lastUpdatedDate")
        self.limit_combo = QComboBox()
        self.limit_combo.addItem("20 篇", 20)
        self.limit_combo.addItem("35 篇", 35)
        self.limit_combo.addItem("50 篇", 50)
        self.refresh_button = QPushButton("换一批")
        self.refresh_button.setObjectName("TinyButton")
        self.refresh_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_button.setToolTip("每次请求之间至少间隔 3 秒，避免触发 arXiv 限流。")
        self.refresh_button.clicked.connect(lambda: self.refresh_feed(randomize=True))
        self.cache_button = QPushButton("仅看缓存")
        self.cache_button.setObjectName("TinyButton")
        self.cache_button.setToolTip("不联网，只读取最近一次成功获取的 arXiv 结果。")
        self.cache_button.clicked.connect(self.show_cached_feed)

        layout.addWidget(back_button)
        layout.addWidget(title)
        layout.addWidget(self.search_edit, 1)
        layout.addWidget(self.sort_combo)
        layout.addWidget(self.limit_combo)
        layout.addWidget(self.cache_button)
        layout.addWidget(self.refresh_button)
        return toolbar

    def _build_left_rail(self) -> QWidget:
        rail_scroll = QScrollArea()
        rail_scroll.setObjectName("DiscoveryRailScroll")
        rail_scroll.setWidgetResizable(True)
        rail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        rail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rail_scroll.setFixedWidth(236)

        rail = QFrame()
        rail.setObjectName("DiscoveryRail")
        rail.setMinimumWidth(236)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        heading = QLabel("发现")
        heading.setObjectName("RailHeading")
        layout.addWidget(heading)
        for label in ["今日推荐", "最新发布", "关注领域"]:
            button = QPushButton(label)
            button.setObjectName("RailButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked=False, value=label: self.apply_quick_filter(value))
            layout.addWidget(button)

        topics = QLabel("关注主题")
        topics.setObjectName("RailHeading")
        layout.addWidget(topics)

        keyword_panel = QFrame()
        keyword_panel.setObjectName("KeywordPanel")
        keyword_layout = QVBoxLayout(keyword_panel)
        keyword_layout.setContentsMargins(8, 8, 8, 8)
        keyword_layout.setSpacing(8)

        self.keyword_chip_grid = QGridLayout()
        self.keyword_chip_grid.setContentsMargins(0, 0, 0, 0)
        self.keyword_chip_grid.setHorizontalSpacing(6)
        self.keyword_chip_grid.setVerticalSpacing(6)
        keyword_layout.addLayout(self.keyword_chip_grid)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.keyword_input = QLineEdit()
        self.keyword_input.setObjectName("KeywordInput")
        self.keyword_input.setPlaceholderText("添加关键词")
        self.keyword_input.returnPressed.connect(self.add_focus_term_from_input)
        add_button = QPushButton("+")
        add_button.setObjectName("KeywordAddButton")
        add_button.setFixedWidth(32)
        add_button.clicked.connect(self.add_focus_term_from_input)
        add_row.addWidget(self.keyword_input, 1)
        add_row.addWidget(add_button)
        keyword_layout.addLayout(add_row)
        layout.addWidget(keyword_panel)

        preset_title = QLabel("快捷添加")
        preset_title.setObjectName("RailHeading")
        layout.addWidget(preset_title)
        self.keyword_preset_grid = QGridLayout()
        self.keyword_preset_grid.setContentsMargins(0, 0, 0, 0)
        self.keyword_preset_grid.setHorizontalSpacing(6)
        self.keyword_preset_grid.setVerticalSpacing(6)
        for index, term in enumerate(SUGGESTED_FOCUS_TERMS[:8]):
            preset = QPushButton(term)
            preset.setObjectName("KeywordPreset")
            preset.setCursor(Qt.CursorShape.PointingHandCursor)
            preset.clicked.connect(lambda checked=False, value=term: self.add_focus_term(value))
            self.keyword_preset_grid.addWidget(preset, index, 0)
        layout.addLayout(self.keyword_preset_grid)
        self.sync_keyword_chips()
        layout.addStretch(1)

        hint = QLabel("入库后会进入原有文献库，现有 PDF、翻译、AI 精读、分类功能都继续可用。")
        hint.setObjectName("RailHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        rail_scroll.setWidget(rail)
        return rail_scroll

    def _build_feed_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("DiscoveryFeedPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        tab_row = QHBoxLayout()
        self.feed_title = QLabel("今日推荐")
        self.feed_title.setObjectName("PanelTitle")
        self.feed_status = QLabel("等待刷新")
        self.feed_status.setObjectName("MutedText")
        tab_row.addWidget(self.feed_title, 1)
        tab_row.addWidget(self.feed_status)

        self.feed_list = QListWidget()
        self.feed_list.setObjectName("DiscoveryFeedList")
        self.feed_list.setSpacing(10)
        self.feed_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.feed_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.feed_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.feed_list.itemSelectionChanged.connect(self.on_feed_selection_changed)

        layout.addLayout(tab_row)
        layout.addWidget(self.feed_list, 1)
        return panel

    def _build_inspector_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("DiscoveryInspector")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        self.inspector_meta = QLabel("选择一篇论文")
        self.inspector_meta.setObjectName("MutedText")
        self.inspector_title = QLabel("arXiv 论文详情")
        self.inspector_title.setObjectName("InspectorTitle")
        self.inspector_title.setWordWrap(True)
        self.inspector_authors = QLabel("")
        self.inspector_authors.setObjectName("DiscoveryMeta")
        self.inspector_authors.setWordWrap(True)

        self.summary_box = QTextEdit()
        self.summary_box.setObjectName("InspectorText")
        self.summary_box.setReadOnly(True)
        self.summary_box.setMinimumHeight(170)
        self.abstract_lang_button = QPushButton("中/英")
        self.abstract_lang_button.setObjectName("GhostButton")
        self.abstract_lang_button.setToolTip("在 arXiv 原始摘要和中文译文之间切换。")
        self.abstract_lang_button.clicked.connect(self.toggle_abstract_language)

        self.score_box = QTextEdit()
        self.score_box.setObjectName("InspectorText")
        self.score_box.setReadOnly(True)
        self.score_box.setMinimumHeight(120)

        self.info_box = QTextEdit()
        self.info_box.setObjectName("InspectorText")
        self.info_box.setReadOnly(True)
        self.info_box.setMinimumHeight(110)

        action_grid = QVBoxLayout()
        row1 = QHBoxLayout()
        row2 = QHBoxLayout()
        self.import_button = QPushButton("保存到文献库")
        self.import_button.setObjectName("PrimaryButton")
        self.import_button.setToolTip("保存论文元数据和 arXiv 链接到文献库；不会自动下载 PDF。")
        self.import_button.clicked.connect(self.import_current_paper)
        self.download_button = QPushButton("下载 PDF")
        self.download_button.setObjectName("GhostButton")
        self.download_button.setToolTip("仅下载 PDF 到本地；需要时再单独保存到文献库。")
        self.download_button.clicked.connect(self.download_current_pdf)
        self.deep_read_button = QPushButton("AI 精读")
        self.deep_read_button.setObjectName("GhostButton")
        self.deep_read_button.setToolTip("先验证 API 配置，再保存论文到文献库并启动 AI 分析。")
        self.deep_read_button.clicked.connect(self.deep_read_current_paper)
        self.hide_button = QPushButton("不感兴趣")
        self.hide_button.setObjectName("DangerButton")
        self.hide_button.clicked.connect(self.hide_current_paper)
        self.bookmark_button = QPushButton("收藏")
        self.bookmark_button.setObjectName("GhostButton")
        self.bookmark_button.clicked.connect(self.toggle_bookmark_current)
        self.open_button = QPushButton("打开 arXiv")
        self.open_button.setObjectName("GhostButton")
        self.open_button.clicked.connect(self.open_current_arxiv)
        row1.addWidget(self.import_button)
        row1.addWidget(self.download_button)
        row1.addWidget(self.bookmark_button)
        row2.addWidget(self.deep_read_button)
        row2.addWidget(self.open_button)
        row2.addWidget(self.hide_button)
        action_grid.addLayout(row1)
        action_grid.addLayout(row2)

        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(12)
        scroll_layout.addWidget(self.inspector_meta)
        scroll_layout.addWidget(self.inspector_title)
        scroll_layout.addWidget(self.inspector_authors)
        summary_header = QHBoxLayout()
        summary_header.addWidget(self._section_label("摘要"), 1)
        summary_header.addWidget(self.abstract_lang_button)
        scroll_layout.addLayout(summary_header)
        scroll_layout.addWidget(self.summary_box)
        scroll_layout.addWidget(self._section_label("AI 评价"))
        scroll_layout.addWidget(self.score_box)
        scroll_layout.addWidget(self._section_label("关键信息"))
        scroll_layout.addWidget(self.info_box)
        scroll_layout.addLayout(action_grid)
        scroll_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setObjectName("InspectorScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)
        self._set_actions_enabled(False)
        return panel

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("SectionLabel")
        return label

    def sync_keyword_chips(self) -> None:
        while self.keyword_chip_grid.count():
            item = self.keyword_chip_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, term in enumerate(self.focus_terms):
            chip = QPushButton(f"{term}  ×")
            chip.setObjectName("KeywordChip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip("点击移除这个关注主题")
            chip.clicked.connect(lambda checked=False, value=term: self.remove_focus_term(value))
            self.keyword_chip_grid.addWidget(chip, index, 0)

    def current_focus_terms(self) -> list[str]:
        seen: set[str] = set()
        terms: list[str] = []
        for term in self.focus_terms:
            cleaned = " ".join(term.strip().split())
            key = cleaned.lower()
            if cleaned and key not in seen:
                seen.add(key)
                terms.append(cleaned)
        return terms

    def add_focus_term_from_input(self) -> None:
        self.add_focus_term(self.keyword_input.text())
        self.keyword_input.clear()

    def add_focus_term(self, term: str) -> None:
        cleaned = " ".join(term.strip().split())
        if not cleaned:
            return
        if cleaned.lower() not in {item.lower() for item in self.focus_terms}:
            self.focus_terms.append(cleaned)
            self.sync_keyword_chips()

    def remove_focus_term(self, term: str) -> None:
        target = term.strip().lower()
        self.focus_terms = [item for item in self.focus_terms if item.strip().lower() != target]
        if not self.focus_terms:
            self.focus_terms = list(DEFAULT_FOCUS_TERMS[:3])
        self.sync_keyword_chips()

    def build_refresh_request(self, randomize: bool = True) -> tuple[str, int]:
        limit = int(self.limit_combo.currentData())
        manual_query = self.search_edit.text().strip()
        if manual_query:
            start = random.randint(0, 6) * limit if randomize else 0
            return manual_query, start

        focus_terms = self.current_focus_terms()
        candidate_terms = focus_terms + [
            term for term in SUGGESTED_FOCUS_TERMS if term.lower() not in {item.lower() for item in focus_terms}
        ]
        if randomize and len(candidate_terms) > 2:
            sample_size = min(len(candidate_terms), random.choice([2, 3, 4]))
            query_terms = random.sample(candidate_terms, sample_size)
        else:
            query_terms = candidate_terms[:4]
        start = random.randint(0, 8) * limit if randomize else 0
        return " OR ".join(query_terms), start

    def apply_quick_filter(self, label: str) -> None:
        if label == "最新发布":
            self.sort_combo.setCurrentIndex(self.sort_combo.findData("submittedDate"))
            self.search_edit.clear()
        elif label == "关注领域":
            self.search_edit.setText(" OR ".join(self.current_focus_terms()[:4]))
            self.sort_combo.setCurrentIndex(self.sort_combo.findData("relevance"))
        else:
            self.sort_combo.setCurrentIndex(self.sort_combo.findData("relevance"))
            self.search_edit.clear()
        self.feed_title.setText(label)
        self.refresh_feed(randomize=True)

    def refresh_feed(self, randomize: bool = True) -> None:
        if self.fetch_thread is not None:
            QMessageBox.information(self, "正在刷新", "arXiv 发现流正在刷新，请稍后。")
            return
        self.focus_terms = self.current_focus_terms()
        query, start = self.build_refresh_request(randomize=randomize)
        self.feed_status.setText("正在刷新 arXiv，保留当前结果...")
        self.refresh_button.setEnabled(False)
        self.fetch_thread = QThread(self)
        self.fetch_worker = ArxivFetchWorker(
            query,
            int(self.limit_combo.currentData()),
            str(self.sort_combo.currentData()),
            self.focus_terms,
            start=start,
        )
        if self.task_manager is not None:
            self.fetch_task_id = self.task_manager.add(
                "刷新 arXiv 发现流",
                detail=query,
                cancel=self._cancel_fetch_worker,
                retry=lambda: self.refresh_feed(randomize=False),
            )
        self.fetch_worker.moveToThread(self.fetch_thread)
        self.fetch_thread.started.connect(self.fetch_worker.run)
        self.fetch_worker.finished.connect(self.on_fetch_finished)
        self.fetch_worker.failed.connect(self.on_fetch_failed)
        self.fetch_worker.cancelled.connect(self.on_fetch_cancelled)
        self.fetch_worker.finished.connect(self.fetch_thread.quit)
        self.fetch_worker.failed.connect(self.fetch_thread.quit)
        self.fetch_worker.cancelled.connect(self.fetch_thread.quit)
        self.fetch_thread.finished.connect(self.fetch_worker.deleteLater)
        self.fetch_thread.finished.connect(self.fetch_thread.deleteLater)
        self.fetch_thread.finished.connect(self.clear_fetch_thread)
        self.fetch_thread.start()

    def on_fetch_finished(self, papers: list[ArxivPaper]) -> None:
        # 拉到新数据后去掉标题上的「（缓存）」标注
        self.feed_title.setText(self.feed_title.text().replace("（缓存）", ""))
        if self.task_manager is not None:
            self.task_manager.succeed(
                self.fetch_task_id,
                f"获取 {len(papers)} 篇候选论文",
            )
        for paper in papers:
            self.cache.apply_state(paper)
        self.cache.remember_papers(papers)
        self.papers = [paper for paper in papers if not paper.is_hidden]
        self.sort_papers_for_feed()
        self.populate_feed()
        self.feed_status.setText(f"{len(self.papers)} 篇候选论文")

    def on_fetch_failed(self, message: str) -> None:
        if self.task_manager is not None:
            self.task_manager.fail(self.fetch_task_id, message)
        is_rate_limited = "HTTP 429" in message
        retry_match = re.search(r"建议约\s*([^；。]+后重试)", message)
        rate_limit_prefix = "arXiv 限流"
        if retry_match:
            rate_limit_prefix += f"（{retry_match.group(1)}）"
        self.refresh_button.setToolTip(message)
        if self.papers:
            self.feed_status.setText(
                f"{rate_limit_prefix}，正在保留当前结果"
                if is_rate_limited
                else "刷新失败，正在保留当前结果"
            )
            return
        cached_papers = self.load_cached_feed()
        if cached_papers:
            prefix = rate_limit_prefix if is_rate_limited else "网络不可用"
            self.feed_status.setText(f"{prefix}，正在显示上次缓存的 {cached_papers} 篇论文")
            return
        self.feed_status.setText(
            f"{rate_limit_prefix}，暂无可用缓存"
            if is_rate_limited
            else "刷新失败，暂无可用缓存"
        )
        QMessageBox.warning(self, "arXiv 刷新失败", message)

    def on_fetch_cancelled(self) -> None:
        if self.task_manager is not None:
            self.task_manager.cancelled(self.fetch_task_id)
        self.feed_status.setText("arXiv 刷新已取消，保留当前结果")

    def _cancel_fetch_worker(self) -> None:
        if self.fetch_worker is not None:
            self.fetch_worker.cancel()

    def clear_fetch_thread(self) -> None:
        self.fetch_thread = None
        self.fetch_worker = None
        self.fetch_task_id = None
        self.refresh_button.setEnabled(True)

    def load_cached_feed(self) -> int:
        cached_papers = [paper for paper in self.cache.recent_paper_list() if not paper.is_hidden]
        if not cached_papers:
            return 0
        recommender = DiscoveryRecommender(self.current_focus_terms())
        for paper in cached_papers:
            paper.score = recommender.score(paper)
        self.papers = cached_papers
        self.sort_papers_for_feed()
        self.populate_feed()
        return len(self.papers)

    def show_cached_feed(self) -> None:
        cached_count = self.load_cached_feed()
        if cached_count:
            refreshed_at = self._formatted_cache_time()
            if self._cache_is_stale():
                self.feed_title.setText("今日推荐（缓存）")
                self.feed_status.setText(
                    f"缓存更新于 {refreshed_at or '更早'}，内容可能已过时；点击「换一批」拉取最新论文"
                )
                return
            suffix = f"，上次更新 {refreshed_at}" if refreshed_at else ""
            self.feed_status.setText(f"缓存模式：{cached_count} 篇论文{suffix}")
            return
        self.feed_status.setText("缓存模式：暂无缓存，请点击“换一批”联网获取")

    def _cache_is_stale(self) -> bool:
        value = self.cache.last_successful_refresh
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
        try:
            age = (datetime.now().astimezone() - parsed).total_seconds()
        except TypeError:
            return False
        return age > 24 * 3600

    def _formatted_cache_time(self) -> str:
        value = self.cache.last_successful_refresh
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M")

    def sort_papers_for_feed(self) -> None:
        self.papers.sort(
            key=lambda paper: (
                paper.score.overall if paper.score is not None else 0,
                paper.published,
            ),
            reverse=True,
        )

    def populate_feed(self, selected_arxiv_id: str | None = None) -> None:
        target_arxiv_id = selected_arxiv_id or (
            self.current_paper.arxiv_id if self.current_paper is not None else None
        )
        self.feed_list.clear()
        for paper in self.papers:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, paper.arxiv_id)
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                (
                    f"{paper.title or '未命名 arXiv 论文'}，"
                    f"{paper.authors_text or '未知作者'}，arXiv {paper.arxiv_id}"
                ),
            )
            card = DiscoveryPaperCard(paper, self.summary_text_for_paper(paper))
            item.setSizeHint(QSize(0, max(182, card.sizeHint().height())))
            self.feed_list.addItem(item)
            self.feed_list.setItemWidget(item, card)
        if self.papers:
            selected_row = 0
            if target_arxiv_id:
                for row, paper in enumerate(self.papers):
                    if paper.arxiv_id == target_arxiv_id:
                        selected_row = row
                        break
            self.feed_list.setCurrentRow(selected_row)
        else:
            self.current_paper = None
            self.clear_inspector()

    def on_feed_selection_changed(self) -> None:
        items = self.feed_list.selectedItems()
        if not items:
            self.current_paper = None
            self.clear_inspector()
            return
        arxiv_id = items[0].data(Qt.ItemDataRole.UserRole)
        self.current_paper = next((paper for paper in self.papers if paper.arxiv_id == arxiv_id), None)
        self.update_inspector()
        self.sync_card_selection()

    def sync_card_selection(self) -> None:
        current = self.feed_list.currentItem()
        for row in range(self.feed_list.count()):
            item = self.feed_list.item(row)
            card = self.feed_list.itemWidget(item)
            if card is None:
                continue
            card.setProperty("selected", item is current)
            card.style().unpolish(card)
            card.style().polish(card)

    def update_inspector(self) -> None:
        paper = self.current_paper
        if paper is None:
            self.clear_inspector()
            return
        score = paper.score
        self.inspector_meta.setText(f"arXiv: {paper.arxiv_id}        {paper.published}")
        self.inspector_title.setText(paper.title)
        self.inspector_authors.setText(paper.authors_text)
        self.summary_box.setPlainText(self.summary_text_for_paper(paper))
        self.abstract_lang_button.setText("原文" if self.abstract_language == "zh" else "中/英")
        if score:
            score_text = (
                f"新颖性 Novelty    {score.novelty}/10\n"
                f"相关性 Relevance  {score.relevance}/10\n"
                f"可复现性 Reproducibility  {score.reproducibility}/10\n\n"
                f"综合评分  {score.overall}/100\n\n"
                f"{score.reason}"
            )
        else:
            score_text = "等待 AI 价值评估"
        self.score_box.setPlainText(score_text)
        links = [
            f"领域：{paper.primary_category or '-'}",
            f"关键词：{'、'.join(score.matched_terms) if score and score.matched_terms else '-'}",
            f"PDF：{paper.local_pdf_path or paper.pdf_url or '-'}",
            f"主页：{paper.entry_url}",
        ]
        self.info_box.setPlainText("\n".join(links))
        self.bookmark_button.setText("已收藏" if paper.is_bookmarked else "收藏")
        self.import_button.setText("已入库" if paper.is_saved else "入库")
        self.import_button.setEnabled(not paper.is_saved)
        self.abstract_lang_button.setEnabled(True)
        self._set_actions_enabled(True, keep_import_state=True)

    def summary_text_for_paper(self, paper: ArxivPaper) -> str:
        if self.abstract_language == "zh":
            translation = self.cache.abstract_translation(paper)
            if translation:
                return translation
        return paper.summary

    def toggle_abstract_language(self) -> None:
        if self.current_paper is None:
            return
        current_arxiv_id = self.current_paper.arxiv_id
        if self.abstract_language == "zh":
            self.abstract_language = "en"
            self.abstract_lang_button.setText("中/英")
            self.populate_feed(current_arxiv_id)
            self.update_inspector()
            return
        cached = self.cache.abstract_translation(self.current_paper)
        self.abstract_language = "zh"
        if cached:
            self.abstract_lang_button.setText("原文")
            self.populate_feed(current_arxiv_id)
            self.update_inspector()
            return
        self.start_current_abstract_translation()

    def start_current_abstract_translation(self) -> None:
        if self.current_paper is None:
            return
        if self.abstract_translate_thread is not None:
            QMessageBox.information(self, "正在翻译", "当前已有一个摘要翻译任务正在运行。")
            return
        settings = LLMSettings.load(self.data_dir / "llm_settings.json")
        if not get_llm_api_key(settings.base_url):
            QMessageBox.information(self, "需要 API Key", "请先在 API 设置中填写 API Key。")
            self.abstract_language = "en"
            return
        settings_path = self.data_dir / "llm_settings.json"
        if not confirm_external_request(
            settings_path,
            operation="翻译 arXiv 摘要",
            content_summary=(
                f"论文标题和摘要，共约 {len(self.current_paper.summary):,} 个字符"
            ),
            output_summary="中文摘要将保存在本地发现页缓存中",
            parent=self,
        ):
            self.abstract_language = "en"
            self.abstract_lang_button.setText("中/英")
            return
        self.abstract_lang_button.setText("翻译中")
        self.abstract_lang_button.setEnabled(False)
        self.feed_status.setText("正在翻译摘要...")
        # B35:逐任务绑定身份,线程清理只清仍指向本任务的槽位(同 main_window)
        thread = QThread(self)
        worker = DiscoveryAbstractTranslateWorker(
            settings_path,
            self.current_paper,
            usage_recorder=self.usage_recorder,
        )
        paper = self.current_paper
        task_id = None
        if self.task_manager is not None:
            task_id = self.task_manager.add(
                "arXiv 摘要翻译",
                detail=paper.title,
                cancel=lambda w=worker: self._cancel_abstract_translation_worker(w),
                retry=lambda p=paper: self.retry_abstract_translation(p),
            )
        worker.task_id = task_id
        self.abstract_translate_thread = thread
        self.abstract_translate_worker = worker
        self.abstract_translate_task_id = task_id
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
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

    def _sender_task_id(self) -> int | None:
        """B35:从发出信号的 worker 上取回本任务的 id(逐任务绑定身份)。"""
        sender = self.sender()
        return getattr(sender, "task_id", None) if sender is not None else None

    def on_abstract_translation_finished(self, paper: ArxivPaper, translation: str) -> None:
        task_id = self._sender_task_id()
        if self.task_manager is not None and task_id is not None:
            self.task_manager.succeed(task_id, "中文摘要已保存到发现页缓存")
        self.abstract_lang_button.setEnabled(True)
        if translation.strip():
            self.cache.set_abstract_translation(paper, translation)
        self.abstract_language = "zh"
        self.populate_feed(paper.arxiv_id)
        self.update_inspector()
        self.feed_status.setText("摘要翻译完成")

    def on_abstract_translation_failed(self, message: str) -> None:
        task_id = self._sender_task_id()
        if self.task_manager is not None and task_id is not None:
            self.task_manager.fail(task_id, message)
        self.abstract_lang_button.setEnabled(True)
        self.abstract_lang_button.setText("中/英")
        self.abstract_language = "en"
        self.feed_status.setText("摘要翻译失败")
        QMessageBox.warning(self, "摘要翻译失败", message)

    def on_abstract_translation_cancelled(self) -> None:
        task_id = self._sender_task_id()
        if self.task_manager is not None and task_id is not None:
            self.task_manager.cancelled(task_id)
        self.abstract_lang_button.setEnabled(True)
        self.abstract_lang_button.setText("中/英")
        self.abstract_language = "en"
        self.feed_status.setText("摘要翻译已取消")

    def _cancel_abstract_translation_worker(self, worker: DiscoveryAbstractTranslateWorker) -> None:
        worker.cancel()

    def retry_abstract_translation(self, paper: ArxivPaper) -> None:
        self.current_paper = paper
        self.start_current_abstract_translation()

    def clear_abstract_translate_thread(self) -> None:
        # B35:作用域清理——只清空仍指向本线程的槽位
        if self.abstract_translate_thread is self.sender():
            self.abstract_translate_thread = None
            self.abstract_translate_worker = None
            self.abstract_translate_task_id = None


    def clear_inspector(self) -> None:
        self.inspector_meta.setText("选择一篇论文")
        self.inspector_title.setText("arXiv 论文详情")
        self.inspector_authors.setText("")
        self.summary_box.setPlainText("")
        self.score_box.setPlainText("")
        self.info_box.setPlainText("")
        self.abstract_lang_button.setText("中/英")
        self.abstract_lang_button.setEnabled(False)
        self._set_actions_enabled(False)

    def _set_actions_enabled(self, enabled: bool, keep_import_state: bool = False) -> None:
        for button in [
            self.download_button,
            self.deep_read_button,
            self.hide_button,
            self.bookmark_button,
            self.open_button,
        ]:
            button.setEnabled(enabled)
        if not keep_import_state:
            self.import_button.setEnabled(enabled)

    def import_current_paper(self) -> None:
        if self.current_paper is None:
            return
        self.importRequested.emit(self.current_paper)

    def mark_current_saved(self, paper: ArxivPaper) -> None:
        self.cache.mark_saved(paper)
        paper.is_saved = True
        self.update_inspector()
        self.populate_feed()

    def deep_read_current_paper(self) -> None:
        if self.current_paper is not None:
            self.deepReadRequested.emit(self.current_paper)

    def download_current_pdf(self) -> None:
        if self.current_paper is None:
            return
        if self.download_thread is not None:
            QMessageBox.information(self, "正在下载", "已有一个 PDF 下载任务正在运行。")
            return
        self.feed_status.setText("正在下载 PDF...")
        self.download_button.setEnabled(False)
        self.download_thread = QThread(self)
        self.download_worker = ArxivDownloadWorker(
            self.current_paper,
            self.data_dir / "papers" / "arxiv_discovery",
        )
        paper = self.current_paper
        if self.task_manager is not None:
            self.download_task_id = self.task_manager.add(
                "下载 arXiv PDF",
                detail=paper.title,
                cancel=self._cancel_download_worker,
                retry=lambda: self.retry_download(paper),
            )
        self.download_worker.moveToThread(self.download_thread)
        self.download_thread.started.connect(self.download_worker.run)
        self.download_worker.finished.connect(self.on_download_finished)
        self.download_worker.failed.connect(self.on_download_failed)
        self.download_worker.cancelled.connect(self.on_download_cancelled)
        self.download_worker.finished.connect(self.download_thread.quit)
        self.download_worker.failed.connect(self.download_thread.quit)
        self.download_worker.cancelled.connect(self.download_thread.quit)
        self.download_thread.finished.connect(self.download_worker.deleteLater)
        self.download_thread.finished.connect(self.download_thread.deleteLater)
        self.download_thread.finished.connect(self.clear_download_thread)
        self.download_thread.start()

    def on_download_finished(self, paper: ArxivPaper, path: Path) -> None:
        if self.task_manager is not None:
            self.task_manager.succeed(self.download_task_id, f"PDF 已保存：{path}")
        self.cache.set_local_pdf(paper, path)
        self.feed_status.setText(f"PDF 已下载：{path.name}")
        self.update_inspector()

    def on_download_failed(self, message: str) -> None:
        if self.task_manager is not None:
            self.task_manager.fail(self.download_task_id, message)
        self.feed_status.setText("PDF 下载失败")
        if self.current_paper is not None:
            self.download_button.setEnabled(True)
        QMessageBox.warning(self, "PDF 下载失败", message)

    def on_download_cancelled(self) -> None:
        if self.task_manager is not None:
            self.task_manager.cancelled(self.download_task_id)
        self.download_button.setEnabled(self.current_paper is not None)
        self.feed_status.setText("PDF 下载已取消")

    def _cancel_download_worker(self) -> None:
        if self.download_worker is not None:
            self.download_worker.cancel()

    def retry_download(self, paper: ArxivPaper) -> None:
        self.current_paper = paper
        self.download_current_pdf()

    def clear_download_thread(self) -> None:
        self.download_thread = None
        self.download_worker = None
        self.download_task_id = None

    def hide_current_paper(self) -> None:
        paper = self.current_paper
        if paper is None:
            return
        self.cache.mark_hidden(paper)
        self.papers = [item for item in self.papers if item.arxiv_id != paper.arxiv_id]
        self.populate_feed()
        self.feed_status.setText(f"已隐藏 {paper.arxiv_id}")

    def toggle_bookmark_current(self) -> None:
        if self.current_paper is None:
            return
        bookmarked = self.cache.toggle_bookmark(self.current_paper)
        self.bookmark_button.setText("已收藏" if bookmarked else "收藏")
        self.populate_feed()

    def open_current_arxiv(self) -> None:
        if self.current_paper is None or not self.current_paper.entry_url:
            return
        webbrowser.open(self.current_paper.entry_url)


    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.initial_feed_requested:
            return
        self.initial_feed_requested = True
        if self.load_cached_feed():
            refreshed_at = self._formatted_cache_time()
            if self._cache_is_stale():
                self.feed_title.setText("今日推荐（缓存）")
                self.feed_status.setText(
                    f"缓存更新于 {refreshed_at or '更早'}，内容可能已过时；点击「换一批」拉取最新论文"
                )
                return
            suffix = f"，上次更新 {refreshed_at}" if refreshed_at else ""
            self.feed_status.setText(f"正在显示上次缓存的 {len(self.papers)} 篇论文{suffix}")
            return
        self.feed_status.setText("暂无缓存，点击“换一批”后联网获取 arXiv 论文")
