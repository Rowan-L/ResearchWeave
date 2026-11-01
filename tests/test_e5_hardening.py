"""E5 测试补强 + 本批新功能回归。

- F10:主题深浅变换引擎、外观对话框应用与持久化、卡片密度;
- U12:平滑滚动动画、刷新后滚动位置恢复;
- U16:快捷键注册、快捷键一览、右键复制标题;
- E5 补强:任务中心取消排队任务(按钮路径)、发现页刷新 worker 生命周期
  (重复点击只起一个线程、结束自清)、阅读器批注保存跨重开持久。
"""

import time
from typing import ClassVar

import pymupdf as fitz
import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QLabel, QListWidgetItem

import synthrecall.ui.discovery_view as discovery_view_module
from synthrecall.database import create_session_factory, init_database
from synthrecall.repositories import LibraryRepository
from synthrecall.ui import theme
from synthrecall.ui.appearance_settings import AppearanceDialog
from synthrecall.ui.discovery_view import DiscoveryView
from synthrecall.ui.main_window import MainWindow
from synthrecall.ui.pdf_reader import ContinuousPDFReader
from synthrecall.ui.shortcuts_dialog import SHORTCUT_GROUPS, ShortcutsDialog
from synthrecall.ui.task_center import TaskCenterDialog
from synthrecall.ui.widgets import PaperCard, SmoothPaperList


@pytest.fixture(autouse=True)
def restore_default_appearance(tmp_path):
    """主题是全局状态:每个用例前重置为浅色默认,结束后还原,避免互相污染。"""
    theme.configure(tmp_path)
    theme.set_theme_mode("light")
    theme.set_font_size("standard")
    theme.set_density("standard")
    yield
    theme.set_theme_mode("light")
    theme.set_font_size("standard")
    theme.set_density("standard")


def write_plain_pdf(path, *, pages=2):
    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=400, height=560)
        page.insert_text((48, 70), f"sample page {index + 1}")
    document.save(path)
    document.close()


def build_window(tmp_path, qtbot):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    session_factory = create_session_factory(data_dir / "library.db")
    init_database(session_factory)
    repository = LibraryRepository(session_factory)
    repository.ensure_default_categories()
    window = MainWindow(repository=repository, data_dir=data_dir)
    window._prompt_window_close_action = lambda: "exit"
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(30)
    return window, repository


# ---------------------------------------------------------------------------
# F10 主题
# ---------------------------------------------------------------------------


def test_theme_light_qss_untouched_and_dark_transforms_by_role():
    theme.set_theme_mode("light")
    qss = "QMainWindow { background: #fff7ed; } QLabel { color: #172033; }"
    assert theme.apply_to_qss(qss) == qss, "浅色模式必须逐字节保持原样"

    theme.set_theme_mode("dark")
    dark = theme.apply_to_qss(qss)
    assert "#12161d" in dark, "窗口暖白底应映射为深色窗口底"
    assert "#172033" not in dark, "深色文字应翻转为亮色"
    assert "#e7ecf3" in dark

    mixed = "QPushButton { background: #0f9f91; color: #ffffff; } QFrame { background: #ffffff; }"
    dark_mixed = theme.apply_to_qss(mixed)
    assert "#1f2731" in dark_mixed, "白底应映射为暗色面板"
    assert "background: #ffffff" not in dark_mixed
    assert "color: #f2f6fa" in dark_mixed, "按钮白字不能被当成背景变黑"


def test_theme_border_and_accent_rules():
    theme.set_theme_mode("dark")
    qss = (
        "QLineEdit { border: 1px solid #e2e8f0; }"
        "QLineEdit:focus { border: 1px solid #0f9f91; }"
        "QLabel { color: #1d4ed8; }"
    )
    dark = theme.apply_to_qss(qss)
    assert "#e2e8f0" not in dark, "浅色边框应变为暗色"
    assert "#0f9f91" in dark, "聚焦青色强调边框应保留"
    assert "#1d4ed8" not in dark, "饱和链接蓝应提亮为暗底可读色"


def test_appearance_dialog_applies_and_persists(tmp_path, qtbot):
    theme.configure(tmp_path)
    dialog = AppearanceDialog()
    qtbot.addWidget(dialog)

    changes = []
    dialog.appearance_changed.connect(lambda: changes.append(1))

    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData("dark"))
    dialog.font_combo.setCurrentIndex(dialog.font_combo.findData("large"))
    dialog.density_combo.setCurrentIndex(dialog.density_combo.findData("compact"))
    assert changes, "改动应发出 appearance_changed"
    assert theme.theme_mode() == "dark"
    assert theme.base_font_pt() == 11.0
    assert theme.card_row_height() == 104

    # 重新 configure(模拟重启)后设置仍在(ui_state.ini 持久化)
    theme.configure(tmp_path)
    assert theme.theme_mode() == "dark"
    assert theme.density_key() == "compact"


def test_paper_card_height_follows_density():
    theme.set_density("standard")
    from synthrecall.models import Paper

    paper = Paper(id=1, title="密度测试", importance=1)
    assert PaperCard(paper).maximumHeight() == 126
    theme.set_density("compact")
    assert PaperCard(paper).maximumHeight() == 104
    theme.set_density("relaxed")
    assert PaperCard(paper).maximumHeight() == 148


# ---------------------------------------------------------------------------
# U12 平滑滚动与滚动位置
# ---------------------------------------------------------------------------


