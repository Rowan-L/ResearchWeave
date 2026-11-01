"""后台任务 Worker(AI 解析 / PDF 翻译 / arXiv 下载 / URL 导入 / 摘要翻译;E1 从 main_window 拆出,行为不变)。"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from synthrecall.ai import LLMClient
from synthrecall.arxiv_discovery import ArxivClient, ArxivDownloadCancelledError
from synthrecall.paper_url import PaperURLImportCancelled, PaperURLImporter
from synthrecall.repositories import LibraryRepository
from synthrecall.settings import LLMSettings, get_llm_api_key
from synthrecall.translation import (
    PDFTranslationRequest,
    TranslationCancelledError,
    translate_pdf_with_babeldoc,
)


class _StreamProgressThrottle:
    """B23:流式增量节流器——累积字符数,按时间间隔发进度文本。

    LLM 每 token 一个 delta,直接透传会把 UI 刷爆;0.4s 一帧足够"活着"的感知。
    """

    def __init__(self) -> None:
        self.chars = 0
        self._last_emit = 0.0

    def feed(self, piece: str, *, interval: float = 0.4) -> str | None:
        self.chars += len(piece)
        now = time.monotonic()
        if now - self._last_emit < interval:
            return None
        self._last_emit = now
        return f"已接收 {self.chars:,} 字符"


class AIAnalyzeWorker(QObject):
    finished = Signal(int, object)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(str)

    def __init__(
        self,
        repository: LibraryRepository,
        settings_path: Path,
        paper_id: int,
        *,
        usage_recorder: Callable[[dict], None] | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.settings_path = settings_path
        self.paper_id = paper_id
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
            client = LLMClient(
                settings=settings,
                api_key=get_llm_api_key(settings.base_url),
                usage_callback=self._report_usage,
            )
            source = self.repository.get_ai_source(self.paper_id, max_chars=settings.max_chars)
            throttle = _StreamProgressThrottle()

            def on_delta(piece: str) -> None:
                text = throttle.feed(piece)
                if text is not None:
                    self.progress.emit(text)

            analysis = client.analyze_paper(source, on_delta=on_delta)
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.repository.apply_ai_analysis(self.paper_id, analysis)
        except Exception as exc:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
            return
        self.finished.emit(self.paper_id, analysis)

class CategorySuggestWorker(QObject):
    """F19:AI 建议主分类——从现有分类列表中选最贴切者(或 NEW::名称)。"""

    finished = Signal(int, str)
    failed = Signal(str)

    def __init__(
        self,
        settings_path: Path,
        paper_id: int,
        source: dict[str, str],
        category_names: list[str],
        *,
        usage_recorder: Callable[[dict], None] | None = None,
    ) -> None:
        super().__init__()
        self.settings_path = settings_path
        self.paper_id = paper_id
        self.source = source
        self.category_names = category_names
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
            suggestion = client.suggest_category(self.source, self.category_names)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(self.paper_id, suggestion)


class PDFTranslateWorker(QObject):
    finished = Signal(int, object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        settings_path: Path,
        paper_id: int,
        input_pdf: Path,
        output_dir: Path,
        *,
        babeldoc_home: Path,
        app_root: Path,
    ) -> None:
        super().__init__()
        self.settings_path = settings_path
        self.paper_id = paper_id
        self.input_pdf = input_pdf
        self.output_dir = output_dir
        self.babeldoc_home = babeldoc_home
        self.app_root = app_root
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def run(self) -> None:
        try:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            settings = LLMSettings.load(self.settings_path)
            result = translate_pdf_with_babeldoc(
                PDFTranslationRequest(
                    input_pdf=self.input_pdf,
                    output_dir=self.output_dir,
                    settings=settings,
                    api_key=get_llm_api_key(settings.base_url),
                    # F20-3:默认只出译文单本,设置里可打开双语对照
                    dual_output=settings.translation_dual_output,
                    babeldoc_home=self.babeldoc_home,
                    app_root=self.app_root,
                    cancel_requested=lambda: self._cancel_requested,
                )
            )
        except TranslationCancelledError:
            self.cancelled.emit()
            return
        except Exception as exc:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
            return
        self.finished.emit(self.paper_id, result)

class ArxivPDFDownloadWorker(QObject):
    finished = Signal(int, object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        paper_id: int,
        pdf_url: str,
        title: str,
        arxiv_id: str,
        output_dir: Path,
    ) -> None:
        super().__init__()
        self.paper_id = paper_id
        self.pdf_url = pdf_url
        self.title = title
        self.arxiv_id = arxiv_id
        self.output_dir = output_dir
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def run(self) -> None:
        try:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            path = ArxivClient().download_pdf_url(
                pdf_url=self.pdf_url,
                title=self.title,
                arxiv_id=self.arxiv_id,
                output_dir=self.output_dir,
                cancel_requested=lambda: self._cancel_requested,
            )
        except ArxivDownloadCancelledError:
            self.cancelled.emit()
            return
        except Exception as exc:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
            return
        self.finished.emit(self.paper_id, path)

class URLPDFImportWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, source_url: str, output_dir: Path) -> None:
        super().__init__()
        self.source_url = source_url
        self.output_dir = output_dir
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def run(self) -> None:
        try:
            result = PaperURLImporter().download(
                self.source_url,
                self.output_dir,
                cancel_requested=lambda: self._cancel_requested,
            )
        except PaperURLImportCancelled:
            self.cancelled.emit()
            return
        except Exception as exc:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)

class AbstractTranslateWorker(QObject):
    finished = Signal(int, str)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(str)

    def __init__(
        self,
        settings_path: Path,
        paper_id: int,
        title: str,
        abstract: str,
        *,
        usage_recorder: Callable[[dict], None] | None = None,
    ) -> None:
        super().__init__()
        self.settings_path = settings_path
        self.paper_id = paper_id
        self.title = title
        self.abstract = abstract
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
            client = LLMClient(
                settings=settings,
                api_key=get_llm_api_key(settings.base_url),
                usage_callback=self._report_usage,
            )
            throttle = _StreamProgressThrottle()

            def on_delta(piece: str) -> None:
                text = throttle.feed(piece)
                if text is not None:
                    self.progress.emit(text)

            translation = client.translate_abstract_to_zh(
                self.abstract, title=self.title, on_delta=on_delta
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
        self.finished.emit(self.paper_id, translation)