def test_smooth_paper_list_wheel_animates_and_clamps(qtbot):
    paper_list = SmoothPaperList()
    qtbot.addWidget(paper_list)
    paper_list.resize(320, 300)
    paper_list.show()
    for index in range(60):
        item = QListWidgetItem(f"item {index}")
        item.setSizeHint(QSize(0, 126))
        paper_list.addItem(item)
    qtbot.wait(120)

    scrollbar = paper_list.verticalScrollBar()
    assert scrollbar.maximum() > 0, "60 个 126px 条目在 300px 高视口内应有可滚动范围"
    wheel = QWheelEvent(
        QPointF(10, 10),
        QPointF(10, 10),
        QPoint(0, 0),
        QPoint(0, -720),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(paper_list.viewport(), wheel)
    qtbot.wait(300)
    assert scrollbar.value() > 0, "滚轮应触发平滑滚动动画"

    down = QWheelEvent(
        QPointF(10, 10),
        QPointF(10, 10),
        QPoint(0, 0),
        QPoint(0, -72000),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(paper_list.viewport(), down)
    qtbot.wait(400)
    assert scrollbar.value() == scrollbar.maximum(), "滚动目标应截断在范围内"


def test_reconcile_scroll_restore_clamps(tmp_path, qtbot):
    window, _repository = build_window(tmp_path, qtbot)
    scrollbar = window.paper_list.verticalScrollBar()
    scrollbar.setValue(5000)  # 空列表 maximum=0,值会被钳回 0
    window._restore_paper_list_scroll(5000, was_at_bottom=False)
    assert scrollbar.value() == scrollbar.maximum()
    window._restore_paper_list_scroll(0, was_at_bottom=True)
    assert scrollbar.value() == scrollbar.maximum()


# ---------------------------------------------------------------------------
# U16 快捷键与右键
# ---------------------------------------------------------------------------


def test_window_registers_u16_shortcuts(tmp_path, qtbot):
    window, _repository = build_window(tmp_path, qtbot)
    expected = {
        "search", "import", "new", "library", "dataset", "discovery", "tasks",
        "refresh", "refresh2", "export", "backup", "appearance", "shortcuts_help",
        "search_clear", "select_all", "trash", "nav_back",
    }
    assert expected <= set(window.shortcuts.keys()), (
        f"缺少快捷键: {expected - set(window.shortcuts.keys())}"
    )


def test_shortcuts_dialog_lists_all_groups(qtbot):
    dialog = ShortcutsDialog()
    qtbot.addWidget(dialog)
    assert dialog.tree.topLevelItemCount() == len(SHORTCUT_GROUPS)
    for row in range(dialog.tree.topLevelItemCount()):
        assert dialog.tree.topLevelItem(row).childCount() > 0


def test_copy_current_paper_title(tmp_path, qtbot):
    window, repository = build_window(tmp_path, qtbot)
    from synthrecall.repositories import PaperUpdate

    paper = repository.create_blank_paper()
    repository.update_paper(
        paper.id,
        PaperUpdate(
            title="可复制的标题",
            authors="", year=None, venue="", doi="", arxiv_id="", url="",
            abstract="", pdf_path="", reading_status="待读", importance=1,
            summary="", datasets="", dataset_metrics="", methods="",
            contributions="", limitations="", experiment_notes="",
            reproducibility_notes="", memory_anchor="", personal_notes="",
        ),
    )
    window.current_paper_id = paper.id
    window.copy_current_paper_title()
    assert QApplication.clipboard().text() == "可复制的标题"


# ---------------------------------------------------------------------------
# E5 补强:任务中心 / 发现页 worker / 阅读器批注
# ---------------------------------------------------------------------------


def test_task_center_cancel_queued_via_button(qtbot):
    from synthrecall.tasks import TASK_CANCELLED, TaskManager

    manager = TaskManager()
    manager.add("占用运行槽", detail="")
    manager.enqueue("排队任务", detail="", start=lambda: None)
    dialog = TaskCenterDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    queued_row = next(
        row
        for row in range(dialog.table.rowCount())
        if dialog.table.item(row, 1).text() == "排队中"
    )
    dialog.table.selectRow(queued_row)
    assert dialog.cancel_button.isEnabled(), "选中排队任务时取消按钮应可用"
    dialog.cancel_button.click()
    qtbot.waitUntil(lambda: manager.pending_count() == 0, timeout=2000)
    cancelled_id = dialog.table.item(queued_row, 0).data(Qt.ItemDataRole.UserRole)
    if cancelled_id is None:
        # 表格未存 id 时退回按状态断言
        assert any(r.state == TASK_CANCELLED for r in manager.records())
    else:
        assert manager.get(cancelled_id).state == TASK_CANCELLED


class _BlockingFetchWorker(QObject):
    finished = Signal(list)
    failed = Signal(str)
    cancelled = Signal()

    instances: ClassVar[list["_BlockingFetchWorker"]] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__()
        _BlockingFetchWorker.instances.append(self)

    def run(self) -> None:
        time.sleep(0.2)
        self.finished.emit([])


def test_discovery_double_click_starts_single_worker_and_cleans_up(
    tmp_path, qtbot, monkeypatch
):
    warnings: list[str] = []

    class FakeMessageBox:
        @staticmethod
        def information(*args, **kwargs) -> None:
            warnings.append("busy")

    monkeypatch.setattr(discovery_view_module, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(discovery_view_module, "ArxivFetchWorker", _BlockingFetchWorker)
    _BlockingFetchWorker.instances.clear()

    discovery = DiscoveryView(tmp_path)
    qtbot.addWidget(discovery)

    discovery.refresh_feed(randomize=False)
    first_thread = discovery.fetch_thread
    assert first_thread is not None, "刷新应启动拉取线程"
    discovery.refresh_feed(randomize=False)  # 运行中再点:被守卫拦截
    assert warnings == ["busy"], "运行中重复点击应提示而不是再起线程"
    assert discovery.fetch_thread is first_thread
    assert len(_BlockingFetchWorker.instances) == 1

    qtbot.waitUntil(lambda: discovery.fetch_thread is None, timeout=4000)
    assert len(_BlockingFetchWorker.instances) == 1


def test_reader_annotation_save_persists_across_reopen(tmp_path, qtbot):
    pdf_path = tmp_path / "annotated.pdf"
    write_plain_pdf(pdf_path, pages=1)

    reader = ContinuousPDFReader()
    qtbot.addWidget(reader)
    reader.resize(760, 700)
    reader.show()
    reader.set_documents(original=pdf_path)
    reader._render_page(0)

    reader.set_annotation_tool("highlight")
    page_label = reader._page_labels[0]
    qtbot.mousePress(page_label, Qt.MouseButton.LeftButton, pos=QPoint(40, 40))
    qtbot.mouseMove(page_label, pos=QPoint(220, 100))
    qtbot.mouseRelease(page_label, Qt.MouseButton.LeftButton, pos=QPoint(220, 100))
    assert reader.has_unsaved_annotations
    assert reader.save_annotations()

    reader.close_documents()
    reopened = ContinuousPDFReader()
    qtbot.addWidget(reopened)
    reopened.resize(760, 700)
    reopened.show()
    reopened.set_documents(original=pdf_path)
    summary = reopened.annotations_summary()
    assert summary, "重开文档后批注应仍在"
    assert any(item["kind"] == "Highlight" for item in summary)


# ---------------------------------------------------------------------------
# 用户反馈修复:AI 建议应用后视图同步 + 左栏滚动容器
# ---------------------------------------------------------------------------


def _make_titled_paper(repository, title: str):
    from synthrecall.repositories import PaperUpdate

    paper = repository.create_blank_paper()
    repository.update_paper(
        paper.id,
        PaperUpdate(
            title=title,
            authors="", year=None, venue="", doi="", arxiv_id="", url="",
            abstract="", pdf_path="", reading_status="待读", importance=1,
            summary="", datasets="", dataset_metrics="", methods="",
            contributions="", limitations="", experiment_notes="",
            reproducibility_notes="", memory_anchor="", personal_notes="",
        ),
    )
    return paper


def _categories_of(repository, paper_id):
    return [c.name for c in repository.get_paper(paper_id).categories]


def test_suggestion_apply_refreshes_tree_and_detail(tmp_path, qtbot):
    """应用 AI 分类建议后,树/列表卡片/详情三处必须同步刷新。"""
    window, repository = build_window(tmp_path, qtbot)
    paper = _make_titled_paper(repository, "建议刷新文献")
    window.refresh_papers()
    window.select_paper(paper.id)

    window._on_category_suggestion(paper.id, "NEW::流场重建")
    assert window.category_suggestion_bar.isVisible()
    window.category_suggestion_apply.click()

    assert _categories_of(repository, paper.id) == ["流场重建"]
    matches = window.category_tree.findItems(
        "流场重建", Qt.MatchFlag.MatchStartsWith | Qt.MatchFlag.MatchRecursive, 0
    )
    assert matches, "应用后分类树必须出现新分类节点"
    assert window._pending_suggestion is None
    assert not window.category_suggestion_bar.isVisible()
    assert "流场重建" in window.inspector_categories_label.text()
    card_texts = [
        label.text() for label in window.paper_list.findChildren(QLabel)
    ]
    assert any("流场重建" in text for text in card_texts), (
        "应用后列表卡片分类 chip 必须刷新"
    )


def test_suggestion_apply_existing_category_refreshes_counts(tmp_path, qtbot):
    """应用已有分类建议(此前完全没刷树):未分类计数必须同步。"""
    window, repository = build_window(tmp_path, qtbot)
    repository.add_category("光流估计")
    window.refresh_categories()  # 与现实一致:建议前分类已在树中
    paper = _make_titled_paper(repository, "已有分类建议文献")
    window.refresh_papers()
    window.select_paper(paper.id)

    window._on_category_suggestion(paper.id, "光流估计")
    window._apply_suggested_category()

    assert _categories_of(repository, paper.id) == ["光流估计"]
    uncat = window.category_tree.findItems(
        "未分类", Qt.MatchFlag.MatchStartsWith, 0
    )
    assert not uncat or uncat[0].text(0).rstrip().endswith("0"), (
        f"应用后未分类计数应归零或节点隐藏,实际: "
        f"{[item.text(0) for item in uncat]}"
    )
    assert "光流估计" in window.inspector_categories_label.text()


def test_stale_suggestion_clears_on_paper_switch(tmp_path, qtbot):
    """切换文献后残留的建议条必须清空,否则应用会写到之前那篇文献上。"""
    window, repository = build_window(tmp_path, qtbot)
    paper_a = _make_titled_paper(repository, "建议甲")
    paper_b = _make_titled_paper(repository, "建议乙")
    window.refresh_papers()
    window.select_paper(paper_a.id)

    window._on_category_suggestion(paper_a.id, "NEW::暂存分类")
    assert window.category_suggestion_bar.isVisible()

    window.select_paper(paper_b.id)
    assert not window.category_suggestion_bar.isVisible()
    assert window._pending_suggestion is None


def test_left_rail_wrapped_in_scroll_area(tmp_path, qtbot):
    """左栏(导航/快捷排序/标签/分类)在空间不足时应滚动而非裁切叠压。"""
    from PySide6.QtWidgets import QScrollArea

    window, _repository = build_window(tmp_path, qtbot)
    rail = window.library_page.widget(0)
    assert isinstance(rail, QScrollArea), "左栏应为 QScrollArea 包裹"
    assert rail.widget().objectName() == "NavigationRail"


def test_unlinked_metric_click_opens_editor_instead_of_modal(
    tmp_path, qtbot, monkeypatch
):
    """点击未关联文献的排行榜条目:直接进编辑以关联,不再弹模态框(B32)。"""
    import synthrecall.ui.main_window as mw_module
    from synthrecall.repositories import DatasetMetricUpdate

    window, repository = build_window(tmp_path, qtbot)
    repository.add_dataset_metric_entry(
        DatasetMetricUpdate(
            dataset="TAP-Vid",
            metric="delta avg",
            value="73.6%",
            model="MegaFlow-L",
            task="点跟踪",
            notes="零样本 73.6%,微调后 79.6%",
        )
    )
    window.refresh_dataset_metrics()
    assert window.dataset_table.rowCount() >= 1
    assert window.dataset_table.item(0, 4).text() == "未关联"

    modals = []

    class FakeMessageBox:
        @staticmethod
        def information(*args, **kwargs):
            modals.append(args)

    class FakeDialog:
        opened_with = None

        def __init__(self, papers, parent=None, initial=None):
            FakeDialog.opened_with = dict(initial or {})

        def exec(self):
            return 0  # Rejected:只验证编辑器被打开,不真正保存

    monkeypatch.setattr(mw_module, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(mw_module, "DatasetMetricDialog", FakeDialog)

    window.open_metric_paper(window.dataset_table.item(0, 1))

    assert modals == [], "未关联文献不得再弹模态框"
    assert FakeDialog.opened_with is not None
    assert FakeDialog.opened_with.get("model") == "MegaFlow-L"
    assert FakeDialog.opened_with.get("paper_id") is None


# ---------------------------------------------------------------------------
# 用户反馈:排行榜双击才跳转 + 浏览历史返回(鼠标后退键/Alt+←)
# ---------------------------------------------------------------------------


def test_dataset_single_click_hints_and_double_click_jumps(
    tmp_path, qtbot, monkeypatch
):
    """单击只提示不打断;双击(同一处理器)跳转关联文献。"""
    import synthrecall.ui.main_window as mw_module
    from synthrecall.repositories import DatasetMetricUpdate

    window, repository = build_window(tmp_path, qtbot)
    paper = _make_titled_paper(repository, "关联跳转文献")
    repository.add_dataset_metric_entry(
        DatasetMetricUpdate(
            dataset="Sintel", metric="EPE", value="1.1%",
            model="Model-X", task="光流", paper_id=paper.id,
        )
    )
    window.refresh_dataset_metrics()
    window.show_dataset_page()

    modals = []

    class FakeMessageBox:
        @staticmethod
        def information(*args, **kwargs):
            modals.append(args)

    monkeypatch.setattr(mw_module, "QMessageBox", FakeMessageBox)
    window.on_dataset_cell_clicked(0, 1)
    assert modals == []
    assert window.main_stack.currentWidget() is window.dataset_page
    assert "双击打开关联文献" in window.statusBar().currentMessage()

    window.open_metric_paper(window.dataset_table.item(0, 1))
    assert window.main_stack.currentWidget() is window.library_page
    assert window.current_paper_id == paper.id


def test_nav_back_returns_to_previous_view(tmp_path, qtbot):
    """按浏览顺序回退:发现→数据集→文献库,而不是回到"上一层"。"""
    window, _repository = build_window(tmp_path, qtbot)
    assert window.main_stack.currentWidget() is window.library_page

    window.show_dataset_page()
    window.show_discovery_page()
    window.go_back_previous_view()
    assert window.main_stack.currentWidget() is window.dataset_page
    window.go_back_previous_view()
    assert window.main_stack.currentWidget() is window.library_page

    # 历史耗尽:不崩溃,仅状态栏提示
    window.go_back_previous_view()
    assert "没有上一页" in window.statusBar().currentMessage()


def test_nav_back_reenters_reader_with_same_paper(tmp_path, qtbot):
    """文献库→阅读页→数据集,Back 应带着同一篇文献回到阅读页。"""
    window, repository = build_window(tmp_path, qtbot)
    paper = _make_titled_paper(repository, "阅读返回文献")
    window.refresh_papers()
    window.select_paper(paper.id)
    window.open_paper_id_in_reader(paper.id)
    assert window.main_stack.currentWidget() is window.reader_page

    window.show_dataset_page()
    window.go_back_previous_view()
    assert window.main_stack.currentWidget() is window.reader_page
    assert window.current_paper_id == paper.id


def test_mouse_back_button_triggers_nav_back(tmp_path, qtbot):
    """鼠标侧键后退(XButton1)= 返回上一页。"""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent

    window, _repository = build_window(tmp_path, qtbot)
    window.show_dataset_page()
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(10, 10),
        QPointF(10, 10),
        Qt.MouseButton.XButton1,
        Qt.MouseButton.XButton1,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window, event)
    assert window.main_stack.currentWidget() is window.library_page


# ---------------------------------------------------------------------------
# B35 批量任务队列:逐任务绑定身份,迟到清理不再杀死下一个任务
# ---------------------------------------------------------------------------


class _StubTranslateWorker(QObject):
    """替换真实 PDFTranslateWorker:不发真实事件,由测试手动发信号驱动。"""

    finished = Signal(int, object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, **_kwargs):
        super().__init__()
        self.cancel_requested = False

    def run(self):  # 线程里跑但不做任何事
        return

    def cancel(self):
        self.cancel_requested = True


def _translation_result(output_dir):
    from synthrecall.translation import PDFTranslationResult

    return PDFTranslationResult(mono_pdf=None, dual_pdf=None, output_dir=output_dir)


def test_pdf_translation_queue_survives_stale_cleanup(tmp_path, qtbot, monkeypatch):
    """B35 核心回归:任务1完成后的迟到线程清理不得抹掉任务2的身份引用;
    队列必须继续排空,且批量时完成不逐个弹模态框。"""
    import synthrecall.ui.main_window as mw_module

    class FakeMessageBox:
        information_calls: ClassVar[list[str]] = []
        warning_calls: ClassVar[list[str]] = []

        @staticmethod
        def information(_parent, title, _text=None, **_kwargs):
            FakeMessageBox.information_calls.append(title)

        @staticmethod
        def warning(_parent, title, _text=None, **_kwargs):
            FakeMessageBox.warning_calls.append(title)

    monkeypatch.setattr(mw_module, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(mw_module, "PDFTranslateWorker", _StubTranslateWorker)

    window, repository = build_window(tmp_path, qtbot)
    paper_a = _make_titled_paper(repository, "翻译队列甲")
    paper_b = _make_titled_paper(repository, "翻译队列乙")
    pdf_a = tmp_path / "a.pdf"
    pdf_b = tmp_path / "b.pdf"
    write_plain_pdf(pdf_a)
    write_plain_pdf(pdf_b)

    window.start_pdf_translation(paper_a.id, pdf_a, confirm=False)
    worker_a = window.translate_worker
    thread_a = window.translate_thread
    task_a = window.translate_task_id
    assert worker_a is not None and thread_a is not None

    window._enqueue_task(
        "PDF 翻译",
        pdf_b.name,
        lambda: window.start_pdf_translation(paper_b.id, pdf_b, confirm=False),
    )
    qtbot.wait(50)
    assert window.task_manager.pending_count() == 1, "任务 1 运行中,任务 2 应在排队"

    # 任务 1 完成 → 队列排空启动任务 2(完成回调发生在任务 1 的线程清理之前)
    worker_a.finished.emit(paper_a.id, _translation_result(pdf_a.parent))
    qtbot.wait(120)
    worker_b = window.translate_worker
    task_b = window.translate_task_id
    assert worker_b is not None and worker_b is not worker_a, "排队任务应已启动"
    assert task_b != task_a

    # 任务 1 的线程退出并完成清理(deleteLater 已销毁 C++ 对象)——
    # 不得抹掉任务 2 的引用(emit 已触发 finished→quit 清理链)
    import shiboken6

    qtbot.waitUntil(lambda: not shiboken6.isValid(thread_a), timeout=3000)
    qtbot.wait(30)
    assert window.translate_thread is not None, "迟到的清理把当前任务引用清掉了"
    assert window.translate_task_id == task_b

    # 任务 2 完成 → 队列全部收尾,不残留"运行中"
    worker_b.finished.emit(paper_b.id, _translation_result(pdf_b.parent))
    qtbot.wait(120)
    states = {record.id: record.state for record in window.task_manager.records()}
    from synthrecall.tasks import TASK_SUCCEEDED

    assert states[task_a] == TASK_SUCCEEDED
    assert states[task_b] == TASK_SUCCEEDED
    # 批量时第一个完成不弹模态框,最后一个才弹
    assert FakeMessageBox.information_calls == ["PDF 翻译完成"], (
        FakeMessageBox.information_calls
    )
    _quit_translation_thread(window, qtbot)


def _quit_translation_thread(window, qtbot) -> None:
    """stub 线程在测试结束时必须先退出,否则窗口销毁会撞上运行中的 QThread。"""
    import shiboken6

    thread = window.translate_thread
    if thread is None or not shiboken6.isValid(thread):
        return
    thread.quit()
    # 先判 deleteLater 是否已销毁对象,再访问 isFinished
    qtbot.waitUntil(
        lambda: not shiboken6.isValid(thread) or thread.isFinished(), timeout=3000
    )


def test_pdf_translation_cancel_then_retry_roundtrip(tmp_path, qtbot, monkeypatch):
    """B35:取消要真正送达当次任务的 worker;取消后重试按钮可用且能重新启动。"""
    import synthrecall.ui.main_window as mw_module
    from synthrecall.tasks import TASK_CANCELLED, TASK_RUNNING

    class FakeMessageBox:
        @staticmethod
        def information(*_args, **_kwargs):
            return None

        @staticmethod
        def warning(*_args, **_kwargs):
            return None

    monkeypatch.setattr(mw_module, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(mw_module, "PDFTranslateWorker", _StubTranslateWorker)
    monkeypatch.setattr(
        mw_module, "confirm_external_request", lambda *_args, **_kwargs: True
    )

    window, repository = build_window(tmp_path, qtbot)
    paper = _make_titled_paper(repository, "取消重试文献")
    pdf_path = tmp_path / "c.pdf"
    write_plain_pdf(pdf_path)

    window.start_pdf_translation(paper.id, pdf_path, confirm=False)
    worker = window.translate_worker
    task_id = window.translate_task_id

    assert window.task_manager.request_cancel(task_id) is True
    assert worker.cancel_requested is True, "取消必须送达当次任务的 worker"
    worker.cancelled.emit()
    qtbot.wait(80)
    record = window.task_manager.get(task_id)
    assert record is not None and record.state == TASK_CANCELLED
    assert record.can_retry is True

    assert window.task_manager.retry(task_id) is True
    qtbot.wait(80)
    retried = window.task_manager.get(task_id)
    new_record = next(
        item for item in window.task_manager.records() if item.id != task_id
    )
    assert new_record.state == TASK_RUNNING
    assert window.translate_worker is not worker
    assert window.translate_worker is not None
    assert retried is not None
    _quit_translation_thread(window, qtbot)


def test_task_manager_safe_start_reports_failure_and_continues(tmp_path, qtbot):
    """B35:排队任务的启动回调抛异常时,记为失败任务且队列继续排空。"""
    from synthrecall.tasks import TASK_FAILED, TaskManager

    manager = TaskManager()
    started: list[str] = []

    def bad_start():
        raise RuntimeError("boom")

    manager.enqueue("坏任务", start=bad_start)
    manager.enqueue("好任务", start=lambda: started.append("good"))
    qtbot.wait(120)

    assert started == ["good"]
    records = manager.records()
    failed = [item for item in records if item.state == TASK_FAILED]
    assert len(failed) == 1 and "boom" in failed[0].error
    assert manager.active_count() == 0


# ---------------------------------------------------------------------------
# B36 卡片复用残留分类 chip
# ---------------------------------------------------------------------------


def _paper_with_category(repository, category_name: str):
    category = repository.add_category(category_name)
    paper = repository.create_blank_paper()
    repository.add_category_to_papers([paper.id], category.id)
    return paper


def test_paper_card_reuse_leaves_no_stale_chips(tmp_path, qtbot):
    """B36:卡片被列表复用时,上一张卡片的分类 chip 不得残留。"""
    from PySide6.QtWidgets import QLabel

    _window, repository = build_window(tmp_path, qtbot)
    paper_a = _paper_with_category(repository, "视觉 Transformer")
    paper_b = _paper_with_category(repository, "光流估计")
    # get_paper 带 joinedload,避免离线对象懒加载失败
    paper_a = repository.get_paper(paper_a.id)
    paper_b = repository.get_paper(paper_b.id)

    card = PaperCard(paper_a)
    qtbot.addWidget(card)
    chips = [
        widget
        for index in range(card.chips_layout.count())
        if (item := card.chips_layout.itemAt(index)) is not None
        and isinstance((widget := item.widget()), QLabel)
    ]
    assert [chip.text() for chip in chips] == ["视觉 Transformer"]

    # 复用同一张卡片切换到另一篇文献:旧 chip 必须清空
    card.update_content(paper_b)
    chips = [
        widget
        for index in range(card.chips_layout.count())
        if (item := card.chips_layout.itemAt(index)) is not None
        and isinstance((widget := item.widget()), QLabel)
    ]
    assert [chip.text() for chip in chips] == ["光流估计"], (
        "上一张卡片残留了分类 chip"
    )

    # 两枚 chip(分类+搜索命中)切到一枚 chip 的文献,同样不得残留
    card.update_content(paper_a, search_query="流", match_fields=["title"])
    chips = [
        widget
        for index in range(card.chips_layout.count())
        if (item := card.chips_layout.itemAt(index)) is not None
        and isinstance((widget := item.widget()), QLabel)
    ]
    assert len(chips) == 2, [chip.text() for chip in chips]
    card.update_content(paper_b)
    chips = [
        widget
        for index in range(card.chips_layout.count())
        if (item := card.chips_layout.itemAt(index)) is not None
        and isinstance((widget := item.widget()), QLabel)
    ]
    assert [chip.text() for chip in chips] == ["光流估计"]


# ---------------------------------------------------------------------------
# F21 收藏星标 + 真实阅读时长自动升级状态
# ---------------------------------------------------------------------------


def test_paper_card_favorite_toggle(tmp_path, qtbot):
    """F21:卡片星标点击切换收藏,信号带文献 id,落库持久,复用不串卡。"""
    window, repository = build_window(tmp_path, qtbot)
    paper = repository.create_blank_paper()
    paper = repository.get_paper(paper.id)

    card = PaperCard(paper)
    qtbot.addWidget(card)
    assert card.favorite_button.text() == "☆"
    assert repository.get_paper(paper.id).is_favorite is False

    recorded: list[tuple[int, bool]] = []
    card.favoriteToggled.connect(lambda pid, fav: recorded.append((pid, fav)))
    card.favorite_button.click()
    assert recorded == [(paper.id, True)]
    assert card.favorite_button.text() == "★"

    # 走真实 handler 落库(等价于 main_window 的连接)
    window._on_card_favorite_toggled(paper.id, True)
    assert repository.get_paper(paper.id).is_favorite is True

    card.favorite_button.click()
    assert card.favorite_button.text() == "☆"
    window._on_card_favorite_toggled(paper.id, False)
    assert repository.get_paper(paper.id).is_favorite is False

    # 复用:另一篇已收藏文献的卡片必须显示亮星(不残留未收藏态)
    paper_b = repository.create_blank_paper()
    window._on_card_favorite_toggled(paper_b.id, True)
    paper_b = repository.get_paper(paper_b.id)
    card.update_content(paper_b)
    assert card.favorite_button.text() == "★"


def test_reading_time_promotes_status_once(tmp_path, qtbot):
    """F21:累计真实阅读秒数自动升级 待读→浏览(30s)→已读(300s);
    只升不降,精读/复现中等手动状态不受影响。"""
    window, repository = build_window(tmp_path, qtbot)
    paper = repository.create_blank_paper()
    window._reading_paper_id = paper.id
    window._reading_promoted = set()

    window._apply_reading_promotion(29)
    assert repository.get_paper(paper.id).reading_status == "待读"

    window._apply_reading_promotion(30)
    assert repository.get_paper(paper.id).reading_status == "浏览"
    # 同一会话不重复触发
    window._apply_reading_promotion(31)
    assert repository.get_paper(paper.id).reading_status == "浏览"

    window._apply_reading_promotion(299)
    assert repository.get_paper(paper.id).reading_status == "浏览"
    window._apply_reading_promotion(300)
    assert repository.get_paper(paper.id).reading_status == "已读"

    # 手动设为精读后,自动升级不再动它
    repository.set_papers_reading_status([paper.id], "精读")
    window._reading_promoted = set()
    window._apply_reading_promotion(30)
    window._apply_reading_promotion(300)
    assert repository.get_paper(paper.id).reading_status == "精读"


def test_reader_status_combo_manual_mark(tmp_path, qtbot):
    """F21:阅读页状态下拉手动标记,与文献同步。"""
    window, repository = build_window(tmp_path, qtbot)
    paper = repository.create_blank_paper()
    window.refresh_papers()
    window.select_paper(paper.id)
    assert window.current_paper_id == paper.id
    assert window.reader_status_combo.currentText() == "待读"

    window.reader_status_combo.setCurrentText("精读")  # 触发 _on_reader_status_changed
    assert repository.get_paper(paper.id).reading_status == "精读"

    repository.set_papers_reading_status([paper.id], "浏览")
    window.load_paper(paper.id)  # 同 id 的 select_paper 会跳过重载,直接 load
    assert window.reader_status_combo.currentText() == "浏览"


def test_favorite_migration_and_persistence(tmp_path):
    """F21:迁移 5 给 papers 加 is_favorite 列;仓库方法可持久切换。"""
    import sqlite3

    from synthrecall.database import create_session_factory, init_database

    factory = create_session_factory(tmp_path / "fav.db")
    init_database(factory)
    columns = {
        row[1]
        for row in sqlite3.connect(tmp_path / "fav.db")
        .execute("PRAGMA table_info(papers)")
        .fetchall()
    }
    assert "is_favorite" in columns

    repository = LibraryRepository(factory)
    paper = repository.create_blank_paper()
    assert repository.get_paper(paper.id).is_favorite is False
    repository.set_paper_favorite(paper.id, True)
    assert repository.get_paper(paper.id).is_favorite is True


# ---------------------------------------------------------------------------
# F22 批量翻译 / AI 实验数据抽取 / 阅读时长统计
# ---------------------------------------------------------------------------


def test_batch_translate_enqueues_selected(tmp_path, qtbot, monkeypatch):
    """F22:多选批量翻译——首篇直启、其余入队、无 PDF 跳过、一次确认。"""
    import synthrecall.ui.main_window as mw_module

    class FakeMessageBox:
        @staticmethod
        def information(*_args, **_kwargs):
            return None

        @staticmethod
        def warning(*_args, **_kwargs):
            return None

    monkeypatch.setattr(mw_module, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(mw_module, "PDFTranslateWorker", _StubTranslateWorker)
    monkeypatch.setattr(
        mw_module, "confirm_external_request", lambda *_args, **_kwargs: True
    )

    window, repository = build_window(tmp_path, qtbot)
    papers = []
    for index in range(2):
        paper = repository.create_blank_paper()
        pdf_path = tmp_path / f"batch{index}.pdf"
        write_plain_pdf(pdf_path)
        repository.update_paper_pdf_path(paper.id, str(pdf_path))
        papers.append(paper)
    no_pdf = repository.create_blank_paper()
    window.refresh_papers()

    # 批量菜单包含入口
    window._rebuild_batch_menu()
    actions = [action.text() for action in window._batch_menu.actions()]
    assert "翻译所选文献" in actions

    window.current_paper_id = papers[0].id
    for paper in papers:
        window.paper_list.setProperty("b36_noop", True)
    monkeypatch.setattr(
        type(window), "selected_paper_ids", lambda self: [p.id for p in papers] + [no_pdf.id]
    )
    window.batch_translate_papers()

    assert window.translate_thread is not None, "第一篇应立即启动"
    assert window.task_manager.pending_count() == 1, "第二篇应排队"
    states = [(r.name, r.state) for r in window.task_manager.records()]
    assert sum(1 for name, _ in states if name == "PDF 翻译") == 2
    _quit_translation_thread(window, qtbot)


def test_ai_experiment_metrics_normalization():
    """F22:experiment_metrics 严格归一化——坏行丢弃、去重、numeric 容错。"""
    from synthrecall.ai import PaperAIAnalysis

    analysis = PaperAIAnalysis(
        raw={
            "experiment_metrics": [
                {"dataset": "COCO", "metric": "mAP", "value": "46.2",
                 "numeric_value": "46.2", "model": "DemoNet", "task": "检测"},
                {"dataset": "COCO", "metric": "mAP", "value": "46.2",
                 "model": "DemoNet"},  # 重复
                {"dataset": "ADE20K", "metric": "mIoU"},  # 缺 value → 丢弃
                {"dataset": "", "metric": "x", "value": "1"},  # 缺 dataset → 丢弃
                "garbage",
            ]
        }
    )
    rows = analysis.experiment_metrics
    assert rows == [
        {"dataset": "COCO", "metric": "mAP", "value": "46.2", "numeric_value": 46.2,
         "unit": "", "model": "DemoNet", "task": "检测"}
    ]
    assert PaperAIAnalysis(raw={}).experiment_metrics == []
    assert PaperAIAnalysis(raw={"experiment_metrics": "nope"}).experiment_metrics == []


def test_apply_ai_experiment_metrics_verification_and_dedup(tmp_path):
    """F22:实验数据落库——原文核对标注、与手工条目去重、二次解析不重复。"""
    source_text = "Our DemoNet achieves 46.2 mAP on the COCO benchmark. ADE20K results are strong."
    factory = create_session_factory(tmp_path / "metrics.db")
    init_database(factory)
    repository = LibraryRepository(factory)
    paper = repository.create_blank_paper()

    rows = [
        {"dataset": "COCO", "metric": "mAP", "value": "46.2",
         "numeric_value": 46.2, "unit": "", "model": "DemoNet", "task": "检测"},
        {"dataset": "Cityscapes", "metric": "mIoU", "value": "99.9",
         "numeric_value": 99.9, "unit": "", "model": "DemoNet", "task": ""},
    ]
    added, verified = repository.apply_ai_experiment_metrics(paper.id, rows, source_text)
    assert (added, verified) == (2, 1), "COCO 应核对通过,Cityscapes 未核对到"

    entries = repository.list_dataset_metrics()
    by_metric = {
        entry.metric: entry for entry in entries if entry.paper_id == paper.id
    }
    assert "已核对原文" in by_metric["mAP"].notes
    assert "未核对" in by_metric["mIoU"].notes

    # 二次解析:同样内容不重复入库
    added_again, _ = repository.apply_ai_experiment_metrics(
        paper.id, rows, source_text
    )
    assert added_again == 0

    # 手工条目优先:同键手工行已存在时 AI 行跳过
    from synthrecall.repositories import DatasetMetricUpdate

    repository.add_dataset_metric_entry(
        DatasetMetricUpdate(dataset="KITTI", metric="mAP", value="88.8",
                            model="HandModel", paper_id=paper.id)
    )
    added_manual_guard, _ = repository.apply_ai_experiment_metrics(
        paper.id,
        [{"dataset": "KITTI", "metric": "mAP", "value": "88.8", "model": "HandModel"}],
        "",
    )
    assert added_manual_guard == 0


def test_reading_time_log_and_summary(tmp_path):
    """F22:阅读秒数按文献按日累计;四桶汇总与 14 天序列。"""
    factory = create_session_factory(tmp_path / "reading.db")
    init_database(factory)
    repository = LibraryRepository(factory)
    paper_a = repository.create_blank_paper()
    paper_b = repository.create_blank_paper()

    repository.add_reading_seconds(paper_a.id, 30)
    repository.add_reading_seconds(paper_a.id, 45)
    repository.add_reading_seconds(paper_b.id, 600)

    summary = repository.reading_time_summary()
    assert summary["today"] == 675
    assert summary["week"] == 675
    assert summary["month"] == 675
    assert summary["total"] == 675
    assert len(summary["series"]) == 14
    assert summary["series"][-1][1] == 675


def test_reading_seconds_flush_to_repo(tmp_path, qtbot):
    """F22:计时缓冲到 60 秒/离开阅读页时落库。"""
    window, repository = build_window(tmp_path, qtbot)
    paper = repository.create_blank_paper()
    window._reading_paper_id = paper.id
    window._reading_unsaved_seconds = 65
    window._flush_reading_seconds()
    assert repository.reading_time_summary()["today"] == 65
    assert window._reading_unsaved_seconds == 0


def test_statistics_panel_shows_reading_section(tmp_path, qtbot):
    """F22:统计页阅读时长区块——有记录显示汇总,无记录显示空态。"""
    from synthrecall.ui.statistics_panel import StatisticsDialog

    factory = create_session_factory(tmp_path / "stat.db")
    init_database(factory)
    repository = LibraryRepository(factory)
    paper = repository.create_blank_paper()
    repository.add_reading_seconds(paper.id, 900)

    dialog = StatisticsDialog(repository)
    qtbot.addWidget(dialog)
    assert "今日" in dialog.reading_summary_label.text()
    assert "15 分钟" in dialog.reading_summary_label.text()

    empty_dir = tmp_path / "empty2"
    empty_dir.mkdir(parents=True)
    empty_factory = create_session_factory(empty_dir / "s.db")
    init_database(empty_factory)
    empty_dialog = StatisticsDialog(LibraryRepository(empty_factory))
    qtbot.addWidget(empty_dialog)
    assert "还没有阅读记录" in empty_dialog.reading_summary_label.text()
